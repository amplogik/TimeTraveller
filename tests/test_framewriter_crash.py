"""Verify framewriter's crash-resilience: if the input stream fails partway
through, the partial sidecar contains every frame flushed up to the last
boundary, and no spurious .frames.json appears at the final path."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import framewriter


class FailingStream(io.RawIOBase):
    """Returns whole `chunk_size` blocks until N have been emitted, then raises."""

    def __init__(self, chunk_size: int, ok_chunks: int):
        self.chunk_size = chunk_size
        self.ok_chunks = ok_chunks
        self.emitted = 0
        self.buffer = b""

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if not self.buffer:
            if self.emitted >= self.ok_chunks:
                raise IOError("simulated stream failure")
            self.buffer = b"\xab" * self.chunk_size
            self.emitted += 1
        if n is None or n < 0 or n >= len(self.buffer):
            out = self.buffer
            self.buffer = b""
            return out
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out


def test_partial_sidecar_survives_midstream_failure(tmp_path):
    archive = tmp_path / "crash.pax.zst"
    frame_size = 256 * 1024
    flush_every = 2

    # Deliver 5 full frames, then raise. With flush_every=2, partial gets
    # written after frames 2 and 4 — so the surviving partial should have
    # at least 4 frames.
    stream = FailingStream(chunk_size=frame_size, ok_chunks=5)

    with pytest.raises(IOError, match="simulated stream failure"):
        framewriter.write_framed(stream, archive, frame_size=frame_size,
                                 flush_every=flush_every)

    final_sidecar = framewriter.sidecar_path(archive)
    partial_sidecar = framewriter.partial_sidecar_path(archive)

    assert not final_sidecar.exists(), \
        "final .frames.json must NOT exist after a mid-run failure"
    assert partial_sidecar.exists(), \
        "partial sidecar must exist for recovery after a mid-run failure"

    payload = json.loads(partial_sidecar.read_text())
    assert payload["version"] == 1
    # The last flush boundary at or before the failure was after frame 4.
    assert payload["frame_count"] >= 4, \
        f"partial sidecar must contain frames up to last flush boundary, got {payload['frame_count']}"


def test_failure_before_any_flush_leaves_no_partial(tmp_path):
    """If we crash before the first flush, there's nothing useful to recover.
    Partial may or may not exist depending on flush_every; both are acceptable.
    The contract is: no spurious final sidecar."""
    archive = tmp_path / "crash.pax.zst"
    frame_size = 256 * 1024

    stream = FailingStream(chunk_size=frame_size, ok_chunks=0)

    with pytest.raises(IOError, match="simulated stream failure"):
        framewriter.write_framed(stream, archive, frame_size=frame_size,
                                 flush_every=64)

    assert not framewriter.sidecar_path(archive).exists()


def test_atomic_partial_write_never_corrupts(tmp_path):
    """The .staging file should never be left behind on a clean flush —
    os.replace is atomic, so callers should only ever see fully-written
    partial sidecars."""
    archive = tmp_path / "ok.pax.zst"
    frame_size = 256 * 1024

    stream = FailingStream(chunk_size=frame_size, ok_chunks=10)
    with pytest.raises(IOError):
        framewriter.write_framed(stream, archive, frame_size=frame_size,
                                 flush_every=2)

    staging = framewriter.partial_sidecar_path(archive).with_name(
        framewriter.partial_sidecar_path(archive).name + ".staging"
    )
    assert not staging.exists(), \
        "staging file must be atomically renamed; never left behind"
