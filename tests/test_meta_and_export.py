"""D: per-shard .meta.json self-describing sidecars, manifest rebuild from a
bare directory, and the group-atomic export bundle.

None of this re-reads archive bytes on the backup path: .meta.json is a dump of
the in-memory ArchiveEntry. (Export copies files, which is inherent to copying.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as manifestlib
from timetraveller import worker
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)


def _entry(group, kind, idx, n, *, status="ok"):
    suffix = "" if n == 1 else f".s{idx}of{n}"
    cid = group.split("_")[0]
    return manifestlib.ArchiveEntry(
        filename=f"{group}{suffix}.pax.zst", kind=kind, cycle_id=cid,
        date_started=f"{cid}T09:00:0{idx}+00:00",
        date_finished=f"{cid}T09:30:00+00:00", size_bytes=100, status=status,
        hostname="h", plan_name="tp", shard_index=idx, shard_count=n,
        shard_group=group,
    )


def _set(group, kind, n, *, statuses=None):
    statuses = statuses or ["ok"] * n
    return [_entry(group, kind, i + 1, n, status=statuses[i]) for i in range(n)]


def _plan(dest: Path) -> PlanConfig:
    return PlanConfig(
        plan_name="tp", sources=["/x"], destination=str(dest),
        include_hostname_in_path=False,
        schedule=Schedule(mode="weekly", full=FullSchedule(days=["sun"]),
                          incr=IncrSchedule(mode="except_full")),
        retention=Retention(),
    )


def _args(**kw):
    base = dict(quiet=True, log_file=None, export_cycle=None, export_set=None,
                into=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _touch(archive_dir, entries, *, failed=False):
    """Create archive (or .failed) + idx + frames + meta for each entry."""
    for e in entries:
        name = e.filename + ".failed" if failed else e.filename
        (archive_dir / name).write_text("archive")
        (archive_dir / (e.filename + ".idx.zst")).write_text("idx")
        (archive_dir / (e.filename + ".frames.json")).write_text("{}")
        manifestlib.write_entry_meta(archive_dir, e)


# ---------- .meta.json round-trip + rebuild ----------

def test_entry_meta_roundtrip(tmp_path):
    e = _entry("2026-06-13_full", "full", 2, 4)
    manifestlib.write_entry_meta(tmp_path, e)
    p = manifestlib.entry_meta_path(tmp_path, e.filename)
    assert p.exists()
    back = manifestlib.read_entry_meta(p)
    assert back == e                       # full round-trip incl. shard fields


def test_manifest_from_meta_regroups_into_sets(tmp_path):
    full = _set("2026-06-13_full", "full", 4)
    incr = _set("2026-06-13T10_incr", "incr", 2)
    for e in full + incr:
        manifestlib.write_entry_meta(tmp_path, e)
    man = manifestlib.manifest_from_meta(tmp_path, "tp")
    assert len(man.archives) == 6
    sets = manifestlib.shard_sets(man)
    assert {s.group_id for s in sets} == {"2026-06-13_full", "2026-06-13T10_incr"}
    full_set = next(s for s in sets if s.group_id == "2026-06-13_full")
    assert full_set.shard_count == 4 and full_set.is_complete


def test_manifest_from_meta_skips_garbage(tmp_path):
    manifestlib.write_entry_meta(tmp_path, _entry("2026-06-13_full", "full", 1, 1))
    (tmp_path / "broken.meta.json").write_text("{not json")
    (tmp_path / "foreign.meta.json").write_text(json.dumps({"unexpected": 1}))
    man = manifestlib.manifest_from_meta(tmp_path, "tp")
    assert len(man.archives) == 1          # only the valid one


# ---------- export bundle ----------

def _setup(tmp_path, archives):
    plan = _plan(tmp_path / "dest")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    manifestlib.save(manifestlib.Manifest(plan_name="tp", archives=list(archives)),
                     manifestlib.manifest_path(archive_dir))
    return plan, archive_dir


def test_export_set_copies_every_file_and_a_manifest_slice(tmp_path):
    full = _set("2026-06-13_full", "full", 2)
    plan, archive_dir = _setup(tmp_path, full)
    _touch(archive_dir, full)
    out = tmp_path / "bundle"

    rc = worker.action_export(
        _args(export_set="2026-06-13_full", into=out), plan)
    assert rc == 0
    # Each shard: archive + idx + frames + meta = 4 files; 2 shards = 8.
    copied = [p.name for p in out.iterdir() if p.name != "manifest.json"]
    assert len(copied) == 8
    # Self-contained manifest slice with exactly the two entries.
    slice_man = manifestlib.load(manifestlib.manifest_path(out))
    assert {e.filename for e in slice_man.archives} == {
        "2026-06-13_full.s1of2.pax.zst", "2026-06-13_full.s2of2.pax.zst"}


def test_export_cycle_includes_full_and_incrementals(tmp_path):
    full = _set("2026-06-13_full", "full", 2)
    incr = _set("2026-06-13T10_incr", "incr", 1)
    plan, archive_dir = _setup(tmp_path, full + incr)
    _touch(archive_dir, full + incr)
    out = tmp_path / "bundle"

    rc = worker.action_export(_args(export_cycle="2026-06-13", into=out), plan)
    assert rc == 0
    slice_man = manifestlib.load(manifestlib.manifest_path(out))
    assert len(slice_man.archives) == 3
    assert {s.group_id for s in manifestlib.shard_sets(slice_man)} == {
        "2026-06-13_full", "2026-06-13T10_incr"}


def test_export_refuses_partial_set(tmp_path):
    full = _set("2026-06-13_full", "full", 2)
    plan, archive_dir = _setup(tmp_path, full)
    _touch(archive_dir, [full[0]])         # only shard 1 on disk; shard 2 missing
    out = tmp_path / "bundle"

    rc = worker.action_export(
        _args(export_set="2026-06-13_full", into=out), plan)
    assert rc == 1
    # Refused before copying: no manifest slice written.
    assert not (out / "manifest.json").exists()


def test_export_includes_failed_shard_variant(tmp_path):
    # A failed shard's primary lives at .failed; export must still find it.
    s = _set("2026-06-13T10_incr", "incr", 2, statuses=["ok", "failed"])
    # Give it a parent full so the set isn't the newest-complete edge case.
    full = _set("2026-06-13_full", "full", 1)
    plan, archive_dir = _setup(tmp_path, full + s)
    _touch(archive_dir, full)
    _touch(archive_dir, [s[0]])
    _touch(archive_dir, [s[1]], failed=True)
    out = tmp_path / "bundle"

    rc = worker.action_export(
        _args(export_set="2026-06-13T10_incr", into=out), plan)
    assert rc == 0
    names = {p.name for p in out.iterdir()}
    assert "2026-06-13T10_incr.s2of2.pax.zst.failed" in names


def test_export_unknown_id_errors(tmp_path):
    full = _set("2026-06-13_full", "full", 2)
    plan, _ = _setup(tmp_path, full)
    assert worker.action_export(_args(export_set="nope", into=tmp_path / "b"), plan) == 1
