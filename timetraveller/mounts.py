"""Mount enumeration and classification.

Used to decide which filesystems get backed up. Defaults are conservative:
remote (NFS/CIFS) and removable filesystems are excluded unless explicitly
opted in. Pseudo filesystems are always excluded.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Anything in this set is a kernel pseudo-filesystem with no real on-disk data.
PSEUDO_FSTYPES = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "debugfs", "securityfs", "bpf", "pstore", "fusectl", "configfs",
    "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "ramfs", "nsfs",
    "rpc_pipefs", "tracefs", "efivarfs", "fuse.gvfsd-fuse", "fuse.portal",
    "fuse.gvfs-fuse-daemon", "fuse.snapfuse", "squashfs",
})

NFS_FSTYPES = frozenset({"nfs", "nfs4"})
CIFS_FSTYPES = frozenset({"cifs", "smbfs", "smb3"})
# Filesystems typical of removable media when mounted under /media or /mnt.
REMOVABLE_FSTYPES = frozenset({"vfat", "exfat", "ntfs", "ntfs3", "udf", "iso9660"})

# Mountpoint prefixes that conventionally hold removable media.
REMOVABLE_PREFIXES = ("/media/", "/mnt/", "/run/media/")


@dataclass(frozen=True)
class Mount:
    target: str        # mountpoint
    source: str        # device or remote spec
    fstype: str
    options: str


@dataclass
class ClassifiedMount:
    mount: Mount
    kind: str          # local | nfs | cifs | removable | pseudo | destination
    is_destination: bool = False

    @property
    def target(self) -> str:
        return self.mount.target

    @property
    def fstype(self) -> str:
        return self.mount.fstype


def list_mounts() -> list[Mount]:
    """Enumerate all mounted filesystems via findmnt."""
    out = subprocess.run(
        ["findmnt", "-J", "-l", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    return [
        Mount(
            target=fs["target"],
            source=fs.get("source", ""),
            fstype=fs.get("fstype", ""),
            options=fs.get("options", ""),
        )
        for fs in data.get("filesystems", [])
    ]


def _is_removable_block_device(source: str) -> bool:
    """Best-effort check via /sys/block/<dev>/removable."""
    if not source.startswith("/dev/"):
        return False
    name = source.removeprefix("/dev/")
    # strip partition suffix: sda1 -> sda, nvme0n1p1 -> nvme0n1
    while name and name[-1].isdigit():
        name = name[:-1]
    if name.endswith("p"):
        name = name[:-1]
    p = Path(f"/sys/block/{name}/removable")
    try:
        return p.read_text().strip() == "1"
    except OSError:
        return False


def classify(mount: Mount, destination_mountpoint: str | None = None) -> ClassifiedMount:
    """Determine the kind of a single mount."""
    fs = mount.fstype
    if fs in PSEUDO_FSTYPES or fs.startswith("fuse."):
        return ClassifiedMount(mount, "pseudo")
    if fs in NFS_FSTYPES:
        return ClassifiedMount(mount, "nfs")
    if fs in CIFS_FSTYPES:
        return ClassifiedMount(mount, "cifs")

    is_removable = False
    if fs in REMOVABLE_FSTYPES and mount.target != "/":
        is_removable = True
    elif any(mount.target.startswith(p) for p in REMOVABLE_PREFIXES) and mount.target != "/":
        # Anything mounted under /media or /run/media is treated as removable.
        # /mnt is more ambiguous; check whether the block device is flagged.
        if mount.target.startswith(("/media/", "/run/media/")):
            is_removable = True
        elif _is_removable_block_device(mount.source):
            is_removable = True
    if is_removable:
        return ClassifiedMount(mount, "removable")

    cm = ClassifiedMount(mount, "local")
    if destination_mountpoint and mount.target == destination_mountpoint:
        cm.is_destination = True
        cm.kind = "destination"
    return cm


def mountpoint_for(path: str) -> str:
    """Return the longest mountpoint that is a prefix of `path`."""
    p = Path(path).resolve()
    while not p.is_mount() and p != p.parent:
        p = p.parent
    return str(p)


def classify_all(destination: str | None = None) -> list[ClassifiedMount]:
    """List + classify every mount on the system."""
    dest_mp = mountpoint_for(destination) if destination else None
    return [classify(m, dest_mp) for m in list_mounts()]


# ---------- filtering for backup ----------

@dataclass
class FilterReport:
    """Result of applying mount filtering to a plan's sources."""
    excluded_mounts: list[ClassifiedMount]      # excluded automatically
    included_mounts: list[ClassifiedMount]      # explicitly included via config
    destination_mount: ClassifiedMount | None
    additional_excludes: list[str]              # mount target paths to add to pax excludes


