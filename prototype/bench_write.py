#!/usr/bin/env python3
"""Write-path profiling harness — isolate where backup write throughput is lost.

Drives the REAL producer (the actual `tar --format=pax ... --files-from=-`
subprocess + the 64 MiB read/compress/write loop the framed backup path uses)
into several sinks, while instrumenting per-stage time and sampling kernel
writeback + NIC counters as a time series. The goal is to settle *why* the
10GbE link to the NAS sits idle ~75-80% of the time during backups.

Three suspects, three signatures this harness makes visible:

  * producer-bound (CPU)  -> the `discard` sink is also slow; one core pegged;
                             per-stage time is dominated by compress (or read).
  * writeback batching    -> write() returns instantly, Dirty pages climb into
                             the GBs, the NIC tx series sawtooths while the
                             producer's own write-rate series is flat, and the
                             FINAL fsync is huge (it drains the dirty backlog).
  * NFS / network-bound   -> local NVMe is smooth and fast, NFS stalls anyway.

Sinks:
  discard  - read + compress, write nowhere (producer ceiling)
  tmpfs    - /dev/shm (RAM-backed; "free" sink, exercises the file/write path)
  nvme     - <scratch>/bench_out (local NVMe; no network, no NFS)
  nfs      - under the backup mount (the real target; writes a few GB to NAS)

Example:
  python3 prototype/bench_write.py --scratch /home/kim/Workspace \
      --gen-size-gb 16 --sinks discard,tmpfs,nvme,nfs
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import zstandard as zstd  # noqa: E402

from timetraveller import framewriter  # noqa: E402
from timetraveller import pax as paxlib  # noqa: E402

FRAME_SIZE = framewriter.FRAME_SIZE
_read_full = framewriter._read_full

NFS_BENCH_SUBDIR = "timetraveller/_bench"


# --------------------------------------------------------------------------- #
# System context + sampling
# --------------------------------------------------------------------------- #

def _read_int(path: str) -> int:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return 0


def _meminfo_kb(key: str) -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1])  # kB
    except OSError:
        pass
    return 0


def _sysctl(name: str) -> str:
    p = "/proc/sys/" + name.replace(".", "/")
    try:
        return Path(p).read_text().strip()
    except OSError:
        return "?"


def nfs_server_for(path: Path) -> str | None:
    """Return the NFS server host for the mount containing `path`, or None."""
    best = ("", None)
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            dev, mnt, fstype = line.split()[:3]
            if fstype.startswith("nfs") and str(path).startswith(mnt) and len(mnt) > len(best[0]):
                best = (mnt, dev.split(":")[0])
    except OSError:
        pass
    return best[1]


def iface_to(host: str) -> str | None:
    try:
        ip = socket.gethostbyname(host)
        out = subprocess.run(["ip", "route", "get", ip], capture_output=True,
                             text=True, check=True).stdout
        toks = out.split()
        return toks[toks.index("dev") + 1] if "dev" in toks else None
    except Exception:
        return None


class Sampler(threading.Thread):
    """Samples (t, dirty_kb, writeback_kb, tx_bytes, produced_bytes) on an
    interval until stop() is called. produced_bytes is the running count of
    compressed bytes the producer has handed to the sink (shared dict)."""

    def __init__(self, iface: str | None, progress: dict, interval: float = 0.5,
                 produced_fn=None):
        super().__init__(daemon=True)
        self._iface = iface
        self._progress = progress
        self._interval = interval
        self._produced_fn = produced_fn
        self._stop = threading.Event()
        self.samples: list[tuple] = []

    def _tx(self) -> int:
        if self._iface:
            return _read_int(f"/sys/class/net/{self._iface}/statistics/tx_bytes")
        # No iface known: sum all non-lo interfaces.
        total = 0
        for d in Path("/sys/class/net").glob("*"):
            if d.name != "lo":
                total += _read_int(f"{d}/statistics/tx_bytes")
        return total

    def run(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            produced = (self._produced_fn() if self._produced_fn
                        else self._progress.get("produced", 0))
            self.samples.append((
                time.monotonic() - t0,
                _meminfo_kb("Dirty"),
                _meminfo_kb("Writeback"),
                self._tx(),
                produced,
            ))
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #

def gen_dataset(src: Path, size_gb: float) -> list[str]:
    """Create (once) a mixed dataset under `src`; return relative file paths.

    Mix: alternating incompressible (urandom) and compressible (text) large
    files, plus a directory of many small files to exercise tar per-file
    overhead. Skipped if `src` already holds ~the requested size.
    """
    target = int(size_gb * (1 << 30))
    marker = src / ".bench_bytes"
    if marker.exists() and abs(int(marker.read_text() or "0") - target) < (1 << 30):
        return _walk_rel(src)

    if src.exists():
        shutil.rmtree(src)
    (src / "big").mkdir(parents=True)
    (src / "small").mkdir(parents=True)

    rng = os.urandom(8 << 20)  # 8 MiB incompressible base
    text = (b"the quick brown fox jumps over the lazy dog 1234567890\n" * 4096)  # compressible
    written = 0
    i = 0
    # Big files: ~256 MiB each, alternating incompressible / compressible.
    while written < target * 0.92:
        f = src / "big" / f"f{i:03d}.bin"
        with open(f, "wb") as fh:
            for _ in range(32):  # 32 * 8 MiB = 256 MiB
                if i % 2 == 0:
                    fh.write(os.urandom(8 << 20))      # incompressible
                else:
                    fh.write(text * 38)                # ~compressible, ~8 MiB
                written += 8 << 20
        i += 1
    # Many small files (per-file overhead).
    for j in range(2000):
        f = src / "small" / f"s{j:05d}.txt"
        f.write_bytes(text[: (j % 16 + 1) * 256])
        written += (j % 16 + 1) * 256
    marker.write_text(str(target))
    return _walk_rel(src)


def _walk_rel(src: Path) -> list[str]:
    rel = []
    for root, _dirs, files in os.walk(src):
        for fn in files:
            if fn == ".bench_bytes":
                continue
            rel.append("./" + str((Path(root) / fn).relative_to(src)))
    return rel


# --------------------------------------------------------------------------- #
# One run
# --------------------------------------------------------------------------- #

def run_once(file_list: list[str], src: Path, sink: str, sink_path: Path | None,
             *, level: int, compress: bool, iface: str | None,
             writer_mode: str = "serial", sync_every_bytes: int = 0) -> dict:
    """Run the real pax producer once into `sink`. Returns a metrics dict.

    writer_mode:
      "serial" - one worker reads -> compresses -> writes inline; a single
                 fsync at the very end (the current production behavior).
      "pipe"   - producer thread reads -> compresses -> bounded queue; a
                 separate writer thread drains it to the fd and issues
                 fdatasync every `sync_every_bytes` (0 = only at end). This
                 overlaps compression with the NFS write+flush and forces
                 steady writeback instead of one terminal flush — the shape
                 a production "continuous writer" (option A) would take.
    """
    inv = paxlib.PaxInvocation(sources=[], chdir=str(src),
                               archive_path=Path("/dev/null"), excludes=[],
                               extra_mount_excludes=[], framed=True)
    argv = inv.pax_argv_incremental()

    progress: dict = {"produced": 0}
    sampler = Sampler(iface, progress)
    box: dict = {}

    pax = subprocess.Popen(argv, cwd=str(src), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert pax.stdin is not None and pax.stdout is not None

    threads: list[threading.Thread] = []

    if writer_mode == "pipe":
        q: queue.Queue = queue.Queue(maxsize=8)
        wbox: dict = {}

        def producer():
            comp = zstd.ZstdCompressor(level=level) if compress else None
            read_s = comp_s = 0.0
            tu = tc = 0
            try:
                while True:
                    t = time.monotonic()
                    chunk = _read_full(pax.stdout, FRAME_SIZE)
                    read_s += time.monotonic() - t
                    if not chunk:
                        break
                    if comp is not None:
                        t = time.monotonic()
                        data = comp.compress(chunk)
                        comp_s += time.monotonic() - t
                    else:
                        data = chunk
                    tu += len(chunk)
                    tc += len(data)
                    q.put(data)
                box.update(tu=tu, tc=tc, read_s=read_s, comp_s=comp_s)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                q.put(None)
                pax.stdout.close()

        def writer():
            out = open(sink_path, "wb")
            write_s = sync_s = 0.0
            since = written = 0
            try:
                while True:
                    data = q.get()
                    if data is None:
                        break
                    t = time.monotonic()
                    out.write(data)
                    write_s += time.monotonic() - t
                    written += len(data)
                    since += len(data)
                    progress["produced"] = written
                    if sync_every_bytes and since >= sync_every_bytes:
                        t = time.monotonic()
                        out.flush()
                        os.fdatasync(out.fileno())
                        sync_s += time.monotonic() - t
                        since = 0
                t = time.monotonic()
                out.flush()
                os.fsync(out.fileno())
                sync_s += time.monotonic() - t
                out.close()
                wbox.update(write_s=write_s, fsync_s=sync_s)
            except BaseException as exc:  # noqa: BLE001
                wbox["error"] = exc

        threads = [threading.Thread(target=producer),
                   threading.Thread(target=writer)]
    else:
        def worker():
            comp = zstd.ZstdCompressor(level=level) if compress else None
            out = open(sink_path, "wb") if sink_path is not None else None
            read_s = comp_s = write_s = 0.0
            tu = tc = 0
            try:
                while True:
                    t = time.monotonic()
                    chunk = _read_full(pax.stdout, FRAME_SIZE)
                    read_s += time.monotonic() - t
                    if not chunk:
                        break
                    if comp is not None:
                        t = time.monotonic()
                        data = comp.compress(chunk)
                        comp_s += time.monotonic() - t
                    else:
                        data = chunk
                    if out is not None:
                        t = time.monotonic()
                        out.write(data)
                        write_s += time.monotonic() - t
                    tu += len(chunk)
                    tc += len(data)
                    progress["produced"] = tc
                fsync_s = 0.0
                if out is not None:
                    t = time.monotonic()
                    out.flush()
                    os.fsync(out.fileno())
                    fsync_s = time.monotonic() - t
                    out.close()
                box.update(tu=tu, tc=tc, read_s=read_s, comp_s=comp_s,
                           write_s=write_s, fsync_s=fsync_s)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                pax.stdout.close()

        threads = [threading.Thread(target=worker)]

    started = time.monotonic()
    sampler.start()
    for t in threads:
        t.start()
    for p in file_list:
        pax.stdin.write(p.encode("utf-8", errors="surrogateescape"))
        pax.stdin.write(b"\0")
    pax.stdin.close()
    for t in threads:
        t.join()
    pax_rc = pax.wait()
    sampler.stop()
    sampler.join()
    wall = time.monotonic() - started

    if writer_mode == "pipe":
        if "error" in box:
            raise box["error"]
        if "error" in wbox:
            raise wbox["error"]
        box.update(write_s=wbox["write_s"], fsync_s=wbox["fsync_s"])
    elif "error" in box:
        raise box["error"]
    box.update(sink=sink, wall=wall, pax_rc=pax_rc, samples=sampler.samples,
               writer_mode=writer_mode)
    return box


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_BLOCKS = "▁▂▃▄▅▆▇█"


def spark(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi <= lo:
        return _BLOCKS[0] * len(values)
    return "".join(_BLOCKS[int((v - lo) / (hi - lo) * (len(_BLOCKS) - 1))]
                   for v in values)


def _rates(samples: list[tuple], idx: int) -> list[float]:
    """MB/s between consecutive samples for cumulative-counter column idx."""
    out = []
    for a, b in zip(samples, samples[1:]):
        dt = b[0] - a[0]
        out.append((b[idx] - a[idx]) / dt / 1e6 if dt > 0 else 0.0)
    return out


def report(m: dict) -> None:
    wall = m["wall"]
    tu, tc = m["tu"], m["tc"]
    ratio = tu / tc if tc else 0
    mode = m.get("writer_mode", "serial")
    print(f"\n=== sink: {m['sink']}  [{mode}] ===")
    print(f"  wall {wall:6.1f}s   in {tu/1e9:5.2f} GB -> out {tc/1e9:5.2f} GB "
          f"(ratio {ratio:.2f}x)   pax_rc={m['pax_rc']}")
    print(f"  SUSTAINED throughput: {tu/1e6/wall:7.1f} MB/s uncompressed | "
          f"{tc/1e6/wall:7.1f} MB/s compressed  ({tc*8/1e9/wall:.2f} Gbit/s on wire)")
    note = "  (read+compress overlap write+sync; %% can exceed 100)" if mode == "pipe" else ""
    print(f"  stage time:  read {m['read_s']:6.1f}s ({100*m['read_s']/wall:4.0f}%)   "
          f"compress {m['comp_s']:6.1f}s ({100*m['comp_s']/wall:4.0f}%)   "
          f"write {m['write_s']:6.1f}s ({100*m['write_s']/wall:4.0f}%)   "
          f"sync {m['fsync_s']:6.1f}s ({100*m['fsync_s']/wall:4.0f}%){note}")
    s = m["samples"]
    if len(s) > 2:
        prod = _rates(s, 4)   # produced (compressed) bytes -> to sink/page cache
        txr = _rates(s, 3)    # NIC tx bytes
        dirty = [x[1] / 1024 for x in s]  # MiB
        print(f"  producer write-rate (to sink):  {spark(prod)}  "
              f"avg {sum(prod)/len(prod):.0f}  max {max(prod):.0f} MB/s")
        print(f"  NIC tx rate            :        {spark(txr)}  "
              f"avg {sum(txr)/len(txr):.0f}  max {max(txr):.0f} MB/s")
        print(f"  Dirty pages (MiB)      :        {spark(dirty)}  "
              f"min {min(dirty):.0f}  max {max(dirty):.0f} MiB")


def _experiment_fsync(file_list, src: Path, nfs_path: Path, iface: str | None,
                      level: int, sync_mb: str) -> int:
    """Compare the serial NFS baseline against a pipelined writer at several
    fdatasync intervals. Tests option A: does keeping the socket continuously
    fed (overlap + steady drain) recover the bursty-writeback throughput loss?"""
    nfs_path.parent.mkdir(parents=True, exist_ok=True)
    intervals = [int(x) for x in sync_mb.split(",") if x.strip()]

    jobs = [
        ("discard-ceiling", "discard", None, "serial", 0),
        ("nfs-serial-baseline", "nfs", nfs_path, "serial", 0),
        ("nfs-pipe-endfsync", "nfs", nfs_path, "pipe", 0),
    ]
    for mb in intervals:
        jobs.append((f"nfs-pipe-fsync{mb}", "nfs", nfs_path, "pipe", mb << 20))

    rows = []
    for label, sink, sp, mode, sb in jobs:
        print(f"\n>>> {label} …", flush=True)
        m = run_once(file_list, src, sink, sp, level=level, compress=True,
                     iface=iface if sink == "nfs" else None,
                     writer_mode=mode, sync_every_bytes=sb)
        m["label"] = label
        report(m)
        rows.append(m)
        if sp is not None:
            try:
                sp.unlink()
            except OSError:
                pass

    base = next((r for r in rows if r["label"] == "nfs-serial-baseline"), None)
    base_mbs = (base["tc"] / 1e6 / base["wall"]) if base else 0
    print("\n" + "=" * 72)
    print("fsync experiment summary (sustained compressed MB/s to NFS):")
    for r in rows:
        mbs = r["tc"] / 1e6 / r["wall"]
        gbit = r["tc"] * 8 / 1e9 / r["wall"]
        vs = f"{mbs/base_mbs:.2f}x baseline" if base_mbs and r["label"] != "discard-ceiling" else ""
        print(f"  {r['label']:24s} {mbs:7.1f} MB/s  ({gbit:4.1f} Gbit/s)  {vs}")
    print("\n  If a pipe-fsync variant lands well above the serial baseline and")
    print("  approaches the discard ceiling / the ~450-480 MB/s 'pushed' rate,")
    print("  option A (continuous writer) is confirmed — and at which interval.")
    return 0


def _shard_by_size(file_list, src: Path, n: int) -> list[list[str]]:
    """Greedy balanced partition of files into n shards by byte size."""
    sized = []
    for rel in file_list:
        p = src / (rel[2:] if rel.startswith("./") else rel)
        try:
            sized.append((p.stat().st_size, rel))
        except OSError:
            sized.append((0, rel))
    sized.sort(reverse=True)
    shards = [[] for _ in range(n)]
    loads = [0] * n
    for sz, rel in sized:
        i = loads.index(min(loads))
        shards[i].append(rel)
        loads[i] += sz
    return shards


def _run_stream(shard, src: Path, out_path: Path, level: int,
                sync_every: int, tcs: list, idx: int) -> None:
    """One independent pax|zstd -> NFS pipeline (its own subprocess + file)."""
    inv = paxlib.PaxInvocation(sources=[], chdir=str(src),
                               archive_path=Path("/dev/null"), excludes=[],
                               extra_mount_excludes=[], framed=True)
    pax = subprocess.Popen(inv.pax_argv_incremental(), cwd=str(src),
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)

    def feed():
        for rel in shard:
            pax.stdin.write(rel.encode("utf-8", errors="surrogateescape"))
            pax.stdin.write(b"\0")
        pax.stdin.close()

    ft = threading.Thread(target=feed)
    ft.start()
    comp = zstd.ZstdCompressor(level=level)
    out = open(out_path, "wb")
    tc = since = 0
    while True:
        chunk = _read_full(pax.stdout, FRAME_SIZE)
        if not chunk:
            break
        data = comp.compress(chunk)
        out.write(data)
        tc += len(data)
        since += len(data)
        tcs[idx] = tc
        if sync_every and since >= sync_every:
            out.flush()
            os.fdatasync(out.fileno())
            since = 0
    out.flush()
    os.fsync(out.fileno())
    out.close()
    pax.stdout.close()
    ft.join()
    pax.wait()
    tcs[idx] = tc


def _experiment_parallel(file_list, src: Path, nfs_dir: Path, iface: str | None,
                         level: int, streams: str) -> int:
    """Write N concurrent archive streams to NFS; measure aggregate throughput
    vs N. Scaling -> per-connection/server-concurrency bound (sharding helps).
    Plateau at the single-stream rate -> pool-sync bound (fix is NAS-side)."""
    nfs_dir.mkdir(parents=True, exist_ok=True)
    sync_every = 256 << 20
    counts = [int(x) for x in streams.split(",") if x.strip()]

    rows = []
    for n in counts:
        shards = _shard_by_size(file_list, src, n)
        tcs = [0] * n
        progress: dict = {}
        sampler = Sampler(iface, progress, produced_fn=lambda: sum(tcs))
        threads = [threading.Thread(target=_run_stream,
                                    args=(shards[i], src, nfs_dir / f"p{i}.pax.zst",
                                          level, sync_every, tcs, i))
                   for i in range(n)]
        print(f"\n>>> {n} stream(s) …", flush=True)
        started = time.monotonic()
        sampler.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sampler.stop()
        sampler.join()
        wall = time.monotonic() - started
        total = sum(tcs)
        mbs = total / 1e6 / wall
        gbit = total * 8 / 1e9 / wall
        rows.append((n, wall, total, mbs, gbit))
        txr = _rates(sampler.samples, 3)
        print(f"  {n} stream(s): wall {wall:6.1f}s   out {total/1e9:5.2f} GB   "
              f"AGGREGATE {mbs:7.1f} MB/s  ({gbit:4.1f} Gbit/s)")
        if txr:
            print(f"    NIC tx: {spark(txr)}  avg {sum(txr)/len(txr):.0f}  max {max(txr):.0f} MB/s")
        for i in range(n):
            try:
                (nfs_dir / f"p{i}.pax.zst").unlink()
            except OSError:
                pass

    print("\n" + "=" * 72)
    print("parallel-streams summary (aggregate compressed MB/s to NFS):")
    base = rows[0][3] if rows else 0
    for n, wall, total, mbs, gbit in rows:
        scale = f"{mbs/base:.2f}x vs 1-stream" if base else ""
        print(f"  {n:2d} stream(s):  {mbs:7.1f} MB/s  ({gbit:4.1f} Gbit/s)  {scale}")
    print("\n  scales with N  -> per-connection/server-concurrency bound: "
          "sharding / nconnect would help.")
    print("  plateaus       -> pool sync-write bound: fix is NAS-side "
          "(SLOG / sync= / pool), not the client.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scratch", default="/home/kim/Workspace",
                    help="Idle local NVMe dir for the dataset + nvme sink.")
    ap.add_argument("--nfs-base", default="/mnt/Backups",
                    help="Backup mount root for the nfs sink.")
    ap.add_argument("--source", default=None,
                    help="Use an existing dir as the source instead of generating one.")
    ap.add_argument("--gen-size-gb", type=float, default=16.0,
                    help="Synthetic dataset size (one-time; reused). Make this > "
                         "vm.dirty_background_ratio * RAM to reach writeback steady state.")
    ap.add_argument("--sinks", default="discard,tmpfs,nvme",
                    help="Comma list: discard,tmpfs,nvme,nfs")
    ap.add_argument("--level", type=int, default=3)
    ap.add_argument("--iface", default=None, help="NIC to sample (auto-detected for nfs).")
    ap.add_argument("--experiment", choices=("fsync", "parallel"), default=None,
                    help="fsync: serial NFS baseline vs a pipelined writer at several "
                         "fdatasync intervals (option A). parallel: N concurrent NFS "
                         "streams to test whether sharding/nconnect would help.")
    ap.add_argument("--sync-mb", default="64,256,1024",
                    help="With --experiment fsync: comma list of fdatasync intervals (MiB).")
    ap.add_argument("--streams", default="1,2,4,8",
                    help="With --experiment parallel: comma list of concurrent stream counts.")
    args = ap.parse_args()

    scratch = Path(args.scratch)
    ram_kb = _meminfo_kb("MemTotal")
    bg = float(_sysctl("vm.dirty_background_ratio") or 0)
    dr = float(_sysctl("vm.dirty_ratio") or 0)
    print("system context:")
    print(f"  RAM {ram_kb/1024/1024:.1f} GiB | dirty_background_ratio={bg}% "
          f"(~{ram_kb*bg/100/1024/1024:.1f} GiB) | dirty_ratio={dr}% "
          f"(~{ram_kb*dr/100/1024/1024:.1f} GiB)")
    print(f"  dirty_expire={_sysctl('vm.dirty_expire_centisecs')}cs | "
          f"dirty_writeback={_sysctl('vm.dirty_writeback_centisecs')}cs")

    if args.source:
        src = Path(args.source)
        file_list = _walk_rel(src)
        print(f"source: {src} (existing, {len(file_list)} files)")
    else:
        src = scratch / "bench_src"
        print(f"dataset: generating/reusing ~{args.gen_size_gb} GiB at {src} …")
        file_list = gen_dataset(src, args.gen_size_gb)
        print(f"  {len(file_list)} files")

    iface = args.iface or iface_to(nfs_server_for(Path(args.nfs_base)) or "")
    if "nfs" in args.sinks:
        print(f"  NFS NIC for sampling: {iface or '(all non-lo, summed)'}")

    sink_paths = {
        "discard": None,
        "tmpfs": Path("/dev/shm/tt_bench/bench.pax.zst"),
        "nvme": scratch / "bench_out" / "bench.pax.zst",
        "nfs": Path(args.nfs_base) / NFS_BENCH_SUBDIR / "bench.pax.zst",
    }

    if args.experiment == "fsync":
        return _experiment_fsync(file_list, src, sink_paths["nfs"], iface,
                                 args.level, args.sync_mb)
    if args.experiment == "parallel":
        return _experiment_parallel(file_list, src,
                                    Path(args.nfs_base) / NFS_BENCH_SUBDIR, iface,
                                    args.level, args.streams)

    results = []
    for sink in [s.strip() for s in args.sinks.split(",") if s.strip()]:
        sp = sink_paths[sink]
        if sp is not None:
            sp.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n>>> running sink={sink} …", flush=True)
        m = run_once(file_list, src, sink, sp, level=args.level,
                     compress=True, iface=iface if sink == "nfs" else None)
        report(m)
        results.append(m)
        if sp is not None:
            try:
                sp.unlink()
                framewriter.sidecar_path(sp).unlink(missing_ok=True)
            except OSError:
                pass

    print("\n" + "=" * 72)
    print("interpretation:")
    print("  * discard slow + a core pegged + compress% high -> COMPRESSION-bound")
    print("  * discard slow + read% high                     -> SOURCE-READ/tar-bound")
    print("  * nvme fast+smooth but nfs sawtooths            -> NFS/network path")
    print("  * write% ~0 + big final-fsync + Dirty climbs    -> WRITEBACK batching")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
