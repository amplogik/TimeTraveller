"""Tests for the generalized system-class plan mechanism.

Covers:
- SYSTEM_PLAN_NAMES allowlist contents.
- defaults_homes() shape (new in 1.0.2).
- defaults_home() resolves to the actual user's $HOME (new in 1.0.2).
- resolve_config_path routes both "system" and "homes" to /etc.
- Pkexec helper input validation: size cap, plan-name allowlist, shape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller.config import (
    SYSTEM_PLAN_NAMES,
    defaults_home,
    defaults_homes,
    defaults_system,
    resolve_config_path,
    system_config_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "libexec" / "timetraveller-write-system-config"


# ---------- SYSTEM_PLAN_NAMES ----------

def test_system_plan_names_contains_system_and_homes():
    assert "system" in SYSTEM_PLAN_NAMES
    assert "homes" in SYSTEM_PLAN_NAMES


def test_system_plan_names_excludes_home():
    # "home" is a user-crontab plan and must NOT be routed through pkexec.
    assert "home" not in SYSTEM_PLAN_NAMES


# ---------- defaults_* ----------

def test_defaults_home_uses_actual_home():
    plan = defaults_home()
    assert plan.plan_name == "home"
    assert plan.sources == [str(Path.home())]


def test_defaults_homes_shape():
    plan = defaults_homes()
    assert plan.plan_name == "homes"
    assert plan.sources == ["/home"]
    # Should use the shared home-excludes set (cache, trash, node_modules…).
    assert "**/.cache/" in plan.excludes


def test_defaults_system_excludes_home():
    plan = defaults_system()
    assert plan.plan_name == "system"
    assert "/home/**" in plan.excludes


# ---------- resolve_config_path ----------

def test_resolve_config_path_routes_homes_to_etc(tmp_path, monkeypatch):
    # Pre-create a /etc-style path under tmp by monkeypatching.
    etc = tmp_path / "etc" / "timetraveller"
    etc.mkdir(parents=True)
    homes_yaml = etc / "homes.yaml"
    homes_yaml.write_text("plan_name: homes\nsources:\n- /home\n")
    monkeypatch.setattr(
        "timetraveller.config.system_config_path",
        lambda name: etc / f"{name}.yaml",
    )
    found = resolve_config_path("homes")
    assert found == homes_yaml


# ---------- pkexec helper input validation ----------
#
# These tests invoke the helper as the current (non-root) user. The helper's
# first guard is geteuid()==0, so every test expects rejection at that point
# unless we monkeypatch the guard. To exercise the *input* validation paths
# we set TT_HELPER_SKIP_ROOT_CHECK=1 (recognised below in the helper) — but
# we don't want to modify the helper for tests. Instead, we run a small
# wrapper that imports the helper module and calls its functions directly.

def _import_helper():
    # The helper has no .py extension; spec-from-file-location won't infer a
    # loader, so we supply SourceFileLoader explicitly.
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("ttwrite", str(HELPER))
    spec = importlib.util.spec_from_loader("ttwrite", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_helper_rejects_unknown_plan_name():
    mod = _import_helper()
    with pytest.raises(SystemExit) as exc:
        mod.validate_plan_name("evil")
    assert exc.value.code == 2


def test_helper_accepts_allowed_plan_names():
    mod = _import_helper()
    mod.validate_plan_name("system")
    mod.validate_plan_name("homes")


def test_helper_rejects_non_dict_yaml():
    mod = _import_helper()
    with pytest.raises(SystemExit):
        mod.parse_yaml(b"- not\n- a\n- dict\n")
    with pytest.raises(SystemExit):
        mod.parse_yaml(b"just a string")


def test_helper_rejects_invalid_yaml():
    mod = _import_helper()
    with pytest.raises(SystemExit):
        mod.parse_yaml(b": : : not yaml :\n")


def test_helper_rejects_plan_name_mismatch():
    mod = _import_helper()
    plan = asdict(defaults_system())
    with pytest.raises(SystemExit) as exc:
        mod.validate_shape(plan, "homes")
    assert exc.value.code == 2


def test_helper_accepts_valid_plan():
    mod = _import_helper()
    plan = asdict(defaults_system())
    # Should not raise.
    mod.validate_shape(plan, "system")
    plan = asdict(defaults_homes())
    mod.validate_shape(plan, "homes")


def test_helper_rejects_missing_sources():
    mod = _import_helper()
    plan = asdict(defaults_system())
    del plan["sources"]
    with pytest.raises(SystemExit):
        mod.validate_shape(plan, "system")


def test_helper_rejects_empty_sources():
    mod = _import_helper()
    plan = asdict(defaults_system())
    plan["sources"] = []
    with pytest.raises(SystemExit):
        mod.validate_shape(plan, "system")


def test_helper_rejects_wrong_field_type():
    mod = _import_helper()
    plan = asdict(defaults_system())
    plan["compression"] = ["not", "a", "string"]
    with pytest.raises(SystemExit):
        mod.validate_shape(plan, "system")


def test_helper_rejects_oversized_input(monkeypatch):
    """read_stdin_bounded() must refuse > MAX_BYTES of input.

    We swap in a fake stdin whose .buffer.read returns oversized bytes.
    """
    mod = _import_helper()
    big = b"a" * (mod.MAX_BYTES + 1)

    class FakeBuffer:
        def read(self, n):
            return big[:n]

    class FakeStdin:
        buffer = FakeBuffer()

    monkeypatch.setattr(mod.sys, "stdin", FakeStdin())
    with pytest.raises(SystemExit) as exc:
        mod.read_stdin_bounded()
    assert exc.value.code == 2


def test_helper_rejects_empty_input(monkeypatch):
    mod = _import_helper()

    class FakeBuffer:
        def read(self, n):
            return b"   \n"

    class FakeStdin:
        buffer = FakeBuffer()

    monkeypatch.setattr(mod.sys, "stdin", FakeStdin())
    with pytest.raises(SystemExit):
        mod.read_stdin_bounded()
