"""Input-validation tests for the pkexec delete helper.

Tests import the helper as a module and call its validation functions directly,
bypassing the geteuid()==0 guard (same approach as test_system_plan_names.py).
The security surface is: plan allowlist, action allowlist, and — most important —
the identifier charset, which must reject path traversal and argument smuggling.
"""

from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HELPER = os.path.join(REPO_ROOT, "libexec", "timetraveller-delete-system-archives")


def _import_helper():
    loader = SourceFileLoader("ttdelete", HELPER)
    spec = importlib.util.spec_from_loader("ttdelete", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_plan_allowlist():
    mod = _import_helper()
    mod.validate_plan("system")
    mod.validate_plan("homes")
    for bad in ("home", "evil", "../system", "system\n", ""):
        with pytest.raises(SystemExit):
            mod.validate_plan(bad)


def test_action_allowlist():
    mod = _import_helper()
    mod.validate_action("--delete-cycle")
    mod.validate_action("--delete-set")
    for bad in ("--delete", "--backup", "--prune", "; rm -rf /", "--delete-cycle "):
        with pytest.raises(SystemExit):
            mod.validate_action(bad)


def test_ident_accepts_real_identifiers():
    mod = _import_helper()
    for good in ("2026-06-14", "2026-06-14T132920", "2026-06-14_full",
                 "2026-06-14T132920_incr", "2026-06-14_full"):
        mod.validate_ident(good)   # must not raise


def test_ident_rejects_traversal_and_metacharacters():
    mod = _import_helper()
    bad = [
        "../../etc/passwd",        # path traversal
        "2026-06-14/../../x",      # slash
        "a.b",                     # dot (no extensions / no ..)
        "2026-06-14_full.pax.zst", # a filename, not a stem
        "foo;rm -rf /",            # shell metachars
        "foo bar",                 # space → arg smuggling
        "--force",                 # flag smuggling
        "-rf",                     # leading dash
        "$(whoami)",               # command substitution chars
        "",                        # empty
        "x" * 65,                  # over length cap
    ]
    for b in bad:
        with pytest.raises(SystemExit):
            mod.validate_ident(b)


def test_build_command_shape(monkeypatch):
    mod = _import_helper()
    # Pretend the canonical binary + config exist so we exercise argv assembly.
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/timetraveller-backup")
    monkeypatch.setattr(mod.os.path, "isfile", lambda p: True)
    cmd = mod.build_command("system", "--delete-cycle", "2026-06-14", force=True)
    assert cmd == [
        "/usr/bin/timetraveller-backup", "--plan", "system",
        "--config", "/etc/timetraveller/system.yaml",
        "--delete-cycle", "2026-06-14", "--force",
    ]
    # Without force, no --force is appended.
    cmd = mod.build_command("homes", "--delete-set", "2026-06-14_full", force=False)
    assert "--force" not in cmd
    assert cmd[-2:] == ["--delete-set", "2026-06-14_full"]


def test_build_command_validates_before_assembling(monkeypatch):
    """A bad identifier is rejected even if the binary/config exist."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/timetraveller-backup")
    monkeypatch.setattr(mod.os.path, "isfile", lambda p: True)
    with pytest.raises(SystemExit):
        mod.build_command("system", "--delete-cycle", "../etc/passwd", force=False)
