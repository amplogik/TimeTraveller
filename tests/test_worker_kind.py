"""Tests for worker kind-resolution and same-day defer logic."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import _is_full_day, _is_incr_day, _resolve_kind


def _weekly(full_days=("sun",), incr_mode="weekdays",
            incr_days=("mon", "tue", "wed", "thu", "fri", "sat")):
    return PlanConfig(
        plan_name="home",
        sources=["/home"],
        schedule=Schedule(
            mode="weekly",
            full=FullSchedule(days=list(full_days)),
            incr=IncrSchedule(mode=incr_mode, days=list(incr_days)),
        ),
        retention=Retention(),
    )


def _monthly(dom=1, incr_mode="every_n_days", n=3,
             incr_days=("mon", "wed", "fri")):
    return PlanConfig(
        plan_name="system",
        sources=["/"],
        schedule=Schedule(
            mode="monthly",
            full=FullSchedule(day_of_month=dom),
            incr=IncrSchedule(mode=incr_mode, every_n_days=n,
                              days=list(incr_days)),
        ),
        retention=Retention(),
    )


def _at(year, month, day):
    return datetime(year, month, day, 2, 0, tzinfo=timezone.utc)


def test_is_full_day_weekly():
    p = _weekly(full_days=["sun", "wed"])
    assert _is_full_day(p, _at(2026, 5, 17))  # Sunday
    assert _is_full_day(p, _at(2026, 5, 20))  # Wednesday
    assert not _is_full_day(p, _at(2026, 5, 18))  # Monday


def test_is_incr_day_weekly_except_full_skips_full_days():
    p = _weekly(full_days=["sun"], incr_mode="except_full")
    assert _is_incr_day(p, _at(2026, 5, 18))      # Monday — yes
    assert not _is_incr_day(p, _at(2026, 5, 17))  # Sunday — full day, skipped


def test_is_full_day_monthly():
    p = _monthly(dom=15)
    assert _is_full_day(p, _at(2026, 5, 15))
    assert not _is_full_day(p, _at(2026, 5, 14))


def test_is_incr_day_monthly_every_n():
    p = _monthly(dom=1, n=3)
    # */3 in cron DOM means 1, 4, 7, ...
    assert _is_incr_day(p, _at(2026, 5, 1))
    assert _is_incr_day(p, _at(2026, 5, 4))
    assert _is_incr_day(p, _at(2026, 5, 7))
    assert not _is_incr_day(p, _at(2026, 5, 2))
    assert not _is_incr_day(p, _at(2026, 5, 3))


def test_resolve_kind_prefers_full_when_both_match():
    """On a day where both schedules fire, --kind auto picks full."""
    p = _monthly(dom=1, n=3)
    # Day 1 is both a full day and an incr day (1 mod 3 == 0).
    assert _resolve_kind(None, p, _at(2026, 5, 1)) == "full"


def test_resolve_kind_explicit_kind_passes_through():
    p = _weekly()
    # Even when today wouldn't fire either schedule, explicit --kind is honored.
    assert _resolve_kind("full", p, _at(2026, 5, 17)) == "full"
    assert _resolve_kind("incr", p, _at(2026, 5, 17)) == "incr"


def test_resolve_kind_auto_errors_when_nothing_scheduled():
    p = _weekly(full_days=["sun"], incr_mode="disabled")
    # Monday — full only fires Sunday; incr disabled.
    with pytest.raises(SystemExit):
        _resolve_kind(None, p, _at(2026, 5, 18))
