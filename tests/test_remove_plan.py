"""Tests for worker.action_remove_plan() — wired to the GUI Remove Plan button."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from pathlib import Path

from timetraveller import config as configlib
from timetraveller import manifest as manifestlib
from timetraveller.manifest import ArchiveEntry, Manifest
from timetraveller.worker import action_remove_plan


def _entry(date: str, kind: str) -> ArchiveEntry:
    return ArchiveEntry(
        filename=f"{date}_{kind}.pax.zst",
        kind=kind, cycle_id=date,
        date_started=f"{date}T02:00:00+00:00",
        date_finished=f"{date}T02:30:00+00:00",
        size_bytes=100, status="ok", hostname="h", plan_name="tobedeleted",
    )


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def _setup_plan(tmp_path: Path, monkeypatch) -> tuple[configlib.PlanConfig, Path, Path, Path]:
    """Create a full plan with config + archives + mirror + log. Returns
    (plan, yaml_path, archive_dir, state_dir)."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    archive_dir = tmp_path / "mount" / "tobedeleted"
    archive_dir.mkdir(parents=True)
    entries = [_entry("2025-01-01", "full"), _entry("2026-05-20", "full")]
    for e in entries:
        _touch(archive_dir / e.filename)
        _touch(archive_dir / (e.filename + ".idx.zst"))
    m = Manifest(plan_name="tobedeleted", archives=entries)
    manifestlib.save(m, manifestlib.manifest_path(archive_dir))

    plan = configlib.PlanConfig(
        plan_name="tobedeleted",
        sources=["/tmp/whatever"],
        destination=str(tmp_path / "mount"),
        include_hostname_in_path=False,
    )
    yaml_path = configlib.user_config_path("tobedeleted")
    configlib.save(plan, yaml_path)

    # Seed the local mirror state so we can verify it gets cleared.
    mirror_dir = manifestlib.mirror_manifest_path("tobedeleted").parent
    mirror_dir.mkdir(parents=True, exist_ok=True)
    (mirror_dir / "manifest.json").write_text("{}")
    (mirror_dir / "sidecars").mkdir(exist_ok=True)
    (mirror_dir / "sidecars" / "fake.idx.zst").write_bytes(b"x")

    # Seed a log file in the standard location.
    log_path = state_dir / "timetraveller" / "tobedeleted.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("some log entries\n")

    return plan, yaml_path, archive_dir, state_dir


def _args(plan_name: str, config_path: Path, remove_backups: bool = False) -> argparse.Namespace:
    """A minimal argparse Namespace shaped like the real CLI gives us."""
    return argparse.Namespace(
        plan=plan_name, config=str(config_path),
        remove_backups=remove_backups, quiet=True,
    )


def test_remove_plan_keeps_backups_by_default(tmp_path, monkeypatch):
    plan, yaml_path, archive_dir, state_dir = _setup_plan(tmp_path, monkeypatch)
    # Pretend no cron is installed (the action_uninstall_schedule path will be
    # invoked and should no-op cleanly).
    monkeypatch.setattr("timetraveller.worker._read_user_crontab", lambda: "")

    rc = action_remove_plan(_args("tobedeleted", yaml_path), plan)
    assert rc == 0

    # YAML gone.
    assert not yaml_path.exists()
    # Local mirror cleared.
    mirror_dir = manifestlib.mirror_manifest_path("tobedeleted").parent
    assert not mirror_dir.exists()
    # Log gone.
    assert not (state_dir / "timetraveller" / "tobedeleted.log").exists()
    # Backups preserved.
    assert (archive_dir / "2025-01-01_full.pax.zst").exists()
    assert (archive_dir / "2026-05-20_full.pax.zst").exists()
    assert manifestlib.manifest_path(archive_dir).exists()


def test_remove_plan_with_remove_backups_clears_archive_dir(tmp_path, monkeypatch):
    plan, yaml_path, archive_dir, state_dir = _setup_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("timetraveller.worker._read_user_crontab", lambda: "")

    rc = action_remove_plan(_args("tobedeleted", yaml_path, remove_backups=True), plan)
    assert rc == 0

    assert not yaml_path.exists()
    # Every archive file + sidecar + manifest gone, dir itself gone too.
    assert not archive_dir.exists()


def test_remove_plan_idempotent(tmp_path, monkeypatch):
    """Running twice should still exit clean — every step tolerates missing state."""
    plan, yaml_path, _, _ = _setup_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("timetraveller.worker._read_user_crontab", lambda: "")

    assert action_remove_plan(_args("tobedeleted", yaml_path), plan) == 0
    # Second run: YAML/mirror/log all gone; archive dir still has files.
    assert action_remove_plan(_args("tobedeleted", yaml_path), plan) == 0


def test_remove_plan_without_mount_does_not_fail(tmp_path, monkeypatch):
    """If the archive dir was never created (no backups taken), removal still works."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("timetraveller.worker._read_user_crontab", lambda: "")

    plan = configlib.PlanConfig(
        plan_name="never-ran",
        sources=["/tmp/whatever"],
        destination=str(tmp_path / "nonexistent_mount"),
        include_hostname_in_path=False,
    )
    yaml_path = configlib.user_config_path("never-ran")
    configlib.save(plan, yaml_path)

    rc = action_remove_plan(_args("never-ran", yaml_path, remove_backups=True), plan)
    assert rc == 0
    assert not yaml_path.exists()
