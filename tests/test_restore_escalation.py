"""Restore-to-original-location privilege escalation.

Three surfaces, in descending order of how much damage a bug would do:

  1. The pkexec restore helper's input validation. It runs as root and writes
     into the live filesystem, so its validators ARE the security boundary. Same
     import-the-script-and-call-the-validators approach as test_delete_helper.py.
  2. `extract.dest_needs_root()` — the routing decision. Getting this wrong in
     the permissive direction means a silent partial restore; in the restrictive
     direction it means a password prompt for a plain extract into $HOME.
  3. `worker.action_extract`'s routing — and specifically that it REFUSES rather
     than escalating for a root-owned destination that is not "/", because the
     helper hardcodes "/" and would otherwise scatter files to their original
     locations instead of where the user asked.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import extract as extractlib
from timetraveller import worker
from timetraveller.config import defaults_system

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HELPER = os.path.join(REPO_ROOT, "libexec", "timetraveller-restore-system-files")


def _import_helper():
    loader = SourceFileLoader("ttrestore", HELPER)
    spec = importlib.util.spec_from_loader("ttrestore", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------- 1. helper validation (the security boundary) ----------

def test_helper_plan_allowlist():
    mod = _import_helper()
    mod.validate_plan("system")
    mod.validate_plan("homes")
    for bad in ("home", "evil", "../system", "system\n", "", "system;rm -rf /"):
        with pytest.raises(SystemExit):
            mod.validate_plan(bad)


def test_helper_ident_rejects_traversal_and_dots():
    mod = _import_helper()
    mod.validate_ident("2026-07-22T210655_full")
    mod.validate_ident("2026-06-14_incr")
    # Dots are excluded outright, so a shard filename or sidecar name is refused
    # — callers must normalise to the group stem first.
    for bad in ("2026-06-14_full.pax.zst", "../../etc", "a/b", "", ".",
                "-oops", "x" * 65, "2026 06 14"):
        with pytest.raises(SystemExit):
            mod.validate_ident(bad)


def test_helper_member_must_be_dot_rooted():
    """The './' requirement is argv-injection defence as well as a format check:
    a member can never be mistaken for an option by the worker's parser."""
    mod = _import_helper()
    mod.validate_member("./etc/fstab")
    mod.validate_member("./usr/lib/vst/")
    for bad in ("/etc/fstab", "etc/fstab", "--into", "-rf", ""):
        with pytest.raises(SystemExit):
            mod.validate_member(bad)


def test_helper_member_rejects_traversal():
    mod = _import_helper()
    for bad in ("./../etc/shadow", "./etc/../../root/.ssh/authorized_keys",
                "./..", "./a/../../b"):
        with pytest.raises(SystemExit):
            mod.validate_member(bad)


def test_helper_member_rejects_control_characters_and_nul():
    mod = _import_helper()
    for bad in ("./etc/pass\0wd", "./etc/a\nb", "./etc/a\tb"):
        with pytest.raises(SystemExit):
            mod.validate_member(bad)


def test_helper_member_length_cap():
    mod = _import_helper()
    with pytest.raises(SystemExit):
        mod.validate_member("./" + "a" * (mod.MAX_MEMBER_LEN + 1))


def test_helper_rejects_too_many_members(tmp_path, monkeypatch):
    mod = _import_helper()
    monkeypatch.setattr(mod, "ETC_DIR", str(tmp_path))
    (tmp_path / "system.yaml").write_text("plan_name: system\n")
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/true")
    members = [f"./etc/f{i}" for i in range(mod.MAX_MEMBERS + 1)]
    with pytest.raises(SystemExit):
        mod.build_command("system", "2026-06-14_full", members)


