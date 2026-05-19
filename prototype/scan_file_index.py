#!/usr/bin/env python3
"""File-index scanner (Phase C prototype).

Reads a framed `.pax.zst` archive, walks the decompressed tar stream
via Python's tarfile module in streaming mode, and emits a sidecar
JSON that maps every member's path to its byte offset in the
uncompressed tar stream.

Composed with the frame-index sidecar (`.frames.json` from
frame_write.py), this is what makes seek-extract possible:

    file_index    : path -> (offset_data, size)
    frame_index   : uncompressed byte range -> compressed byte range
    seek-extract  : path -> compute compressed range -> range-read NFS

This scanner is a ONE-TIME cost for archives produced before Phase B
integration. Once Phase B emits the file-index during backup, this
script becomes obsolete for new archives but remains useful for
back-filling sidecars on existing ones.

Output schema (file-index sidecar):
  {
    "version": 1,
    "archive": "<basename>",
    "file_count": N,
    "files": [
      {"name": "...", "type": "f|d|l|s|...", "size": N,
       "offset": header_byte, "offset_data": content_byte,
       "mode": int, "mtime": int, "uname": str, "gname": str},
      ...
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
from pathlib import Path

import zstandard as zstd


# Map tarfile type-flag bytes to short codes that survive a JSON round-trip.
_TYPE_CODE = {
    tarfile.REGTYPE:  "f",
    tarfile.AREGTYPE: "f",
    tarfile.LNKTYPE:  "l",   # hard link
    tarfile.SYMTYPE:  "s",   # symlink
    tarfile.CHRTYPE:  "c",
    tarfile.BLKTYPE:  "b",
    tarfile.DIRTYPE:  "d",
    tarfile.FIFOTYPE: "p",
    tarfile.CONTTYPE: "f",
    tarfile.GNUTYPE_SPARSE: "S",
}


def _member_record(m: tarfile.TarInfo) -> dict:
    rec = {
        "name": m.name,
        "type": _TYPE_CODE.get(m.type, "?"),
        "size": m.size,
        "offset": m.offset,            # start of this member's header block
        "offset_data": m.offset_data,  # start of content (after headers)
        "mode": m.mode,
        "mtime": int(m.mtime) if m.mtime is not None else None,
        "uname": m.uname,
        "gname": m.gname,
    }
    if m.issym() or m.islnk():
        rec["linkname"] = m.linkname
    return rec


def scan(archive: Path, output: Path, progress_every: int = 1000) -> dict:
    started = time.monotonic()
    files: list[dict] = []
    dctx = zstd.ZstdDecompressor()

    with open(archive, "rb") as comp, \
         dctx.stream_reader(comp, read_across_frames=True) as reader, \
         tarfile.open(fileobj=reader, mode="r|") as tar:
        for m in tar:
            files.append(_member_record(m))
            if len(files) % progress_every == 0:
                elapsed = time.monotonic() - started
                pos = m.offset_data + m.size
                print(f"  {len(files):>7,} members  "
                      f"uncompressed-pos={pos/1024**3:>8.2f} GiB  "
                      f"elapsed={elapsed:>6.1f}s", file=sys.stderr)

    elapsed = time.monotonic() - started
    last_offset = files[-1]["offset_data"] + files[-1]["size"] if files else 0
    index = {
        "version": 1,
        "archive": archive.name,
        "file_count": len(files),
        "uncompressed_bytes_seen": last_offset,
        "scan_elapsed_seconds": elapsed,
        "files": files,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(index, f, indent=2)

    print(f"members:      {len(files):,}", file=sys.stderr)
    print(f"last offset:  {last_offset:,} bytes ({last_offset/1024**3:.2f} GiB)",
          file=sys.stderr)
    print(f"elapsed:      {elapsed:.1f}s ({elapsed/60:.1f} min)", file=sys.stderr)
    print(f"throughput:   {last_offset/elapsed/1024**2:.1f} MiB/s uncompressed"
          if elapsed else "", file=sys.stderr)
    print(f"index:        {output}", file=sys.stderr)
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path, help="Framed .pax.zst archive to scan.")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output path for the file-index sidecar. "
                         "Default: <archive>.files.json")
    ap.add_argument("--progress-every", type=int, default=1000,
                    help="Print a progress line every N members (default 1000).")
    args = ap.parse_args(argv)

    if args.output is None:
        args.output = args.archive.with_suffix(args.archive.suffix + ".files.json")

    scan(args.archive, args.output, progress_every=args.progress_every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
