#!/usr/bin/env python3
"""In-process framed zstd writer using the `zstandard` library.

Drop-in CLI replacement for frame_write.py — same flags, same output
layout, same frame-index format — that eliminates per-frame fork/exec
of `zstd` by calling libzstd in-process via ZstdCompressor.compress().

Purpose: isolate the +11% wall-clock penalty observed in the
subprocess-based prototype run on 2026-05-19. If this version closes
the gap, fork overhead was the cause. If it doesn't, the cause is
elsewhere (likely lack of I/O/CPU overlap) and we'd next try a
writer-thread pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import zstandard as zstd


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame-size", default="64M",
                    help="Uncompressed bytes per zstd frame (default: 64M).")
    ap.add_argument("--zstd-level", type=int, default=3,
                    help="zstd compression level (default: 3).")
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

    cctx = zstd.ZstdCompressor(level=args.zstd_level)
    started = time.monotonic()
    frames: list[dict] = []
    uo = 0
    co = 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "wb") as out:
        while True:
            buf = sys.stdin.buffer.read(frame_size)
            if not buf:
                break
            # ZstdCompressor.compress() emits a complete, self-contained
            # zstd frame (magic bytes + frame header + data + checksum)
            # — concatenating these is a valid multi-frame zstd stream,
            # byte-compatible with what `zstd -3 -c` produces per-chunk.
            compressed = cctx.compress(buf)
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

    print(f"frames:          {len(frames)}", file=sys.stderr)
    print(f"uncompressed:    {uo:,} bytes ({uo/1024**3:.2f} GiB)", file=sys.stderr)
    print(f"compressed:      {co:,} bytes ({co/1024**3:.2f} GiB, "
          f"{100*co/uo:.1f}% of uncompressed)" if uo else "compressed: 0",
          file=sys.stderr)
    print(f"elapsed:         {elapsed:.1f}s ({elapsed/60:.1f} min)", file=sys.stderr)
    print(f"throughput:      {uo/elapsed/1024**2:.1f} MiB/s uncompressed"
          if elapsed else "", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
