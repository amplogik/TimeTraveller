"""Render plan schedules into cron entries and manage them inside a crontab.

A "managed block" for plan P looks like:

    # >>> TimeTraveller managed: plan=P  (do not edit between these markers)
    0 2 * * 0 /usr/local/bin/timetraveller-backup --plan P --kind full
    0 2 * * 1-6 /usr/local/bin/timetraveller-backup --plan P --kind incr
    # <<< TimeTraveller managed: plan=P

Installation works by reading the existing crontab, replacing the block for
this plan with a freshly-rendered one, and writing the result back. Lines
outside our markers are preserved verbatim — the user owns the rest of the
crontab.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import INCR_MODES, SCHEDULE_MODES, WEEKDAYS, PlanConfig  # noqa: F401

# Cron weekday numbers: 0=sun, 1=mon, ..., 6=sat. (POSIX cron also accepts
# 7=sun; we emit 0 for portability.)
_CRON_DOW = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

START_MARKER_FMT = "# >>> TimeTraveller managed: plan={plan}  (do not edit between these markers)"
END_MARKER_FMT = "# <<< TimeTraveller managed: plan={plan}"


@dataclass(frozen=True)
class CronEntry:
    minute: str
    hour: str
    day_of_month: str
    month: str
    day_of_week: str
    command: str

    def render(self) -> str:
        return f"{self.minute} {self.hour} {self.day_of_month} {self.month} {self.day_of_week} {self.command}"


def _parse_time(hhmm: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        raise ValueError(f"bad time {hhmm!r}; expected HH:MM")
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"bad time {hhmm!r}; hours 0-23 minutes 0-59")
    return hour, minute


def _dow_range(days: list[str]) -> str:
    """Compact a list of weekdays into cron syntax.

    [mon,tue,wed,thu,fri,sat] → '1-6'
    [mon,wed,fri] → '1,3,5'
    """
    if not days:
        raise ValueError("at least one day required")
    for d in days:
        if d not in WEEKDAYS:
            raise ValueError(f"bad weekday {d!r}")
    nums = sorted({_CRON_DOW[d] for d in days})
    # Detect contiguous run for nicer rendering.
    runs: list[tuple[int, int]] = []
    start = nums[0]
    prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    parts = [f"{a}-{b}" if a != b else str(a) for a, b in runs]
    return ",".join(parts)


def render_entries(plan: PlanConfig, binary_path: str) -> list[CronEntry]:
    """Render a plan's schedule into cron entries (without markers).

    Output entries depend on the schedule mode and the incr sub-mode:
      - weekly + (weekdays|except_full|disabled)
      - monthly + (weekdays|every_n_days|disabled)
    Disabled incr emits only the full entry.
    """
    fh, fm = _parse_time(plan.schedule.full.time)
    ih, im = _parse_time(plan.schedule.incr.time)

    full_cmd = f"{binary_path} --plan {plan.plan_name} --kind full"
    incr_cmd = f"{binary_path} --plan {plan.plan_name} --kind incr"

    entries: list[CronEntry] = []

    sch = plan.schedule
    if sch.mode == "weekly":
        entries.append(CronEntry(
            minute=str(fm), hour=str(fh),
            day_of_month="*", month="*",
            day_of_week=_dow_range(sch.full.days),
            command=full_cmd,
        ))
        if sch.incr.mode == "disabled":
            return entries
        if sch.incr.mode == "except_full":
            # All weekdays minus the ones we'd run a full on.
            full_set = set(sch.full.days)
            incr_days = [d for d in WEEKDAYS if d not in full_set]
            if not incr_days:
                # User picked every day as a full day; no incr to schedule.
                return entries
        else:  # weekdays
            incr_days = sch.incr.days
        entries.append(CronEntry(
            minute=str(im), hour=str(ih),
            day_of_month="*", month="*",
            day_of_week=_dow_range(incr_days),
            command=incr_cmd,
        ))
        return entries

    if sch.mode == "monthly":
        entries.append(CronEntry(
            minute=str(fm), hour=str(fh),
            day_of_month=str(sch.full.day_of_month),
            month="*", day_of_week="*",
            command=full_cmd,
        ))
        if sch.incr.mode == "disabled":
            return entries
        if sch.incr.mode == "every_n_days":
            n = int(sch.incr.every_n_days)
            entries.append(CronEntry(
                minute=str(im), hour=str(ih),
                day_of_month=f"*/{n}", month="*", day_of_week="*",
                command=incr_cmd,
            ))
            return entries
        # weekdays in monthly mode: still useful (e.g., monthly full + every
        # weekday incr).
        entries.append(CronEntry(
            minute=str(im), hour=str(ih),
            day_of_month="*", month="*",
            day_of_week=_dow_range(sch.incr.days),
            command=incr_cmd,
        ))
        return entries

    raise ValueError(f"unknown schedule mode {sch.mode!r}")


def render_block(plan: PlanConfig, binary_path: str) -> str:
    """Render a managed block (with start/end markers) for this plan."""
    entries = render_entries(plan, binary_path)
    lines = [
        START_MARKER_FMT.format(plan=plan.plan_name),
        *(e.render() for e in entries),
        END_MARKER_FMT.format(plan=plan.plan_name),
    ]
    return "\n".join(lines) + "\n"


# ---------- crontab merging ----------

_MARKER_RE = re.compile(
    r"^# >>> TimeTraveller managed: plan=(?P<plan>[A-Za-z0-9_-]+).*$"
)
_END_MARKER_RE = re.compile(
    r"^# <<< TimeTraveller managed: plan=(?P<plan>[A-Za-z0-9_-]+).*$"
)


def replace_block(crontab_text: str, plan_name: str, new_block: str) -> str:
    """Replace (or insert) the managed block for `plan_name` in a crontab.

    If no managed block for this plan exists, the new block is appended after
    a single blank line at the end of the crontab. Lines outside our markers
    are preserved.
    """
    lines = crontab_text.splitlines()
    out: list[str] = []
    in_block = False
    found = False
    for line in lines:
        if not in_block:
            sm = _MARKER_RE.match(line)
            if sm and sm.group("plan") == plan_name:
                in_block = True
                continue
            out.append(line)
        else:
            em = _END_MARKER_RE.match(line)
            if em and em.group("plan") == plan_name:
                in_block = False
                # Insert the new block in place of the old one.
                out.extend(new_block.rstrip("\n").splitlines())
                found = True
            # Otherwise we're inside the old block: drop the line.

    if not found:
        # Append. Make sure there's a separating blank line.
        if out and out[-1].strip() != "":
            out.append("")
        out.extend(new_block.rstrip("\n").splitlines())

    return "\n".join(out).rstrip("\n") + "\n"


def remove_block(crontab_text: str, plan_name: str) -> str:
    """Strip the managed block for plan_name. No-op if absent."""
    lines = crontab_text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            sm = _MARKER_RE.match(line)
            if sm and sm.group("plan") == plan_name:
                in_block = True
                continue
            out.append(line)
        else:
            em = _END_MARKER_RE.match(line)
            if em and em.group("plan") == plan_name:
                in_block = False
                # drop the end marker too
    return "\n".join(out).rstrip("\n") + ("\n" if out else "")


def find_block(crontab_text: str, plan_name: str) -> str | None:
    """Return the existing managed block (including markers) or None."""
    lines = crontab_text.splitlines()
    in_block = False
    captured: list[str] = []
    for line in lines:
        if not in_block:
            sm = _MARKER_RE.match(line)
            if sm and sm.group("plan") == plan_name:
                in_block = True
                captured.append(line)
        else:
            captured.append(line)
            em = _END_MARKER_RE.match(line)
            if em and em.group("plan") == plan_name:
                return "\n".join(captured) + "\n"
    return None


# ---------- suspend / resume ----------

# Prefix prepended to a cron entry to suspend it. We use "# " (hash + space)
# so the result is a normal cron comment that the daemon will ignore.
_SUSPEND_PREFIX_RE = re.compile(r"^#\s*")


def _is_entry_line(line: str) -> bool:
    """True if `line` is an uncommented cron entry that matches our allowlist."""
    return bool(ENTRY_VALIDATION_RE.match(line))


def _stripped_entry(line: str) -> str | None:
    """If `line` looks like a commented-out entry (matches our allowlist after
    stripping a leading `#\\s*`), return the stripped version. Otherwise None.
    """
    m = _SUSPEND_PREFIX_RE.match(line)
    if not m:
        return None
    candidate = line[m.end():]
    if ENTRY_VALIDATION_RE.match(candidate):
        return candidate
    return None


def suspend_block(crontab_text: str, plan_name: str) -> str:
    """Prepend `# ` to each cron entry line in the managed block."""
    return _transform_block(crontab_text, plan_name, _suspend_line)


def resume_block(crontab_text: str, plan_name: str) -> str:
    """Strip the `# ` prefix from suspended entries in the managed block.

    Lines that don't match our entry allowlist after stripping are left
    alone, which prevents Resume from ever activating a non-managed comment.
    """
    return _transform_block(crontab_text, plan_name, _resume_line)


def _suspend_line(line: str) -> str:
    if _is_entry_line(line):
        return "# " + line
    return line


def _resume_line(line: str) -> str:
    stripped = _stripped_entry(line)
    if stripped is not None:
        return stripped
    return line


def _transform_block(crontab_text: str, plan_name: str, fn) -> str:
    lines = crontab_text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            sm = _MARKER_RE.match(line)
            if sm and sm.group("plan") == plan_name:
                in_block = True
                out.append(line)
                continue
            out.append(line)
        else:
            em = _END_MARKER_RE.match(line)
            if em and em.group("plan") == plan_name:
                in_block = False
                out.append(line)
                continue
            out.append(fn(line))
    return "\n".join(out).rstrip("\n") + ("\n" if out else "")


def is_block_suspended(crontab_text: str, plan_name: str) -> bool | None:
    """Returns:
      None  — no managed block found
      True  — every entry-shaped line in the block is commented (fully suspended)
      False — at least one entry-shaped line is active (active or partial)
    """
    block = find_block(crontab_text, plan_name)
    if block is None:
        return None
    has_active = False
    has_suspended = False
    for line in block.splitlines():
        if _MARKER_RE.match(line) or _END_MARKER_RE.match(line):
            continue
        if _is_entry_line(line):
            has_active = True
        elif _stripped_entry(line) is not None:
            has_suspended = True
    if has_active:
        return False
    if has_suspended:
        return True
    return None  # no entries at all — block is empty/broken


# ---------- validation ----------

# Each non-comment, non-blank line in our managed block must match this. The
# pkexec helper validates against the same regex before writing root's
# crontab — that's where it really matters; here it's for early feedback.
ENTRY_VALIDATION_RE = re.compile(
    r"^"
    r"(?P<min>[0-9*/,\-]+)\s+"
    r"(?P<hr>[0-9*/,\-]+)\s+"
    r"(?P<dom>[0-9*/,\-]+)\s+"
    r"(?P<mon>[0-9*/,\-]+)\s+"
    r"(?P<dow>[0-9*/,\-]+)\s+"
    # Command: an absolute path to timetraveller-backup with fixed args.
    r"(?P<path>/[A-Za-z0-9_./\-]+/timetraveller-backup)\s+"
    r"--plan\s+(?P<plan>[A-Za-z0-9_-]+)\s+"
    r"--kind\s+(?P<kind>full|incr|auto)\s*"
    r"$"
)


def validate_block(block_text: str, plan_name: str) -> list[str]:
    """Return a list of validation errors; empty list = OK."""
    errors: list[str] = []
    lines = block_text.splitlines()
    if not lines or not _MARKER_RE.match(lines[0]):
        errors.append("missing start marker")
    elif _MARKER_RE.match(lines[0]).group("plan") != plan_name:  # type: ignore[union-attr]
        errors.append("start marker plan name mismatch")
    if not lines or not _END_MARKER_RE.match(lines[-1] if lines else ""):
        errors.append("missing end marker")
    for i, line in enumerate(lines[1:-1], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ENTRY_VALIDATION_RE.match(line)
        if not m:
            errors.append(f"line {i} does not match allowed pattern: {line!r}")
            continue
        if m.group("plan") != plan_name:
            errors.append(f"line {i} plan name mismatch: got {m.group('plan')!r}, expected {plan_name!r}")
    return errors
