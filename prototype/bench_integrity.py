#!/usr/bin/env python3
"""Integrity-mitigation cost harness — measure the two numbers that decide the
D1 (verify-after-write) vs D3 (inline par2 parity) balance BEFORE we touch the
production write path.

Motivated by the 2026-06-28 home full: 2 isolated bad frames in 17,732, a
non-ECC single-event-upset signature. Both mitigations detect/repair that, but
they spend different budgets — D1 spends a read pass, D3 spends CPU. This
harness measures both against the real target (the NAS mount) and the real
producer ceiling (~490 MB/s, tar-bound).

Experiments:
  verify  - write a framed zstd archive to the NAS, fsync, then verify-after-
            write it: DROP the client page cache (posix_fadvise DONTNEED, so we
            read what actually LANDED, not our own just-written buffer) and
            re-read the compressed frame ranges + re-hash. Reports write MB/s,
            verify MB/s (cache-dropped AND cached, to prove the drop worked),
            and the serial wall-clock overhead fraction. The huge local page
            cache (100+ GiB) makes the DONTNEED drop essential — without it the
            "verify" just measures RAM.
  parity  - measure Reed-Solomon encode throughput on THIS cpu with a real
            (table-based GF(2^8)) inner loop — a conservative floor vs a SIMD
            par2 implementation. Answers: does inline parity keep up with the
            ~490 MB/s producer, per core and across cores?

Example:
  python3 prototype/bench_integrity.py --experiment both --gen-size-gb 8
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import numpy as np  # noqa: E402

from timetraveller import framewriter  # noqa: E402

NFS_BENCH_SUBDIR = "timetraveller/_bench"


# --------------------------------------------------------------------------- #
# D1: verify-after-write
# --------------------------------------------------------------------------- #

class MixedStream:
    """A BinaryIO-ish source of `total` bytes with a realistic zstd ratio:
    each 64 MiB block is half incompressible (urandom) and half compressible
    (repeated text), so frames compress ~1.5-2x like a real home tree."""

    def __init__(self, total: int):
        self.total = total
        self.sent = 0
        rnd = os.urandom(32 << 20)                       # 32 MiB incompressible
        txt = (b"the quick brown fox 0123456789\n" * (1 << 20))[:32 << 20]  # 32 MiB compressible
        self._pat = rnd + txt                            # 64 MiB pattern
        self._plen = len(self._pat)

    def read(self, n: int) -> bytes:
        if self.sent >= self.total:
            return b""
        n = min(n, self.total - self.sent)
        off = self.sent % self._plen
        if off + n <= self._plen:
            out = self._pat[off:off + n]
        else:                                            # wrap the pattern
            out = self._pat[off:] + self._pat[:n - (self._plen - off)]
        self.sent += n
        return out


def _hash_frames(path: Path, frames: list[dict], *, drop_cache: bool) -> tuple[float, int]:
    """Re-read each frame's [co, co+cl) compressed range and SHA-256 it, exactly
    as a real verify-after-write would. Returns (seconds, bytes_read)."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        if drop_cache:
            # Evict this file's pages from the CLIENT page cache so pread goes to
            # the server (which serves from its ARC, hot right after write). This
            # is what makes the number mean "verify what landed", not "re-read RAM".
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        t = time.monotonic()
        nbytes = 0
        for fr in frames:
            data = os.pread(fd, fr["cl"], fr["co"])
            if len(data) != fr["cl"] or hashlib.sha256(data).hexdigest() != fr["csum"]:
                raise RuntimeError(f"verify mismatch at frame {fr['id']}")
            nbytes += len(data)
        return time.monotonic() - t, nbytes
    finally:
        os.close(fd)


