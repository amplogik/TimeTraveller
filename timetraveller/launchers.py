"""Post-restore audit: desktop launchers whose `Exec=` target no longer exists.

A home-scoped restore brings back `~/.local/share/applications/*.desktop` but
NOT the programs those entries point at: anything under `/usr/bin`, `/opt`, or
installed via flatpak lives outside `$HOME` and was never in the archive. The
launcher comes back looking perfectly healthy — present in the app grid, a
valid entry, sometimes even an executable wrapper — pointing at a binary that
is gone. Nothing announces the mismatch until someone clicks it, and a GUI
click shows no error at all: the process dies at exit 127 without drawing a
window.

Found on bast 2026-08-24, where OpenCode's icon had been dead since a Jun 13
recovery — ~2 months unnoticed — because `/usr/bin/OpenCode` was never
restored. Two others went with it: `/opt/CBD/CHITUBOX_Basic/`, and
`org.gimp.GIMP.desktop` whose `Exec=` is `/usr/bin/flatpak` — flatpak itself
was absent, so every flatpak app was dead at once.

This is reported as a WARNING and never as a failure. A missing launcher
target says nothing about the integrity of the backup; it says the restore's
scope did not include something that lives outside `$HOME`.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

# XDG application directories, most-specific first. The user directory shadows
# the system one by basename, so a working ~/.local override of a broken
# /usr/share entry must not be reported (and vice versa).
_USER_APPLICATIONS = ".local/share/applications"
_SYSTEM_APPLICATIONS = "/usr/share/applications"


@dataclass(frozen=True)
class Orphan:
    """A .desktop entry whose Exec= program is not on this system."""
    desktop: Path
    name: str    # the entry's Name=, or the filename stem if it has none
    target: str  # the Exec= program that failed to resolve


def user_home() -> Path:
    """The home directory of the user this restore is FOR.

    Under the pkexec/sudo leg of a restore-to-original-locations we run as
    root, and `Path.home()` would be `/root` — the wrong desktop session
    entirely. The launchers that matter belong to the user who invoked us.
    """
    if os.geteuid() == 0:
        uid = os.environ.get("PKEXEC_UID")
        name = os.environ.get("SUDO_USER")
        try:
            if uid:
                return Path(pwd.getpwuid(int(uid)).pw_dir)
            if name:
                return Path(pwd.getpwnam(name).pw_dir)
        except (KeyError, ValueError):
            pass  # fall through to this process's own home
    return Path.home()


def launcher_dirs(home: Path | None = None) -> list[Path]:
    """The live XDG application directories, in precedence order."""
    home = home if home is not None else user_home()
    return [home / _USER_APPLICATIONS, Path(_SYSTEM_APPLICATIONS)]


def dirs_under(into: Path, home: Path | None = None) -> list[Path]:
    """Which application directories a restore into `into` could have written.

    A restore to the original locations (`--into /`) writes the live dirs, so
    those are what we check. A restore into a staging directory writes copies
    rebased under it — `--into ~/Restored` puts them at
    `~/Restored/home/kim/.local/share/applications`. Checking the staged copies
    warns BEFORE the user moves them into place, and costs nothing when the
    restore contained no launchers: the rebased dirs simply do not exist.

    Only directories that exist are returned.
    """
    live = launcher_dirs(home)
    try:
        rebase = into.resolve() != Path("/")
    except OSError:
        rebase = True
    if rebase:
        live = [into / d.relative_to("/") for d in live]
    return [d for d in live if d.is_dir()]


def exec_target(text: str) -> str | None:
    """The program named by the first `Exec=` of a .desktop file's body.

    Returns None when there is nothing to check: no `[Desktop Entry]` group, no
    `Exec=` (Type=Link and Type=Directory entries have none), or a `Hidden=true`
    tombstone, which the spec says to treat as if the entry were deleted.
    """
    entry = _desktop_entry_group(text)
    if entry is None:
        return None
    if entry.get("hidden", "").strip().lower() == "true":
        return None
    value = entry.get("exec")
    if value is None:
        return None
    target = _first_word(value)
    return target or None


def resolves(target: str) -> bool:
    """True if `target` is something this system can actually run.

    A path (anything containing a separator) is tested with os.path.exists so a
    directory target like `/opt/CBD/CHITUBOX_Basic/` is judged on its own terms;
    a bare command goes through $PATH.
    """
    if not target:
        return False
    if "/" in target:
        return os.path.exists(os.path.expanduser(target))
    return shutil.which(target) is not None


def scan(dirs: list[Path]) -> list[Orphan]:
    """Every launcher in `dirs` whose Exec= program is missing.

    Exec targets are always tested against the LIVE filesystem, even when the
    .desktop file itself is a staged copy: the target is an absolute path that
    has to exist wherever the launcher ends up being clicked.
    """
    orphans: list[Orphan] = []
    for path in _entries_in_precedence_order(dirs):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue  # unreadable is not our business; a warning must not fail
        try:
            target = exec_target(text)
        except Exception:  # noqa: BLE001 - a malformed entry is not a restore error
            continue
        if target is None or resolves(target):
            continue
        entry = _desktop_entry_group(text) or {}
        orphans.append(Orphan(desktop=path,
                              name=entry.get("name") or path.stem,
                              target=target))
    return sorted(orphans, key=lambda o: str(o.desktop))


# ---------- parsing ----------

def _entries_in_precedence_order(dirs: list[Path]) -> list[Path]:
    """*.desktop across `dirs`, with earlier dirs shadowing later ones by
    basename — the XDG lookup rule. Without this a /usr/share entry that the
    user has already overridden with a working ~/.local copy gets reported as
    broken when nothing is broken."""
    chosen: dict[str, Path] = {}
    for d in dirs:
        try:
            found = sorted(d.glob("*.desktop"))
        except OSError:
            continue
        for path in found:
            chosen.setdefault(path.name, path)
    return list(chosen.values())


def _desktop_entry_group(text: str) -> dict[str, str] | None:
    """The `[Desktop Entry]` group as {lowercased key: value}, first key wins.

    Hand-rolled rather than configparser: .desktop files in the wild carry
    duplicate keys, locale suffixes (`Name[de]=`) and `%` field codes that
    configparser either rejects or interpolates.
    """
    entry: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if entry is not None:
                break  # a later group ends the one we want
            if line.rstrip().lower() == "[desktop entry]":
                entry = {}
            continue
        if entry is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if "[" in key:
            continue  # a localised variant, e.g. Name[de]
        entry.setdefault(key.lower(), value.strip())
    return entry


def _first_word(value: str) -> str:
    """The program out of an `Exec=` value, honouring quotes.

    The spec allows the program to be double-quoted, which is how a path with
    spaces is written. Splitting on whitespace instead turns
    `"/home/kim/.local/share/Embodme/Erae Lab/erae-lab" %U` into three
    fragments and reports all of them as missing — three false positives from
    one healthy launcher.
    """
    value = value.strip()
    if not value:
        return ""
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = []
    if parts:
        return parts[0]
    # Unbalanced quote: shlex refused, so take the quoted run by hand rather
    # than fall back to a whitespace split that would re-introduce the bug.
    if value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split()[0]
