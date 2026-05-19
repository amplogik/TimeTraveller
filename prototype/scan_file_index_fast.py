#!/usr/bin/env python3
"""Frame-aware file-index scanner (optimized Phase C prototype).

The naive scan_file_index.py walks the entire decompressed tar stream
sequentially. For an archive with one huge file (e.g. data.img at
256 GiB) and a few small ones, that means reading + discarding 256 GiB
through zstd at NFS speed — ~50 min.

This optimized version uses the frame index (`.frames.json`) to expose
the framed archive as a *seekable* file-like to tarfile. When tarfile
seeks past a file's content (between reading its header and reading
the next header), we just update an in-memory uncompressed cursor —
no actual decompression of the skipped frames. We only decompress the
frames that contain tar headers.

For the winboat-test archive (~4097 frames, 8 files), this reduces
the scan from ~50 min to ~seconds: we decompress only frame 0 (first
few headers) and the frame containing the post-data.img region (where
windows.base..windows.ver headers live), plus any frame containing
the final stream-terminator blocks.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
from bisect import bisect_right
from pathlib import Path

import zstandard as zstd


# Same type-code map as scan_file_index.py.
_TYPE_CODE = {
    tarfile.REGTYPE:  "f",
    tarfile.AREGTYPE: "f",
    tarfile.LNKTYPE:  "l",
    tarfile.SYMTYPE:  "s",
    tarfile.CHRTYPE:  "c",
    tarfile.BLKTYPE:  "b",
    tarfile.DIRTYPE:  "d",
    tarfile.FIFOTYPE: "p",
    tarfile.CONTTYPE: "f",
    tarfile.GNUTYPE_SPARSE: "S",
}


class FrameSeekableZstdReader:
    """File-like over a framed zstd archive with O(1) seek via frame index.

    Implements just enough of the file protocol for tarfile: read, seek,
    tell, close, plus a `seekable()` returning True. Frames are
    decompressed lazily — only frames that are actually `read()` get
    decompressed. A `seek(...)` to the middle of an unread frame does
    NOT trigger decompression until a `read(...)` lands there.

    The current-frame cache is single-entry (last decompressed frame).
    For a sequential walk that's enough.
    """

    def __init__(self, archive_path: Path, frame_index: dict):
        self.fp = open(archive_path, "rb")
        self.frames = frame_index["frames"]
        self.frame_starts = [f["uo"] for f in self.frames]
        self.total = sum(f["ul"] for f in self.frames)
        self.dctx = zstd.ZstdDecompressor()
        self._frame_id: int | None = None
        self._frame_data: bytes = b""
        self._pos = 0
        self.frames_decompressed = 0

    def _find_frame(self, offset: int) -> int:
        i = bisect_right(self.frame_starts, offset) - 1
        return max(i, 0)

    def _ensure_frame(self, fid: int) -> None:
        if self._frame_id == fid:
            return
        f = self.frames[fid]
        self.fp.seek(f["co"])
        compressed = self.fp.read(f["cl"])
        self._frame_data = self.dctx.decompress(compressed, max_output_size=f["ul"])
        self._frame_id = fid
        self.frames_decompressed += 1

    # ----- file protocol -----

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            # Read to EOF. Decompresses everything from current pos onward —
            # rare path for tarfile usage but supported for completeness.
            parts: list[bytes] = []
            while self._pos < self.total:
                fid = self._find_frame(self._pos)
                self._ensure_frame(fid)
                f = self.frames[fid]
                start = self._pos - f["uo"]
                parts.append(self._frame_data[start:])
                self._pos = f["uo"] + f["ul"]
            return b"".join(parts)

        if n == 0 or self._pos >= self.total:
            return b""

        out = bytearray()
        remaining = n
        while remaining > 0 and self._pos < self.total:
            fid = self._find_frame(self._pos)
            self._ensure_frame(fid)
            f = self.frames[fid]
            start = self._pos - f["uo"]
            available = f["ul"] - start
            take = min(remaining, available)
            out += self._frame_data[start:start + take]
            self._pos += take
            remaining -= take
        return bytes(out)

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self.total + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self.fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _member_record(m: tarfile.TarInfo) -> dict:
    rec = {
        "name": m.name,
        "type": _TYPE_CODE.get(m.type, "?"),
        "size": m.size,
        "offset": m.offset,
        "offset_data": m.offset_data,
        "mode": m.mode,
        "mtime": int(m.mtime) if m.mtime is not None else None,
        "uname": m.uname,
        "gname": m.gname,
    }
    if m.issym() or m.islnk():
        rec["linkname"] = m.linkname
    return rec


def scan(archive: Path, output: Path) -> dict:
    frame_idx_path = archive.with_suffix(archive.suffix + ".frames.json")
    if not frame_idx_path.exists():
        sys.exit(f"missing frame index: {frame_idx_path}")
    with open(frame_idx_path) as f:
        frame_idx = json.load(f)

    started = time.monotonic()
    files: list[dict] = []
    with FrameSeekableZstdReader(archive, frame_idx) as reader, \
         tarfile.open(fileobj=reader, mode="r:") as tar:
        for m in tar:
            files.append(_member_record(m))

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

    print(f"members:             {len(files):,}", file=sys.stderr)
    print(f"last uncomp offset:  {last_offset:,} bytes "
          f"({last_offset/1024**3:.2f} GiB)", file=sys.stderr)
    print(f"frames decompressed: {reader.frames_decompressed} / {len(frame_idx['frames'])}",
          file=sys.stderr)
    print(f"elapsed:             {elapsed:.3f}s", file=sys.stderr)
    print(f"index:               {output}", file=sys.stderr)
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.output is None:
        args.output = args.archive.with_suffix(args.archive.suffix + ".files.json")
    scan(args.archive, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
