"""Tests for the archive enumeration API split (mirror-only vs mount-touching)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import archive as archivelib
from timetraveller import manifest as manifestlib


def _entry(filename: str, kind: str = "full", status: str = "ok",
           cycle_id: str = "2026-05-19", size: int = 1024) -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename,
        kind=kind,
        cycle_id=cycle_id,
        date_started="2026-05-19T12:00:00+00:00",
        date_finished="2026-05-19T13:00:00+00:00",
        size_bytes=size,
        status=status,
        hostname="testhost",
        plan_name="testplan",
    )


def _write_manifest(path: Path, archives: list[manifestlib.ArchiveEntry]) -> None:
    m = manifestlib.Manifest(plan_name="testplan", archives=archives)
    manifestlib.save(m, path)


def test_list_from_manifest_reads_only_the_mirror(tmp_path, monkeypatch):
    """list_from_manifest must read XDG_STATE_HOME/timetraveller/<plan>/manifest.json
    and not the on-mount path."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    mirror = manifestlib.mirror_manifest_path("testplan")
    _write_manifest(mirror, [_entry("2026-05-19T120000_full.pax.zst")])

    # Build an archive_dir that has a DIFFERENT manifest — to prove we're
    # reading the mirror, not the on-mount path.
    archive_dir = tmp_path / "mount" / "plan"
    archive_dir.mkdir(parents=True)
    _write_manifest(
        manifestlib.manifest_path(archive_dir),
        [_entry("DECOY_full.pax.zst")],
    )

    listing = archivelib.list_from_manifest("testplan", archive_dir)

    assert len(listing.cycles) == 1
    assert listing.cycles[0].full is not None
    assert listing.cycles[0].full.filename == "2026-05-19T120000_full.pax.zst", \
        "should have read the mirror, not the on-mount manifest"
    assert listing.archive_dir == archive_dir


def test_list_from_manifest_never_stats_archive_dir(tmp_path, monkeypatch):
    """The archive_dir parameter is purely informational — list_from_manifest
    must not touch it (no .exists(), no glob)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    mirror = manifestlib.mirror_manifest_path("testplan")
    _write_manifest(mirror, [_entry("2026-05-19T120000_full.pax.zst")])

    # archive_dir doesn't exist — and if list_from_manifest tried to scan it,
    # the test would either error or silently miss the manifest content.
    bogus_dir = tmp_path / "this" / "does" / "not" / "exist"

    listing = archivelib.list_from_manifest("testplan", bogus_dir)

    assert len(listing.cycles) == 1
    assert listing.archive_dir == bogus_dir  # recorded as-is, not validated


def test_list_from_manifest_empty_when_mirror_missing(tmp_path, monkeypatch):
    """When the mirror doesn't exist, return an empty listing (no exception).
    The caller can decide whether to prompt for --refresh-from-mount."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    archive_dir = tmp_path / "mount" / "plan"
    listing = archivelib.list_from_manifest("never-backed-up", archive_dir)

    assert listing.cycles == []
    assert listing.plan_name == "never-backed-up"  # fallback when mirror is empty


def test_discover_orphans_finds_files_not_in_manifest(tmp_path):
    archive_dir = tmp_path / "plan"
    archive_dir.mkdir()
    _write_manifest(
        manifestlib.manifest_path(archive_dir),
        [_entry("KNOWN_full.pax.zst")],
    )
    # Drop two archive files on disk: one known, one orphaned.
    (archive_dir / "KNOWN_full.pax.zst").write_bytes(b"x" * 100)
    (archive_dir / "ORPHAN_full.pax.zst").write_bytes(b"y" * 200)

    orphans = archivelib.discover_orphans(archive_dir)

    assert len(orphans) == 1
    assert orphans[0].filename == "ORPHAN_full.pax.zst"
    assert orphans[0].status == "orphan"
    assert orphans[0].size_bytes == 200


def test_discover_orphans_empty_when_all_known(tmp_path):
    archive_dir = tmp_path / "plan"
    archive_dir.mkdir()
    _write_manifest(
        manifestlib.manifest_path(archive_dir),
        [_entry("A_full.pax.zst"), _entry("B_incr.pax.zst", kind="incr")],
    )
    (archive_dir / "A_full.pax.zst").write_bytes(b"x")
    (archive_dir / "B_incr.pax.zst").write_bytes(b"y")

    assert archivelib.discover_orphans(archive_dir) == []


def test_discover_orphans_handles_missing_dir(tmp_path):
    """If the archive_dir doesn't exist (mount not present), return empty."""
    assert archivelib.discover_orphans(tmp_path / "missing") == []


def test_list_for_plan_combines_manifest_and_orphans(tmp_path):
    """The wrapper must still surface both the manifest cycles AND the orphans."""
    archive_dir = tmp_path / "plan"
    archive_dir.mkdir()
    _write_manifest(
        manifestlib.manifest_path(archive_dir),
        [_entry("KNOWN_full.pax.zst")],
    )
    (archive_dir / "KNOWN_full.pax.zst").write_bytes(b"x")
    (archive_dir / "ORPHAN_full.pax.zst").write_bytes(b"y")

    listing = archivelib.list_for_plan(archive_dir)

    cycle_ids = [c.cycle_id for c in listing.cycles]
    assert "(orphan)" in cycle_ids
    # The "known" archive lands in its own cycle (by its cycle_id from the manifest).
    assert any(c.full and c.full.filename == "KNOWN_full.pax.zst"
               for c in listing.cycles)
