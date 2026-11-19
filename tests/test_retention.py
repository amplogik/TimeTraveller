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


# --- Incomplete cycles outside the retention window (1.7.2) -----------------

def _bast_like(n_complete: int) -> Manifest:
    """An old complete cycle, then one incomplete cycle holding several failed
    fulls and their incrementals (the 2026-09-27 shape on bast), then
    ``n_complete`` complete weekly cycles."""
    ar = [_entry("2026-03-01", "full")]
    ar.append(_entry("2026-03-08", "full", status="corrupt"))
    for d in ("2026-03-09", "2026-03-10"):
        ar.append(_entry(d, "incr", cycle_id="2026-03-08"))
    for d in ("2026-03-15", "2026-03-22"):
        ar.append(_entry(d, "full", status="failed", cycle_id="2026-03-08"))
    start = datetime(2026, 3, 29, tzinfo=timezone.utc)
    for i in range(n_complete):
        ar.append(_entry((start + timedelta(weeks=i)).date().isoformat(), "full"))
    return Manifest(plan_name="home", archives=ar)


def _ids(cs):
    return sorted(c.cycle_id for c in cs)


def test_incomplete_kept_while_policy_not_saturated():
    """Fewer complete cycles than max_cycles: the window reaches back past
    everything, so nothing is outside it -- incomplete or not."""
    plan = apply(_bast_like(2), policy="max_cycles", max_cycles=8, now=NOW)
    assert plan.delete == []
    assert "inside the retention window" in plan.reasons["2026-03-08"]


def test_incomplete_deleted_once_outside_max_cycles_window():
    plan = apply(_bast_like(3), policy="max_cycles", max_cycles=3, now=NOW)
    assert _ids(plan.delete) == ["2026-03-01", "2026-03-08"]
    assert "outside the retention window" in plan.reasons["2026-03-08"]
    # Deleting the incomplete cycle takes every one of its sets with it.
    gone = {a.filename for c in plan.delete for a in c.archives}
    assert "2026-03-22_full.pax.zst" in gone and "2026-03-10_incr.pax.zst" in gone


def test_incomplete_inside_window_is_kept_and_not_counted():
    """An incomplete cycle sandwiched between kept complete cycles stays, and
    doesn't use up one of the N slots."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-04-19", "full"),
        _entry("2026-04-26", "full"),
        _entry("2026-05-03", "full", status="corrupt"),
        _entry("2026-05-10", "full"),
        _entry("2026-05-17", "full"),
    ])
    plan = apply(m, policy="max_cycles", max_cycles=3, now=NOW)
    assert _ids(plan.keep) == ["2026-04-26", "2026-05-03", "2026-05-10", "2026-05-17"]
    assert _ids(plan.delete) == ["2026-04-19"]


def test_incomplete_newer_than_every_complete_is_never_deleted():
    m = Manifest(plan_name="home", archives=[
        _entry("2026-05-03", "full"),
        _entry("2026-05-10", "full", status="failed"),
        _entry("2026-05-17", "full", status="corrupt"),
    ])
    for kw in ({"policy": "max_cycles", "max_cycles": 1},
               {"policy": "max_age_days", "max_age_days": 1},
               {"policy": "max_size_gb", "max_size_gb": 0.0000001}):
        plan = apply(m, now=NOW + timedelta(days=365), **kw)
        assert "2026-05-17" in _ids(plan.keep), kw
        assert "2026-05-03" in _ids(plan.keep), kw   # newest complete floor


def test_incomplete_max_age_judged_by_newest_archive_not_cycle_start():
    """The cycle STARTED before the cutoff but its last incremental is after it:
    still inside the window."""
    m = Manifest(plan_name="home", archives=[
        _entry("2026-04-26", "full", status="corrupt"),
        _entry("2026-05-08", "incr", cycle_id="2026-04-26"),
        _entry("2026-05-10", "full"),
        _entry("2026-05-17", "full"),
    ])
    plan = apply(m, policy="max_age_days", max_age_days=14, now=NOW)  # cutoff 05-06
    assert "2026-04-26" in _ids(plan.keep)
    plan = apply(m, policy="max_age_days", max_age_days=10, now=NOW)  # cutoff 05-10
    assert "2026-04-26" in _ids(plan.delete)


def test_incomplete_max_size_only_when_cap_is_binding():
    m = _bast_like(3)
    roomy = apply(m, policy="max_size_gb", max_size_gb=1000, now=NOW)
    assert roomy.delete == []
    tight = apply(m, policy="max_size_gb", max_size_gb=200 / 1024**3, now=NOW)
    assert "2026-03-08" in _ids(tight.delete)
    assert "2026-03-01" in _ids(tight.delete)


def test_keep_all_still_keeps_old_incomplete():
    plan = apply(_bast_like(5), policy="keep_all", max_cycles=1, now=NOW)
    assert plan.delete == []
