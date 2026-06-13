"""Phase 1: shard-aware manifest model.

A logical backup may be split into N shard archives, each its own ArchiveEntry
sharing a shard_group. shard_sets() groups them; cycles() collapses a full's N
shards into ONE cycle; retention treats the set as one unit. shard_count=1
(every legacy/unsharded backup) must behave exactly as before.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as m
from timetraveller import retention as retentionlib


def _entry(group: str, kind: str, idx: int, n: int, *, status: str = "ok",
           started: str = "", size: int = 100) -> m.ArchiveEntry:
    suffix = "" if n == 1 else f".s{idx}of{n}"
    fname = f"{group}{suffix}.pax.zst"
    return m.ArchiveEntry(
        filename=fname, kind=kind, cycle_id=group.split("_")[0],
        date_started=started or f"{group.split('_')[0]}T09:00:0{idx}+00:00",
        date_finished=f"{group.split('_')[0]}T09:30:00+00:00" if status != "in-progress" else "",
        size_bytes=size, status=status, hostname="h", plan_name="p",
        shard_index=idx, shard_count=n, shard_group=group,
    )


def _full_set(group: str, n: int, *, statuses=None, size: int = 100):
    statuses = statuses or ["ok"] * n
    return [_entry(group, "full", i + 1, n, status=statuses[i], size=size) for i in range(n)]


# ---------- group id derivation ----------

def test_group_id_from_filename():
    assert m._group_id_from_filename("2026-06-13_full.pax.zst") == "2026-06-13_full"
    assert m._group_id_from_filename("2026-06-13_full.s2of4.pax.zst") == "2026-06-13_full"
    assert m._group_id_from_filename("2026-06-13T143022_incr.s10of12.pax.zst") == "2026-06-13T143022_incr"


# ---------- shard_sets grouping ----------

def test_shard_sets_groups_siblings():
    man = m.Manifest(plan_name="p", archives=_full_set("2026-06-13_full", 4))
    sets = m.shard_sets(man)
    assert len(sets) == 1
    s = sets[0]
    assert s.group_id == "2026-06-13_full"
    assert [e.shard_index for e in s.members] == [1, 2, 3, 4]
    assert s.shard_count == 4
    assert s.total_size == 400
    assert s.kind == "full"
    assert s.is_complete is True
    assert s.status == "ok"


def test_shardset_status_precedence():
    man = m.Manifest(plan_name="p", archives=_full_set("g_full", 4,
                     statuses=["ok", "ok-with-warnings", "failed", "ok"]))
    s = m.shard_sets(man)[0]
    assert s.status == "failed"        # any failed dominates
    assert s.is_complete is False      # a single failed shard => incomplete
    man2 = m.Manifest(plan_name="p", archives=_full_set("g_full", 2,
                      statuses=["ok", "ok-with-warnings"]))
    assert m.shard_sets(man2)[0].status == "ok-with-warnings"
    assert m.shard_sets(man2)[0].is_complete is True


# ---------- cycles() collapses N full shards into ONE cycle ----------

def test_cycles_collapses_full_shards():
    man = m.Manifest(plan_name="p", archives=_full_set("2026-06-13_full", 4))
    cs = m.cycles(man)
    assert len(cs) == 1                       # not 4!
    c = cs[0]
    assert c.is_complete is True
    assert len(c.archives) == 4               # all shards present for deletion
    assert c.full is not None and c.full.shard_index == 1   # representative
    assert c.total_size == 400


def test_cycles_incomplete_full_set_does_not_open_cycle():
    # 4-shard full where shard 3 failed -> incomplete -> no cycle opened.
    arcs = _full_set("2026-06-13_full", 4, statuses=["ok", "ok", "failed", "ok"])
    cs = m.cycles(m.Manifest(plan_name="p", archives=arcs))
    assert len(cs) == 1
    assert cs[0].is_complete is False
    assert cs[0].full_set is None             # parked as incr_sets stub
    assert len(cs[0].archives) == 4


def test_cycles_incr_attaches_to_sharded_full():
    arcs = _full_set("2026-06-13_full", 4)
    # a single-stream incremental the next day
    arcs.append(_entry("2026-06-14_incr", "incr", 1, 1, started="2026-06-14T09:00:00+00:00"))
    cs = m.cycles(m.Manifest(plan_name="p", archives=arcs))
    assert len(cs) == 1                       # incr joins the full's cycle
    c = cs[0]
    assert c.is_complete is True
    assert len(c.incrementals) == 1
    assert len(c.archives) == 5               # 4 full shards + 1 incr


# ---------- shard_count=1 regression ----------

def test_unsharded_behaves_as_before():
    arcs = [_entry("2026-06-13_full", "full", 1, 1),
            _entry("2026-06-14_incr", "incr", 1, 1, started="2026-06-14T09:00:00+00:00"),
            _entry("2026-06-20_full", "full", 1, 1, started="2026-06-20T09:00:00+00:00")]
    cs = m.cycles(m.Manifest(plan_name="p", archives=arcs))
    assert len(cs) == 2
    assert cs[0].full.filename == "2026-06-13_full.pax.zst"
    assert [e.filename for e in cs[0].incrementals] == ["2026-06-14_incr.pax.zst"]
    assert cs[1].full.filename == "2026-06-20_full.pax.zst"


# ---------- retention treats a shard set as ONE cycle ----------

def test_retention_counts_sharded_cycle_as_one():
    arcs = []
    for day in ("2026-06-01", "2026-06-08", "2026-06-15"):
        arcs += _full_set(f"{day}_full", 4, size=1000)
    plan = retentionlib.apply(m.Manifest(plan_name="p", archives=arcs),
                              policy="max_cycles", max_cycles=2)
    assert len(plan.delete) == 1              # oldest cycle dropped (3 -> keep 2)
    assert plan.delete[0].cycle_id == "2026-06-01"
    assert len(plan.delete[0].archives) == 4  # all 4 shards deleted together
    assert {c.cycle_id for c in plan.keep} == {"2026-06-08", "2026-06-15"}


# ---------- load: legacy backfill + v2 round-trip ----------

def test_load_backfills_legacy_and_roundtrips_shards(tmp_path):
    # Legacy v1 entry: no shard_* fields in the JSON.
    legacy = {
        "plan_name": "p", "schema_version": 1,
        "archives": [{
            "filename": "2026-05-01_full.pax.zst", "kind": "full",
            "cycle_id": "2026-05-01", "date_started": "2026-05-01T09:00:00+00:00",
            "date_finished": "2026-05-01T09:30:00+00:00", "size_bytes": 10,
            "status": "ok", "hostname": "h", "plan_name": "p",
        }],
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(legacy))
    man = m.load(p)
    assert man.schema_version == m.SCHEMA_VERSION == 2
    e = man.archives[0]
    assert e.shard_count == 1 and e.shard_index == 1
    assert e.shard_group == "2026-05-01_full"     # backfilled from filename
    assert len(m.cycles(man)) == 1

    # Save a sharded manifest and reload: shard fields survive.
    man2 = m.Manifest(plan_name="p", archives=_full_set("2026-06-13_full", 3))
    m.save(man2, p)
    back = m.load(p)
    s = m.shard_sets(back)[0]
    assert s.shard_count == 3 and [x.shard_index for x in s.members] == [1, 2, 3]
    assert s.group_id == "2026-06-13_full"
