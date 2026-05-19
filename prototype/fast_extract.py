#!/usr/bin/env python3
"""Fast-extract prototype (Phase D).

Extracts files from a framed `.pax.zst` archive without decompressing
the whole stream. Composes:

  file-index   (path -> uncompressed offset/length)   from scan_file_index.py
  frame-index  (uncompressed range -> compressed range) from frame_write*.py

For each requested path, determines the minimum set of frames whose
uncompressed range overlaps the file's content, range-reads only those
compressed bytes from the archive, decompresses just those frames, and
slices the file body out of the concatenated frame data.

Frames needed by multiple requested files are read+decompressed once
(simple coalescing). True range-read coalescing across adjacent frames
is a later optimization — for now we issue one `read()` per needed
frame, sorted by compressed offset so the kernel can prefetch.

Demo target: extract a small file from a 100+ GiB archive in seconds
instead of the ~50 min a full decompression takes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from bisect import bisect_right
from pathlib import Path

import zstandard as zstd


def _load_indexes(archive: Path) -> tuple[dict, dict]:
    frame_idx_path = archive.with_suffix(archive.suffix + ".frames.json")
    file_idx_path = archive.with_suffix(archive.suffix + ".files.json")
    if not frame_idx_path.exists():
        sys.exit(f"missing frame index: {frame_idx_path}")
    if not file_idx_path.exists():
        sys.exit(f"missing file index: {file_idx_path}  "
                 f"(run scan_file_index.py first)")
    with open(frame_idx_path) as f:
        frame_idx = json.load(f)
    with open(file_idx_path) as f:
        file_idx = json.load(f)
    return file_idx, frame_idx


def _frames_for_range(frames: list[dict], frame_starts: list[int],
                      offset: int, size: int) -> list[dict]:
    """Return the frames whose uncompressed range overlaps [offset, offset+size)."""
    if size == 0:
        return []
    end = offset + size
    # First frame containing `offset`: largest start <= offset.
    i = bisect_right(frame_starts, offset) - 1
    if i < 0:
        i = 0
    out = []
    while i < len(frames):
        f = frames[i]
        if f["uo"] >= end:
            break
        if f["uo"] + f["ul"] > offset:
            out.append(f)
        i += 1
    return out


def extract(archive: Path, paths: list[str], output_dir: Path,
            verbose: bool = False) -> int:
    file_idx, frame_idx = _load_indexes(archive)
    files_by_name = {f["name"]: f for f in file_idx["files"]}
    frames = frame_idx["frames"]
    frame_starts = [f["uo"] for f in frames]

    started = time.monotonic()

    # Resolve paths.
    targets = []
    for p in paths:
        if p in files_by_name:
            targets.append(files_by_name[p])
        else:
            # Be helpful: also try a leading "./" variant since tar archives
            # frequently store paths that way.
            alt = "./" + p if not p.startswith("./") else p[2:]
            if alt in files_by_name:
                targets.append(files_by_name[alt])
            else:
                print(f"NOT FOUND in index: {p}", file=sys.stderr)
    if not targets:
        return 1

    # Determine union of frames needed.
    needed_ids: set[int] = set()
    per_file_frames: dict[str, list[dict]] = {}
    for t in targets:
        if t["type"] == "d":
            continue  # directories have no content to extract
        fs = _frames_for_range(frames, frame_starts,
                               t["offset_data"], t["size"])
        per_file_frames[t["name"]] = fs
        for f in fs:
            needed_ids.add(f["id"])

    needed = sorted(needed_ids)
    if verbose:
        print(f"need {len(needed)} of {len(frames)} frames "
              f"for {len(targets)} target(s)", file=sys.stderr)

    # Read + decompress needed frames.
    dctx = zstd.ZstdDecompressor()
    decompressed: dict[int, bytes] = {}
    compressed_bytes_read = 0
    read_started = time.monotonic()
    with open(archive, "rb") as fp:
        for fid in needed:
            f = frames[fid]
            fp.seek(f["co"])
            blob = fp.read(f["cl"])
            compressed_bytes_read += len(blob)
            decompressed[fid] = dctx.decompress(blob, max_output_size=f["ul"])
    read_elapsed = time.monotonic() - read_started

    # Write each requested file.
    output_dir.mkdir(parents=True, exist_ok=True)
    extract_started = time.monotonic()
    extracted = 0
    extracted_bytes = 0
    for t in targets:
        out = output_dir / t["name"].lstrip("./")
        if t["type"] == "d":
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)

        off = t["offset_data"]
        sz = t["size"]
        parts: list[bytes] = []
        for f in per_file_frames[t["name"]]:
            frame_data = decompressed[f["id"]]
            start = max(0, off - f["uo"])
            end = min(f["ul"], off + sz - f["uo"])
            parts.append(frame_data[start:end])
        body = b"".join(parts)
        out.write_bytes(body)
        extracted += 1
        extracted_bytes += len(body)
        if verbose:
            print(f"  {t['name']}  ({len(body):,} bytes -> {out})",
                  file=sys.stderr)

    extract_elapsed = time.monotonic() - extract_started
    total_elapsed = time.monotonic() - started

    print(f"frames read:      {len(needed)} / {len(frames)}", file=sys.stderr)
    print(f"compressed bytes: {compressed_bytes_read:,} "
          f"({compressed_bytes_read/1024**2:.2f} MiB)", file=sys.stderr)
    print(f"extracted files:  {extracted}", file=sys.stderr)
    print(f"extracted bytes:  {extracted_bytes:,} "
          f"({extracted_bytes/1024**2:.2f} MiB)", file=sys.stderr)
    print(f"read time:        {read_elapsed:.3f}s "
          f"({compressed_bytes_read/read_elapsed/1024**2:.1f} MiB/s)"
          if read_elapsed > 0 else "read time: instant", file=sys.stderr)
    print(f"extract time:     {extract_elapsed:.3f}s", file=sys.stderr)
    print(f"total time:       {total_elapsed:.3f}s", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path,
                    help="Framed .pax.zst archive (sidecars must exist next to it).")
    ap.add_argument("paths", nargs="+",
                    help="Paths to extract, as they appear in the file index "
                         "(use `--list` to inspect).")
    ap.add_argument("-C", "--to-dir", type=Path, default=Path("."),
                    help="Output directory (default: current directory).")
    ap.add_argument("--list", action="store_true",
                    help="Just list paths in the file index that match the given "
                         "patterns (substring match), do not extract.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        file_idx, _ = _load_indexes(args.archive)
        for f in file_idx["files"]:
            if any(p in f["name"] for p in args.paths):
                print(f"{f['type']:1s}  {f['size']:>16,}  {f['name']}")
        return 0

    return extract(args.archive, args.paths, args.to_dir,
                   verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