def experiment_verify(nfs_dir: Path, size_gb: float) -> None:
    nfs_dir.mkdir(parents=True, exist_ok=True)
    archive = nfs_dir / "verify_bench.pax.zst"
    total = int(size_gb * (1 << 30))
    print(f"\n=== D1: verify-after-write cost  ({size_gb:.1f} GiB uncompressed → NFS) ===")

    try:
        # --- write phase (real framed writer, incl. inline sha256 + fsync) ---
        t = time.monotonic()
        result = framewriter.write_framed(MixedStream(total), archive)
        write_s = time.monotonic() - t
        frames = result["frames"]
        comp = result["total_compressed"]
        uncomp = result["total_uncompressed"]
        print(f"  write:          {uncomp/1e9:5.2f} GB in → {comp/1e9:5.2f} GB out "
              f"({uncomp/comp:.2f}x)  in {write_s:5.1f}s")
        print(f"                  {uncomp/1e6/write_s:7.1f} MB/s uncompressed | "
              f"{comp/1e6/write_s:7.1f} MB/s compressed-to-NAS  ({len(frames)} frames)")

        # --- verify phase: cache-dropped (the real cost) vs cached (the trap) ---
        drop_s, drop_bytes = _hash_frames(archive, frames, drop_cache=True)
        cache_s, _ = _hash_frames(archive, frames, drop_cache=False)
        print(f"  verify (DROP):  {drop_bytes/1e6/drop_s:7.1f} MB/s  ({drop_s:5.1f}s)  "
              f"← reads from NAS (what landed)")
        print(f"  verify (cache): {drop_bytes/1e6/cache_s:7.1f} MB/s  ({cache_s:5.1f}s)  "
              f"← from local page cache (NOT a real verify)")

        # --- the number that answers the worry ---
        serial_overhead = drop_s / write_s
        print(f"\n  SERIAL overhead: verify adds {drop_s:.1f}s to a {write_s:.1f}s write "
              f"= +{100*serial_overhead:.0f}% wall-clock if run strictly after the write.")
        print(f"  With per-shard OVERLAP (verify shard i while shard i+1 still writes),")
        print(f"  the real add is a fraction of that — bounded by read/write contention")
        print(f"  on the NAS, not by the full re-read time.")
        if cache_s > 0 and drop_s / cache_s > 3:
            print(f"  (cache-drop confirmed working: dropped read is "
                  f"{drop_s/cache_s:.0f}x slower than the cached re-read.)")
    finally:
        for p in (archive, framewriter.sidecar_path(archive)):
            try:
                p.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# D3: Reed-Solomon encode throughput (table-based GF(2^8) — conservative floor)
# --------------------------------------------------------------------------- #

def _gf_tables():
    exp = np.zeros(512, dtype=np.uint16)
    log = np.zeros(256, dtype=np.uint16)
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11d
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


def _gf_mul_acc(acc: np.ndarray, block: np.ndarray, c: int, exp, log) -> None:
    """acc ^= block * c   over GF(2^8), table-based (a real RS inner loop)."""
    if c == 0:
        return
    if c == 1:
        acc ^= block
        return
    lc = int(log[c])
    prod = exp[log[block].astype(np.uint16) + lc].astype(np.uint8)
    prod[block == 0] = 0
    acc ^= prod


def experiment_parity(producer_mbs: float = 490.0) -> None:
    exp, log = _gf_tables()
    K = 16                     # data blocks per group
    P = 2                      # parity blocks (2/16 = 12.5% redundancy, par2-ish)
    block = 16 << 20           # 16 MiB blocks
    rng = np.random.default_rng(1)
    data = [rng.integers(0, 256, size=block, dtype=np.uint8) for _ in range(K)]
    # Distinct nonzero coefficients per (parity, data) pair — a Vandermonde-ish row.
    coeffs = [[(1 + ((j * 31 + i * 7) % 255)) for i in range(K)] for j in range(P)]

    print(f"\n=== D3: Reed-Solomon encode throughput  (GF(2^8) table-based floor) ===")
    print(f"  config: {K} data + {P} parity blocks ({100*P/K:.0f}% redundancy), "
          f"{block>>20} MiB blocks")

    # XOR-only floor (single parity, all coeffs=1) — the memory-bandwidth ceiling.
    iters = 6
    t = time.monotonic()
    for _ in range(iters):
        acc = data[0].copy()
        for i in range(1, K):
            acc ^= data[i]
    xor_s = time.monotonic() - t
    xor_mbs = iters * K * block / 1e6 / xor_s
    print(f"  XOR parity floor (P=1): {xor_mbs:8.0f} MB/s/core source  "
          f"(memory-bandwidth ceiling)")

    # Real GF RS encode: P parity blocks, distinct coefficients.
    iters = 3
    t = time.monotonic()
    for _ in range(iters):
        for j in range(P):
            acc = np.zeros(block, dtype=np.uint8)
            for i in range(K):
                _gf_mul_acc(acc, data[i], coeffs[j][i], exp, log)
    rs_s = time.monotonic() - t
    rs_mbs = iters * K * block / 1e6 / rs_s     # source bytes consumed
    print(f"  RS encode (P={P}):        {rs_mbs:8.0f} MB/s/core source  "
          f"(table GF; a SIMD par2 impl is 3-5x faster)")

    cores = os.cpu_count() or 1
    shards = min(8, cores)
    print(f"\n  vs producer ceiling ~{producer_mbs:.0f} MB/s:")
    print(f"    single-core RS: {rs_mbs:.0f} MB/s  → "
          f"{'KEEPS UP' if rs_mbs >= producer_mbs else 'BOTTLENECK single-core'}")
    print(f"    {shards} shards each on a core: ~{rs_mbs*shards:.0f} MB/s aggregate  → "
          f"{'ample headroom' if rs_mbs*shards >= producer_mbs else 'may bottleneck'}")
    print(f"  (Backups already shard across cores, so parity rides the same "
          f"per-shard parallelism the writer uses.)")


