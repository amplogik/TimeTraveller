"""Tests for action_list_archives CLI behaviors (mirror-only by default,
--refresh-from-mount populates mirror, --check-orphans scans on-mount)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as manifestlib
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import action_list_archives


def _entry(filename: str, kind: str = "full", status: str = "ok",
           cycle_id: str = "2026-05-19", size: int = 1024) -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename, kind=kind, cycle_id=cycle_id,
        date_started="2026-05-19T12:00:00+00:00",
        date_finished="2026-05-19T13:00:00+00:00",
        size_bytes=size, status=status, hostname="testhost",
        plan_name="testplan",
    )


def _plan(destination: Path) -> PlanConfig:
    return PlanConfig(
        plan_name="testplan",
        sources=["/tmp"],
        destination=str(destination),
        include_hostname_in_path=True,
        schedule=Schedule(mode="weekly", full=FullSchedule(),
                          incr=IncrSchedule(mode="weekdays")),
        retention=Retention(),
    )


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        refresh_from_mount=False,
        check_orphans=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_list_archives_errors_when_mirror_missing(tmp_path, monkeypatch, capsys):
    """First call ever: no mirror, no on-mount manifest yet. The error must
    name --refresh-from-mount so the user knows the way out."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")

    rc = action_list_archives(_args(), plan)

    assert rc == 1
    err = capsys.readouterr().err
    assert "No local manifest mirror" in err
    assert "--refresh-from-mount" in err


def test_list_archives_reads_mirror_when_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")

    mirror = manifestlib.mirror_manifest_path("testplan")
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("MIRROR_full.pax.zst")]),
        mirror,
    )

    rc = action_list_archives(_args(), plan)

    assert rc == 0
    out = capsys.readouterr().out
    assert "MIRROR_full.pax.zst" in out
    assert "Cycle 2026-05-19 [complete]" in out


def test_list_archives_refresh_from_mount_populates_mirror(tmp_path, monkeypatch, capsys):
    """Without an existing mirror, --refresh-from-mount must (1) read the
    on-mount manifest, (2) write the mirror, (3) print using the mirror."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")

    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("ON_MOUNT_full.pax.zst")]),
        manifestlib.manifest_path(archive_dir),
    )

    mirror = manifestlib.mirror_manifest_path("testplan")
    assert not mirror.exists(), "mirror must be missing before the test"

    rc = action_list_archives(_args(refresh_from_mount=True), plan)

    assert rc == 0
    assert mirror.exists(), "mirror must be created by --refresh-from-mount"
    out = capsys.readouterr().out
    assert "ON_MOUNT_full.pax.zst" in out


def test_list_archives_check_orphans_lists_unmanifested_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")

    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("KNOWN_full.pax.zst")]),
        manifestlib.manifest_path(archive_dir),
    )
    # An orphaned archive on the mount, not in the manifest:
    (archive_dir / "ORPHAN_full.pax.zst").write_bytes(b"x" * 4096)

    # Seed the mirror so the function reaches the orphan branch.
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("KNOWN_full.pax.zst")]),
        manifestlib.mirror_manifest_path("testplan"),
    )

    rc = action_list_archives(_args(check_orphans=True), plan)

    assert rc == 0
    out = capsys.readouterr().out
    assert "ORPHAN_full.pax.zst" in out
    assert "Orphans on mount" in out


def test_list_archives_check_orphans_clean_when_no_orphans(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")

    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("KNOWN_full.pax.zst")]),
        manifestlib.manifest_path(archive_dir),
    )
    (archive_dir / "KNOWN_full.pax.zst").write_bytes(b"x")

    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("KNOWN_full.pax.zst")]),
        manifestlib.mirror_manifest_path("testplan"),
    )

    rc = action_list_archives(_args(check_orphans=True), plan)

    assert rc == 0
    out = capsys.readouterr().out
    assert "No orphans on mount." in out
