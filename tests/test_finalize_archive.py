"""Tests for action_finalize_archive — post-crash manifest fix-up."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import framewriter
from timetraveller import index as indexlib
from timetraveller import manifest as manifestlib
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import action_finalize_archive


def _entry(filename: str, status: str = "in-progress") -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename, kind="full", cycle_id="2026-05-31",
        date_started="2026-05-31T09:00:00+00:00",
        date_finished="",
        size_bytes=0, status=status, hostname="testhost",
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


def _args(filename: str, **overrides) -> argparse.Namespace:
    defaults = dict(
        finalize_archive=filename,
        status="ok-with-warnings",
        force=False,
        quiet=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _seed(tmp_path: Path, *, with_sidecar=False, with_frames=False,
          entry_status="in-progress") -> tuple[PlanConfig, Path, str]:
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    fname = "2026-05-31_full.pax.zst"
    archive = archive_dir / fname
    archive.write_bytes(b"x" * 4096)
    if with_sidecar:
        indexlib.sidecar_path(archive).write_bytes(b"sidecar")
    if with_frames:
        framewriter.sidecar_path(archive).write_text("{}")

    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry(fname, status=entry_status)]),
        manifestlib.manifest_path(archive_dir),
    )
    return plan, archive, fname


def test_finalize_archive_missing_archive_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    plan.archive_dir().mkdir(parents=True)

    rc = action_finalize_archive(_args("does-not-exist.pax.zst"), plan)

    assert rc == 1
    assert "archive not found" in capsys.readouterr().err


def test_finalize_archive_missing_manifest_entry_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    fname = "orphan.pax.zst"
    (archive_dir / fname).write_bytes(b"x")
    # No manifest entry for this file.
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan", archives=[]),
        manifestlib.manifest_path(archive_dir),
    )

    rc = action_finalize_archive(_args(fname), plan)

    assert rc == 1
    err = capsys.readouterr().err
    assert "no manifest entry" in err


def test_finalize_archive_refuses_terminal_status_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, _, fname = _seed(tmp_path, entry_status="ok")

    rc = action_finalize_archive(_args(fname), plan)

    assert rc == 1
    err = capsys.readouterr().err
    assert "already 'ok'" in err
    assert "--force" in err


def test_finalize_archive_updates_in_progress_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, archive, fname = _seed(tmp_path, with_sidecar=True, with_frames=True)

    rc = action_finalize_archive(_args(fname), plan)
    assert rc == 0

    # On-mount manifest reflects the finalize.
    m = manifestlib.load(manifestlib.manifest_path(plan.archive_dir()))
    e = m.archives[0]
    assert e.status == "ok-with-warnings"
    assert e.date_finished != ""
    assert e.size_bytes == archive.stat().st_size
    assert e.has_sidecar is True
    assert e.has_frames is True

    # Mirror was updated by _save_manifest.
    mirror = manifestlib.load(manifestlib.mirror_manifest_path("testplan"))
    assert mirror.archives[0].status == "ok-with-warnings"

    # Sidecar mirror was copied alongside.
    assert indexlib.sidecar_mirror_path("testplan", fname).exists()


def test_finalize_archive_reflects_missing_sidecars(tmp_path, monkeypatch):
    """If no sidecar/frames file is on disk, the entry must record that
    accurately rather than asserting True."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, _, fname = _seed(tmp_path, with_sidecar=False, with_frames=False)

    rc = action_finalize_archive(_args(fname), plan)
    assert rc == 0

    e = manifestlib.load(manifestlib.manifest_path(plan.archive_dir())).archives[0]
    assert e.has_sidecar is False
    assert e.has_frames is False
    # And the mirror sidecar must NOT have been created.
    assert not indexlib.sidecar_mirror_path("testplan", fname).exists()


def test_finalize_archive_force_overrides_terminal_status(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, _, fname = _seed(tmp_path, entry_status="ok")

    rc = action_finalize_archive(_args(fname, force=True, status="failed"), plan)
    assert rc == 0

    e = manifestlib.load(manifestlib.manifest_path(plan.archive_dir())).archives[0]
    assert e.status == "failed"


def test_finalize_archive_honors_custom_status(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, _, fname = _seed(tmp_path)

    rc = action_finalize_archive(_args(fname, status="ok"), plan)
    assert rc == 0

    e = manifestlib.load(manifestlib.manifest_path(plan.archive_dir())).archives[0]
    assert e.status == "ok"
