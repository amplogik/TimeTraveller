"""Tests for worker.prune_to_newest_cycle() — used by Active→Archive switch."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from pathlib import Path

from timetraveller import config as configlib
from timetraveller import manifest as manifestlib
from timetraveller.manifest import ArchiveEntry, Manifest
from timetraveller.worker import prune_to_newest_cycle


def _entry(date: str, kind: str, cycle_id: str | None = None) -> ArchiveEntry:
    return ArchiveEntry(
        filename=f"{date}_{kind}.pax.zst",
        kind=kind, cycle_id=cycle_id or date,
        date_started=f"{date}T02:00:00+00:00",
        date_finished=f"{date}T02:30:00+00:00",
        size_bytes=100, status="ok", hostname="h", plan_name="archive",
    )


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def _setup_three_cycles(tmp_path: Path, monkeypatch) -> tuple[configlib.PlanConfig, Path]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    archive_dir = tmp_path / "mount" / "archive"
    archive_dir.mkdir(parents=True)

    # Three cycles, oldest to newest.
    entries = [
        _entry("2025-01-01", "full"),
        _entry("2025-06-01", "full"),
        _entry("2026-05-20", "full"),
    ]
    for e in entries:
        _touch(archive_dir / e.filename)
        _touch(archive_dir / (e.filename + ".idx.zst"))

    m = Manifest(plan_name="archive", archives=entries)
    manifestlib.save(m, manifestlib.manifest_path(archive_dir))

    plan = configlib.PlanConfig(
        plan_name="archive",
        sources=["/tmp/whatever"],
        destination=str(tmp_path / "mount"),
        include_hostname_in_path=False,
    )
    return plan, archive_dir


def test_prune_to_newest_deletes_older_cycles(tmp_path, monkeypatch):
    plan, archive_dir = _setup_three_cycles(tmp_path, monkeypatch)

    deleted = prune_to_newest_cycle(plan)
    deleted_ids = sorted(c.cycle_id for c in deleted)
    assert deleted_ids == ["2025-01-01", "2025-06-01"]

    # Older archives + sidecars gone from disk.
    assert not (archive_dir / "2025-01-01_full.pax.zst").exists()
    assert not (archive_dir / "2025-01-01_full.pax.zst.idx.zst").exists()
    assert not (archive_dir / "2025-06-01_full.pax.zst").exists()
    assert not (archive_dir / "2025-06-01_full.pax.zst.idx.zst").exists()

    # Newest cycle untouched.
    assert (archive_dir / "2026-05-20_full.pax.zst").exists()
    assert (archive_dir / "2026-05-20_full.pax.zst.idx.zst").exists()


def test_prune_to_newest_updates_manifest(tmp_path, monkeypatch):
    plan, archive_dir = _setup_three_cycles(tmp_path, monkeypatch)

    prune_to_newest_cycle(plan)

    m = manifestlib.load(manifestlib.manifest_path(archive_dir))
    remaining = sorted(a.filename for a in m.archives)
    assert remaining == ["2026-05-20_full.pax.zst"]


def test_prune_to_newest_single_cycle_is_noop(tmp_path, monkeypatch):
    """If only one cycle exists, nothing to delete — the archive basis already is."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    archive_dir = tmp_path / "mount" / "archive"
    archive_dir.mkdir(parents=True)
    e = _entry("2026-05-20", "full")
    _touch(archive_dir / e.filename)
    _touch(archive_dir / (e.filename + ".idx.zst"))
    m = Manifest(plan_name="archive", archives=[e])
    manifestlib.save(m, manifestlib.manifest_path(archive_dir))

    plan = configlib.PlanConfig(
        plan_name="archive",
        sources=["/tmp/whatever"],
        destination=str(tmp_path / "mount"),
        include_hostname_in_path=False,
    )

    deleted = prune_to_newest_cycle(plan)
    assert deleted == []
    assert (archive_dir / e.filename).exists()


def test_prune_to_newest_preserves_incrementals_of_newest(tmp_path, monkeypatch):
    """A newest complete cycle with incrementals — all its archives survive."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    archive_dir = tmp_path / "mount" / "archive"
    archive_dir.mkdir(parents=True)
    entries = [
        _entry("2025-01-01", "full"),
        _entry("2026-05-20", "full"),
        _entry("2026-05-21", "incr", cycle_id="2026-05-20"),
        _entry("2026-05-22", "incr", cycle_id="2026-05-20"),
    ]
    for e in entries:
        _touch(archive_dir / e.filename)
        _touch(archive_dir / (e.filename + ".idx.zst"))
    m = Manifest(plan_name="archive", archives=entries)
    manifestlib.save(m, manifestlib.manifest_path(archive_dir))

    plan = configlib.PlanConfig(
        plan_name="archive",
        sources=["/tmp/whatever"],
        destination=str(tmp_path / "mount"),
        include_hostname_in_path=False,
    )

    prune_to_newest_cycle(plan)

    # Old full gone.
    assert not (archive_dir / "2025-01-01_full.pax.zst").exists()
    # Newest cycle's full + both incrementals preserved.
    assert (archive_dir / "2026-05-20_full.pax.zst").exists()
    assert (archive_dir / "2026-05-21_incr.pax.zst").exists()
    assert (archive_dir / "2026-05-22_incr.pax.zst").exists()

    m_after = manifestlib.load(manifestlib.manifest_path(archive_dir))
    assert {a.filename for a in m_after.archives} == {
        "2026-05-20_full.pax.zst",
        "2026-05-21_incr.pax.zst",
        "2026-05-22_incr.pax.zst",
    }