def filter_sources(
    sources: list[str],
    destination: str,
    *,
    include_removable: bool = False,
    include_nfs: bool = False,
    include_cifs: bool = False,
    include_mounts: list[str] | None = None,
    exclude_mounts: list[str] | None = None,
) -> FilterReport:
    """Compute which mounts under the source tree should be excluded.

    The returned `additional_excludes` is a list of mountpoint paths that the
    pax invocation should exclude (in addition to the user's static excludes).
    """
    include_mounts = include_mounts or []
    exclude_mounts = exclude_mounts or []

    all_mounts = classify_all(destination)
    dest_mount = next((m for m in all_mounts if m.is_destination), None)

    # Roots from which we'd descend during backup. A mount is "under" the
    # backup tree if its target starts with any source path.
    src_paths = [str(Path(s).resolve()) for s in sources]

    excluded: list[ClassifiedMount] = []
    included: list[ClassifiedMount] = []
    extra: list[str] = []

    for cm in all_mounts:
        target = cm.target
        # Only consider mounts that are nested under one of our sources.
        under_source = any(
            target == sp or target.startswith(sp.rstrip("/") + "/")
            for sp in src_paths
        )
        if not under_source:
            continue

        # The source root itself is always included (we'd be backing up nothing
        # otherwise). Skip the root mount in this loop.
        if target in src_paths:
            continue

        # Explicit user decisions win.
        if target in exclude_mounts:
            excluded.append(cm)
            extra.append(target)
            continue
        if target in include_mounts:
            included.append(cm)
            continue

        # The destination itself is always excluded — never back up onto itself.
        if cm.is_destination:
            excluded.append(cm)
            extra.append(target)
            continue

        if cm.kind == "pseudo":
            excluded.append(cm)
            extra.append(target)
            continue
        if cm.kind == "nfs" and not include_nfs:
            excluded.append(cm)
            extra.append(target)
            continue
        if cm.kind == "cifs" and not include_cifs:
            excluded.append(cm)
            extra.append(target)
            continue
        if cm.kind == "removable" and not include_removable:
            excluded.append(cm)
            extra.append(target)
            continue

        # Local non-special mount, not explicitly opted out: include it.
        included.append(cm)

    return FilterReport(
        excluded_mounts=excluded,
        included_mounts=included,
        destination_mount=dest_mount,
        additional_excludes=extra,
    )


def find_nested_mounts(sources: list[str]) -> list[ClassifiedMount]:
    """Mounts whose target is strictly beneath any source path.

    Used by the GUI to surface "hidden" filesystems users might not know are
    nested under their sources — VM stores, removable USB drives temporarily
    mounted in /home, /boot/efi nested under /, etc. With pax `-X` they'd be
    silently skipped; this gives the user a chance to opt in.

    Pseudo filesystems are filtered out — they're never backup-relevant.
    The destination mount is also filtered (we'd never want to back it up).
    """
    src_paths: list[str] = []
    for s in sources:
        try:
            src_paths.append(str(Path(s).resolve()))
        except (OSError, RuntimeError):
            continue
    all_mounts = classify_all()
    nested: list[ClassifiedMount] = []
    for cm in all_mounts:
        if cm.kind == "pseudo" or cm.is_destination:
            continue
        target = cm.target
        for sp in src_paths:
            sp_pfx = sp.rstrip("/") + "/"
            if target != sp and target.startswith(sp_pfx):
                nested.append(cm)
                break
    nested.sort(key=lambda cm: cm.target)
    return nested


def format_report(report: FilterReport) -> str:
    """Pretty-print mount classification for --show-mounts."""
    lines = []
    if report.destination_mount:
        m = report.destination_mount
        lines.append(f"destination : {m.target}  ({m.fstype})  source={m.mount.source}")
    if report.included_mounts:
        lines.append("\nincluded under backup:")
        for cm in report.included_mounts:
            lines.append(f"  {cm.kind:10s} {cm.target:40s}  {cm.fstype}")
    if report.excluded_mounts:
        lines.append("\nexcluded from backup:")
        for cm in report.excluded_mounts:
            lines.append(f"  {cm.kind:10s} {cm.target:40s}  {cm.fstype}")
    return "\n".join(lines)
