"""Manifest of archives for a plan.

Each plan has a manifest.json in its archive directory tracking every archive
TimeTraveller has written. The manifest is authoritative for retention
decisions; the filesystem is checked at read time to surface orphans.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


@dataclass
class ArchiveEntry:
    filename: str               # e.g., "2026-05-17_full.pax.zst"
    kind: str                   # "full" | "incr"
    cycle_id: str               # ISO date of this cycle's full, e.g., "2026-05-17"
    date_started: str           # ISO 8601 timestamp
    date_finished: str          # ISO 8601 timestamp; "" if still running
    size_bytes: int
    status: str                 # "ok" | "failed" | "in-progress"
    hostname: str
    plan_name: str
    incr_window_from: str = ""  # ISO 8601, for incremental archives
    incr_window_to: str = ""    # ISO 8601, for incremental archives
    file_count: int | None = None
    notes: str = ""
    has_sidecar: bool = False   # True if the .idx.zst sidecar exists for this archive
    has_frames: bool = False    # True if the .frames.json sidecar exists (framed-zstd archive)


@dataclass
class Manifest:
    plan_name: str
    schema_version: int = SCHEMA_VERSION
    archives: list[ArchiveEntry] = field(default_factory=list)

    def append(self, entry: ArchiveEntry) -> None:
        self.archives.append(entry)

    def update_status(self, filename: str, *, status: str, date_finished: str = "",
                      size_bytes: int | None = None, file_count: int | None = None) -> None:
        for a in self.archives:
            if a.filename == filename:
                a.status = status
                if date_finished:
                    a.date_finished = date_finished
                if size_bytes is not None:
                    a.size_bytes = size_bytes
                if file_count is not None:
                    a.file_count = file_count
                return
        raise KeyError(f"no manifest entry for {filename!r}")

    def remove(self, filename: str) -> None:
        self.archives = [a for a in self.archives if a.filename != filename]


@dataclass
class Cycle:
    """A full backup plus all incrementals taken before the next successful full."""
    cycle_id: str
    full: ArchiveEntry | None              # None if the full failed or is missing
    incrementals: list[ArchiveEntry]

    @property
    def archives(self) -> list[ArchiveEntry]:
        return ([self.full] if self.full else []) + self.incrementals

    @property
    def is_complete(self) -> bool:
        """A cycle is complete iff it has a successful full backup.

        "ok-with-warnings" counts as successful — the archive is trustworthy
        (pax exit 1 is non-fatal warnings on the walk, not on the stream).
        """
        return self.full is not None and self.full.status in ("ok", "ok-with-warnings")

    @property
    def total_size(self) -> int:
        return sum(a.size_bytes for a in self.archives if a.size_bytes)


def cycles(manifest: Manifest) -> list[Cycle]:
    """Group archives into cycles.

    Rule: a successful full opens a new cycle. Incrementals attach to the most
    recent open cycle. A failed full does NOT open a new cycle — subsequent
    incrementals stay attached to the previous one. This keeps the restore
    chain intact when a full backup fails.

    Returned cycles are sorted oldest-first.
    """
    sorted_archives = sorted(manifest.archives, key=lambda a: a.date_started)

    result: list[Cycle] = []
    current: Cycle | None = None

    for a in sorted_archives:
        if a.kind == "full":
            if a.status in ("ok", "ok-with-warnings"):
                # Successful full — open a new cycle.
                current = Cycle(cycle_id=a.cycle_id, full=a, incrementals=[])
                result.append(current)
            else:
                # Failed full — don't open a new cycle. Record it on the
                # current cycle as a failed full for visibility, but don't
                # replace the cycle's full backup.
                if current is None:
                    # Failed full with no prior cycle. Create a stub cycle
                    # marked incomplete so the GUI can surface the failure.
                    current = Cycle(cycle_id=a.cycle_id, full=None, incrementals=[])
                    result.append(current)
                current.incrementals.append(a)
        else:  # incr
            if current is None:
                # Stray incremental with no prior full. Park it in a stub
                # cycle so it doesn't disappear.
                current = Cycle(cycle_id=a.cycle_id, full=None, incrementals=[])
                result.append(current)
            current.incrementals.append(a)

    return result


def load(path: Path) -> Manifest:
    """Load a manifest, returning an empty one if the file is missing."""
    if not path.exists():
        # Caller will fill in plan_name on first append; use empty placeholder.
        return Manifest(plan_name="")
    with open(path) as f:
        data = json.load(f)
    archives = [ArchiveEntry(**a) for a in data.get("archives", [])]
    return Manifest(
        plan_name=data.get("plan_name", ""),
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        archives=archives,
    )


def save(manifest: Manifest, path: Path) -> None:
    """Atomically save a manifest to JSON (tempfile + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = {
        "plan_name": manifest.plan_name,
        "schema_version": manifest.schema_version,
        "archives": [asdict(a) for a in manifest.archives],
    }
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def manifest_path(archive_dir: Path) -> Path:
    return archive_dir / MANIFEST_NAME


def mirror_manifest_path(plan_name: str) -> Path:
    """Local-disk mirror of the on-mount manifest, used by browse paths
    that must never block on the NFS mount.

    The on-mount file remains authoritative for backup writes; the mirror
    is rewritten alongside it (see worker._save_manifest).
    """
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(xdg) / "timetraveller" / plan_name / MANIFEST_NAME
