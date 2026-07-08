"""Tests for frame verification, frame→files mapping, and D2 cross-cycle heal,
plus the D1 corrupt-marking that folds verify results into the manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import extract as extractlib
from timetraveller import framewriter as fwlib
from timetraveller import heal as heallib
from timetraveller import index as indexlib
from timetraveller import manifest as manifestlib

FRAME = 64 * 1024
BIG = b"X" * (3 * FRAME + 12345)   # spans ~4 frames


# ---------- fixtures ----------

def _build_archive(root: Path, name: str) -> Path:
    """Build a framed .pax.zst (with .frames.json + .idx.zst) containing a few
    small files and a multi-frame big.bin. Identical content across names, so
    one archive can heal another."""
    src = root / f"{name}_src"
    src.mkdir(parents=True)
    (src / "small.txt").write_bytes(b"hello\n" * 10)
    (src / "big.bin").write_bytes(BIG)

    archive = root / f"{name}.pax.zst"
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        with os.fdopen(w, "wb") as fw:
            with tarfile.open(fileobj=fw, mode="w|", format=tarfile.PAX_FORMAT) as tf:
                tf.add(str(src), arcname="./src", recursive=True)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as stream:
        fwlib.write_framed(stream, archive, frame_size=FRAME)
    os.waitpid(pid, 0)
    indexlib.write_sidecar(archive)
    return archive


def _big_frame_ids(archive: Path) -> list[int]:
    recs = extractlib._load_v2_sidecar(indexlib.sidecar_path(archive))
    rec = recs["./src/big.bin"]
    frames = json.loads(fwlib.sidecar_path(archive).read_text())["frames"]
    s, e = rec["data_offset"], rec["data_offset"] + rec["size"]
    return [fr["id"] for fr in frames if fr["uo"] < e and fr["uo"] + fr["ul"] > s]


def _corrupt_frame(archive: Path, frame_id: int) -> None:
    fr = json.loads(fwlib.sidecar_path(archive).read_text())["frames"][frame_id]
    raw = bytearray(archive.read_bytes())
    raw[fr["co"] + fr["cl"] // 2] ^= 0xFF
    archive.write_bytes(raw)


# ---------- verify_frame_checksums ----------

def test_verify_clean(tmp_path):
    archive = _build_archive(tmp_path, "a")
    algo, n, bad = heallib.verify_frame_checksums(archive)
    assert algo == "sha256" and n > 0 and bad == []


def test_verify_detects_corruption(tmp_path):
    archive = _build_archive(tmp_path, "a")
    victim = _big_frame_ids(archive)[1]
    _corrupt_frame(archive, victim)
    _algo, _n, bad = heallib.verify_frame_checksums(archive)
    assert [b["id"] for b in bad] == [victim]


def test_verify_drop_cache_still_detects(tmp_path):
    """The cache-dropping read path (used by verify-after-write) must find the
    same corruption as the plain path."""
    archive = _build_archive(tmp_path, "a")
    victim = _big_frame_ids(archive)[0]
    _corrupt_frame(archive, victim)
    _algo, _n, bad = heallib.verify_frame_checksums(archive, drop_cache=True)
    assert [b["id"] for b in bad] == [victim]


def test_verify_none_without_v2_sidecar(tmp_path):
    archive = _build_archive(tmp_path, "a")
    fwlib.sidecar_path(archive).unlink()
    assert heallib.verify_frame_checksums(archive) is None


# ---------- frames_to_files + damaged_files ----------

def test_frames_to_files_maps_to_big(tmp_path):
    archive = _build_archive(tmp_path, "a")
    victim = _big_frame_ids(archive)[1]
    files = heallib.frames_to_files(archive, [victim])
    assert files == ["./src/big.bin"]


def test_damaged_files_end_to_end(tmp_path):
    archive = _build_archive(tmp_path, "a")
    _corrupt_frame(archive, _big_frame_ids(archive)[1])
    assert heallib.damaged_files(archive) == ["./src/big.bin"]


def test_damaged_files_clean_is_empty(tmp_path):
    archive = _build_archive(tmp_path, "a")
    assert heallib.damaged_files(archive) == []


# ---------- _member_is_clean + heal_files ----------

def test_member_is_clean_true_and_false(tmp_path):
    archive = _build_archive(tmp_path, "a")
    assert heallib._member_is_clean(archive, "./src/big.bin") is True
    _corrupt_frame(archive, _big_frame_ids(archive)[1])
    assert heallib._member_is_clean(archive, "./src/big.bin") is False


def test_heal_from_sibling_recovers_bytes(tmp_path):
    good = _build_archive(tmp_path, "good")     # clean copy
    bad = _build_archive(tmp_path, "bad")       # same content, but corrupt big.bin
    _corrupt_frame(bad, _big_frame_ids(bad)[1])

    damaged = heallib.damaged_files(bad)
    assert damaged == ["./src/big.bin"]

    out = tmp_path / "out"
    # bad searched first (skipped, it's corrupt) → recovered from good.
    res = heallib.heal_files(damaged, into=out, candidate_archives=[bad, good])
    assert res.healed == {"./src/big.bin": good.name}
    assert res.unrecoverable == []
    assert (out / "src" / "big.bin").read_bytes() == BIG


def test_heal_unrecoverable_when_no_clean_copy(tmp_path):
    bad = _build_archive(tmp_path, "bad")
    _corrupt_frame(bad, _big_frame_ids(bad)[1])
    out = tmp_path / "out"
    res = heallib.heal_files(["./src/big.bin"], into=out, candidate_archives=[bad])
    assert res.healed == {}
    assert res.unrecoverable == ["./src/big.bin"]


# ---------- D1: manifest corrupt-marking + status plumbing ----------

def _entry(fn: str, status: str = "ok", corrupt_frames: int = 0) -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=fn, kind="full", cycle_id="2026-06-28",
        date_started="2026-06-28T02:00:00+00:00",
        date_finished="2026-06-28T03:00:00+00:00",
        size_bytes=1, status=status, hostname="h", plan_name="p",
        corrupt_frames=corrupt_frames)


def test_shardset_status_corrupt(tmp_path):
    s = manifestlib.ShardSet(group_id="g", members=[
        _entry("a.s1of2.pax.zst", "ok"),
        _entry("a.s2of2.pax.zst", "ok", corrupt_frames=2)])
    assert s.status == "corrupt"
    assert s.is_complete is False


def test_shardset_failed_beats_corrupt(tmp_path):
    s = manifestlib.ShardSet(group_id="g", members=[
        _entry("a.s1of2.pax.zst", "failed"),
        _entry("a.s2of2.pax.zst", "ok", corrupt_frames=1)])
    assert s.status == "failed"


def test_corrupt_frames_survives_manifest_round_trip(tmp_path):
    m = manifestlib.Manifest(plan_name="p", archives=[_entry("a.pax.zst", "corrupt", 3)])
    p = tmp_path / "manifest.json"
    manifestlib.save(m, p)
    loaded = manifestlib.load(p)
    assert loaded.archives[0].corrupt_frames == 3
    assert loaded.archives[0].status == "corrupt"


def test_mark_corrupt_shards_updates_entry_and_meta(tmp_path):
    from timetraveller.worker import _mark_corrupt_shards
    archive = _build_archive(tmp_path, "a")
    victim = _big_frame_ids(archive)[1]
    _corrupt_frame(archive, victim)
    bad = heallib.verify_frame_checksums(archive)[2]

    m = manifestlib.Manifest(plan_name="p", archives=[_entry(archive.name, "ok")])
    specs = [(archive.name, archive, [], tmp_path / "log")]
    args = argparse.Namespace(quiet=True)

    n = _mark_corrupt_shards(m, tmp_path, specs, [bad], args)
    assert n == 1
    e = m.archives[0]
    assert e.status == "corrupt"
    assert e.corrupt_frames == len(bad)
    assert "big.bin" in e.notes
    # meta sidecar rewritten with the corrupt status
    meta = manifestlib.read_entry_meta(manifestlib.entry_meta_path(tmp_path, archive.name))
    assert meta.status == "corrupt" and meta.corrupt_frames == len(bad)
