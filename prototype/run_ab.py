#!/usr/bin/env python3
"""A/B test harness: framed write vs the existing winboat-test baseline.

Reproduces TimeTraveller's tar invocation for the winboat-test plan but
pipes the tar stream into prototype/frame_write.py instead of zstd. Writes
the framed archive alongside the baseline on the NAS so both runs share
the same write bottleneck. After the framed run completes, compares:

  - wall-clock vs baseline's recorded 2943.6s
  - final archive size vs baseline's 110.2 GiB
  - byte-identity of the decompressed streams (cmp <(zstdcat) <(zstdcat))
  - frame-index size

The baseline is NOT re-run. Its size and elapsed time come from the
manifest entry written by the original 2026-05-19T16:06:04 backup.

This script does not modify anything under timetraveller/ — it imports
the pure helpers (iter_archivable_files, glob_to_regexes, etc.) to keep
the file-walk identical to production, but routes I/O entirely through
the prototype.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _REPO)

from timetraveller import config as configlib
from timetraveller import manifest as manifestlib
from timetraveller import mounts as mountslib
from timetraveller import pax as paxlib


PLAN_NAME = "winboat-test"
BASELINE = Path("/mnt/Backups/timetraveller/bast/winboat-test/"
                "2026-05-19T160604_full.pax.zst")
# FRAMED_TAG (env) gets suffixed to the output filename so different
# framer experiments can coexist on disk for comparison.
_FRAMED_TAG = os.environ.get("FRAMED_TAG", "")
FRAMED = Path("/mnt/Backups/timetraveller/bast/winboat-test/"
              f"2026-05-19T160604_full_FRAMED{_FRAMED_TAG}.pax.zst")
FRAMED_INDEX = Path("/mnt/Backups/timetraveller/bast/winboat-test/"
                    f"2026-05-19T160604_full_FRAMED{_FRAMED_TAG}.pax.zst.frames.json")
FRAME_SIZE = "64M"
# FRAMER_NAME (env) picks which framer script to invoke. Default is the
# original subprocess-based prototype.
FRAMER_NAME = os.environ.get("FRAMER_NAME", "frame_write.py")


def _human(n: int) -> str:
    for unit, div in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= div:
            return f"{n/div:.2f} {unit}"
    return f"{n} B"


def _baseline_stats() -> tuple[int, float]:
    """Pull the baseline's size + elapsed seconds from the on-mount manifest.

    Falls back to the file's stat() if the manifest entry is missing.
    """
    archive_dir = BASELINE.parent
    m = manifestlib.load(manifestlib.manifest_path(archive_dir))
    for a in m.archives:
        if a.filename == BASELINE.name:
            try:
                from datetime import datetime
                start = datetime.fromisoformat(a.date_started)
                end = datetime.fromisoformat(a.date_finished)
                elapsed = (end - start).total_seconds()
                return a.size_bytes, elapsed
            except Exception:
                pass
    return BASELINE.stat().st_size, float("nan")


def _build_file_iter(plan: configlib.PlanConfig):
    """Mirror TimeTraveller's action_backup setup for kind=full."""
    report = mountslib.filter_sources(
        plan.sources, plan.destination,
        include_removable=plan.include_removable,
        include_nfs=plan.include_nfs,
        include_cifs=plan.include_cifs,
        include_mounts=plan.include_mounts,
        exclude_mounts=plan.exclude_mounts,
    )
    sources_abs = [str(Path(s).resolve()) for s in plan.sources]
    excludes_re: list[str] = []
    for g in plan.excludes:
        excludes_re.extend(paxlib.glob_to_regexes(g))
    return paxlib.iter_archivable_files(
        sources_abs, excludes_re, report.additional_excludes,
        mtime_window=None,
        include_dirs=True,
        one_filesystem=True,
        skip_special=True,
    )


