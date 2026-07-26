#!/usr/bin/env python3
"""Verify that every place the version is written agrees — or sync them.

The version lives in FIVE places, and they have drifted apart on nearly every
release this project has cut:

  * v1.5.1-v1.5.3 shipped with `__version__` stuck at 1.5.0. Not cosmetic:
    worker.py stamps it into archive provenance via `created_by`, so those
    archives permanently misreport what wrote them.
  * v1.5.4 fixed that BY HAND, with no guard, so nothing stopped it recurring.
  * v1.6.0 found README's status line stale at v1.5.3.
  * v1.6.1 found the README install example still telling people to install
    v1.4.3 — three months out of date.

`debian/changelog` is the authority, not an equal peer: dpkg derives the actual
package version from it (`dpkg-parsechangelog`), so if anything disagrees with
the changelog it is the other file that is wrong. That makes `--sync` safe and
one-directional — it never edits the changelog, because the changelog stanza is
the human-authored part of a release.

Usage:
    check-versions.py            # verify; exit 1 on any mismatch
    check-versions.py --sync     # rewrite the other four to match the changelog
    check-versions.py --quiet    # only speak up on failure (for the build)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A site is one place a version string appears: how to find it, and how to
# rewrite it. `pattern` must have exactly one capturing group — the version.
@dataclass
class Site:
    name: str
    path: Path
    pattern: re.Pattern
    description: str

    def read(self) -> tuple[str | None, int | None]:
        """Return (version, 1-based line number), or (None, None) if not found."""
        try:
            text = self.path.read_text()
        except OSError:
            return None, None
        m = self.pattern.search(text)
        if not m:
            return None, None
        line = text[: m.start()].count("\n") + 1
        return m.group(1), line

    def write(self, version: str) -> bool:
        """Rewrite this site to `version`. Returns True if the file changed."""
        text = self.path.read_text()
        m = self.pattern.search(text)
        if not m:
            return False
        # Replace only the captured group, preserving everything around it.
        start, end = m.span(1)
        updated = text[:start] + version + text[end:]
        if updated == text:
            return False
        self.path.write_text(updated)
        return True


SEMVER = r"(\d+\.\d+\.\d+)"

# debian/changelog is deliberately FIRST and is treated as the source of truth.
SITES = [
    Site("debian/changelog", REPO / "debian/changelog",
         re.compile(r"^timetraveller \(" + SEMVER + r"\)", re.MULTILINE),
         "top stanza — dpkg derives the package version from this"),
    Site("timetraveller/__init__.py", REPO / "timetraveller/__init__.py",
         re.compile(r'^__version__ = "' + SEMVER + r'"', re.MULTILINE),
         "stamped into archive provenance via created_by"),
    Site("pyproject.toml", REPO / "pyproject.toml",
         re.compile(r'^version = "' + SEMVER + r'"', re.MULTILINE),
         "project metadata"),
    Site("README.md (status)", REPO / "README.md",
         re.compile(r"^Status: \*\*v" + SEMVER + r"\*\*", re.MULTILINE),
         "the status line readers see first"),
    Site("README.md (install example)", REPO / "README.md",
         re.compile(r"timetraveller_" + SEMVER + r"_all\.deb"),
         "the command users copy/paste to install"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sync", action="store_true",
                    help="rewrite the other sites to match debian/changelog")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless something is wrong")
    args = ap.parse_args()

    authority, _ = SITES[0].read()
    if authority is None:
        print(f"ERROR: could not read a version from {SITES[0].name}; "
              f"cannot determine the intended release version.", file=sys.stderr)
        return 2

    readings = [(s, *s.read()) for s in SITES]
    mismatched = [(s, v) for s, v, _ln in readings if v != authority]

    if args.sync:
        changed = []
        for site, version, _ln in readings:
            if site is SITES[0] or version == authority:
                continue
            if site.write(authority):
                changed.append((site, version))
        if not changed:
            if not args.quiet:
                print(f"All version sites already agree at {authority}.")
            return 0
        print(f"Synced to {authority} (from {SITES[0].name}):")
        for site, was in changed:
            print(f"  {site.name}: {was or '(not found)'} -> {authority}")
        return 0

    if not mismatched:
        if not args.quiet:
            print(f"Version agreement OK — all {len(SITES)} sites at {authority}.")
        return 0

    print(f"ERROR: version sites disagree. {SITES[0].name} says "
          f"{authority}, so that is the intended release version.",
          file=sys.stderr)
    print(file=sys.stderr)
    for site, version, line in readings:
        mark = "  " if version == authority else "->"
        where = f"{site.path.relative_to(REPO)}:{line}" if line else \
                f"{site.path.relative_to(REPO)}"
        shown = version or "NOT FOUND"
        print(f" {mark} {shown:12} {where:34} {site.description}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"Fix them all with:  python3 tools/check-versions.py --sync",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
