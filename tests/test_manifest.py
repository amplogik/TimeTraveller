"""Manifest cycle-grouping tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller.manifest import ArchiveEntry, Manifest, cycles


def _entry(date: str, kind: str, status: str = "ok", size: int = 100,
           cycle_id: str | None = None) -> ArchiveEntry:
    return ArchiveEntry(
        filename=f"{date}_{kind}.pax.zst",
        kind=kind,
        cycle_id=cycle_id or date,
        date_started=f"{date}T02:00:00+00:00",
        date_finished=f"{date}T02:30:00+00:00",
        size_bytes=size,
        status=status,
        hostname="testhost",
        plan_name="home",
    )


def test_cycles_groups_full_with_following_incrementals():
    m = Manifest(plan_name="home", archives=[
        _entry("2026-05-10", "full", cycle_id="2026-05-10"),
        _entry("2026-05-11", "incr", cycle_id="2026-05-10"),
        _entry("2026-05-12", "incr", cycle_id="2026-05-10"),
        _entry("2026-05-17", "full", cycle_id="2026-05-17"),
        _entry("2026-05-18", "incr", cycle_id="2026-05-17"),
    ])
    cs = cycles(m)
    assert len(cs) == 2
    assert cs[0].cycle_id == "2026-05-10"
    assert cs[0].full is not None
    assert len(cs[0].incrementals) == 2
    assert cs[0].is_complete
    assert cs[1].cycle_id == "2026-05-17"
    assert len(cs[1].incrementals) == 1


def test_failed_full_does_not_open_new_cycle():
    """A failed full leaves subsequent incrementals attached to the previous cycle."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-05-10", "full", cycle_id="2026-05-10"),
        _entry("2026-05-11", "incr", cycle_id="2026-05-10"),
        _entry("2026-05-17", "full", status="failed", cycle_id="2026-05-17"),
        _entry("2026-05-18", "incr", cycle_id="2026-05-10"),
    ])
    cs = cycles(m)
    assert len(cs) == 1, "the failed full should not open a new cycle"
    c = cs[0]
    assert c.cycle_id == "2026-05-10"
    assert c.is_complete
    # The failed full and the subsequent incremental are both attached to the previous cycle.
    assert len(c.incrementals) == 3
    statuses = [a.status for a in c.incrementals]
    assert "failed" in statuses


def test_ok_with_warnings_full_opens_a_cycle():
    """A full with status 'ok-with-warnings' is trustworthy and opens a cycle,
    just like a clean 'ok' full. Subsequent incrementals attach to it."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-05-10", "full", cycle_id="2026-05-10"),
        _entry("2026-05-17", "full", status="ok-with-warnings", cycle_id="2026-05-17"),
        _entry("2026-05-18", "incr", cycle_id="2026-05-17"),
    ])
    cs = cycles(m)
    assert len(cs) == 2
    assert cs[1].cycle_id == "2026-05-17"
    assert cs[1].full is not None
    assert cs[1].full.status == "ok-with-warnings"
    assert cs[1].is_complete, "ok-with-warnings must be treated as complete"
    assert len(cs[1].incrementals) == 1


def test_orphan_incrementals_create_stub_cycle():
    """Incrementals with no prior full get a stub cycle so they're not lost."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-05-11", "incr"),
        _entry("2026-05-12", "incr"),
    ])
    cs = cycles(m)
    assert len(cs) == 1
    assert cs[0].full is None
    assert not cs[0].is_complete
    assert len(cs[0].incrementals) == 2