def test_helper_rejects_empty_member_list(tmp_path, monkeypatch):
    mod = _import_helper()
    monkeypatch.setattr(mod, "ETC_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        mod.build_command("system", "2026-06-14_full", [])


def test_helper_destination_is_hardcoded_root(tmp_path, monkeypatch):
    """The single most important property: the destination is not a parameter."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "ETC_DIR", str(tmp_path))
    (tmp_path / "system.yaml").write_text("plan_name: system\n")
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/timetraveller-backup")

    cmd = mod.build_command("system", "2026-06-14_full", ["./etc/fstab"])

    assert mod.RESTORE_INTO == "/"
    assert "--into" in cmd
    assert cmd[cmd.index("--into") + 1] == "/"
    # The config is derived from the plan name against a fixed prefix, never
    # taken from the caller.
    assert cmd[cmd.index("--config") + 1] == str(tmp_path / "system.yaml")
    assert cmd[-1] == "./etc/fstab"


def test_helper_refuses_when_config_missing(tmp_path, monkeypatch):
    mod = _import_helper()
    monkeypatch.setattr(mod, "ETC_DIR", str(tmp_path))   # no system.yaml written
    with pytest.raises(SystemExit):
        mod.build_command("system", "2026-06-14_full", ["./etc/fstab"])


# ---------- 2. the routing decision ----------

def test_dest_needs_root_false_for_owned_dir(tmp_path):
    assert extractlib.dest_needs_root(tmp_path) is False


def test_dest_needs_root_uses_nearest_existing_ancestor(tmp_path):
    """The destination usually does not exist yet; the ancestor's permissions
    are what decide whether the mkdir+write chain can start."""
    deep = tmp_path / "a" / "b" / "c"
    assert extractlib.dest_needs_root(deep) is False


def test_dest_needs_root_true_for_unwritable_ancestor(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)          # readable + searchable, NOT writable
    try:
        assert extractlib.dest_needs_root(locked / "sub") is True
    finally:
        locked.chmod(0o700)      # so tmp_path cleanup can remove it


def test_dest_needs_root_true_for_unsearchable_ancestor(tmp_path):
    """Writable but not searchable still blocks the write chain, which is why
    the probe asks for W_OK|X_OK rather than W_OK alone."""
    odd = tmp_path / "odd"
    odd.mkdir()
    odd.chmod(0o600)             # readable + writable, NOT searchable
    try:
        assert extractlib.dest_needs_root(odd / "sub") is True
    finally:
        odd.chmod(0o700)


def test_dest_needs_root_false_when_already_root(tmp_path, monkeypatch):
    """A root caller needs no helper, so the probe short-circuits."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setattr(extractlib.os, "geteuid", lambda: 0)
    try:
        assert extractlib.dest_needs_root(locked / "sub") is False
    finally:
        locked.chmod(0o700)


# ---------- 3. action_extract routing ----------

def _extract_args(**kw):
    base = dict(extract="2026-06-14_full", paths=["./etc/fstab"], into=None,
                quiet=True, verbose=False, log_file=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_extract_routes_to_pkexec_for_root_destination(tmp_path, monkeypatch):
    plan = defaults_system()
    monkeypatch.setattr(worker, "_needs_root_escalation", lambda p: True)
    monkeypatch.setattr(extractlib, "dest_needs_root", lambda d: True)
    monkeypatch.setattr(worker, "_resolve_extract_targets",
                        lambda adir, ident: [tmp_path / "a.pax.zst"])
    monkeypatch.setattr(worker.configlib.PlanConfig, "archive_dir",
                        lambda self: tmp_path)
    seen = {}

    def fake_pkexec(args, p, ident, paths):
        seen["call"] = (p.plan_name, ident, paths)
        return 0

    monkeypatch.setattr(worker, "_restore_via_pkexec", fake_pkexec)
    rc = worker.action_extract(_extract_args(into=Path("/")), plan)
    assert rc == 0
    assert seen["call"] == ("system", "2026-06-14_full", ["./etc/fstab"])


def test_extract_refuses_root_destination_that_is_not_slash(tmp_path, monkeypatch, capsys):
    """Must NOT escalate: the helper hardcodes '/' as its destination, so routing
    a /opt/foo request through it would scatter files to their original
    locations instead of the requested directory."""
    plan = defaults_system()
    monkeypatch.setattr(worker, "_needs_root_escalation", lambda p: True)
    monkeypatch.setattr(extractlib, "dest_needs_root", lambda d: True)
    monkeypatch.setattr(worker, "_resolve_extract_targets",
                        lambda adir, ident: [tmp_path / "a.pax.zst"])
    monkeypatch.setattr(worker.configlib.PlanConfig, "archive_dir",
                        lambda self: tmp_path)

    def boom(*a, **k):
        raise AssertionError("must not escalate for a non-'/' destination")

    monkeypatch.setattr(worker, "_restore_via_pkexec", boom)
    rc = worker.action_extract(_extract_args(into=Path("/opt/foo")), plan)
    assert rc == 1
    assert "not writable" in capsys.readouterr().err


def test_extract_does_not_escalate_for_writable_destination(tmp_path, monkeypatch):
    """A system-plan extract into a directory the user owns must stay
    unprivileged and prompt-free."""
    plan = defaults_system()
    monkeypatch.setattr(worker, "_resolve_extract_targets",
                        lambda adir, ident: [tmp_path / "a.pax.zst"])
    monkeypatch.setattr(worker.configlib.PlanConfig, "archive_dir",
                        lambda self: tmp_path)

    def boom(*a, **k):
        raise AssertionError("must not escalate for a writable destination")

    monkeypatch.setattr(worker, "_restore_via_pkexec", boom)
    calls = []

    def fake_extract(shard, paths, *, into):
        calls.append((shard, paths, into))
        return extractlib.ExtractStats(
            requested_patterns=1, matched_files=1, matched_dirs=0,
            matched_symlinks=0, matched_hardlinks=0, frames_read=1,
            nfs_bytes_read=10, bytes_written=10, seconds_total=0.0)

    monkeypatch.setattr(extractlib, "extract_files", fake_extract)
    rc = worker.action_extract(_extract_args(into=tmp_path / "out"), plan)
    assert rc == 0
    assert calls and calls[0][2] == tmp_path / "out"