# --------------------------------------------------------------------------- #

def experiment_overlap(nfs_dir: Path, size_gb: float) -> None:
    """Does a verify-read coexist with a concurrent shard write, or do they
    strangle each other? This is the linchpin of 'verify-after-write is cheap':
    if reader+writer each keep ~their solo throughput, per-shard overlap works
    and the real cost is only the last shard's tail verify."""
    import threading
    nfs_dir.mkdir(parents=True, exist_ok=True)
    a = nfs_dir / "overlap_a.pax.zst"
    b = nfs_dir / "overlap_b.pax.zst"
    total = int(size_gb * (1 << 30))
    print(f"\n=== overlap: concurrent verify-read + shard-write ===")
    try:
        ra = framewriter.write_framed(MixedStream(total), a)["frames"]

        # solo baselines
        w_t = time.monotonic()
        rb = framewriter.write_framed(MixedStream(total), b)["frames"]
        w_solo = sum(f["cl"] for f in rb) / 1e6 / (time.monotonic() - w_t)
        r_s, r_bytes = _hash_frames(a, ra, drop_cache=True)
        r_solo = r_bytes / 1e6 / r_s
        b.unlink(); framewriter.sidecar_path(b).unlink(missing_ok=True)

        # concurrent: writer writes B once; reader re-verifies A (dropped) until writer done.
        box = {}
        stop = threading.Event()

        def writer():
            t = time.monotonic()
            fr = framewriter.write_framed(MixedStream(total), b)["frames"]
            box["w"] = sum(f["cl"] for f in fr) / 1e6 / (time.monotonic() - t)
            stop.set()

        def reader():
            rb_total = 0.0; rt = 0.0
            while not stop.is_set():
                s, n = _hash_frames(a, ra, drop_cache=True)
                rb_total += n; rt += s
            box["r"] = rb_total / 1e6 / rt if rt else 0.0

        wt = threading.Thread(target=writer); rt_ = threading.Thread(target=reader)
        wt.start(); rt_.start(); wt.join(); rt_.join()

        print(f"  solo:        write {w_solo:6.0f} MB/s | verify-read {r_solo:6.0f} MB/s")
        print(f"  concurrent:  write {box['w']:6.0f} MB/s | verify-read {box['r']:6.0f} MB/s")
        print(f"  retention:   write {100*box['w']/w_solo:3.0f}% | "
              f"read {100*box['r']/r_solo:3.0f}%  "
              f"(sum {box['w']+box['r']:.0f} MB/s on the link)")
        if box['w'] >= 0.8 * w_solo:
            print("  → writes barely slowed by concurrent verify → OVERLAP WORKS: "
                  "verify-after-write hides behind sibling-shard writes.")
        else:
            print("  → concurrent verify measurably steals write bandwidth; prefer "
                  "verifying AFTER the write burst, or throttle the verify reads.")
    finally:
        for p in (a, b, framewriter.sidecar_path(a), framewriter.sidecar_path(b)):
            try:
                p.unlink()
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nfs-base", default="/mnt/Backups",
                    help="Backup mount root (the real verify target).")
    ap.add_argument("--gen-size-gb", type=float, default=8.0,
                    help="Uncompressed data to write+verify for the D1 measurement.")
    ap.add_argument("--producer-mbs", type=float, default=490.0,
                    help="Producer ceiling to compare D3 against (~tar-bound).")
    ap.add_argument("--experiment", choices=("verify", "parity", "overlap", "both"),
                    default="both")
    args = ap.parse_args()

    nfs_dir = Path(args.nfs_base) / NFS_BENCH_SUBDIR
    if args.experiment in ("verify", "both"):
        experiment_verify(nfs_dir, args.gen_size_gb)
    if args.experiment in ("overlap", "both"):
        experiment_overlap(nfs_dir, args.gen_size_gb)
    if args.experiment in ("parity", "both"):
        experiment_parity(args.producer_mbs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
