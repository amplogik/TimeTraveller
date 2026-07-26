"""A corrupt-but-readable full must open its own cycle.

Found in the field 2026-07-26 on bast's system plan: 2026-07-19_full had ONE
corrupt frame affecting ONE file, and as a result:

  * it did not appear in the cycle list at all;
  * its shards were reparented onto Cycle 2026-07-12, which still reported
    "complete" while concealing them;
  * every incremental from 07-20..07-25 was shown under the 07-12 cycle even
    though they were computed against the 07-19 full; and
  * worst -- because `Cycle.archives` includes `incr_sets`, pruning the 07-12
    cycle would have DELETED the later, perfectly usable 07-19 full with it.

The cause was `cycles()` gating on `is_complete` (which forbids corrupt frames)
rather than on "is this full readable at all". `failed`/`empty` fulls must still
not open a cycle -- that part was always right.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as m
from timetraveller import retention


def _e(fn, kind, cycle_id, group, *, status="ok", corrupt_frames=0,
       shard_index=1, shard_count=1, started=None):
    return m.ArchiveEntry(
        filename=fn, kind=kind, cycle_id=cycle_id,
        date_started=started or f"{cycle_id}T02:00:00",
        date_finished=f"{cycle_id}T03:00:00", size_bytes=100, status=status,
        hostname="h", plan_name="p", shard_group=group,
        shard_index=shard_index, shard_count=shard_count,
        corrupt_frames=corrupt_frames)


def _bast_shaped_manifest():
    """The real shape that exposed this: clean full, incrementals, a corrupt
    full, then more incrementals."""
    ar = [
        _e("2026-07-12_full.s1of2.pax.zst", "full", "2026-07-12",
           "2026-07-12_full", shard_index=1, shard_count=2),
        _e("2026-07-12_full.s2of2.pax.zst", "full", "2026-07-12",
           "2026-07-12_full", shard_index=2, shard_count=2),
        _e("2026-07-13_incr.pax.zst", "incr", "2026-07-12", "2026-07-13_incr"),
        # One corrupt shard out of two -- one bad frame, one affected file.
        _e("2026-07-19_full.s1of2.pax.zst", "full", "2026-07-19",
           "2026-07-19_full", shard_index=1, shard_count=2),
        _e("2026-07-19_full.s2of2.pax.zst", "full", "2026-07-19",
           "2026-07-19_full", status="corrupt", corrupt_frames=1,
           shard_index=2, shard_count=2),
        _e("2026-07-20_incr.pax.zst", "incr", "2026-07-19", "2026-07-20_incr"),
    ]
    return m.Manifest(plan_name="p", archives=ar)


def test_corrupt_full_opens_its_own_cycle():
    cs = m.cycles(_bast_shaped_manifest())
    assert [c.cycle_id for c in cs] == ["2026-07-12", "2026-07-19"]


def test_corrupt_cycle_is_flagged_incomplete_not_hidden():
    """It must be visible AND honestly labelled -- the old behaviour concealed it
    inside a cycle that still claimed to be complete."""
    cs = m.cycles(_bast_shaped_manifest())
    by_id = {c.cycle_id: c for c in cs}
    assert by_id["2026-07-12"].is_complete is True
    assert by_id["2026-07-19"].is_complete is False
    assert by_id["2026-07-19"].full_set is not None
    assert by_id["2026-07-19"].full_set.status == "corrupt"


def test_incrementals_attach_to_the_full_they_were_based_on():
    """The restore-chain correctness point."""
    cs = m.cycles(_bast_shaped_manifest())
    by_id = {c.cycle_id: c for c in cs}
    names = lambda c: [e.filename for e in c.incrementals]
    assert names(by_id["2026-07-12"]) == ["2026-07-13_incr.pax.zst"]
    assert names(by_id["2026-07-19"]) == ["2026-07-20_incr.pax.zst"]


def test_pruning_the_previous_cycle_no_longer_takes_the_later_full():
    """The data-loss path. Cycle.archives includes incr_sets, so while the
    corrupt full was misfiled onto 2026-07-12, deleting that cycle would have
    deleted the 07-19 full's shards too."""
    cs = m.cycles(_bast_shaped_manifest())
    by_id = {c.cycle_id: c for c in cs}
    doomed = {e.filename for e in by_id["2026-07-12"].archives}
    assert not any("2026-07-19_full" in f for f in doomed)


def test_retention_keeps_the_corrupt_cycle_and_does_not_count_it():
    """It becomes an incomplete cycle, which retention always keeps and excludes
    from the limit -- so the change is protective, not destructive."""
    plan = retention.apply(_bast_shaped_manifest(), policy="max_cycles",
                           max_cycles=1)
    kept = {c.cycle_id for c in plan.keep}
    deleted = {c.cycle_id for c in plan.delete}
    assert "2026-07-19" in kept
    assert "2026-07-19" not in deleted
    assert plan.reasons["2026-07-19"] == "incomplete cycle; not subject to retention"


def test_failed_full_still_does_not_open_a_cycle():
    """The original rule, unchanged: a failed full is not trustworthy, so the
    restore chain stays anchored on the last good full."""
    ar = [
        _e("2026-07-12_full.pax.zst", "full", "2026-07-12", "2026-07-12_full"),
        _e("2026-07-19_full.pax.zst", "full", "2026-07-19", "2026-07-19_full",
           status="failed"),
        _e("2026-07-20_incr.pax.zst", "incr", "2026-07-19", "2026-07-20_incr"),
    ]
    cs = m.cycles(m.Manifest(plan_name="p", archives=ar))
    assert [c.cycle_id for c in cs] == ["2026-07-12"]
    assert "2026-07-19_full.pax.zst" in [e.filename for e in cs[0].incrementals]


def test_partly_failed_full_does_not_open_a_cycle():
    """A full missing a shard is not restorable even if the other shard is fine
    -- data is genuinely absent, unlike corruption within a present shard."""
    ar = [
        _e("2026-07-12_full.pax.zst", "full", "2026-07-12", "2026-07-12_full"),
        _e("2026-07-19_full.s1of2.pax.zst", "full", "2026-07-19",
           "2026-07-19_full", shard_index=1, shard_count=2),
        _e("2026-07-19_full.s2of2.pax.zst", "full", "2026-07-19",
           "2026-07-19_full", status="failed", shard_index=2, shard_count=2),
    ]
    cs = m.cycles(m.Manifest(plan_name="p", archives=ar))
    assert [c.cycle_id for c in cs] == ["2026-07-12"]


def test_is_restorable_matrix():
    def s(*statuses):
        return m.ShardSet(group_id="g", members=[
            _e(f"a.s{i+1}of{len(statuses)}.pax.zst", "full", "2026-07-19",
               "a", status=st, shard_index=i + 1, shard_count=len(statuses))
            for i, st in enumerate(statuses)])

    assert s("ok").is_restorable is True
    assert s("ok", "ok-with-warnings").is_restorable is True
    assert s("ok", "corrupt").is_restorable is True          # the fix
    assert s("corrupt", "corrupt").is_restorable is True
    assert s("ok", "failed").is_restorable is False
    assert s("in-progress").is_restorable is False
    assert s("empty").is_restorable is False
    # is_complete stays strict -- corruption still means "not a clean base".
    assert s("ok", "corrupt").is_complete is False
    assert s("ok", "ok-with-warnings").is_complete is True
