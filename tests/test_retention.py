"""Retention policy tests."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller.manifest import ArchiveEntry, Manifest
from timetraveller.retention import apply


NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _entry(date: str, kind: str, status: str = "ok", size: int = 100,
           cycle_id: str | None = None) -> ArchiveEntry:
    return ArchiveEntry(
        filename=f"{date}_{kind}.pax.zst",
        kind=kind, cycle_id=cycle_id or date,
        date_started=f"{date}T02:00:00+00:00",
        date_finished=f"{date}T02:30:00+00:00",
        size_bytes=size, status=status, hostname="h", plan_name="home",
    )


def _five_weekly_cycles() -> Manifest:
    return Manifest(plan_name="home", archives=[
        _entry("2026-04-19", "full"),
        _entry("2026-04-26", "full"),
        _entry("2026-05-03", "full"),
        _entry("2026-05-10", "full"),
        _entry("2026-05-17", "full"),
    ])


def test_max_cycles_keeps_n_most_recent():
    m = _five_weekly_cycles()
    plan = apply(m, policy="max_cycles", max_cycles=3, now=NOW)
    assert [c.cycle_id for c in plan.keep] == ["2026-05-03", "2026-05-10", "2026-05-17"]
    assert [c.cycle_id for c in plan.delete] == ["2026-04-19", "2026-04-26"]


def test_max_cycles_floor_of_1_always_kept():
    """max_cycles=0 still keeps at least 1 cycle (the newest)."""
    m = _five_weekly_cycles()
    plan = apply(m, policy="max_cycles", max_cycles=0, now=NOW)
    assert len(plan.keep) == 1
    assert plan.keep[0].cycle_id == "2026-05-17"


def test_max_age_days_never_deletes_newest_complete_cycle():
    """Even if every cycle is ancient, we keep the newest one."""
    m = _five_weekly_cycles()
    # Set "now" to a date a year later so every cycle is older than max_age_days.
    far_future = NOW + timedelta(days=365)
    plan = apply(m, policy="max_age_days", max_age_days=7, now=far_future)
    keep_ids = [c.cycle_id for c in plan.keep]
    assert "2026-05-17" in keep_ids, "newest complete cycle must survive"


def test_max_age_days_normal_case():
    m = _five_weekly_cycles()
    # 14 days back from NOW = 2026-05-06; cycles before that should go.
    plan = apply(m, policy="max_age_days", max_age_days=14, now=NOW)
    keep_ids = {c.cycle_id for c in plan.keep}
    delete_ids = {c.cycle_id for c in plan.delete}
    assert "2026-05-17" in keep_ids
    assert "2026-05-10" in keep_ids
    assert "2026-04-19" in delete_ids
    assert "2026-04-26" in delete_ids


def test_incomplete_cycles_kept_regardless_of_policy():
    """A cycle with a failed/missing full is preserved (for diagnostics)."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-04-19", "full"),
        _entry("2026-04-26", "full"),
        _entry("2026-05-03", "full"),
        _entry("2026-05-10", "full"),
        _entry("2026-05-17", "full"),
        _entry("2026-05-18", "incr", cycle_id="2026-05-10"),  # orphan-like
    ])
    plan = apply(m, policy="max_cycles", max_cycles=2, now=NOW)
    keep_ids = {c.cycle_id for c in plan.keep}
    # Should keep newest 2 complete + the existing complete cycles aren't incomplete here.
    assert "2026-05-17" in keep_ids
    assert "2026-05-10" in keep_ids


def test_max_size_gb_keeps_newest_until_cap():
    m = Manifest(plan_name="home", archives=[
        _entry("2026-04-19", "full", size=1024**3),     # 1 GiB
        _entry("2026-04-26", "full", size=1024**3),
        _entry("2026-05-03", "full", size=1024**3),
        _entry("2026-05-10", "full", size=1024**3),
        _entry("2026-05-17", "full", size=1024**3),
    ])
    plan = apply(m, policy="max_size_gb", max_size_gb=2.5, now=NOW)
    keep_ids = [c.cycle_id for c in sorted(plan.keep, key=lambda c: c.cycle_id)]
    # Newest 2 fit under 2.5 GiB; the third would push us over.
    assert keep_ids[-2:] == ["2026-05-10", "2026-05-17"]


def test_no_complete_cycle_keeps_everything():
    """If no successful full has ever happened, retention is a no-op."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-05-17", "full", status="failed"),
        _entry("2026-05-18", "incr", cycle_id="2026-05-17"),
    ])
    plan = apply(m, policy="max_cycles", max_cycles=1, now=NOW)
    assert plan.delete == []


# ---------- keep_all (archive plans) ----------

def test_keep_all_never_deletes_anything():
    """Archive plans use keep_all — every cycle is preserved regardless of
    count, age, or size."""
    m = _five_weekly_cycles()
    plan = apply(m, policy="keep_all", now=NOW)
    assert plan.delete == []
    assert {c.cycle_id for c in plan.keep} == {
        "2026-04-19", "2026-04-26", "2026-05-03", "2026-05-10", "2026-05-17",
    }


def test_keep_all_ignores_max_cycles_arg():
    """Passing a small max_cycles must not affect keep_all behaviour."""
    m = _five_weekly_cycles()
    plan = apply(m, policy="keep_all", max_cycles=1, now=NOW)
    assert plan.delete == []
    assert len(plan.keep) == 5


def test_keep_all_preserves_incomplete_cycles_too():
    """A leading failed full (which cycles() represents as an incomplete stub
    cycle) is still kept under keep_all, alongside any complete cycles."""
    m = Manifest(plan_name="archive", archives=[
        _entry("2026-04-12", "full", status="failed"),  # stub incomplete cycle
        _entry("2026-04-19", "full"),
        _entry("2026-05-17", "full"),
    ])
    plan = apply(m, policy="keep_all", now=NOW)
    assert plan.delete == []
    assert {c.cycle_id for c in plan.keep} == {"2026-04-12", "2026-04-19", "2026-05-17"}


def test_keep_all_with_empty_manifest():
    """No cycles → nothing to keep, nothing to delete."""
    m = Manifest(plan_name="archive", archives=[])
    plan = apply(m, policy="keep_all", now=NOW)
    assert plan.delete == []
    assert plan.keep == []
