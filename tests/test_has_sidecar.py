"""Tests for the has_sidecar field and its population during backup/reindex/refresh."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as manifestlib
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import action_list_archives, action_reindex


def _entry(filename: str, has_sidecar: bool = False) -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename, kind="full", cycle_id="2026-05-19",
        date_started="2026-05-19T12:00:00+00:00",
        date_finished="2026-05-19T13:00:00+00:00",
        size_bytes=1024, status="ok", hostname="testhost",
        plan_name="testplan", has_sidecar=has_sidecar,
    )


def _plan(destination: Path) -> PlanConfig:
    return PlanConfig(
        plan_name="testplan", sources=["/tmp"], destination=str(destination),
        include_hostname_in_path=True,
        schedule=Schedule(mode="weekly", full=FullSchedule(),
                          incr=IncrSchedule(mode="weekdays")),
        retention=Retention(),
    )


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(refresh_from_mount=False, check_orphans=False, quiet=True,
                    reindex=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_has_sidecar_defaults_false():
    e = manifestlib.ArchiveEntry(
        filename="x.pax.zst", kind="full", cycle_id="c",
        date_started="", date_finished="", size_bytes=0, status="ok",
        hostname="h", plan_name="p",
    )
    assert e.has_sidecar is False


def test_old_manifest_without_has_sidecar_field_loads(tmp_path):
    """Backward compatibility: a manifest written before the field existed
    must still load, with has_sidecar defaulting to False."""
    old_manifest = {
        "plan_name": "legacy",
        "schema_version": 1,
        "archives": [{
            "filename": "OLD_full.pax.zst",
            "kind": "full", "cycle_id": "2024-01-01",
            "date_started": "2024-01-01T00:00:00+00:00",
            "date_finished": "2024-01-01T01:00:00+00:00",
            "size_bytes": 100, "status": "ok",
            "hostname": "host", "plan_name": "legacy",
            "incr_window_from": "", "incr_window_to": "",
            "file_count": None, "notes": "",
            # NOTE: no has_sidecar key
        }],
    }
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(old_manifest))

    m = manifestlib.load(mpath)
    assert len(m.archives) == 1
    assert m.archives[0].has_sidecar is False


def test_has_sidecar_survives_save_load_round_trip(tmp_path):
    m = manifestlib.Manifest(
        plan_name="testplan",
        archives=[_entry("A.pax.zst", has_sidecar=True),
                  _entry("B.pax.zst", has_sidecar=False)],
    )
    p = tmp_path / "manifest.json"
    manifestlib.save(m, p)

    loaded = manifestlib.load(p)
    by_name = {a.filename: a for a in loaded.archives}
    assert by_name["A.pax.zst"].has_sidecar is True
    assert by_name["B.pax.zst"].has_sidecar is False


def test_refresh_from_mount_backfills_has_sidecar(tmp_path, monkeypatch):
    """Existing archive on disk has a sidecar but the manifest says
    has_sidecar=False (because the field didn't exist when it was written).
    --refresh-from-mount must detect the sidecar and update the manifest."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    # On-mount manifest claims has_sidecar=False:
    on_mount_path = manifestlib.manifest_path(archive_dir)
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("A.pax.zst", has_sidecar=False)]),
        on_mount_path,
    )
    # But the sidecar file is actually on disk:
    (archive_dir / "A.pax.zst").write_bytes(b"x")
    (archive_dir / "A.pax.zst.idx.zst").write_bytes(b"y")

    rc = action_list_archives(_args(refresh_from_mount=True), plan)
    assert rc == 0

    # Both on-mount and mirror should now reflect has_sidecar=True:
    on_mount = manifestlib.load(on_mount_path)
    assert on_mount.archives[0].has_sidecar is True

    mirror = manifestlib.load(manifestlib.mirror_manifest_path("testplan"))
    assert mirror.archives[0].has_sidecar is True


def test_refresh_from_mount_unsets_has_sidecar_when_file_gone(tmp_path, monkeypatch):
    """The flip side: if the manifest claims has_sidecar=True but the file
    has been deleted (manually removed, NAS bitrot, etc.), --refresh-from-mount
    must correct the manifest back to False."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("A.pax.zst", has_sidecar=True)]),
        manifestlib.manifest_path(archive_dir),
    )
    (archive_dir / "A.pax.zst").write_bytes(b"x")
    # No sidecar file on disk.

    rc = action_list_archives(_args(refresh_from_mount=True), plan)
    assert rc == 0

    mirror = manifestlib.load(manifestlib.mirror_manifest_path("testplan"))
    assert mirror.archives[0].has_sidecar is False


def test_action_reindex_sets_has_sidecar(tmp_path, monkeypatch):
    """Reindex generates sidecars; the manifest entries for those archives
    must be updated to has_sidecar=True so the GUI doesn't have to stat."""
    # Stub indexlib.write_sidecar so we don't need a real .pax.zst.
    from timetraveller import index as indexlib

    def fake_write_sidecar(archive_path: Path) -> None:
        indexlib.sidecar_path(archive_path).write_bytes(b"fake")

    monkeypatch.setattr(indexlib, "write_sidecar", fake_write_sidecar)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    # Archive exists, manifest has has_sidecar=False, no sidecar on disk yet.
    (archive_dir / "A.pax.zst").write_bytes(b"x")
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("A.pax.zst", has_sidecar=False)]),
        manifestlib.manifest_path(archive_dir),
    )

    rc = action_reindex(_args(reindex="*"), plan)
    assert rc == 0

    on_mount = manifestlib.load(manifestlib.manifest_path(archive_dir))
    assert on_mount.archives[0].has_sidecar is True
