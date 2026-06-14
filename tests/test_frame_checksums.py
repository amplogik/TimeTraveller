"""Per-frame SHA-256 integrity (frames.json v2) + decompress-free verify."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import framewriter
from timetraveller.worker import _verify_frames


def _write_archive(tmp_path: Path, payload: bytes, frame_size: int = 4096) -> Path:
    archive = tmp_path / "t.pax.zst"
    framewriter.write_framed(io.BytesIO(payload), archive, frame_size=frame_size)
    return archive


def test_sidecar_is_v2_with_per_frame_sha256(tmp_path):
    payload = os.urandom(4096 * 5 + 17)   # 6 frames (5 full + a short tail)
    archive = _write_archive(tmp_path, payload)
    meta = json.loads(framewriter.sidecar_path(archive).read_text())

    assert meta["version"] == 2
    assert meta["csum_algo"] == "sha256"
    assert meta["frame_count"] == len(meta["frames"]) == 6

    # Each csum is the SHA-256 of exactly that frame's on-disk [co, co+cl) bytes.
    raw = archive.read_bytes()
    for fr in meta["frames"]:
        assert "csum" in fr
        chunk = raw[fr["co"]:fr["co"] + fr["cl"]]
        assert hashlib.sha256(chunk).hexdigest() == fr["csum"]


def test_verify_clean_archive_reports_no_bad_frames(tmp_path):
    archive = _write_archive(tmp_path, os.urandom(4096 * 4))
    algo, nframes, bad = _verify_frames(archive)
    assert algo == "sha256"
    assert nframes == 4
    assert bad == []


def test_verify_detects_corruption_at_the_right_frame(tmp_path):
    payload = os.urandom(4096 * 6)
    archive = _write_archive(tmp_path, payload)
    meta = json.loads(framewriter.sidecar_path(archive).read_text())
    victim = meta["frames"][3]

    # Flip one byte inside frame 3's compressed range, in place.
    raw = bytearray(archive.read_bytes())
    pos = victim["co"] + victim["cl"] // 2
    raw[pos] ^= 0xFF
    archive.write_bytes(raw)

    algo, nframes, bad = _verify_frames(archive)
    assert nframes == 6
    assert [b["id"] for b in bad] == [3]
    assert bad[0]["uo"] == victim["uo"]
    assert bad[0]["ul"] == victim["ul"]


def test_verify_detects_truncated_frame(tmp_path):
    """A short read (truncation) is corruption too, not a clean pass."""
    archive = _write_archive(tmp_path, os.urandom(4096 * 3))
    # Drop the last 10 bytes — the final frame can no longer match its digest.
    raw = archive.read_bytes()
    archive.write_bytes(raw[:-10])
    _, _, bad = _verify_frames(archive)
    assert bad, "truncation should be reported as a corrupt frame"


def test_v1_sidecar_falls_back(tmp_path):
    """An old sidecar with no per-frame csum makes _verify_frames return None
    so the caller uses the full-decompress path."""
    archive = _write_archive(tmp_path, os.urandom(4096 * 2))
    sidecar = framewriter.sidecar_path(archive)
    meta = json.loads(sidecar.read_text())
    meta["version"] = 1
    meta.pop("csum_algo", None)
    for fr in meta["frames"]:
        fr.pop("csum", None)
    sidecar.write_text(json.dumps(meta))

    assert _verify_frames(archive) is None
