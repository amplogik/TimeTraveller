"""Unit tests for the framewriter module — frame layout, schema, atomic flush."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import framewriter


def _payload(size: int) -> bytes:
    """Deterministic but not too compressible — pseudo-random pattern."""
    out = bytearray()
    seed = 0
    while len(out) < size:
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        out.extend(seed.to_bytes(4, "little"))
    return bytes(out[:size])


def test_single_full_frame(tmp_path):
    archive = tmp_path / "a.pax.zst"
    src = io.BytesIO(_payload(1024 * 1024))

    result = framewriter.write_framed(src, archive, frame_size=1024 * 1024)

    assert result["frame_count"] == 1
    assert result["total_uncompressed"] == 1024 * 1024
    assert result["frames"][0]["uo"] == 0
    assert result["frames"][0]["ul"] == 1024 * 1024
    assert result["frames"][0]["co"] == 0
    assert result["frames"][0]["cl"] == result["total_compressed"]


def test_multiple_frames_partial_last(tmp_path):
    archive = tmp_path / "a.pax.zst"
    total = 3 * 1024 * 1024 + 12345  # 3 full frames + a partial
    src = io.BytesIO(_payload(total))

    result = framewriter.write_framed(src, archive, frame_size=1024 * 1024)

    assert result["frame_count"] == 4
    assert result["total_uncompressed"] == total
    assert [f["ul"] for f in result["frames"]] == [
        1024 * 1024, 1024 * 1024, 1024 * 1024, 12345,
    ]
    # Uncompressed offsets must be cumulative.
    assert result["frames"][0]["uo"] == 0
    assert result["frames"][1]["uo"] == 1024 * 1024
    assert result["frames"][2]["uo"] == 2 * 1024 * 1024
    assert result["frames"][3]["uo"] == 3 * 1024 * 1024


def test_compressed_offsets_match_actual_file_layout(tmp_path):
    archive = tmp_path / "a.pax.zst"
    src = io.BytesIO(_payload(5 * 1024 * 1024))

    result = framewriter.write_framed(src, archive, frame_size=1024 * 1024)
    data = archive.read_bytes()

    # Each frame's compressed bytes should sit exactly at (co, co+cl) in the
    # actual archive file.
    for frame in result["frames"]:
        slice_ = data[frame["co"]: frame["co"] + frame["cl"]]
        # Independent zstd frames start with the magic bytes 28 b5 2f fd
        # (little-endian 0xFD2FB528).
        assert slice_[:4] == b"\x28\xb5\x2f\xfd", (
            f"frame {frame['id']} missing zstd magic at co={frame['co']}"
        )

    # Sum of compressed lengths should equal the total file size.
    total_cl = sum(f["cl"] for f in result["frames"])
    assert total_cl == archive.stat().st_size == result["total_compressed"]


def test_sidecar_schema_v1(tmp_path):
    archive = tmp_path / "a.pax.zst"
    src = io.BytesIO(_payload(2 * 1024 * 1024))

    framewriter.write_framed(src, archive, frame_size=1024 * 1024)
    sidecar = framewriter.sidecar_path(archive)
    payload = json.loads(sidecar.read_text())

    for key in ("version", "frame_size", "zstd_level", "total_uncompressed",
                "total_compressed", "elapsed_seconds", "frame_count", "frames"):
        assert key in payload, f"sidecar missing key {key!r}"

    assert payload["version"] == 1
    assert payload["frame_size"] == 1024 * 1024
    assert payload["zstd_level"] == 3
    assert isinstance(payload["frames"], list)


def test_partial_sidecar_cleaned_up_on_success(tmp_path):
    archive = tmp_path / "a.pax.zst"
    src = io.BytesIO(_payload(4 * 1024 * 1024))

    framewriter.write_framed(src, archive, frame_size=1024 * 1024, flush_every=1)

    assert framewriter.sidecar_path(archive).exists()
    assert not framewriter.partial_sidecar_path(archive).exists()


def test_empty_input(tmp_path):
    archive = tmp_path / "a.pax.zst"
    src = io.BytesIO(b"")

    result = framewriter.write_framed(src, archive, frame_size=1024 * 1024)

    assert result["frame_count"] == 0
    assert result["total_uncompressed"] == 0
    assert result["total_compressed"] == 0
    assert archive.stat().st_size == 0
