"""Cached file-listing sidecar for each archive.

Each backup has a `<archive>.idx.zst` companion file. The sidecar lets the
GUI render an archive's file tree without re-scanning the multi-GB archive,
and lets Phase D fast-extract look up per-file byte ranges to seek directly
into the framed-zstd output.

**Format v2 (current):** zstd-compressed JSONL. First line is a header object
with `{"version": 2, "archive": ..., "created_at": ...}`. Subsequent lines
are one-per-member records:

    {"name": "./etc/hostname", "type": "f", "size": 9, "mode": 420,
     "mtime": 1716234567, "uname": "root", "gname": "root",
     "header_offset": 12288, "data_offset": 13312}

`header_offset` is the uncompressed byte offset of the tar header for this
member; `data_offset` is where the file body starts. Combined with the
`.frames.json` sidecar (Phase B), Phase D's fast-extract can compute which
zstd frames to decompress for any single-file restore.

**Format v1 (legacy):** zstd-compressed plain text from `tar -tvf`. Still
readable for backups taken before the v2 cutover. Distinguishable from v2 by
the first non-whitespace character — v2 always starts with `{`.
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import tarfile
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    import zstandard as zstd
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "index sidecar v2 requires the 'zstandard' package. Install with:\n"
        "    sudo apt install python3-zstandard   # Ubuntu/Debian (preferred)\n"
        "    pip install --user 'zstandard>=0.20'  # fallback"
    ) from e


# Map tarfile member types to single-char codes for the JSONL records.
_TYPE_MAP = {
    tarfile.REGTYPE: "f",
    tarfile.AREGTYPE: "f",   # old-form regular file
    tarfile.LNKTYPE: "h",    # hard link
    tarfile.SYMTYPE: "l",    # symbolic link
    tarfile.CHRTYPE: "c",
    tarfile.BLKTYPE: "b",
    tarfile.DIRTYPE: "d",
    tarfile.FIFOTYPE: "p",
}


SIDECAR_VERSION = 2


def sidecar_path(archive_path: Path) -> Path:
    return archive_path.with_name(archive_path.name + ".idx.zst")


def sidecar_mirror_path(plan_name: str, archive_filename: str) -> Path:
    """Local-disk mirror path for an archive's sidecar.

    Sidecars are tens of KB compressed, so mirroring all of them locally is
    cheap (~hundreds of KB to a few MB per plan). This is what lets the GUI
    render archive content trees without touching the backup mount.
    """
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return (Path(xdg) / "timetraveller" / plan_name / "sidecars"
            / (archive_filename + ".idx.zst"))


def copy_sidecar_to_mirror(plan_name: str, source_sidecar: Path,
                           archive_filename: str) -> None:
    """Atomically copy an on-mount sidecar to the local mirror.

    Raises OSError on failure — callers that don't care should swallow.
    """
    dst = sidecar_mirror_path(plan_name, archive_filename)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(source_sidecar, tmp)
    tmp.replace(dst)


def delete_sidecar_mirror(plan_name: str, archive_filename: str) -> None:
    """Remove a sidecar from the local mirror. Idempotent — silent if missing."""
    p = sidecar_mirror_path(plan_name, archive_filename)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _tarinfo_to_record(ti: tarfile.TarInfo) -> dict:
    """Convert a tarfile.TarInfo into a v2 sidecar record."""
    rec = {
        "name": ti.name,
        "type": _TYPE_MAP.get(ti.type, "?"),
        "size": ti.size,
        "mode": ti.mode,
        "mtime": int(ti.mtime),
        "uname": ti.uname or str(ti.uid),
        "gname": ti.gname or str(ti.gid),
        "header_offset": ti.offset,
        "data_offset": ti.offset_data,
    }
    if ti.issym() or ti.islnk():
        rec["link_target"] = ti.linkname
    return rec


def write_sidecar(archive_path: Path) -> Path:
    """Generate `<archive>.idx.zst` (v2 JSONL format) from the archive.

    Streams the (framed-)zstd archive through python-zstandard's stream
    decompressor into Python's `tarfile` reader, emitting one JSONL record
    per archive member with metadata + uncompressed byte offsets. The
    output is zstd-compressed and atomically renamed into place.

    Why Python's tarfile: it exposes `.offset` (header) and `.offset_data`
    (file body start) on each TarInfo — exactly the fields Phase D needs.
    Subprocess `tar -tvf` doesn't expose these. tarfile also handles pax
    extended headers (long names, large files) correctly in streaming mode.
    """
    sidecar = sidecar_path(archive_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")

    header = {
        "version": SIDECAR_VERSION,
        "archive": archive_path.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    cctx = zstd.ZstdCompressor(level=3)
    dctx = zstd.ZstdDecompressor()

    try:
        with open(archive_path, "rb") as comp_in:
            with dctx.stream_reader(comp_in) as tar_stream:
                with open(tmp, "wb") as raw_out:
                    with cctx.stream_writer(raw_out) as zstd_out:
                        zstd_out.write((json.dumps(header) + "\n").encode())
                        with tarfile.open(fileobj=tar_stream, mode="r|") as tf:
                            for ti in tf:
                                rec = _tarinfo_to_record(ti)
                                zstd_out.write((json.dumps(rec) + "\n").encode())
    except BaseException:
        # A truncated/corrupt archive raises mid-stream (tarfile.ReadError, a
        # zstd error, etc.). Don't leave the partial .tmp behind — callers that
        # run this as an integrity gate (e.g. --recover-failed) rely on a clean
        # tree on failure.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    os.replace(tmp, sidecar)
    return sidecar


_INDEX_SENTINEL = object()
_INDEX_QUEUE_MAXSIZE = 4  # bounded: ~4*64 MiB buffered if indexing falls behind


class _QueueReader(io.RawIOBase):
    """Raw file-like over a queue of *uncompressed* tar byte chunks.

    Wrapped in an io.BufferedReader by InlineIndexWriter so tarfile's many
    small reads hit the C buffer rather than incurring Python per-read
    overhead. Holds a partially-consumed chunk as a memoryview — no growing
    accumulator, no repeated big-buffer slicing (the shape validated fastest
    in prototype/inline_index.py).
    """

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self._q = q
        self._cur = memoryview(b"")
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if not len(self._cur) and not self._eof:
            chunk = self._q.get()
            if chunk is _INDEX_SENTINEL:
                self._eof = True
            else:
                self._cur = memoryview(chunk)
        if not len(self._cur):
            return 0
        n = min(len(b), len(self._cur))
        b[:n] = self._cur[:n]
        self._cur = self._cur[n:]
        return n


class InlineIndexWriter:
    """Builds `<archive>.idx.zst` from the uncompressed tar byte stream *as it
    is produced*, in a background thread fed via `feed()`.

    This is the inline counterpart to `write_sidecar`: it emits the identical
    v2 JSONL records (validated for byte-identity in
    prototype/inline_index.py), so `archive.parse_index` / `extract.py` readers
    are unchanged — but it consumes the bytes the framewriter already has in
    hand during the write, eliminating the post-write re-read + re-decompress
    pass (the bulk of which is an NFS read on this tool's target).

    Crash-safety mirrors `write_sidecar`: records stream into a `.tmp` that is
    atomically renamed only on a clean finish; the partial `.tmp` is removed on
    error and `--reindex` remains the rebuild path.

    Producer usage:
        idx = InlineIndexWriter(archive_path); idx.start()
        for chunk in uncompressed_stream: idx.feed(chunk)
        ok = idx.finish()       # True iff the sidecar was written cleanly
    On a producer-side abort (pax died, archive write error), call `abort()`
    instead of `finish()` to unblock + join the thread without surfacing a
    secondary error.
    """

    def __init__(self, archive_path: Path, *, maxsize: int = _INDEX_QUEUE_MAXSIZE):
        self.archive_path = archive_path
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.records = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

    def feed(self, chunk: bytes) -> None:
        self._q.put(chunk)

    def finish(self) -> bool:
        """Signal EOF, join the builder, and report whether it succeeded."""
        self._q.put(_INDEX_SENTINEL)
        if self._thread is not None:
            self._thread.join()
        return self._error is None

    def abort(self) -> None:
        """Unblock + join the builder without raising (the producer failed)."""
        self._q.put(_INDEX_SENTINEL)
        if self._thread is not None:
            self._thread.join()

    @property
    def error(self) -> BaseException | None:
        return self._error

    def _drain(self) -> None:
        """Discard queued chunks until the sentinel, so a producer still
        feeding a bounded (now-full) queue after we've bailed doesn't block."""
        while self._q.get() is not _INDEX_SENTINEL:
            pass

    def _run(self) -> None:
        sidecar = sidecar_path(self.archive_path)
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        header = {
            "version": SIDECAR_VERSION,
            "archive": self.archive_path.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        cctx = zstd.ZstdCompressor(level=3)
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            reader = io.BufferedReader(_QueueReader(self._q), buffer_size=1 << 20)
            n = 0
            with open(tmp, "wb") as raw_out:
                with cctx.stream_writer(raw_out) as zstd_out:
                    zstd_out.write((json.dumps(header) + "\n").encode())
                    with tarfile.open(fileobj=reader, mode="r|") as tf:
                        for ti in tf:
                            zstd_out.write((json.dumps(_tarinfo_to_record(ti)) + "\n").encode())
                            n += 1
            os.replace(tmp, sidecar)
            self.records = n
        except BaseException as exc:  # noqa: BLE001 - any failure -> fall back to re-read
            self._error = exc
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            # The producer may still be feeding a full bounded queue; drain it
            # so feed() doesn't deadlock now that we've stopped consuming.
            self._drain()


def read_sidecar(sidecar: Path) -> list[str]:
    """Return the decompressed sidecar contents as a list of lines.

    Works for both v1 (legacy plain text) and v2 (JSONL) — callers that
    care about the format should peek at the first non-whitespace character
    (`{` = v2 JSONL, otherwise legacy text).
    """
    out = subprocess.run(
        ["zstdcat", str(sidecar)],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.splitlines()
