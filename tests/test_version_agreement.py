"""The version must agree everywhere it is written.

This project has shipped a version mismatch on most of its releases:

  * v1.5.1-v1.5.3 shipped with `__version__` stuck at 1.5.0 — and worker.py
    stamps that into every archive via `created_by`, so those archives
    permanently misreport what wrote them. It was baked into the .deb.
  * v1.5.4 fixed it by hand and added no guard, so nothing stopped a recurrence.
  * v1.6.0 found README's status line stale at v1.5.3.
  * v1.6.1 found the README install example still telling users to install
    v1.4.3 — three months out of date.

`debian/rules` runs the same check at build time, which is the backstop that
cannot be skipped. This test is the fast feedback loop: it fails in a second,
during an ordinary `pytest` run, instead of at `dpkg-buildpackage`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "tools" / "check-versions.py"

sys.path.insert(0, str(REPO / "tools"))


def _load_checker():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("check_versions", str(CHECKER))
    spec = importlib.util.spec_from_loader("check_versions", loader)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the script uses @dataclass together with
    # `from __future__ import annotations`, and dataclasses resolves those
    # string annotations via sys.modules[cls.__module__].
    sys.modules["check_versions"] = mod
    loader.exec_module(mod)
    return mod


def test_all_version_sites_agree():
    """The actual guard. If this fails, run: python3 tools/check-versions.py --sync"""
    r = subprocess.run([sys.executable, str(CHECKER)], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"version sites disagree:\n{r.stdout}\n{r.stderr}\n"
        f"Fix with: python3 tools/check-versions.py --sync")


def test_every_site_is_actually_found():
    """A pattern that silently stops matching would make the guard vacuous —
    it would 'pass' by finding nothing to disagree with."""
    mod = _load_checker()
    for site in mod.SITES:
        version, line = site.read()
        assert version is not None, (
            f"{site.name}: pattern found no version in {site.path}. The guard is "
            f"only as good as its patterns; a silently-unmatched site is worse "
            f"than no check at all.")
        assert line and line > 0


def test_changelog_is_the_authority():
    """--sync must never rewrite debian/changelog: dpkg derives the real package
    version from it, and its stanza is human-authored release notes."""
    mod = _load_checker()
    assert mod.SITES[0].name == "debian/changelog"


def test_guard_detects_a_stale_version(tmp_path, monkeypatch):
    """Reproduces the v1.5.1-v1.5.3 bug: __init__.py left behind."""
    mod = _load_checker()
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "1.5.0"\n')
    site = mod.Site("test", init, mod.SITES[1].pattern, "d")
    assert site.read()[0] == "1.5.0"
    assert site.write("1.6.1") is True
    assert site.read()[0] == "1.6.1"
    assert init.read_text() == '__version__ = "1.6.1"\n'


def test_write_preserves_surrounding_text(tmp_path):
    """Rewriting must touch only the captured version, not the line around it —
    the README install line is a command users copy/paste."""
    mod = _load_checker()
    readme = tmp_path / "README.md"
    original = ("# Title\n\nStatus: **v1.4.3** — stable.\n\n"
                "```bash\nsudo apt install ./timetraveller_1.4.3_all.deb\n```\n")
    readme.write_text(original)

    status = mod.Site("s", readme, mod.SITES[3].pattern, "d")
    install = mod.Site("i", readme, mod.SITES[4].pattern, "d")
    status.write("1.6.1")
    install.write("1.6.1")

    got = readme.read_text()
    assert "Status: **v1.6.1** — stable." in got
    assert "sudo apt install ./timetraveller_1.6.1_all.deb" in got
    assert got.startswith("# Title\n")
    assert "```bash" in got and got.endswith("```\n")


def test_debian_rules_invokes_the_guard():
    """The build-time backstop must stay wired up — a guard nobody runs is not a
    guard, and this one exists precisely because hand-discipline kept failing."""
    rules = (REPO / "debian" / "rules").read_text()
    assert "tools/check-versions.py" in rules
    # It must hang off a dh override, not sit in a comment.
    assert any(line.strip().startswith("python3 tools/check-versions.py")
               for line in rules.splitlines()), rules
