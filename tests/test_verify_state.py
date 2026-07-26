"""verify-after-write must record what it MANAGED to conclude.

Before this, `_verify_shard_after_write` returned a bare [] for four different
outcomes — verified-clean, the check threw, there was nothing to check against,
and checking was switched off — so `corrupt_frames=0` was unfalsifiable and a
backup nobody verified rendered identically to a verified one. That ambiguity
cost three days chasing a corruption bug that did not exist.

The key policy assertion here: an unverified shard is a GAP IN KNOWLEDGE, not
damage. It must not make a cycle incomplete or block retention — only change what
we say about it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import manifest as manifestlib
from timetraveller import worker
from timetraveller.config import defaults_system


def _entry(fn: str, status: str = "ok", **kw) -> manifestlib.ArchiveEntry:
    base = dict(filename=fn, kind="full", cycle_id="2026-07-26",
                date_started="2026-07-26T08:00:00", date_finished="",
                size_bytes=1, status=status, hostname="h", plan_name="p",
                shard_group="2026-07-26_full")
    base.update(kw)
    return manifestlib.ArchiveEntry(**base)


# ---------- what _verify_shard_after_write reports ----------

def test_reports_disabled_when_plan_has_verify_off(tmp_path):
    plan = defaults_system()
    plan.verify_after_write = False
    state, bad = worker._verify_shard_after_write(tmp_path / "a.pax.zst", plan)
    assert state == "disabled"
    assert bad == []


def test_reports_error_when_verify_raises(tmp_path, monkeypatch, capsys):
    plan = defaults_system()
    plan.verify_after_write = True

    def boom(*a, **k):
        raise OSError("simulated NFS read hiccup")

    monkeypatch.setattr(worker.heallib, "verify_frame_checksums", boom)
    state, bad = worker._verify_shard_after_write(tmp_path / "a.pax.zst", plan)
    assert state == "error"
    assert bad == []
    # Still non-fatal, and still says so on stderr.
    assert "not verified" in capsys.readouterr().err


def test_reports_unverified_when_no_csum_sidecar(tmp_path, monkeypatch):
    plan = defaults_system()
    plan.verify_after_write = True
    monkeypatch.setattr(worker.heallib, "verify_frame_checksums",
                        lambda *a, **k: None)
    state, bad = worker._verify_shard_after_write(tmp_path / "a.pax.zst", plan)
    assert state == "unverified"
    assert bad == []


def test_reports_verified_when_check_ran(tmp_path, monkeypatch):
    plan = defaults_system()
    plan.verify_after_write = True
    monkeypatch.setattr(worker.heallib, "verify_frame_checksums",
                        lambda *a, **k: ("sha256", 10, []))
    state, bad = worker._verify_shard_after_write(tmp_path / "a.pax.zst", plan)
    assert state == "verified"
    assert bad == []


def test_verified_is_reported_even_when_frames_are_bad(tmp_path, monkeypatch):
    """'verified' describes the CHECK, not the verdict — the check ran, so
    corrupt_frames is trustworthy. _mark_corrupt_shards handles the verdict."""
    plan = defaults_system()
    plan.verify_after_write = True
    monkeypatch.setattr(worker.heallib, "verify_frame_checksums",
                        lambda *a, **k: ("sha256", 10, [{"id": 3}]))
    state, bad = worker._verify_shard_after_write(tmp_path / "a.pax.zst", plan)
    assert state == "verified"
    assert bad == [{"id": 3}]


# ---------- _record_verify_states ----------

def _specs(tmp_path, name: str):
    return [(name, tmp_path / name, [], tmp_path / "plan.log")]


def test_records_state_on_the_entry(tmp_path):
    m = manifestlib.Manifest(plan_name="p", archives=[_entry("a.pax.zst")])
    args = argparse.Namespace(quiet=True)
    n = worker._record_verify_states(m, tmp_path, _specs(tmp_path, "a.pax.zst"),
                                    ["verified"], args)
    assert n == 0
    assert m.archives[0].verify_state == "verified"


@pytest.mark.parametrize("state", ["error", "unverified", "disabled"])
def test_unverified_states_are_counted_noted_and_logged(tmp_path, state, capsys):
    m = manifestlib.Manifest(plan_name="p", archives=[_entry("a.pax.zst")])
    args = argparse.Namespace(quiet=True)
    log = tmp_path / "plan.log"

    n = worker._record_verify_states(m, tmp_path, _specs(tmp_path, "a.pax.zst"),
                                    [state], args)

    assert n == 1
    e = m.archives[0]
    assert e.verify_state == state
    assert "NOT checked" in e.notes
    assert "WARNING" in capsys.readouterr().err
    # The durable trace: a scheduled run captures neither stdout nor stderr.
    assert log.exists()
    assert "UNVERIFIED" in log.read_text()


def test_verified_state_also_lands_in_the_log(tmp_path):
    m = manifestlib.Manifest(plan_name="p", archives=[_entry("a.pax.zst")])
    args = argparse.Namespace(quiet=True)
    log = tmp_path / "plan.log"
    worker._record_verify_states(m, tmp_path, _specs(tmp_path, "a.pax.zst"),
                                ["verified"], args)
    assert "VERIFIED" in log.read_text()


def test_empty_state_is_skipped(tmp_path):
    """A shard that failed outright never had verify attempted; leave it alone."""
    m = manifestlib.Manifest(plan_name="p",
                             archives=[_entry("a.pax.zst", status="failed")])
    args = argparse.Namespace(quiet=True)
    n = worker._record_verify_states(m, tmp_path, _specs(tmp_path, "a.pax.zst"),
                                    [""], args)
    assert n == 0
    assert m.archives[0].verify_state == ""


def test_log_failure_never_raises(tmp_path):
    """Logging is best-effort; it must not be able to fail a backup."""
    m = manifestlib.Manifest(plan_name="p", archives=[_entry("a.pax.zst")])
    args = argparse.Namespace(quiet=True)
    # Point the log at a path that cannot be created (a file used as a dir).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    specs = [("a.pax.zst", tmp_path / "a.pax.zst", [], blocker / "sub" / "x.log")]
    n = worker._record_verify_states(m, tmp_path, specs, ["error"], args)
    assert n == 1                       # still recorded on the entry


# ---------- ShardSet aggregation, and the policy boundary ----------

def _set_of(*states) -> manifestlib.ShardSet:
    members = [_entry(f"a.s{i+1}of{len(states)}.pax.zst", verify_state=s)
               for i, s in enumerate(states)]
    return manifestlib.ShardSet(group_id="2026-07-26_full", members=members)


def test_shardset_verify_state_reports_worst():
    assert _set_of("verified", "verified").verify_state == "verified"
    assert _set_of("verified", "unverified").verify_state == "unverified"
    assert _set_of("verified", "disabled").verify_state == "disabled"
    # error outranks everything else
    assert _set_of("unverified", "error", "verified").verify_state == "error"
    assert _set_of("disabled", "unverified").verify_state == "unverified"
    assert _set_of("", "").verify_state == ""


def test_is_fully_verified():
    assert _set_of("verified", "verified").is_fully_verified is True
    assert _set_of("verified", "unverified").is_fully_verified is False
    assert _set_of("", "").is_fully_verified is False


def test_unverified_does_not_make_a_cycle_incomplete():
    """The policy boundary. An unverified shard is missing knowledge, not damage:
    marking it incomplete would change retention and destroy the redundancy that
    healing depends on."""
    s = _set_of("unverified", "error")
    assert s.status == "ok"
    assert s.is_complete is True


def test_corruption_still_wins_over_verify_state():
    members = [_entry("a.s1of2.pax.zst", verify_state="verified",
                      status="corrupt", corrupt_frames=2),
               _entry("a.s2of2.pax.zst", verify_state="unverified")]
    s = manifestlib.ShardSet(group_id="g", members=members)
    assert s.status == "corrupt"
    assert s.is_complete is False


# ---------- persistence ----------

def test_verify_state_survives_a_manifest_round_trip(tmp_path):
    m = manifestlib.Manifest(plan_name="p",
                             archives=[_entry("a.pax.zst", verify_state="error")])
    p = tmp_path / "manifest.json"
    manifestlib.save(m, p)
    assert manifestlib.load(p).archives[0].verify_state == "error"


def test_legacy_manifest_without_verify_state_still_loads(tmp_path):
    """Entries written before this field must load, and must NOT be reported as
    unverified — "" means unknown, and greying out all history would be noise."""
    import json
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "plan_name": "p", "schema_version": 2,
        "archives": [{
            "filename": "old.pax.zst", "kind": "full", "cycle_id": "2026-01-01",
            "date_started": "2026-01-01T00:00:00", "date_finished": "",
            "size_bytes": 1, "status": "ok", "hostname": "h", "plan_name": "p",
        }],
    }))
    loaded = manifestlib.load(p)
    assert loaded.archives[0].verify_state == ""
    s = manifestlib.ShardSet(group_id="g", members=loaded.archives)
    assert s.verify_state == ""
