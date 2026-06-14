"""Input-validation tests for the pkexec system-backup helper, mirroring the
delete-helper tests. Functions are called directly, bypassing the geteuid guard.
"""

from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HELPER = os.path.join(REPO_ROOT, "libexec", "timetraveller-run-system-backup")


def _import_helper():
    loader = SourceFileLoader("ttbackup", HELPER)
    spec = importlib.util.spec_from_loader("ttbackup", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_plan_allowlist():
    mod = _import_helper()
    mod.validate_plan("system")
    mod.validate_plan("homes")
    for bad in ("home", "evil", "../system", ""):
        with pytest.raises(SystemExit):
            mod.validate_plan(bad)


def test_kind_allowlist():
    mod = _import_helper()
    for good in ("full", "incr", "auto"):
        mod.validate_kind(good)
    for bad in ("Full", "delete", "--kind", "", "full;rm"):
        with pytest.raises(SystemExit):
            mod.validate_kind(bad)


def test_build_command_shape(monkeypatch):
    mod = _import_helper()
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/timetraveller-backup")
    monkeypatch.setattr(mod.os.path, "isfile", lambda p: True)
    cmd = mod.build_command("system", "full", manual=True)
    assert cmd == [
        "/usr/bin/timetraveller-backup", "--plan", "system",
        "--config", "/etc/timetraveller/system.yaml", "--kind", "full", "--manual",
    ]
    cmd = mod.build_command("homes", "incr", manual=False)
    assert "--manual" not in cmd
    assert cmd[-2:] == ["--kind", "incr"]


def test_build_command_validates_first(monkeypatch):
    mod = _import_helper()
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/timetraveller-backup")
    monkeypatch.setattr(mod.os.path, "isfile", lambda p: True)
    with pytest.raises(SystemExit):
        mod.build_command("home", "full", manual=False)   # plan not allowed
    with pytest.raises(SystemExit):
        mod.build_command("system", "bogus", manual=False)  # kind not allowed
