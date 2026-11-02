"""Tests for the post-restore orphaned-launcher audit.

Regression anchor: bast's Jun 13 recovery left three dead launchers that went
unnoticed until 2026-08-24 — /usr/bin/OpenCode, /opt/CBD/CHITUBOX_Basic/, and
org.gimp.GIMP.desktop pointing at an absent /usr/bin/flatpak.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import launchers as launcherslib


def _desktop(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body)
    return p


def _entry(exec_line: str, *, extra: str = "") -> str:
    return f"[Desktop Entry]\nType=Application\nName={exec_line}\n{extra}Exec={exec_line}\n"


# ---------- Exec= parsing ----------

def test_bare_command_is_the_first_word():
    assert launcherslib.exec_target(_entry("OpenCode %U")) == "OpenCode"


def test_absolute_path_with_field_code():
    assert launcherslib.exec_target(_entry("/usr/bin/flatpak run org.gimp.GIMP %U")) \
        == "/usr/bin/flatpak"


def test_quoted_path_containing_spaces_is_taken_whole():
    """The naive-split bug: this launcher is healthy, and splitting on
    whitespace reports three separate 'missing' fragments from it."""
    line = '"/home/kim/.local/share/Embodme/Erae Lab/erae-lab" %U'
    assert launcherslib.exec_target(_entry(line)) == \
        "/home/kim/.local/share/Embodme/Erae Lab/erae-lab"


def test_single_quoted_path_containing_spaces():
    line = "'/opt/My App/run.sh' --flag"
    assert launcherslib.exec_target(_entry(line)) == "/opt/My App/run.sh"


def test_unbalanced_quote_still_yields_the_whole_path():
    """shlex refuses; we must not fall back to a whitespace split."""
    assert launcherslib.exec_target(_entry('"/opt/My App/run.sh %U')) == \
        "/opt/My App/run.sh %U"


def test_first_exec_wins_over_a_later_duplicate():
    text = "[Desktop Entry]\nExec=/usr/bin/first\nExec=/usr/bin/second\n"
    assert launcherslib.exec_target(text) == "/usr/bin/first"


def test_exec_in_a_later_action_group_is_ignored():
    text = ("[Desktop Entry]\nExec=/usr/bin/main\n\n"
            "[Desktop Action new]\nExec=/usr/bin/other\n")
    assert launcherslib.exec_target(text) == "/usr/bin/main"


def test_exec_outside_the_desktop_entry_group_is_not_used():
    assert launcherslib.exec_target("[Desktop Action x]\nExec=/usr/bin/nope\n") is None


def test_entry_without_exec_is_skipped():
    assert launcherslib.exec_target("[Desktop Entry]\nType=Link\nURL=http://x\n") is None


def test_hidden_tombstone_is_skipped():
    """Hidden=true means 'treat as deleted' per the spec — not an orphan."""
    text = "[Desktop Entry]\nHidden=true\nExec=/usr/bin/gone\n"
    assert launcherslib.exec_target(text) is None


def test_nodisplay_is_still_checked():
    """OpenCode-handler.desktop is a NoDisplay handler entry — the exact file
    that broke. Skipping NoDisplay would skip the original bug."""
    text = "[Desktop Entry]\nNoDisplay=true\nExec=/usr/bin/OpenCode %u\n"
    assert launcherslib.exec_target(text) == "/usr/bin/OpenCode"


def test_comments_and_localised_keys_do_not_confuse_the_parser():
    text = ("# a comment\n[Desktop Entry]\nName[de]=Etwas\nName=Thing\n"
            "Exec=/usr/bin/thing\n")
    assert launcherslib.exec_target(text) == "/usr/bin/thing"


# ---------- resolution ----------

def test_resolves_existing_file(tmp_path):
    f = tmp_path / "prog"
    f.write_text("")
    assert launcherslib.resolves(str(f))


def test_resolves_a_directory_target(tmp_path):
    """/opt/CBD/CHITUBOX_Basic/ is a directory; exists() judges it correctly."""
    d = tmp_path / "CHITUBOX_Basic"
    d.mkdir()
    assert launcherslib.resolves(f"{d}/")


def test_missing_path_does_not_resolve(tmp_path):
    assert not launcherslib.resolves(str(tmp_path / "nope"))


def test_bare_command_goes_through_path():
    assert launcherslib.resolves("sh")
    assert not launcherslib.resolves("definitely-not-a-real-command-xyzzy")


# ---------- scanning ----------

def test_scan_finds_the_three_bast_casualties(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    _desktop(apps, "OpenCode-handler.desktop",
             "[Desktop Entry]\nName=OpenCode\nExec=/usr/bin/OpenCode %u\n")
    _desktop(apps, "chitubox.desktop",
             "[Desktop Entry]\nName=CHITUBOX\nExec=/opt/CBD/CHITUBOX_Basic/ %f\n")
    _desktop(apps, "org.gimp.GIMP.desktop",
             "[Desktop Entry]\nName=GIMP\nExec=/usr/bin/flatpak run org.gimp.GIMP %U\n")

    found = {o.desktop.name: o.target for o in launcherslib.scan([apps])}
    assert found == {
        "OpenCode-handler.desktop": "/usr/bin/OpenCode",
        "chitubox.desktop": "/opt/CBD/CHITUBOX_Basic/",
        "org.gimp.GIMP.desktop": "/usr/bin/flatpak",
    }


def test_scan_does_not_flag_a_healthy_quoted_path(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    prog_dir = tmp_path / "Erae Lab"
    prog_dir.mkdir()
    prog = prog_dir / "erae-lab"
    prog.write_text("")
    _desktop(apps, "erae.desktop",
             f'[Desktop Entry]\nName=Erae Lab\nExec="{prog}" %U\n')
    assert launcherslib.scan([apps]) == []


def test_scan_reports_the_entry_name(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    _desktop(apps, "x.desktop", "[Desktop Entry]\nName=Nice Name\nExec=/nope/x\n")
    assert launcherslib.scan([apps])[0].name == "Nice Name"


def test_a_user_override_shadows_the_system_entry(tmp_path):
    """XDG precedence: a working ~/.local copy must not be reported broken just
    because the /usr/share entry it replaces is."""
    user = tmp_path / "user"
    system = tmp_path / "system"
    user.mkdir()
    system.mkdir()
    _desktop(user, "app.desktop", "[Desktop Entry]\nExec=sh\n")
    _desktop(system, "app.desktop", "[Desktop Entry]\nExec=/nope/app\n")
    assert launcherslib.scan([user, system]) == []


def test_shadowing_works_the_other_way_too(tmp_path):
    user = tmp_path / "user"
    system = tmp_path / "system"
    user.mkdir()
    system.mkdir()
    _desktop(user, "app.desktop", "[Desktop Entry]\nExec=/nope/app\n")
    _desktop(system, "app.desktop", "[Desktop Entry]\nExec=sh\n")
    orphans = launcherslib.scan([user, system])
    assert [o.desktop for o in orphans] == [user / "app.desktop"]


def test_scan_of_a_missing_dir_is_empty(tmp_path):
    assert launcherslib.scan([tmp_path / "nope"]) == []


def test_unreadable_entry_is_skipped_not_raised(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    bad = _desktop(apps, "bad.desktop", "[Desktop Entry]\nExec=/nope/x\n")
    bad.chmod(0o000)
    try:
        if os.geteuid() == 0:
            pytest.skip("root reads anything")
        assert launcherslib.scan([apps]) == []
    finally:
        bad.chmod(0o644)


# ---------- which directories a restore is audited against ----------

def test_restore_to_root_checks_the_live_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home" / "kim"
    (home / ".local/share/applications").mkdir(parents=True)
    dirs = launcherslib.dirs_under(Path("/"), home=home)
    assert home / ".local/share/applications" in dirs


def test_restore_into_a_staging_dir_checks_the_staged_copies(tmp_path):
    """--into ~/Restored puts the launchers at
    <into>/home/kim/.local/share/applications, and warning there is what lets
    the user fix it before moving them into place."""
    home = Path("/home/kim")
    into = tmp_path / "Restored"
    staged = into / "home/kim/.local/share/applications"
    staged.mkdir(parents=True)
    assert launcherslib.dirs_under(into, home=home) == [staged]


def test_a_restore_that_contained_no_launchers_is_silent(tmp_path):
    """The rebased dirs do not exist, so an unrelated single-file extract into
    /tmp says nothing about launchers already installed on the system."""
    assert launcherslib.dirs_under(tmp_path / "empty", home=Path("/home/kim")) == []


# ---------- worker reporting ----------

from timetraveller import worker  # noqa: E402


def test_report_warns_on_stderr_and_never_raises(tmp_path, capsys, monkeypatch):
    apps = tmp_path / "home/kim/.local/share/applications"
    apps.mkdir(parents=True)
    _desktop(apps, "OpenCode-handler.desktop",
             "[Desktop Entry]\nName=OpenCode\nExec=/usr/bin/OpenCode %u\n")
    monkeypatch.setattr(launcherslib, "dirs_under", lambda into, home=None: [apps])

    worker._report_orphan_launchers(tmp_path)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "/usr/bin/OpenCode" in err
    assert "OpenCode-handler.desktop" in err


def test_report_is_silent_when_nothing_is_orphaned(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(launcherslib, "dirs_under", lambda into, home=None: [])
    worker._report_orphan_launchers(tmp_path)
    assert capsys.readouterr().err == ""


def test_a_broken_audit_cannot_fail_the_restore(tmp_path, capsys, monkeypatch):
    def boom(into, home=None):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(launcherslib, "dirs_under", boom)

    worker._report_orphan_launchers(tmp_path)  # must not raise
    assert "check skipped" in capsys.readouterr().err


def test_privileged_pkexec_leg_defers_to_the_parent(tmp_path, capsys, monkeypatch):
    """The helper discards the root run's stderr on success, so the root leg
    must stay quiet and let the unprivileged caller report."""
    monkeypatch.setenv("PKEXEC_UID", "1000")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcherslib, "dirs_under",
                        lambda into, home=None: pytest.fail("should not scan"))
    worker._report_orphan_launchers(tmp_path)
    assert capsys.readouterr().err == ""
