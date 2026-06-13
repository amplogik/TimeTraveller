"""Phase 2: sharded write path through action_backup.

Runs the real worker.action_backup with shards configured, monkeypatching the
size threshold so a tiny test tree still fans out. Verifies N shard archives,
the manifest shard-set, union-completeness, and per-shard restore — plus that
shards=1 is the historical single-archive behaviour.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import extract as extractlib
from timetraveller import index as indexlib
from timetraveller import manifest as manifestlib
from timetraveller import pax as paxlib
from timetraveller import worker
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)


def _plan(dest: Path, src: Path, shards) -> PlanConfig:
    return PlanConfig(
        plan_name="tp", sources=[str(src)], destination=str(dest),
        include_hostname_in_path=False,
        schedule=Schedule(mode="weekly", full=FullSchedule(days=["sun"]),
                          incr=IncrSchedule(mode="except_full")),
        retention=Retention(), shards=shards,
    )


def _args(kind="full", manual=True):
    return argparse.Namespace(kind=kind, manual=manual, no_framed=False,
                              no_retention=True, quiet=True, log_file=None)


def _members(sidecar: Path) -> set[str]:
    return {json.loads(l)["name"] for l in indexlib.read_sidecar(sidecar)[1:] if l.strip()}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_tree(src: Path) -> None:
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    for i in range(20):
        (src / ("a" if i % 2 else "b") / f"f{i:02d}.txt").write_text(f"data {i}\n" * 200)


def test_sharded_full_through_action_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(worker, "_MIN_SHARD_BYTES", 1)  # force sharding on tiny data
    src = tmp_path / "src"
    _make_tree(src)
    plan = _plan(tmp_path / "dest", src, shards=3)

    rc = worker.action_backup(_args("full"), plan)
    assert rc == 0

    adir = plan.archive_dir()
    man = manifestlib.load(manifestlib.manifest_path(adir))

    # One logical backup of 3 shards; one cycle; complete.
    sets = manifestlib.shard_sets(man)
    assert len(sets) == 1
    s = sets[0]
    assert s.shard_count == 3 and len(s.members) == 3 and s.is_complete
    assert all(e.shard_group == s.group_id for e in man.archives)
    assert len(manifestlib.cycles(man)) == 1

    # 3 shard files on disk, each with its inline sidecars.
    shard_files = sorted(adir.glob("*.s*of3.pax.zst"))
    assert len(shard_files) == 3
    for e in man.archives:
        assert e.has_sidecar and e.has_frames

    # Union-completeness: every enumerated member in exactly one shard.
    expected = set(paxlib.iter_archivable_files([str(src)], [], [], mtime_window=None,
                                                include_dirs=True, one_filesystem=True,
                                                skip_special=True))
    per_shard = [_members(indexlib.sidecar_path(adir / e.filename)) for e in man.archives]
    union = set().union(*per_shard)
    assert union == expected
    assert sum(len(p) for p in per_shard) == len(union)   # disjoint, no dups

    # Restore a known file from whichever shard owns it.
    target = next(mem for mem in expected if mem.endswith("f05.txt"))
    owner = next(e.filename for e, ms in zip(man.archives, per_shard) if target in ms)
    out = tmp_path / "restore"
    st = extractlib.extract_files(adir / owner, [target], into=out)
    assert st.matched_files == 1 and not st.fallback_naive
    assert _sha(out / target[2:]) == _sha(Path("/") / target[2:])


def test_cli_extract_across_shards(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(worker, "_MIN_SHARD_BYTES", 1)
    src = tmp_path / "src"
    _make_tree(src)
    plan = _plan(tmp_path / "dest", src, shards=3)
    assert worker.action_backup(_args("full"), plan) == 0

    man = manifestlib.load(manifestlib.manifest_path(plan.archive_dir()))
    stem = manifestlib.shard_sets(man)[0].group_id   # e.g. "<date>_full"

    out = tmp_path / "restore"
    subtree = "./" + str(src).lstrip("/") + "/"      # spans all shards
    eargs = argparse.Namespace(extract=stem, paths=[subtree], into=out, quiet=True)
    assert worker.action_extract(eargs, plan) == 0

    restored = list(out.rglob("f*.txt"))
    assert len(restored) == 20                       # every file, gathered from all shards
    for r in restored:
        assert _sha(r) == _sha(Path("/") / r.relative_to(out))


def test_merged_sidecar_tree_covers_all_shards(tmp_path, monkeypatch):
    """The GUI merges shard sidecar trees into one; the union must equal the
    whole backup's members."""
    from timetraveller import archive as archivelib
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(worker, "_MIN_SHARD_BYTES", 1)
    src = tmp_path / "src"
    _make_tree(src)
    plan = _plan(tmp_path / "dest", src, shards=3)
    assert worker.action_backup(_args("full"), plan) == 0

    adir = plan.archive_dir()
    man = manifestlib.load(manifestlib.manifest_path(adir))
    roots = [archivelib.load_sidecar_tree(indexlib.sidecar_path(adir / e.filename))
             for e in man.archives]
    merged = archivelib.merge_sidecar_trees(roots)

    def paths(node, acc):
        for c in node.children.values():
            acc.add(c.full_path)
            paths(c, acc)
        return acc

    expected = set(paxlib.iter_archivable_files([str(src)], [], [], mtime_window=None,
                                                include_dirs=True, one_filesystem=True,
                                                skip_special=True))
    # Every member appears in the merged tree (it may also carry ancestor
    # scaffolding nodes like ./tmp that aren't archive members themselves).
    assert expected.issubset(paths(merged, set()))


def test_unsharded_full_is_legacy_single_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(worker, "_MIN_SHARD_BYTES", 1)
    src = tmp_path / "src"
    _make_tree(src)
    plan = _plan(tmp_path / "dest", src, shards=1)

    rc = worker.action_backup(_args("full"), plan)
    assert rc == 0
    adir = plan.archive_dir()
    man = manifestlib.load(manifestlib.manifest_path(adir))
    assert len(man.archives) == 1
    e = man.archives[0]
    assert e.shard_count == 1 and e.shard_index == 1
    assert ".s" not in e.filename and e.filename.endswith("_full.pax.zst")
    assert not list(adir.glob("*.sof*")) and not list(adir.glob("*.s*of*.pax.zst"))


def test_sharded_incremental_attaches_to_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(worker, "_MIN_SHARD_BYTES", 1)
    src = tmp_path / "src"
    _make_tree(src)
    plan = _plan(tmp_path / "dest", src, shards=2)

    assert worker.action_backup(_args("full"), plan) == 0
    # Touch a couple of files so the incremental has work, then run it.
    import time
    time.sleep(0.01)
    for i in (1, 3, 5):
        (src / "a" / f"f{i:02d}.txt").write_text("CHANGED\n" * 300)
    assert worker.action_backup(_args("incr"), plan) == 0

    man = manifestlib.load(manifestlib.manifest_path(plan.archive_dir()))
    cs = manifestlib.cycles(man)
    assert len(cs) == 1                       # incr joins the full's cycle
    c = cs[0]
    assert c.is_complete
    incr_sets = [s for s in manifestlib.shard_sets(man) if s.kind == "incr"]
    assert incr_sets and incr_sets[0].shard_count >= 1   # sharded per its own size
