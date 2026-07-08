"""Tests for the portable restore descriptor + config-less backup-location
discovery (timetraveller.restore_source)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as manifestlib
from timetraveller import restore_source as rs
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import action_list_archives


# ---------- helpers ----------

def _plan(destination: Path, name: str = "home", sources=None) -> PlanConfig:
    return PlanConfig(
        plan_name=name, sources=sources or ["/home/kim"],
        excludes=["**/.cache/"], destination=str(destination),
        include_hostname_in_path=True,
        schedule=Schedule(mode="weekly", full=FullSchedule(),
                          incr=IncrSchedule(mode="weekdays")),
        retention=Retention(),
    )


def _entry(filename: str, plan_name: str = "home") -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename, kind="full", cycle_id="2026-06-28",
        date_started="2026-06-28T02:00:00+00:00",
        date_finished="2026-06-28T03:00:00+00:00",
        size_bytes=100, status="ok", hostname="bast", plan_name=plan_name,
    )


def _seed_archive_dir(d: Path, *, plan_name="home", with_descriptor=True,
                      with_manifest=True, with_meta=False, sources=None):
    """Create a realistic archive directory under d."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-06-28_full.pax.zst").write_bytes(b"fake archive")
    if with_manifest:
        manifestlib.save(
            manifestlib.Manifest(plan_name=plan_name,
                                 archives=[_entry("2026-06-28_full.pax.zst", plan_name)]),
            manifestlib.manifest_path(d),
        )
    if with_meta:
        manifestlib.write_entry_meta(d, _entry("2026-06-28_full.pax.zst", plan_name))
    if with_descriptor:
        rs.write_descriptor(d, rs.RestoreDescriptor(
            plan_name=plan_name, sources=sources or ["/home/kim"],
            hostname="bast", written_at="2026-06-28T03:00:00+00:00"))


# ---------- descriptor round-trip ----------

def test_descriptor_round_trip(tmp_path):
    desc = rs.RestoreDescriptor(
        plan_name="home", sources=["/home/kim", "/home/kim/work"],
        hostname="bast", excludes=["**/.cache/"], written_at="2026-06-28T03:00:00+00:00")
    rs.write_descriptor(tmp_path, desc)

    got = rs.read_descriptor(rs.descriptor_path(tmp_path))
    assert got is not None
    assert got.plan_name == "home"
    assert got.sources == ["/home/kim", "/home/kim/work"]
    assert got.hostname == "bast"
    assert got.schema_version == rs.SCHEMA_VERSION


def test_from_plan_captures_sources_and_flags(tmp_path):
    plan = _plan(tmp_path, sources=["/home/kim"])
    desc = rs.from_plan(plan, created_by="timetraveller test")
    assert desc.plan_name == "home"
    assert desc.sources == ["/home/kim"]
    assert desc.include_hostname_in_path is True
    assert desc.excludes == ["**/.cache/"]
    assert desc.created_by == "timetraveller test"
    # hostname is captured from the running host — just assert it's populated.
    assert desc.hostname


def test_read_descriptor_missing_returns_none(tmp_path):
    assert rs.read_descriptor(rs.descriptor_path(tmp_path)) is None


def test_read_descriptor_garbage_returns_none(tmp_path):
    p = rs.descriptor_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json {{{")
    assert rs.read_descriptor(p) is None


def test_read_descriptor_tolerates_unknown_keys(tmp_path):
    """A future-schema descriptor with extra keys must still yield what we can
    use, not crash a restore browse."""
    p = rs.descriptor_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "plan_name": "home", "sources": ["/home/kim"],
        "future_field": {"nested": 1}, "schema_version": 999,
    }))
    got = rs.read_descriptor(p)
    assert got is not None
    assert got.plan_name == "home"
    assert got.sources == ["/home/kim"]


# ---------- discovery ----------

def test_discover_at_root_when_root_is_archive_dir(tmp_path):
    _seed_archive_dir(tmp_path)
    locs = rs.discover_backup_locations(tmp_path)
    assert len(locs) == 1
    assert locs[0].archive_dir == tmp_path
    assert locs[0].plan_name == "home"
    assert locs[0].sources == ["/home/kim"]
    assert locs[0].has_descriptor is True
    assert locs[0].n_archives == 1


def test_discover_nested_host_plan_layout(tmp_path):
    """The canonical <root>/<host>/<plan>/ nesting used with
    include_hostname_in_path — browse the destination root, find both plans."""
    _seed_archive_dir(tmp_path / "bast" / "home", plan_name="home",
                      sources=["/home/kim"])
    _seed_archive_dir(tmp_path / "bast" / "system", plan_name="system",
                      sources=["/", "/boot/efi"])
    locs = rs.discover_backup_locations(tmp_path)
    by_plan = {loc.plan_name: loc for loc in locs}
    assert set(by_plan) == {"home", "system"}
    assert by_plan["home"].archive_dir == tmp_path / "bast" / "home"
    assert by_plan["system"].sources == ["/", "/boot/efi"]


def test_discover_falls_back_to_manifest_without_descriptor(tmp_path):
    """No descriptor (a pre-descriptor backup): identity still recoverable from
    the manifest; sources unknown (empty) but extract-to-dir still viable."""
    _seed_archive_dir(tmp_path / "home", with_descriptor=False)
    locs = rs.discover_backup_locations(tmp_path)
    assert len(locs) == 1
    assert locs[0].plan_name == "home"
    assert locs[0].has_descriptor is False
    assert locs[0].sources == []
    assert locs[0].hostname == "bast"  # from the manifest entry


def test_discover_falls_back_to_meta_when_manifest_missing(tmp_path):
    """A hand-moved directory of archives with no manifest.json rebuilds
    identity from the .meta.json sidecars."""
    _seed_archive_dir(tmp_path / "home", with_descriptor=False,
                      with_manifest=False, with_meta=True)
    locs = rs.discover_backup_locations(tmp_path)
    assert len(locs) == 1
    assert locs[0].plan_name == "home"


def test_discover_does_not_descend_into_archive_dir(tmp_path):
    """An archive dir is a leaf — we must not treat files/subdirs inside it as
    further locations."""
    ad = tmp_path / "bast" / "home"
    _seed_archive_dir(ad)
    (ad / "subdir").mkdir()
    (ad / "subdir" / "stray.pax.zst").write_bytes(b"x")
    locs = rs.discover_backup_locations(tmp_path)
    assert len(locs) == 1
    assert locs[0].archive_dir == ad


def test_discover_empty_tree_returns_nothing(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.txt").write_text("hi")
    assert rs.discover_backup_locations(tmp_path) == []


# ---------- worker integration ----------

def _args(**overrides) -> argparse.Namespace:
    defaults = dict(refresh_from_mount=True, check_orphans=False, quiet=True,
                    reindex=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_refresh_from_mount_backfills_descriptor(tmp_path, monkeypatch):
    """A pre-existing backup with no descriptor gains one on --refresh-from-mount,
    carrying the plan's real source roots."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount", sources=["/home/kim"])
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    (archive_dir / "A.pax.zst").write_bytes(b"x")
    manifestlib.save(
        manifestlib.Manifest(plan_name="home", archives=[_entry("A.pax.zst")]),
        manifestlib.manifest_path(archive_dir),
    )
    assert not rs.descriptor_path(archive_dir).exists()

    rc = action_list_archives(_args(), plan)
    assert rc == 0

    desc = rs.read_descriptor(rs.descriptor_path(archive_dir))
    assert desc is not None
    assert desc.plan_name == "home"
    assert desc.sources == ["/home/kim"]
