"""Verify framed-zstd output is wire-compatible with the unframed zstd format
that the rest of the world (zstdcat, libzstd, etc.) reads.

This is the most important test in Phase B: if any consumer of `.pax.zst`
ever stops working after we ship framing, the format change is a regression.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import framewriter


_HAVE_ZSTD = shutil.which("zstd") is not None
needs_zstd = pytest.mark.skipif(not _HAVE_ZSTD, reason="zstd binary not installed")


def _payload(size: int) -> bytes:
    """Patterned-but-not-trivially-compressible payload."""
    out = bytearray()
    seed = 0
    while len(out) < size:
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        out.extend(seed.to_bytes(4, "little"))
    return bytes(out[:size])


@needs_zstd
def test_zstdcat_roundtrips_framed_output(tmp_path):
    payload = _payload(3 * 1024 * 1024 + 7777)
    archive = tmp_path / "framed.zst"

    framewriter.write_framed(io.BytesIO(payload), archive,
                             frame_size=1024 * 1024)

    decoded = subprocess.check_output(["zstdcat", str(archive)])
    assert decoded == payload


@needs_zstd
def test_framed_decompresses_same_as_unframed(tmp_path):
    payload = _payload(2 * 1024 * 1024 + 100)
    framed = tmp_path / "framed.zst"
    unframed = tmp_path / "unframed.zst"

    framewriter.write_framed(io.BytesIO(payload), framed,
                             frame_size=512 * 1024)
    subprocess.run(["zstd", "-3", "-T0", "-q", "-o", str(unframed)],
                   input=payload, check=True)

    framed_out = subprocess.check_output(["zstdcat", str(framed)])
    unframed_out = subprocess.check_output(["zstdcat", str(unframed)])

    assert framed_out == unframed_out == payload


def test_python_zstandard_can_decompress_framed_output(tmp_path):
    """Round-trip via the same library that writes — sanity check, no
    external dependencies."""
    import zstandard as zstd

    payload = _payload(2 * 1024 * 1024 + 33)
    archive = tmp_path / "framed.zst"

    framewriter.write_framed(io.BytesIO(payload), archive,
                             frame_size=512 * 1024)

    dctx = zstd.ZstdDecompressor()
    decoded = dctx.stream_reader(archive.open("rb")).read()
    assert decoded == payload
