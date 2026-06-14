"""Framed zstd compression for seekable archives.

Streams an input pipe through python-zstandard in fixed-size chunks, emitting
each chunk as an independent zstd frame and recording its placement in a JSON
sidecar. The resulting archive is byte-compatible with the standard zstd
format: any `zstdcat` reads it as a concatenation of frames. The sidecar
({archive}.frames.json) enables random-access readers to decompress only the
frames they need.

Sidecar schema (version 2):
    {
      "version": 2,
      "frame_size": 67108864,
      "zstd_level": 3,
      "csum_algo": "sha256",
      "total_uncompressed": <int>,
      "total_compressed": <int>,
      "elapsed_seconds": <float>,
      "frame_count": <int>,
      "frames": [{"id": <int>, "uo": <int>, "ul": <int>,
                  "co": <int>, "cl": <int>, "csum": <hex sha256>}, ...]
    }

`csum` is the SHA-256 of the frame's *compressed* bytes as written to disk —
the digest of exactly the [co, co+cl) byte range. It is computed inline as the
frame is produced (no extra pass; the bytes are already in hand), so `--verify`
can confirm an archive's persisted integrity by re-reading and re-hashing each
frame, with no decompression. `write_checksum=True` additionally embeds zstd's
own per-frame content checksum, which every decompress (restore, browse,
recover) verifies automatically. Together they catch corruption introduced
anywhere from the moment of compression onward (the client write buffer, NFS,
the network, the storage). Corruption that occurs *before* the SHA-256 is taken
-- e.g. a bit flip in RAM on a non-ECC host -- is faithfully hashed as-is and
cannot be detected here; see docs/design/archive-integrity.md.

Schema is backward compatible: v1 sidecars (no `csum`) are still read by the
seek/extract path, and `--verify` falls back to a full decompress for them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import BinaryIO

try:
    import zstandard as zstd
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "framewriter requires the 'zstandard' package. Install with:\n"
        "    sudo apt install python3-zstandard   # Ubuntu/Debian (preferred)\n"
        "    pip install --user 'zstandard>=0.20'  # fallback"
    ) from e


FRAME_SIZE = 64 * 1024 * 1024
FLUSH_EVERY = 64


def sidecar_path(archive_path: Path) -> Path:
    return archive_path.with_name(archive_path.name + ".frames.json")


def partial_sidecar_path(archive_path: Path) -> Path:
    return archive_path.with_name(archive_path.name + ".frames.json.partial")


def _atomic_write_json(path: Path, payload: dict) -> None:
    staging = path.with_name(path.name + ".staging")
    data = json.dumps(payload).encode("utf-8")
    with open(staging, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(staging, path)


def write_framed(
    input_stream: BinaryIO,
    archive_path: Path,
    *,
    zstd_level: int = 3,
    frame_size: int = FRAME_SIZE,
    flush_every: int = FLUSH_EVERY,
    index_writer=None,
) -> dict:
    """Stream `input_stream` to `archive_path` as a series of independent zstd
    frames; emit `<archive_path>.frames.json` alongside on clean exit.

    Returns the final frame-index dict, plus `index_built` (True iff an inline
    sidecar index was requested and written cleanly).

    If `index_writer` (an index.InlineIndexWriter) is given, each uncompressed
    chunk is tee'd to it so the `.idx.zst` sidecar is built in this same pass —
    no post-write re-read. The archive write is authoritative: an inline-index
    failure is reported via `index_built=False` (caller falls back to the
    post-write `write_sidecar`), never raised.

    On a mid-run crash, the partial sidecar at `<archive_path>.frames.json.partial`
    holds the index up to the last flush boundary. Callers can detect this by
    checking for the partial file's presence after a failed run.
    """
    started = time.monotonic()
    # write_checksum embeds zstd's own per-frame content checksum, so every
    # decompression (restore/browse/recover) self-verifies for free. The
    # explicit per-frame SHA-256 below additionally enables a decompress-free
    # --verify against the persisted compressed bytes.
    compressor = zstd.ZstdCompressor(level=zstd_level, write_checksum=True)

    frames: list[dict] = []
    total_uncompressed = 0
    total_compressed = 0

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = partial_sidecar_path(archive_path)
    final_path = sidecar_path(archive_path)

    def snapshot() -> dict:
        return {
            "version": 2,
            "frame_size": frame_size,
            "zstd_level": zstd_level,
            "csum_algo": "sha256",
            "total_uncompressed": total_uncompressed,
            "total_compressed": total_compressed,
            "elapsed_seconds": time.monotonic() - started,
            "frame_count": len(frames),
            "frames": frames,
        }

    if index_writer is not None:
        index_writer.start()
    index_built = False
    try:
        with open(archive_path, "wb") as out:
            while True:
                chunk = _read_full(input_stream, frame_size)
                if not chunk:
                    break
                compressed = compressor.compress(chunk)

                frames.append({
                    "id": len(frames),
                    "uo": total_uncompressed,
                    "ul": len(chunk),
                    "co": total_compressed,
                    "cl": len(compressed),
                    "csum": hashlib.sha256(compressed).hexdigest(),
                })

                out.write(compressed)
                total_uncompressed += len(chunk)
                total_compressed += len(compressed)

                if index_writer is not None:
                    index_writer.feed(chunk)

                if len(frames) % flush_every == 0:
                    _atomic_write_json(partial_path, snapshot())

            out.flush()
            os.fsync(out.fileno())

        # Archive bytes are committed; finalize the inline index off the same
        # stream. A failure here is non-fatal — the caller re-reads instead.
        if index_writer is not None:
            index_built = index_writer.finish()
    except BaseException:
        # Archive write failed; unblock + join the index thread so it doesn't
        # leak, without masking the original error.
        if index_writer is not None:
            try:
                index_writer.abort()
            except Exception:
                pass
        raise

    final = snapshot()
    _atomic_write_json(final_path, final)

    try:
        partial_path.unlink()
    except FileNotFoundError:
        pass

    # `index_built` is returned to the caller but deliberately NOT written into
    # the on-disk frames.json, whose schema stays at version 1.
    result = dict(final)
    result["index_built"] = index_built
    return result


def _read_full(stream: BinaryIO, n: int) -> bytes:
    """Read up to n bytes, returning a full chunk unless EOF.

    A single stream.read(n) on a pipe can return fewer bytes than requested
    even when more data is coming — this loops until either n bytes are read
    or the stream closes, so frames are exactly `frame_size` apart in the
    uncompressed domain (except the last).
    """
    out = bytearray()
    while len(out) < n:
        piece = stream.read(n - len(out))
        if not piece:
            break
        out.extend(piece)
    return bytes(out)
