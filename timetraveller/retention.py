"""Cycle-aware retention.

Retention always operates on whole cycles, never individual archives. The
newest complete cycle is always kept, even if policy says otherwise, so there is
always at least one complete cycle on disk after retention runs (assuming one
ever existed).

Incomplete cycles (failed, missing or verify-flagged fulls) do not count toward
the limit, and they are kept while they are still inside the retention window:
they may be the only record of a recent run worth diagnosing or recovering. Once
an incomplete cycle falls wholly OUTSIDE the window, and a newer complete cycle
exists, it is deleted like any other expired cycle. Before 1.7.2 incomplete
cycles were kept forever, and a summer of flagged fulls (each one false-positive
or a single unreadable file) grew one plan to 17 weekly fulls / 12 TB while
max_cycles was 4.

"Outside the window" per policy:
  max_cycles    the policy is saturated (at least N complete cycles exist) and
                the cycle is older than the oldest complete cycle kept
  max_size_gb   the cap is binding (it dropped a complete cycle) and the cycle
                is older than the oldest complete cycle kept
  max_age_days  the cycle's newest archive started before the cutoff and the
                cycle is older than the newest complete cycle
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .manifest import Cycle, Manifest, cycles


@dataclass
class RetentionPlan:
    """What retention would do, with reasons for each decision."""
    delete: list[Cycle]
    keep: list[Cycle]
    reasons: dict[str, str]  # cycle_id -> human-readable reason


def apply(manifest: Manifest, *, policy: str, max_cycles: int = 4,
          max_age_days: int | None = None, max_size_gb: float | None = None,
          now: datetime | None = None) -> RetentionPlan:
    """Compute retention plan without touching disk."""
    now = now or datetime.now(timezone.utc)
    all_cycles = cycles(manifest)

    if policy == "keep_all":
        reasons = {c.cycle_id: "keep_all policy; archive plan" for c in all_cycles}
        return RetentionPlan(delete=[], keep=all_cycles, reasons=reasons)

    complete = [c for c in all_cycles if c.is_complete]
    incomplete = [c for c in all_cycles if not c.is_complete]

    # The newest complete cycle is the floor: never delete it. If there are no
    # complete cycles at all, keep everything.
    if not complete:
        reasons = {c.cycle_id: "no complete cycle exists; keeping everything" for c in all_cycles}
        return RetentionPlan(delete=[], keep=all_cycles, reasons=reasons)

    # Order complete cycles oldest-first (cycles() already returns oldest-first,
    # but be explicit).
    complete.sort(key=lambda c: c.cycle_id)
    newest_complete = complete[-1]

    keep: list[Cycle] = []
    delete: list[Cycle] = []
    reasons: dict[str, str] = {}
    # Where the retention window starts, for judging incomplete cycles. None =
    # the policy isn't binding yet, so nothing is outside the window.
    window_start: str | None = None
    window_desc = ""
    age_cutoff: datetime | None = None

    if policy == "max_cycles":
        # Keep the N newest complete cycles.
        n = max(int(max_cycles), 1)
        to_keep = complete[-n:]
        to_delete = complete[:-n] if len(complete) > n else []
        for c in to_keep:
            keep.append(c)
            reasons[c.cycle_id] = f"within max_cycles={n}"
        for c in to_delete:
            delete.append(c)
            reasons[c.cycle_id] = f"older than the {n} most recent complete cycles"
        if len(complete) >= n:
            window_start = to_keep[0].cycle_id
            window_desc = f"max_cycles={n}"

    elif policy == "max_age_days":
        if max_age_days is None:
            raise ValueError("max_age_days policy requires max_age_days value")
        cutoff = now - timedelta(days=int(max_age_days))
        for c in complete:
            assert c.full is not None
            try:
                started = datetime.fromisoformat(c.full.date_started)
            except ValueError:
                started = now  # malformed timestamps: treat as fresh
            if started < cutoff and c is not newest_complete:
                delete.append(c)
                reasons[c.cycle_id] = f"older than max_age_days={max_age_days}"
            else:
                keep.append(c)
                if c is newest_complete:
                    reasons[c.cycle_id] = "newest complete cycle (always kept)"
                else:
                    reasons[c.cycle_id] = f"within max_age_days={max_age_days}"
        age_cutoff = cutoff

    elif policy == "max_size_gb":
        if max_size_gb is None:
            raise ValueError("max_size_gb policy requires max_size_gb value")
        cap_bytes = int(float(max_size_gb) * 1024**3)
        # Greedy from newest: keep cycles until total exceeds cap, but always
        # keep at least the newest complete cycle.
        running = 0
        kept_ids: set[str] = set()
        for c in reversed(complete):
            if not kept_ids or running + c.total_size <= cap_bytes:
                keep.append(c)
                kept_ids.add(c.cycle_id)
                running += c.total_size
                reasons[c.cycle_id] = f"within max_size_gb={max_size_gb} (running {running/1024**3:.2f} GiB)"
            else:
                delete.append(c)
                reasons[c.cycle_id] = f"would exceed max_size_gb={max_size_gb}"
        if delete:
            window_start = min(kept_ids)
            window_desc = f"max_size_gb={max_size_gb}"

    else:
        raise ValueError(f"unknown retention policy {policy!r}")

    for c in incomplete:
        if _incomplete_expired(c, newest_complete, window_start, age_cutoff):
            delete.append(c)
            reasons[c.cycle_id] = ("incomplete cycle outside the retention window "
                                   f"({window_desc or f'max_age_days={max_age_days}'})")
        else:
            keep.append(c)
            reasons[c.cycle_id] = ("incomplete cycle inside the retention window; "
                                   "kept, not counted toward the limit")

    # Final safety check: never delete the newest complete cycle.
    if newest_complete in delete:
        delete.remove(newest_complete)
        keep.append(newest_complete)
        reasons[newest_complete.cycle_id] = "newest complete cycle (always kept)"

    return RetentionPlan(delete=delete, keep=keep, reasons=reasons)


def _last_started(c: Cycle) -> datetime | None:
    """Start time of the cycle's newest archive, or None if any is unparseable
    (unknown age: treat as fresh, never as expired)."""
    latest: datetime | None = None
    for a in c.archives:
        try:
            t = datetime.fromisoformat(a.date_started)
        except (TypeError, ValueError):
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if latest is None or t > latest:
            latest = t
    return latest


def _incomplete_expired(c: Cycle, newest_complete: Cycle,
                        window_start: str | None,
                        age_cutoff: datetime | None) -> bool:
    # Never drop an incomplete cycle that is newer than every complete one: it
    # may be the most recent data there is.
    if c.cycle_id >= newest_complete.cycle_id:
        return False
    if age_cutoff is not None:
        last = _last_started(c)
        return last is not None and last < age_cutoff
    # Cycles are contiguous in time, so a cycle that starts before the oldest
    # kept complete cycle also ends before it.
    return window_start is not None and c.cycle_id < window_start


def format_plan(plan: RetentionPlan) -> str:
    lines = []
    if plan.delete:
        lines.append("Cycles to delete:")
        for c in plan.delete:
            lines.append(f"  {c.cycle_id}  ({len(c.archives)} archives)  — {plan.reasons.get(c.cycle_id, '')}")
    else:
        lines.append("No cycles to delete.")
    lines.append("\nCycles to keep:")
    for c in sorted(plan.keep, key=lambda c: c.cycle_id):
        lines.append(f"  {c.cycle_id}  ({len(c.archives)} archives)  — {plan.reasons.get(c.cycle_id, '')}")
    return "\n".join(lines)
