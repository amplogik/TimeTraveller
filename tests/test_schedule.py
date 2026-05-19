"""Schedule rendering and crontab merging tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.schedule import (
    find_block, remove_block, render_block, render_entries, replace_block,
    validate_block,
)

BIN = "/usr/local/bin/timetraveller-backup"


def _plan(name: str = "home", full_day: str = "sun", full_time: str = "02:00",
          incr_days: list[str] | None = None, incr_time: str = "02:00") -> PlanConfig:
    if incr_days is None:
        incr_days = ["mon", "tue", "wed", "thu", "fri", "sat"]
    return PlanConfig(
        plan_name=name,
        sources=["/home"],
        schedule=Schedule(
            mode="weekly",
            full=FullSchedule(days=[full_day], time=full_time),
            incr=IncrSchedule(mode="weekdays", days=incr_days, time=incr_time),
        ),
        retention=Retention(),
    )


def test_render_entries_default_schedule():
    entries = render_entries(_plan("home"), BIN)
    assert len(entries) == 2
    full, incr = entries
    assert full.render() == f"0 2 * * 0 {BIN} --plan home --kind full"
    assert incr.render() == f"0 2 * * 1-6 {BIN} --plan home --kind incr"


def test_render_entries_discontiguous_days():
    entries = render_entries(
        _plan("home", incr_days=["mon", "wed", "fri"]), BIN,
    )
    assert entries[1].day_of_week == "1,3,5"


def test_render_entries_off_hour_time():
    entries = render_entries(
        _plan("system", full_time="03:15", incr_time="04:45"), BIN,
    )
    assert entries[0].render().startswith("15 3 * * 0 ")
    assert entries[1].render().startswith("45 4 * * 1-6 ")


def test_render_block_has_markers():
    block = render_block(_plan("home"), BIN)
    assert ">>> TimeTraveller managed: plan=home" in block
    assert "<<< TimeTraveller managed: plan=home" in block
    # Block ends in newline so it concatenates cleanly with surrounding text.
    assert block.endswith("\n")


def test_replace_block_inserts_when_absent():
    existing = "# user's other cron\n0 5 * * * /bin/foo\n"
    block = render_block(_plan("home"), BIN)
    out = replace_block(existing, "home", block)
    assert "/bin/foo" in out
    assert ">>> TimeTraveller managed: plan=home" in out


def test_replace_block_replaces_in_place():
    existing = (
        "# user line\n"
        "0 5 * * * /bin/foo\n"
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "0 9 * * 0 /old/path/timetraveller-backup --plan home --kind full\n"
        "0 9 * * 1-6 /old/path/timetraveller-backup --plan home --kind incr\n"
        "# <<< TimeTraveller managed: plan=home\n"
        "30 23 * * * /bin/bar\n"
    )
    new_block = render_block(_plan("home"), BIN)
    out = replace_block(existing, "home", new_block)
    # User lines preserved.
    assert "/bin/foo" in out
    assert "/bin/bar" in out
    # Old paths gone.
    assert "/old/path/timetraveller-backup" not in out
    # New paths present.
    assert "/usr/local/bin/timetraveller-backup --plan home --kind full" in out
    # No duplicate markers.
    assert out.count(">>> TimeTraveller managed: plan=home") == 1


def test_replace_block_isolates_per_plan():
    """Replacing the home block must not touch the system block."""
    home_old = render_block(_plan("home", full_time="01:00"), BIN)
    system_old = render_block(_plan("system", full_time="03:00"), BIN)
    existing = home_old + "\n" + system_old
    home_new = render_block(_plan("home", full_time="02:00"), BIN)
    out = replace_block(existing, "home", home_new)
    # System block intact.
    assert "0 3 * * 0 /usr/local/bin/timetraveller-backup --plan system --kind full" in out
    # Home block updated.
    assert "0 2 * * 0 /usr/local/bin/timetraveller-backup --plan home --kind full" in out
    assert "0 1 * * 0" not in out  # old home time gone


def test_remove_block_strips_managed_block_only():
    existing = (
        "0 5 * * * /bin/foo\n"
        + render_block(_plan("home"), BIN)
        + "0 6 * * * /bin/bar\n"
    )
    out = remove_block(existing, "home")
    assert "TimeTraveller managed" not in out
    assert "/bin/foo" in out
    assert "/bin/bar" in out


def test_find_block_roundtrip():
    block = render_block(_plan("home"), BIN)
    text = "x\n" + block + "y\n"
    got = find_block(text, "home")
    assert got is not None
    assert ">>> TimeTraveller managed: plan=home" in got
    assert "<<< TimeTraveller managed: plan=home" in got


def test_find_block_absent():
    assert find_block("# just user cron\n0 5 * * * /bin/foo\n", "home") is None


def test_validate_block_accepts_well_formed():
    block = render_block(_plan("home"), BIN)
    assert validate_block(block, "home") == []


def test_validate_block_rejects_shell_meta():
    bad = (
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "0 2 * * 0 /usr/local/bin/timetraveller-backup --plan home --kind full; rm -rf /\n"
        "# <<< TimeTraveller managed: plan=home\n"
    )
    errors = validate_block(bad, "home")
    assert errors  # at least one error


def test_validate_block_rejects_other_command():
    bad = (
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "0 2 * * 0 /bin/sh -c 'rm -rf /'\n"
        "# <<< TimeTraveller managed: plan=home\n"
    )
    errors = validate_block(bad, "home")
    assert errors


def test_validate_block_rejects_plan_mismatch():
    bad = (
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "0 2 * * 0 /usr/local/bin/timetraveller-backup --plan system --kind full\n"
        "# <<< TimeTraveller managed: plan=home\n"
    )
    errors = validate_block(bad, "home")
    assert any("plan name mismatch" in e for e in errors)


def test_render_invalid_time():
    with pytest.raises(ValueError):
        render_entries(_plan("home", full_time="25:99"), BIN)


# ---------- weekly with multi-day full ----------

def test_render_weekly_multi_day_full():
    plan = _plan("home")
    plan.schedule.full.days = ["wed", "sun"]
    plan.schedule.incr.days = ["mon", "tue", "thu", "fri", "sat"]
    entries = render_entries(plan, BIN)
    full, incr = entries
    # mon=1, tue=2, wed=3, thu=4, fri=5, sat=6, sun=0
    # full days wed,sun -> sorted: 0,3 -> "0,3"
    assert full.day_of_week == "0,3"
    # incr days mon,tue,thu,fri,sat -> 1,2,4-6
    assert incr.day_of_week == "1-2,4-6"


def test_render_weekly_except_full_computes_diff_at_render():
    plan = _plan("home")
    plan.schedule.full.days = ["sun"]
    plan.schedule.incr.mode = "except_full"
    plan.schedule.incr.days = []  # ignored when mode=except_full
    entries = render_entries(plan, BIN)
    assert len(entries) == 2
    # mon..sat = 1-6
    assert entries[1].day_of_week == "1-6"

    # Change full to wed+sun; except_full should now produce mon,tue,thu,fri,sat
    plan.schedule.full.days = ["wed", "sun"]
    entries = render_entries(plan, BIN)
    assert entries[1].day_of_week == "1-2,4-6"


def test_render_weekly_disabled_incr_emits_only_full():
    plan = _plan("home")
    plan.schedule.incr.mode = "disabled"
    entries = render_entries(plan, BIN)
    assert len(entries) == 1
    assert "--kind full" in entries[0].command


def test_render_weekly_except_full_when_all_days_are_full():
    """If every weekday is a full day, except_full produces no incr entry."""
    plan = _plan("home")
    plan.schedule.full.days = list(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    plan.schedule.incr.mode = "except_full"
    entries = render_entries(plan, BIN)
    assert len(entries) == 1  # full only


# ---------- monthly ----------

def _monthly_plan(name: str = "system", dom: int = 1,
                  incr_mode: str = "every_n_days",
                  every_n: int = 3,
                  incr_days: list[str] | None = None) -> PlanConfig:
    return PlanConfig(
        plan_name=name,
        sources=["/"],
        schedule=Schedule(
            mode="monthly",
            full=FullSchedule(day_of_month=dom, days=["sun"], time="02:00"),
            incr=IncrSchedule(
                mode=incr_mode, days=incr_days or ["mon", "wed", "fri"],
                every_n_days=every_n, time="02:00",
            ),
        ),
        retention=Retention(),
    )


def test_render_monthly_dom_full_and_every_n_incr():
    plan = _monthly_plan(dom=1, every_n=3)
    entries = render_entries(plan, BIN)
    assert len(entries) == 2
    full, incr = entries
    assert full.day_of_month == "1"
    assert full.day_of_week == "*"
    assert incr.day_of_month == "*/3"
    assert incr.day_of_week == "*"


def test_render_monthly_with_weekday_incr():
    plan = _monthly_plan(dom=15, incr_mode="weekdays", incr_days=["wed", "sat"])
    entries = render_entries(plan, BIN)
    full, incr = entries
    assert full.day_of_month == "15"
    # Wed=3, Sat=6 → "3,6"
    assert incr.day_of_week == "3,6"
    assert incr.day_of_month == "*"


def test_render_monthly_disabled_incr():
    plan = _monthly_plan(dom=1, incr_mode="disabled")
    entries = render_entries(plan, BIN)
    assert len(entries) == 1


# ---------- backward compatibility ----------

def test_load_old_style_full_day_singular():
    """Configs written before the schema change use full.day (singular)."""
    from timetraveller.config import _from_dict
    raw = {
        "plan_name": "home",
        "sources": ["/home"],
        "destination": "/mnt/Backups/timetraveller",
        "schedule": {
            "full": {"day": "sun", "time": "02:00"},
            "incr": {"days": ["mon", "tue", "wed", "thu", "fri", "sat"], "time": "02:00"},
        },
        "retention": {"policy": "max_cycles", "max_cycles": 4},
    }
    plan = _from_dict(raw)
    assert plan.schedule.mode == "weekly"
    assert plan.schedule.full.days == ["sun"]
    assert plan.schedule.incr.mode == "weekdays"


# ---------- validation ----------

def test_validation_rejects_overlap_in_weekly():
    plan = _plan("home")
    plan.schedule.full.days = ["sun", "wed"]
    plan.schedule.incr.mode = "weekdays"
    plan.schedule.incr.days = ["wed", "fri"]  # wed overlaps
    with pytest.raises(ValueError, match="overlap"):
        plan.validate()


def test_validation_rejects_bad_dom():
    plan = _monthly_plan(dom=31)  # > 28
    with pytest.raises(ValueError, match="1..28"):
        plan.validate()


def test_validation_rejects_bad_incr_mode_for_weekly():
    plan = _plan("home")
    plan.schedule.incr.mode = "every_n_days"  # not allowed in weekly
    with pytest.raises(ValueError):
        plan.validate()


def test_validation_rejects_bad_incr_mode_for_monthly():
    plan = _monthly_plan(dom=1, incr_mode="except_full")  # not allowed in monthly
    with pytest.raises(ValueError):
        plan.validate()


# ---------- suspend / resume ----------

from timetraveller.schedule import (  # noqa: E402
    is_block_suspended, resume_block, suspend_block,
)


def _crontab_with_active_home() -> str:
    return (
        "# user line\n"
        "0 5 * * * /bin/foo\n"
        + render_block(_plan("home"), BIN)
        + "30 23 * * * /bin/bar\n"
    )


def test_suspend_block_comments_entries():
    txt = _crontab_with_active_home()
    suspended = suspend_block(txt, "home")
    # Markers preserved.
    assert ">>> TimeTraveller managed: plan=home" in suspended
    assert "<<< TimeTraveller managed: plan=home" in suspended
    # Entries commented out.
    assert "# 0 2 * * 0 " + BIN + " --plan home --kind full" in suspended
    assert "# 0 2 * * 1-6 " + BIN + " --plan home --kind incr" in suspended
    # User's other cron lines untouched.
    assert "0 5 * * * /bin/foo" in suspended
    assert "30 23 * * * /bin/bar" in suspended


def test_resume_block_restores_entries():
    txt = _crontab_with_active_home()
    suspended = suspend_block(txt, "home")
    resumed = resume_block(suspended, "home")
    # Should be equivalent to the original (modulo whitespace).
    assert resumed.strip() == txt.strip()


def test_resume_idempotent_on_active_block():
    txt = _crontab_with_active_home()
    out = resume_block(txt, "home")
    assert out == txt


def test_suspend_idempotent_on_suspended_block():
    txt = _crontab_with_active_home()
    once = suspend_block(txt, "home")
    twice = suspend_block(once, "home")
    assert once == twice  # double-suspend doesn't double-prefix


def test_is_block_suspended_states():
    txt = _crontab_with_active_home()
    assert is_block_suspended(txt, "home") is False  # active
    suspended = suspend_block(txt, "home")
    assert is_block_suspended(suspended, "home") is True
    assert is_block_suspended("# random other comment\n", "home") is None


def test_is_block_suspended_partial_reports_active():
    """If at least one entry is uncommented, the block is considered active
    (so the Suspend button is the right one to show)."""
    txt = (
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "0 2 * * 0 " + BIN + " --plan home --kind full\n"
        "# 0 2 * * 1-6 " + BIN + " --plan home --kind incr\n"
        "# <<< TimeTraveller managed: plan=home\n"
    )
    assert is_block_suspended(txt, "home") is False


def test_resume_does_not_activate_malicious_comment():
    """Security: a comment that DOESN'T match the entry allowlist must not be
    activated by Resume, even if it looks cron-shaped."""
    txt = (
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "# 0 * * * * /bin/sh -c 'rm -rf /'\n"   # not in the allowlist
        "# 0 2 * * 0 " + BIN + " --plan home --kind full\n"   # legit suspended
        "# <<< TimeTraveller managed: plan=home\n"
    )
    resumed = resume_block(txt, "home")
    # The malicious comment must remain commented.
    assert "# 0 * * * * /bin/sh -c 'rm -rf /'" in resumed
    # The legit entry should be uncommented.
    assert "0 2 * * 0 " + BIN + " --plan home --kind full" in resumed
    # And not have the comment prefix.
    assert "\n0 2 * * 0 " + BIN in resumed


def test_suspend_resume_leaves_inner_comments_alone():
    """Human-written comments inside the block survive suspend+resume."""
    txt = (
        "# >>> TimeTraveller managed: plan=home  (do not edit between these markers)\n"
        "# my note: this used to fail on Tuesdays\n"
        "0 2 * * 0 " + BIN + " --plan home --kind full\n"
        "# <<< TimeTraveller managed: plan=home\n"
    )
    out = resume_block(suspend_block(txt, "home"), "home")
    assert "# my note: this used to fail on Tuesdays" in out
