#!/usr/bin/env python3
"""Phase 0 prototype: sharded backup via N parallel framed pax|zstd streams.

Proves the core sharding claim using the REAL write path — `pax.run_with_file_list`
(framed=True), which builds each shard's inline `.idx.zst` + `.frames.json` exactly
as a production backup would (v1.0.7). No app changes; this only validates that:

  1. union-completeness: every source member lands in EXACTLY one shard, and the
     union across N shards == the complete single-stream (N=1) backup;
  2. restore: a file extracted from its owning shard via `extract.extract_files`
     is byte-identical to the source (incl. a symlink; hardlinks are skipped by
     the existing fast-extract path, not a sharding regression);
  3. throughput: N parallel shards vs one stream, to a chosen sink.

Run:  python3 prototype/shard_backup.py [--shards 4] [--dest DIR] [--size-mb 800]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import extract as extractlib  # noqa: E402
from timetraveller import index as indexlib  # noqa: E402
from timetraveller import pax as paxlib  # noqa: E402


def _shard_by_size(members_with_size, n):
    """Greedy balanced partition of (size, member) into n shards by byte size."""
    shards = [[] for _ in range(n)]
    loads = [0] * n
    for sz, rel in sorted(members_with_size, reverse=True):
        i = loads.index(min(loads))
        shards[i].append(rel)
        loads[i] += sz
    return shards


def _members_of(sidecar: Path) -> set[str]:
    """Set of member names recorded in a v2 .idx.zst (skips the header line)."""
    lines = indexlib.read_sidecar(sidecar)
    return {json.loads(ln)["name"] for ln in lines[1:] if ln.strip()}


def _abs_of(member: str) -> Path:
    """Member './tmp/x/f' (relative to chdir=/) -> absolute /tmp/x/f."""
    return Path("/") / member[2:]


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_tree(src: Path, size_mb: int) -> None:
    src.mkdir(parents=True, exist_ok=True)
    (src / "big").mkdir(exist_ok=True)
    (src / "docs").mkdir(exist_ok=True)
    written = 0
    i = 0
    while written < size_mb * (1 << 20) * 0.9:
        f = src / "big" / f"f{i:03d}.bin"
        with open(f, "wb") as fh:
            payload = os.urandom(8 << 20) if i % 2 == 0 else b"x" * (8 << 20)
            for _ in range(8):  # 64 MiB
                fh.write(payload)
                written += len(payload)
        i += 1
    for j in range(300):
        (src / "docs" / f"d{j:04d}.txt").write_text(f"doc {j}\n" * (j % 40 + 1))
    # Edge cases: long name (pax extended header), symlink, hardlink.
    deep = src / ("d" * 90)
    deep.mkdir(exist_ok=True)
    (deep / ("n" * 130 + ".txt")).write_text("long-name payload\n")
    (src / "target.bin").write_bytes(b"\x00" * 8192)
    (src / "sym.link").symlink_to("target.bin")
    os.link(src / "target.bin", src / "hard.link")


def _run_shard(archive_path: Path, files: list[str], box: dict, idx: int) -> None:
    inv = paxlib.PaxInvocation(sources=[], chdir="/", archive_path=archive_path,
                               excludes=[], extra_mount_excludes=[], framed=True)
    box[idx] = paxlib.run_with_file_list(inv, iter(files))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--dest", default=None, help="archive dir (default: temp). Use NFS path for a real throughput test.")
    ap.add_argument("--size-mb", type=int, default=800)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="tt_shard_proto_"))
    src = tmp / "src"
    dest = Path(args.dest) if args.dest else (tmp / "out")
    dest.mkdir(parents=True, exist_ok=True)
    print(f"workdir={tmp}  dest={dest}  shards={args.shards}")
    _make_tree(src, args.size_mb)

    members = list(paxlib.iter_archivable_files([str(src)], [], [], mtime_window=None,
                                                include_dirs=True, one_filesystem=True,
                                                skip_special=True))
    sized = []
    for m in members:
        p = _abs_of(m)
        try:
            sized.append((p.stat().st_size if p.is_file() and not p.is_symlink() else 0, m))
        except OSError:
            sized.append((0, m))
    print(f"enumerated {len(members)} members")

    # --- single-stream reference (N=1) ---
    ref = dest / "ref.pax.zst"
    t0 = time.monotonic()
    r1 = paxlib.run_with_file_list(
        paxlib.PaxInvocation(sources=[], chdir="/", archive_path=ref, excludes=[],
                             extra_mount_excludes=[], framed=True), iter(members))
    single_s = time.monotonic() - t0
    assert r1.status in ("ok", "ok-with-warnings"), r1.status
    ref_members = _members_of(indexlib.sidecar_path(ref))

    # --- N parallel shards ---
    shards = _shard_by_size(sized, args.shards)
    box: dict = {}
    threads = [threading.Thread(target=_run_shard,
                                args=(dest / f"shard.s{i+1}of{args.shards}.pax.zst", shards[i], box, i))
               for i in range(args.shards)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    shard_s = time.monotonic() - t0
    for i in range(args.shards):
        assert box[i].status in ("ok", "ok-with-warnings"), (i, box[i].status)

    # --- (1) union-completeness ---
    shard_sidecars = [indexlib.sidecar_path(dest / f"shard.s{i+1}of{args.shards}.pax.zst")
                      for i in range(args.shards)]
    per_shard = [_members_of(sc) for sc in shard_sidecars]
    union = set().union(*per_shard)
    # disjoint?
    overlaps = []
    for a in range(len(per_shard)):
        for b in range(a + 1, len(per_shard)):
            dup = per_shard[a] & per_shard[b]
            if dup:
                overlaps.append((a, b, len(dup)))
    missing = ref_members - union
    extra = union - ref_members
    print("\n--- union-completeness ---")
    print(f"  ref members: {len(ref_members)} | shard union: {len(union)} | "
          f"per-shard: {[len(s) for s in per_shard]}")
    print(f"  overlaps (should be none): {overlaps}")
    print(f"  missing from union: {len(missing)} | extra in union: {len(extra)}")
    ok_union = not overlaps and not missing and not extra
    print(f"  UNION-COMPLETE: {ok_union}")

    # --- (2) restore from owning shard (a regular file + the symlink) ---
    print("\n--- restore ---")
    ok_restore = True
    targets = [m for m in members if m.endswith("target.bin") or m.endswith("sym.link")
               or ("n" * 130) in m]
    restore_dir = tmp / "restore"
    for member in targets:
        owner = next((i for i in range(args.shards) if member in per_shard[i]), None)
        assert owner is not None, f"{member} in no shard"
        shard_arc = dest / f"shard.s{owner+1}of{args.shards}.pax.zst"
        st = extractlib.extract_files(shard_arc, [member], into=restore_dir)
        out = restore_dir / member[2:]
        srcp = _abs_of(member)
        if srcp.is_symlink():
            same = out.is_symlink() and os.readlink(out) == os.readlink(srcp)
        else:
            same = out.exists() and _sha(out) == _sha(srcp)
        print(f"  shard {owner+1}: {member[-40:]:40s} restored={same} (fast={not st.fallback_naive})")
        ok_restore = ok_restore and same

    # --- (3) throughput ---
    ref_bytes = ref.stat().st_size
    shard_bytes = sum((dest / f"shard.s{i+1}of{args.shards}.pax.zst").stat().st_size
                      for i in range(args.shards))
    print("\n--- throughput ---")
    print(f"  single-stream: {single_s:6.2f}s  ({ref_bytes/1e6/single_s:6.1f} MB/s out)")
    print(f"  {args.shards}-shard parallel: {shard_s:6.2f}s  ({shard_bytes/1e6/shard_s:6.1f} MB/s out)  "
          f"speedup {single_s/shard_s:.2f}x")

    print("\nRESULT:", "PASS" if (ok_union and ok_restore) else "FAIL")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    if args.dest:  # clean shard/ref files we wrote into a real dest
        for p in dest.glob("shard.s*"):
            p.unlink(missing_ok=True)
            indexlib.sidecar_path(p).unlink(missing_ok=True)
        for suf in ("", ".idx.zst", ".frames.json"):
            (dest / f"ref.pax.zst{suf}").unlink(missing_ok=True)
    return 0 if (ok_union and ok_restore) else 1


if __name__ == "__main__":
    raise SystemExit(main())
