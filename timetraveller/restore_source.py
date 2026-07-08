"""Self-describing backup locations for config-less, restore-from-anywhere.

A backup directory already carries `manifest.json`, a per-shard `.meta.json`
for every archive, and the `.idx.zst` / `.frames.json` sidecars — together
enough to *list* and *extract* archives with no local config at all. The one
thing missing for a genuinely config-less restore is the plan's identity and
its **original source roots** — where the files came from, i.e. where a
"restore to original location" should put them back.

This module supplies that last piece:

  * `timetraveller.restore.json` — a small descriptor written next to the
    manifest at backup time (and backfilled on `--refresh-from-mount`). It is
    *derived* data: losing it only disables "restore to original location";
    extract-to-a-chosen-directory keeps working from the manifest alone.

  * `discover_backup_locations()` — scan an arbitrary browsed root (a mounted
    USB drive, an NFS/SMB share whose path need not match any local config)
    and report every backup location found beneath it, reading the descriptor
    when present and falling back to the manifest / `.meta.json` sidecars when
    it is not.

Nothing here consults the local config or the local manifest *mirror*: the
browsed directory is the single source of truth, which is exactly what makes
this immune to the "configured path doesn't match the mounted path" trap.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import manifest as manifestlib

DESCRIPTOR_NAME = "timetraveller.restore.json"
SCHEMA_VERSION = 1


# ---------- the portable descriptor ----------

@dataclass
class RestoreDescriptor:
    """Plan identity + original source roots, co-located with the archives.

    Deliberately minimal: identity and restore-targeting only. Schedule and
    retention are policy that has no bearing on restore and would only leak
    stale config into the backup store, so they are intentionally excluded.
    """
    plan_name: str
    sources: list[str]                      # original roots = restore-to targets
    hostname: str = ""
    excludes: list[str] = field(default_factory=list)   # informational only
    include_hostname_in_path: bool = True
    schema_version: int = SCHEMA_VERSION
    created_by: str = ""                     # e.g. "timetraveller 1.4.4"
    written_at: str = ""                     # ISO 8601 UTC


def descriptor_path(archive_dir: Path) -> Path:
    return archive_dir / DESCRIPTOR_NAME


def from_plan(plan, *, created_by: str = "", written_at: str | None = None) -> RestoreDescriptor:
    """Build a descriptor from a live PlanConfig.

    `plan` is a config.PlanConfig; typed loosely to avoid a hard import cycle
    (config imports nothing from here, but keeping the coupling one-way is
    cheap insurance).
    """
    when = written_at if written_at is not None else datetime.now(timezone.utc).isoformat()
    return RestoreDescriptor(
        plan_name=plan.plan_name,
        sources=list(plan.sources),
        hostname=os.uname().nodename,
        excludes=list(getattr(plan, "excludes", []) or []),
        include_hostname_in_path=bool(plan.include_hostname_in_path),
        created_by=created_by,
        written_at=when,
    )


def write_descriptor(archive_dir: Path, descriptor: RestoreDescriptor) -> None:
    """Atomically write the descriptor into an archive directory (tmp+rename).

    Raises OSError on failure — callers that treat the descriptor as best-effort
    (the backup path) should catch and log rather than fail the backup.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = descriptor_path(archive_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(asdict(descriptor), f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def read_descriptor(path: Path) -> RestoreDescriptor | None:
    """Parse a descriptor file, or None if missing/unreadable/foreign.

    Tolerant by design: a bad or future-schema descriptor must never abort a
    restore-browse — the caller degrades to manifest-only discovery instead.
    Unknown keys are dropped so a newer-schema file still yields what we can use.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "plan_name" not in data:
        return None
    known = {f_.name for f_ in RestoreDescriptor.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in data.items() if k in known}
    try:
        return RestoreDescriptor(**kwargs)
    except TypeError:
        return None


# ---------- discovery ----------

@dataclass
class BackupLocation:
    """One backup directory discovered under a browsed root."""
    archive_dir: Path
    plan_name: str
    hostname: str
    sources: list[str]          # restore-to targets; [] when unknown (no descriptor)
    has_descriptor: bool
    n_archives: int             # count of archive files present on disk


def _is_archive_dir(d: Path) -> bool:
    """True if `d` holds backup archives — a manifest, a descriptor, or any
    `.pax.zst` / `.meta.json` file. Cheap: stops at the first hit."""
    if descriptor_path(d).exists() or manifestlib.manifest_path(d).exists():
        return True
    try:
        for entry in os.scandir(d):
            if entry.is_file() and (entry.name.endswith(".pax.zst")
                                    or entry.name.endswith(manifestlib.META_SUFFIX)):
                return True
    except OSError:
        return False
    return False


def _location_from_dir(d: Path) -> BackupLocation:
    """Describe one archive directory, preferring the descriptor, then the
    manifest, then the `.meta.json` sidecars, for identity/sources."""
    desc = read_descriptor(descriptor_path(d))
    plan_name = ""
    hostname = ""
    sources: list[str] = []
    if desc is not None:
        plan_name = desc.plan_name
        hostname = desc.hostname
        sources = list(desc.sources)

    if not plan_name:
        # Fall back to the manifest, then to a meta-rebuilt manifest. Note
        # manifest_from_meta leaves the Manifest.plan_name empty (it's a header
        # field), so recover the name from an entry when the header is blank.
        m = manifestlib.load(manifestlib.manifest_path(d))
        if not m.plan_name and not m.archives:
            m = manifestlib.manifest_from_meta(d, "")
        plan_name = m.plan_name or (m.archives[0].plan_name if m.archives else "")
        if not hostname and m.archives:
            hostname = m.archives[0].hostname

    n_archives = len(list(d.glob("*.pax.zst")))
    return BackupLocation(
        archive_dir=d,
        plan_name=plan_name,
        hostname=hostname,
        sources=sources,
        has_descriptor=desc is not None,
        n_archives=n_archives,
    )


def discover_backup_locations(root: Path, *, max_depth: int = 3) -> list[BackupLocation]:
    """Find every backup location at or beneath `root`, up to `max_depth`.

    The browsed root may itself be an archive directory, or the destination
    root that nests `<hostname>/<plan>/`, or anything in between — so we walk a
    few levels rather than assuming a fixed layout. A directory that qualifies
    as an archive dir is NOT descended into (its `.pax.zst` files aren't
    sub-locations). Results are sorted by (hostname, plan_name, path).

    TOUCHES THE BROWSED PATH (stat/scandir/glob) — the caller has explicitly
    opted into mount access by browsing here, so this is expected. It never
    reads archive bodies, only tiny sidecars/metadata.
    """
    root = Path(root)
    found: list[BackupLocation] = []
    seen: set[Path] = set()

    def walk(d: Path, depth: int) -> None:
        try:
            resolved = d.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        if _is_archive_dir(d):
            found.append(_location_from_dir(d))
            return  # don't descend into an archive dir
        if depth >= max_depth:
            return
        try:
            children = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for entry in sorted(children, key=lambda e: e.name):
            walk(Path(entry.path), depth + 1)

    walk(root, 0)
    found.sort(key=lambda loc: (loc.hostname, loc.plan_name, str(loc.archive_dir)))
    return found
