#!/usr/bin/env python3
"""Framed zstd archive writer (prototype).

Reads a stream from stdin, splits it into fixed-size chunks, compresses each
chunk as an independent zstd frame, concatenates the frames into the output
file. Emits a JSON frame index alongside.

Output is a valid concatenated zstd stream — any `zstdcat` decompresses it
sequentially, byte-identical to the equivalent monolithic compression. The
frame index is the side-channel that lets a future fast-extract tool seek
into the middle of the archive.

This script is for prototype/measurement use only — it lives outside the
timetraveller package and is not imported by anything in production.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1024, s[:-1]
    elif s.endswith("M"):
        mult, s = 1024 * 1024, s[:-1]
    elif s.endswith("G"):
        mult, s = 1024 ** 3, s[:-1]
    elif s.endswith("B"):
        s = s[:-1]
    return int(s) * mult


def _read_exact(fp, n: int) -> bytes:
    """Read up to n bytes; returns whatever is available, possibly < n at EOF.

    BufferedReader.read(n) already does this — but we make the semantics
    explicit in case someone swaps in an unbuffered stream later.
    """
    buf = fp.read(n)
    return buf or b""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame-size", default="64M",
                    help="Uncompressed bytes per zstd frame (default: 64M). "
                         "Must be a tar-block multiple (i.e. a multiple of 512) "
                         "to keep frame boundaries tar-aligned.")
    ap.add_argument("--zstd-level", type=int, default=3,
                    help="zstd compression level (default: 3, matches TimeTraveller).")
    ap.add_argument("--index-out", type=Path, required=True,
                    help="Path to write the frame-index JSON sidecar.")
    ap.add_argument("output", type=Path,
                    help="Path to write the framed zstd archive.")
    args = ap.parse_args(argv)

    frame_size = _parse_size(args.frame_size)
    if frame_size % 512 != 0:
        print(f"frame-size must be a multiple of 512 (tar block size); "
              f"got {frame_size}", file=sys.stderr)
        return 2

    started = time.monotonic()
    frames: list[dict] = []
    uo = 0  # cumulative uncompressed offset
    co = 0  # cumulative compressed offset

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "wb") as out:
        while True:
            buf = _read_exact(sys.stdin.buffer, frame_size)
            if not buf:
                break

            # Single-threaded zstd on each chunk. On a 64 MiB input at level 3,
            # this takes ~150ms on modern CPUs — well within the budget that
            # NFS write throughput (~38 MB/s) imposes on us anyway.
            zstd = subprocess.run(
                ["zstd", f"-{args.zstd_level}", "-q", "-c"],
                input=buf,
                capture_output=True,
                check=True,
            )
            compressed = zstd.stdout
            out.write(compressed)

            frames.append({
                "id": len(frames),
                "uo": uo,
                "ul": len(buf),
                "co": co,
                "cl": len(compressed),
            })
            uo += len(buf)
            co += len(compressed)

    elapsed = time.monotonic() - started

    index = {
        "version": 1,
        "frame_size": frame_size,
        "zstd_level": args.zstd_level,
        "total_uncompressed": uo,
        "total_compressed": co,
        "elapsed_seconds": elapsed,
        "frame_count": len(frames),
        "frames": frames,
    }
    args.index_out.parent.mkdir(parents=True, exist_ok=True)
    args.index_out.write_text(json.dumps(index, indent=2))

    # Summary to stderr — stdout is reserved for tool composability later.
    print(f"frames:          {len(frames)}", file=sys.stderr)
    print(f"uncompressed:    {uo:,} bytes ({uo/1024**3:.2f} GiB)", file=sys.stderr)
    print(f"compressed:      {co:,} bytes ({co/1024**3:.2f} GiB, "
          f"{100*co/uo:.1f}% of uncompressed)" if uo else "compressed: 0",
          file=sys.stderr)
    print(f"elapsed:         {elapsed:.1f}s ({elapsed/60:.1f} min)", file=sys.stderr)
    print(f"throughput:      {uo/elapsed/1024**2:.1f} MiB/s uncompressed" if elapsed else "",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
