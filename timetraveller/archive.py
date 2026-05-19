"""Archive index parsing, enumeration, and extraction.

This module is the data-layer for the GUI's archive browser. It is Qt-free
so it can be tested without spinning up QApplication.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import index as indexlib
from . import manifest as manifestlib


# ---------- index parser ----------

# `tar -tv` output line. Looks like:
#   drwxrwxr-x kim/kim           0 2026-05-18 04:52 ./tmp/tt-source/
#   -rw-rw-r-- kim/kim          16 2026-05-18 04:52 ./tmp/tt-source/regular.txt
#   lrwxrwxr-x kim/kim           0 2026-05-18 04:52 ./home/kim/link -> ./target
# Differences from paxmirabilis `pax -v` style:
#   - owner/group joined with a slash; no separate nlink column
#   - ISO date YYYY-MM-DD plus HH:MM (no month-name form)
#   - directories listed with a trailing `/`
# The path field may contain spaces; runs to end of line, possibly with a
# ` -> target` symlink suffix.
_LINE_RE = re.compile(
    r"^(?P<perms>[-dlspbcDC?][rwxstSTugptT-]{9})\s+"
    r"(?P<owner>[^/\s]+)/(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<path>\..*)$"
)


@dataclass
class IndexNode:
    """One member in a parsed archive index. Directories have children."""
    name: str
    full_path: str            # e.g. "./home/kim/foo.txt"
    is_dir: bool
    size: int = 0
    mtime: str = ""           # raw "May 17 20:50" or "May 17 2024" form from pax -tv
    perms: str = ""           # e.g. "-rwsr-x---"
    owner: str = ""
    group: str = ""
    symlink_target: str = ""  # populated for symlinks
    children: dict[str, "IndexNode"] = field(default_factory=dict)

    def sorted_children(self) -> list["IndexNode"]:
        """Directories first, then alphabetical."""
        return sorted(
            self.children.values(),
            key=lambda n: (not n.is_dir, n.name.lower()),
        )

    def total_entries(self) -> int:
        n = 1
        for c in self.children.values():
            n += c.total_entries()
        return n


def parse_index(text: str) -> IndexNode:
    """Parse the contents of a .idx.zst sidecar (decompressed) into a tree.

    Robust to:
      - Directory entries that appear in any order relative to their children.
        (We synthesize a placeholder dir node when we see a child first.)
      - The trailing `pax: ustar vol 1, N files, ...` line.
      - Symlink targets after ` -> `.
      - Filenames containing spaces.
    """
    return parse_index_lines(text.splitlines())


def parse_index_lines(lines: list[str]) -> IndexNode:
    root = IndexNode(name="", full_path=".", is_dir=True)
    for raw in lines:
        line = raw.rstrip("\n")
        if not line or line.startswith("tar:") or line.startswith("pax:"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue

        perms = m.group("perms")
        is_dir = perms.startswith("d")
        is_symlink = perms.startswith("l")
        path = m.group("path")
        symlink_target = ""
        if is_symlink and " -> " in path:
            path, _, symlink_target = path.partition(" -> ")
            path = path.rstrip()

        # Skip the bare root entry if it ever appears.
        if path in (".", "./"):
            continue
        # tar -tv prints directories with a trailing slash; strip it so paths
        # are consistent with file entries.
        if path.endswith("/") and len(path) > 1:
            path = path[:-1]

        # Trim a leading "./" so we operate on "home/kim/foo.txt".
        rel = path[2:] if path.startswith("./") else path.lstrip("/")
        parts = rel.split("/")
        if not parts or parts == [""]:
            continue

        # Walk/create parent dirs.
        node = root
        for i, part in enumerate(parts[:-1]):
            child = node.children.get(part)
            if child is None:
                child = IndexNode(
                    name=part,
                    full_path="./" + "/".join(parts[: i + 1]),
                    is_dir=True,
                )
                node.children[part] = child
            elif not child.is_dir:
                # Conflicting entry — treat as a directory.
                child.is_dir = True
            node = child

        leaf = parts[-1]
        size = int(m.group("size") or "0")
        mtime = f"{m.group('date')} {m.group('time')}"
        owner = m.group("owner")
        group = m.group("group")

        if leaf in node.children:
            # Filled in by a child first; now we have its metadata.
            existing = node.children[leaf]
            existing.size = size
            existing.mtime = mtime
            existing.perms = perms
            existing.owner = owner
            existing.group = group
            existing.symlink_target = symlink_target
            if is_dir:
                existing.is_dir = True
        else:
            node.children[leaf] = IndexNode(
                name=leaf,
                full_path=path,
                is_dir=is_dir,
                size=size,
                mtime=mtime,
                perms=perms,
                owner=owner,
                group=group,
                symlink_target=symlink_target,
            )
    return root


def load_sidecar_tree(sidecar_path: Path) -> IndexNode:
    """Decompress a .idx.zst sidecar and parse its contents."""
    out = subprocess.run(
        ["zstdcat", str(sidecar_path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return parse_index(out)


# ---------- archive enumeration ----------

@dataclass
class ArchiveListing:
    """The set of archives available for a plan, grouped by cycle."""
    plan_name: str
    archive_dir: Path
    cycles: list["CycleListing"]


@dataclass
class CycleListing:
    cycle_id: str
    is_complete: bool
    full: manifestlib.ArchiveEntry | None
    incrementals: list[manifestlib.ArchiveEntry]

    @property
    def archives(self) -> list[manifestlib.ArchiveEntry]:
        return ([self.full] if self.full else []) + self.incrementals

    @property
    def total_size(self) -> int:
        return sum(a.size_bytes for a in self.archives if a.size_bytes)


def _build_listing(manifest_path: Path, plan_name_fallback: str,
                   archive_dir: Path) -> ArchiveListing:
    """Shared core: load a manifest and shape it into an ArchiveListing.

    Does NOT scan the archive directory — that's discover_orphans's job. By
    keeping this purely manifest-driven, both the mirror-only and the
    on-mount paths share the same cycle-grouping logic.
    """
    m = manifestlib.load(manifest_path)
    cycles_out: list[CycleListing] = []
    for c in manifestlib.cycles(m):
        cycles_out.append(CycleListing(
            cycle_id=c.cycle_id,
            is_complete=c.is_complete,
            full=c.full,
            incrementals=list(c.incrementals),
        ))
    return ArchiveListing(
        plan_name=m.plan_name or plan_name_fallback,
        archive_dir=archive_dir,
        cycles=cycles_out,
    )


def list_from_manifest(plan_name: str, archive_dir: Path) -> ArchiveListing:
    """Build an ArchiveListing from the local mirror manifest only.

    NEVER touches the mount. `archive_dir` is recorded on the result for
    display/extraction-targeting purposes but is not stat'd. Use this from
    any code path that must not block on NFS (interactive CLI, Qt thread).

    Use `list_for_plan` (which composes this with `discover_orphans`) when
    the caller has already opted into mount access.
    """
    mpath = manifestlib.mirror_manifest_path(plan_name)
    return _build_listing(mpath, plan_name, archive_dir)


def discover_orphans(archive_dir: Path) -> list[manifestlib.ArchiveEntry]:
    """Scan the archive directory for .pax.zst files not in the on-mount manifest.

    TOUCHES THE MOUNT. Only call from explicit refresh paths: a CLI
    `--refresh-from-mount`/`--check-orphans` flag, or the GUI's
    MountIOWorker. Casual call sites should not invoke this.
    """
    mpath = manifestlib.manifest_path(archive_dir)
    m = manifestlib.load(mpath)
    known: set[str] = {a.filename for a in m.archives}
    orphans: list[manifestlib.ArchiveEntry] = []
    if archive_dir.exists():
        for f in sorted(archive_dir.glob("*.pax.zst")):
            if f.name in known:
                continue
            orphans.append(manifestlib.ArchiveEntry(
                filename=f.name,
                kind="full" if "_full" in f.name else "incr",
                cycle_id="(orphan)",
                date_started="",
                date_finished="",
                size_bytes=f.stat().st_size,
                status="orphan",
                hostname="",
                plan_name=m.plan_name or "",
            ))
    return orphans


def list_for_plan(archive_dir: Path) -> ArchiveListing:
    """Read the on-mount manifest AND scan for orphans. TOUCHES THE MOUNT.

    Kept as a thin wrapper for callers that have explicitly opted into
    mount access (the GUI's MountIOWorker, the CLI's --refresh-from-mount
    flag). Other call sites should use `list_from_manifest`.
    """
    mpath = manifestlib.manifest_path(archive_dir)
    listing = _build_listing(mpath, "", archive_dir)
    for entry in discover_orphans(archive_dir):
        listing.cycles.append(CycleListing(
            cycle_id="(orphan)",
            is_complete=False,
            full=None,
            incrementals=[entry],
        ))
    return listing


# ---------- extraction ----------

def build_extract_argv(archive_path: Path, paths: list[str],
                       preserve_metadata: bool = True) -> list[list[str]]:
    """Return the argv pieces for `zstdcat <archive> | tar -x [-p] <paths>`.

    We use GNU tar instead of pax to read these archives because they're
    written in POSIX pax-extended-header format; paxmirabilis can't read
    pax-1.0 extended headers correctly.

    Returns two lists (one per stage of the pipeline) so the caller can wire
    them through subprocess.Popen / QProcess as it sees fit.
    """
    zstdcat = ["zstdcat", str(archive_path)]
    tar = ["tar", "-xf", "-"]
    if preserve_metadata:
        # -p / --preserve-permissions: respect owner/group + mode from archive
        # (requires running as root for non-current-uid ownership; tar will
        # warn rather than fail if it can't chown).
        tar.append("-p")
    # Refuse paths that look like options (defence in depth).
    safe_paths: list[str] = []
    for p in paths:
        if not p or p.startswith("-"):
            raise ValueError(f"refusing extraction path {p!r}")
        safe_paths.append(p)
    if safe_paths:
        # `--` prevents subsequent path operands from being parsed as options.
        tar.append("--")
        tar.extend(safe_paths)
    return [zstdcat, tar]


def has_sidecar(archive_path: Path) -> bool:
    return indexlib.sidecar_path(archive_path).exists()