def _run_framed(plan: configlib.PlanConfig) -> tuple[float, int, int, int]:
    """Spawn the tar | frame_write.py pipeline. Returns (elapsed, n_files,
    archive_size, index_size)."""
    file_iter = _build_file_iter(plan)
    tar_argv = [
        "tar", "--format=pax", "--no-recursion", "--null", "--files-from=-", "-c",
    ]
    framer_argv = [
        sys.executable, str(Path(__file__).parent / FRAMER_NAME),
        "--frame-size", FRAME_SIZE,
        "--zstd-level", "3",
        "--index-out", str(FRAMED_INDEX),
        str(FRAMED),
    ]

    print(f"tar argv:    {' '.join(tar_argv)}", file=sys.stderr)
    print(f"framer argv: {' '.join(framer_argv)}", file=sys.stderr)
    print(f"output:      {FRAMED}", file=sys.stderr)
    print(f"index:       {FRAMED_INDEX}", file=sys.stderr)

    FRAMED.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    tar = subprocess.Popen(
        tar_argv, cwd="/",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    framer = subprocess.Popen(
        framer_argv,
        stdin=tar.stdout,
        stderr=sys.stderr,
    )
    tar.stdout.close()

    n = 0
    assert tar.stdin is not None
    write = tar.stdin.write
    for path in file_iter:
        write(path.encode("utf-8", errors="surrogateescape"))
        write(b"\0")
        n += 1
    tar.stdin.close()

    framer_rc = framer.wait()
    tar_rc = tar.wait()
    elapsed = time.monotonic() - started

    if tar_rc not in (0, 1):
        # tar exit 1 = "file changed during read" warnings, same as the
        # tri-state we already accept in production. Anything else is bad.
        raise RuntimeError(f"tar failed (rc={tar_rc})")
    if framer_rc != 0:
        raise RuntimeError(f"framer failed (rc={framer_rc})")

    return (elapsed, n,
            FRAMED.stat().st_size if FRAMED.exists() else 0,
            FRAMED_INDEX.stat().st_size if FRAMED_INDEX.exists() else 0)


def _verify_byte_identity() -> bool:
    """Run cmp <(zstdcat baseline) <(zstdcat framed) and report."""
    print(f"\n=== byte-identity check ===", file=sys.stderr)
    print(f"streaming both archives through zstdcat and cmp...", file=sys.stderr)
    cmd = f"cmp <(zstdcat {BASELINE}) <(zstdcat {FRAMED})"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("✓ byte-identical decompression — framed archive is interchangeable",
              file=sys.stderr)
        return True
    print(f"✗ MISMATCH (cmp rc={result.returncode})", file=sys.stderr)
    if result.stdout:
        print(f"stdout: {result.stdout}", file=sys.stderr)
    if result.stderr:
        print(f"stderr: {result.stderr}", file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    config_path = Path.home() / ".config" / "timetraveller" / f"{PLAN_NAME}.yaml"
    plan = configlib.load(config_path)

    print(f"=== baseline ===", file=sys.stderr)
    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 1
    baseline_size, baseline_elapsed = _baseline_stats()
    print(f"path:    {BASELINE}", file=sys.stderr)
    print(f"size:    {baseline_size:,} bytes ({_human(baseline_size)})", file=sys.stderr)
    print(f"elapsed: {baseline_elapsed:.1f}s ({baseline_elapsed/60:.1f} min)",
          file=sys.stderr)

    if FRAMED.exists():
        print(f"\nfound stale framed run at {FRAMED} — removing", file=sys.stderr)
        FRAMED.unlink()
    if FRAMED_INDEX.exists():
        FRAMED_INDEX.unlink()

    print(f"\n=== framed run (frame-size={FRAME_SIZE}) ===", file=sys.stderr)
    elapsed, n, archive_size, index_size = _run_framed(plan)

    print(f"\n=== framed results ===", file=sys.stderr)
    print(f"files:      {n}", file=sys.stderr)
    print(f"archive:    {archive_size:,} bytes ({_human(archive_size)})", file=sys.stderr)
    print(f"index:      {index_size:,} bytes ({_human(index_size)})", file=sys.stderr)
    print(f"elapsed:    {elapsed:.1f}s ({elapsed/60:.1f} min)", file=sys.stderr)

    print(f"\n=== comparison ===", file=sys.stderr)
    size_delta_pct = 100 * (archive_size - baseline_size) / baseline_size
    time_delta_pct = (100 * (elapsed - baseline_elapsed) / baseline_elapsed
                      if baseline_elapsed == baseline_elapsed else float("nan"))
    print(f"size delta: {size_delta_pct:+.2f}% "
          f"({archive_size - baseline_size:+,} bytes)", file=sys.stderr)
    print(f"time delta: {time_delta_pct:+.2f}% "
          f"({elapsed - baseline_elapsed:+.1f}s)", file=sys.stderr)
    print(f"index/archive ratio: {100*index_size/archive_size:.4f}%", file=sys.stderr)

    identical = _verify_byte_identity()
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
