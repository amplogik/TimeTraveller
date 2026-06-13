"""Manifest of archives for a plan.

Each plan has a manifest.json in its archive directory tracking every archive
TimeTraveller has written. The manifest is authoritative for retention
decisions; the filesystem is checked at read time to surface orphans.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
# v2 adds the shard_* fields to ArchiveEntry. v1 manifests load fine (the new
# fields default to the unsharded values) and are upgraded on next save.
SCHEMA_VERSION = 2


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
    # Sharding: one logical backup may be split across N archive files ("shards"),
    # each its own entry. Defaults describe an unsharded backup (one shard of one).
    shard_index: int = 1        # 1-based position within the shard set
    shard_count: int = 1        # total shards in this backup (1 = unsharded)
    shard_group: str = ""       # logical-backup id shared by sibling shards;
                                # "" means derive from the filename (legacy entries)


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


# Matches the ".sIofN" shard suffix that precedes ".pax.zst" in a shard filename.
_SHARD_SUFFIX_RE = re.compile(r"\.s\d+of\d+(?=\.pax\.zst$)")


def _group_id_from_filename(filename: str) -> str:
    """Logical-backup id derived from a filename: strip the optional `.sIofN`
    shard suffix and the `.pax.zst` extension.

    `2026-06-13_full.s2of4.pax.zst` and `2026-06-13_full.pax.zst` both map to
    `2026-06-13_full`.
    """
    name = _SHARD_SUFFIX_RE.sub("", filename)
    if name.endswith(".pax.zst"):
        name = name[: -len(".pax.zst")]
    return name


def group_id_for(entry: ArchiveEntry) -> str:
    """The shard-group key for an entry: its stored `shard_group`, or (for
    legacy entries that predate sharding) one derived from the filename."""
    return entry.shard_group or _group_id_from_filename(entry.filename)


@dataclass
class ShardSet:
    """The N shard archives that make up ONE logical backup. Unsharded backups
    are a set of one. Sibling shards share kind/cycle_id and partition the file
    list with no overlap."""
    group_id: str
    members: list[ArchiveEntry]   # >= 1, sorted by shard_index

    @property
    def representative(self) -> ArchiveEntry:
        return self.members[0]

    @property
    def kind(self) -> str:
        return self.representative.kind

    @property
    def cycle_id(self) -> str:
        return self.representative.cycle_id

    @property
    def shard_count(self) -> int:
        return self.representative.shard_count

    @property
    def date_started(self) -> str:
        return min(m.date_started for m in self.members)

    @property
    def date_finished(self) -> str:
        fins = [m.date_finished for m in self.members if m.date_finished]
        return max(fins) if fins else ""

    @property
    def total_size(self) -> int:
        return sum(m.size_bytes for m in self.members if m.size_bytes)

    @property
    def status(self) -> str:
        """Aggregate status: failed if any shard failed, in-progress if any is
        still running, empty if all shards were empty, warnings if any shard
        had warnings, else ok."""
        sts = {m.status for m in self.members}
        if "failed" in sts:
            return "failed"
        if "in-progress" in sts:
            return "in-progress"
        if sts == {"empty"}:
            return "empty"
        if "ok-with-warnings" in sts:
            return "ok-with-warnings"
        return "ok"

    @property
    def is_complete(self) -> bool:
        """Complete iff EVERY shard succeeded (ok/ok-with-warnings). A single
        failed shard makes the whole logical backup incomplete."""
        return bool(self.members) and all(
            m.status in ("ok", "ok-with-warnings") for m in self.members)


def group_into_sets(entries: list[ArchiveEntry]) -> list[ShardSet]:
    """Group a flat list of entries into shard sets (one logical backup each),
    sorted oldest-first by start time; members within a set sorted by
    shard_index."""
    groups: dict[str, list[ArchiveEntry]] = {}
    for a in entries:
        groups.setdefault(group_id_for(a), []).append(a)
    sets = [ShardSet(group_id=gid, members=sorted(ms, key=lambda m: m.shard_index))
            for gid, ms in groups.items()]
    sets.sort(key=lambda s: s.date_started)
    return sets


def shard_sets(manifest: Manifest) -> list[ShardSet]:
    """Shard sets for an entire manifest. See group_into_sets."""
    return group_into_sets(manifest.archives)


@dataclass
class Cycle:
    """A full backup plus all incrementals taken before the next successful full.

    Internally grouped by shard SET so a full split into N shards counts as one
    logical full. The `full`/`incrementals`/`archives` properties preserve the
    historical entry-level API for callers that predate sharding.
    """
    cycle_id: str
    full_set: ShardSet | None              # None if the full failed or is missing
    incr_sets: list[ShardSet]

    @property
    def full(self) -> ArchiveEntry | None:
        """Representative full entry (lowest shard_index), or None."""
        return self.full_set.representative if self.full_set else None

    @property
    def incrementals(self) -> list[ArchiveEntry]:
        """Flat list of all incremental (and failed-full) shard entries."""
        out: list[ArchiveEntry] = []
        for s in self.incr_sets:
            out.extend(s.members)
        return out

    @property
    def archives(self) -> list[ArchiveEntry]:
        """Every shard entry in the cycle (full + incrementals) — the unit of
        deletion for retention."""
        out: list[ArchiveEntry] = []
        if self.full_set:
            out.extend(self.full_set.members)
        for s in self.incr_sets:
            out.extend(s.members)
        return out

    @property
    def is_complete(self) -> bool:
        """Complete iff the full shard set is present and every shard succeeded.

        "ok-with-warnings" counts as successful — the archive is trustworthy
        (pax exit 1 is non-fatal warnings on the walk, not on the stream).
        """
        return self.full_set is not None and self.full_set.is_complete

    @property
    def total_size(self) -> int:
        return sum(a.size_bytes for a in self.archives if a.size_bytes)


def cycles(manifest: Manifest) -> list[Cycle]:
    """Group shard sets into cycles.

    Rule: a complete full SET opens a new cycle. Incremental sets attach to the
    most recent open cycle. A failed full set does NOT open a new cycle — its
    shards stay attached to the previous one. This keeps the restore chain
    intact when a full backup fails.

    Returned cycles are sorted oldest-first.
    """
    result: list[Cycle] = []
    current: Cycle | None = None

    for s in shard_sets(manifest):
        if s.kind == "full":
            if s.is_complete:
                # Successful full set — open a new cycle.
                current = Cycle(cycle_id=s.cycle_id, full_set=s, incr_sets=[])
                result.append(current)
            else:
                # Failed full set — don't open a new cycle. Record its shards on
                # the current cycle for visibility without replacing the full.
                if current is None:
                    current = Cycle(cycle_id=s.cycle_id, full_set=None, incr_sets=[])
                    result.append(current)
                current.incr_sets.append(s)
        else:  # incr
            if current is None:
                # Stray incremental with no prior full. Park it in a stub cycle.
                current = Cycle(cycle_id=s.cycle_id, full_set=None, incr_sets=[])
                result.append(current)
            current.incr_sets.append(s)

    return result


def load(path: Path) -> Manifest:
    """Load a manifest, returning an empty one if the file is missing."""
    if not path.exists():
        # Caller will fill in plan_name on first append; use empty placeholder.
        return Manifest(plan_name="")
    with open(path) as f:
        data = json.load(f)
    archives = []
    for a in data.get("archives", []):
        entry = ArchiveEntry(**a)
        # Legacy (v1) entries have no shard_group; derive it so grouping works
        # uniformly. shard_index/shard_count default to the unsharded values.
        if not entry.shard_group:
            entry.shard_group = _group_id_from_filename(entry.filename)
        archives.append(entry)
    # Read both v1 and v2; we always re-emit the current format on save.
    return Manifest(
        plan_name=data.get("plan_name", ""),
        schema_version=SCHEMA_VERSION,
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


# ---- per-shard self-describing sidecars (<archive>.meta.json) ----
#
# One serialized ArchiveEntry written next to each shard archive. It carries the
# shard fields, so a bare directory of archives (detached from manifest.json)
# regroups into shard sets and rebuilds into a manifest. Derived data — losing it
# costs nothing; --reindex backfills it.

META_SUFFIX = ".meta.json"


def entry_meta_path(archive_dir: Path, filename: str) -> Path:
    return archive_dir / (filename + META_SUFFIX)


def write_entry_meta(archive_dir: Path, entry: ArchiveEntry) -> None:
    """Atomically write <archive>.meta.json for one shard entry."""
    path = entry_meta_path(archive_dir, entry.filename)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(asdict(entry), f, indent=2)
        f.write("\n")
    tmp.replace(path)


def read_entry_meta(path: Path) -> ArchiveEntry:
    """Parse one <archive>.meta.json into an ArchiveEntry, deriving shard_group
    from the filename for any legacy meta that predates the shard fields."""
    with open(path) as f:
        data = json.load(f)
    entry = ArchiveEntry(**data)
    if not entry.shard_group:
        entry.shard_group = _group_id_from_filename(entry.filename)
    return entry


def manifest_from_meta(archive_dir: Path, plan_name: str) -> Manifest:
    """Rebuild a manifest by scanning <archive_dir> for *.meta.json sidecars.

    Skips unreadable/foreign meta files rather than failing the whole rebuild.
    Entries are grouped/sorted by the normal model when the manifest is read.
    """
    entries: list[ArchiveEntry] = []
    for p in sorted(archive_dir.glob("*" + META_SUFFIX)):
        try:
            entries.append(read_entry_meta(p))
        except (OSError, ValueError, TypeError):
            continue
    return Manifest(plan_name=plan_name, archives=entries)


def mirror_manifest_path(plan_name: str) -> Path:
    """Local-disk mirror of the on-mount manifest, used by browse paths
    that must never block on the NFS mount.

    The on-mount file remains authoritative for backup writes; the mirror
    is rewritten alongside it (see worker._save_manifest).
    """
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(xdg) / "timetraveller" / plan_name / MANIFEST_NAME
