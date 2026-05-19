"""Tests for the sidecar local mirror: path computation, copy, delete, and
population from --refresh-from-mount / action_prune."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import index as indexlib
from timetraveller import manifest as manifestlib
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import action_list_archives, action_prune


def _entry(filename: str, has_sidecar: bool = False) -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename, kind="full", cycle_id="2026-05-19",
        date_started="2026-05-19T12:00:00+00:00",
        date_finished="2026-05-19T13:00:00+00:00",
        size_bytes=1024, status="ok", hostname="testhost",
        plan_name="testplan", has_sidecar=has_sidecar,
    )


def _plan(destination: Path, max_cycles: int = 4) -> PlanConfig:
    return PlanConfig(
        plan_name="testplan", sources=["/tmp"], destination=str(destination),
        include_hostname_in_path=True,
        schedule=Schedule(mode="weekly", full=FullSchedule(),
                          incr=IncrSchedule(mode="weekdays")),
        retention=Retention(policy="max_cycles", max_cycles=max_cycles),
    )


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(refresh_from_mount=False, check_orphans=False, quiet=True,
                    reindex=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_sidecar_mirror_path_honors_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    p = indexlib.sidecar_mirror_path("home", "ARCHIVE.pax.zst")
    assert p == tmp_path / "xdg" / "timetraveller" / "home" / "sidecars" / "ARCHIVE.pax.zst.idx.zst"


def test_copy_sidecar_to_mirror_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "source.idx.zst"
    source.write_bytes(b"fake sidecar content")

    indexlib.copy_sidecar_to_mirror("home", source, "A.pax.zst")

    dst = indexlib.sidecar_mirror_path("home", "A.pax.zst")
    assert dst.exists()
    assert dst.read_bytes() == b"fake sidecar content"


def test_copy_sidecar_to_mirror_creates_parent_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "x.idx.zst"
    source.write_bytes(b"x")

    # Parent doesn't exist yet.
    assert not (tmp_path / "state" / "timetraveller" / "home" / "sidecars").exists()

    indexlib.copy_sidecar_to_mirror("home", source, "X.pax.zst")

    assert (tmp_path / "state" / "timetraveller" / "home" / "sidecars" / "X.pax.zst.idx.zst").exists()


def test_delete_sidecar_mirror_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # No file present — should not raise.
    indexlib.delete_sidecar_mirror("home", "MISSING.pax.zst")

    # Now create and delete.
    source = tmp_path / "y.idx.zst"
    source.write_bytes(b"y")
    indexlib.copy_sidecar_to_mirror("home", source, "Y.pax.zst")
    p = indexlib.sidecar_mirror_path("home", "Y.pax.zst")
    assert p.exists()
    indexlib.delete_sidecar_mirror("home", "Y.pax.zst")
    assert not p.exists()
    # Second delete still no-op.
    indexlib.delete_sidecar_mirror("home", "Y.pax.zst")


def test_refresh_from_mount_seeds_sidecar_mirror(tmp_path, monkeypatch):
    """When --refresh-from-mount detects an on-mount sidecar, it must also
    copy that sidecar into the local mirror so the GUI can browse offline."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("A.pax.zst", has_sidecar=False)]),
        manifestlib.manifest_path(archive_dir),
    )
    (archive_dir / "A.pax.zst").write_bytes(b"x")
    (archive_dir / "A.pax.zst.idx.zst").write_bytes(b"sidecar content")

    rc = action_list_archives(_args(refresh_from_mount=True), plan)
    assert rc == 0

    mirror_sc = indexlib.sidecar_mirror_path("testplan", "A.pax.zst")
    assert mirror_sc.exists()
    assert mirror_sc.read_bytes() == b"sidecar content"


def test_refresh_from_mount_skips_mirror_copy_when_no_sidecar(tmp_path, monkeypatch):
    """If has_sidecar resolves False (no on-disk file), the mirror must NOT
    be populated — and any stale mirror copy is left alone (purge happens
    only via action_prune, not via refresh)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan",
                             archives=[_entry("A.pax.zst")]),
        manifestlib.manifest_path(archive_dir),
    )
    (archive_dir / "A.pax.zst").write_bytes(b"x")
    # No sidecar on disk.

    rc = action_list_archives(_args(refresh_from_mount=True), plan)
    assert rc == 0
    assert not indexlib.sidecar_mirror_path("testplan", "A.pax.zst").exists()


def test_action_prune_removes_sidecar_mirror(tmp_path, monkeypatch):
    """When retention prunes an archive, its mirror sidecar must also be
    deleted — otherwise the mirror grows unbounded."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount", max_cycles=1)
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)

    # Two complete cycles. max_cycles=1 means the older one gets pruned.
    old = manifestlib.ArchiveEntry(
        filename="OLD_full.pax.zst", kind="full", cycle_id="2026-05-10",
        date_started="2026-05-10T12:00:00+00:00",
        date_finished="2026-05-10T13:00:00+00:00",
        size_bytes=100, status="ok", hostname="h", plan_name="testplan",
        has_sidecar=True,
    )
    new = manifestlib.ArchiveEntry(
        filename="NEW_full.pax.zst", kind="full", cycle_id="2026-05-19",
        date_started="2026-05-19T12:00:00+00:00",
        date_finished="2026-05-19T13:00:00+00:00",
        size_bytes=100, status="ok", hostname="h", plan_name="testplan",
        has_sidecar=True,
    )
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan", archives=[old, new]),
        manifestlib.manifest_path(archive_dir),
    )

    # Create on-disk files and mirror copies for both archives.
    for name in ("OLD_full.pax.zst", "NEW_full.pax.zst"):
        (archive_dir / name).write_bytes(b"x")
        (archive_dir / (name + ".idx.zst")).write_bytes(b"sc")
        # Seed the mirror as if it had been populated by a prior backup.
        indexlib.copy_sidecar_to_mirror(
            "testplan", archive_dir / (name + ".idx.zst"), name)

    assert indexlib.sidecar_mirror_path("testplan", "OLD_full.pax.zst").exists()

    rc = action_prune(_args(), plan)
    assert rc == 0

    # OLD's mirror sidecar should be gone; NEW's should remain.
    assert not indexlib.sidecar_mirror_path("testplan", "OLD_full.pax.zst").exists()
    assert indexlib.sidecar_mirror_path("testplan", "NEW_full.pax.zst").exists()
