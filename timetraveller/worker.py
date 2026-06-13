"""TimeTraveller backup worker CLI.

This is what cron invokes for scheduled backups, and what the GUI invokes for
manual runs and inspection commands. All actions on a plan go through here.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import archive as archivelib
from . import config as configlib
from . import extract as extractlib
from . import framewriter
from . import index as indexlib
from . import manifest as manifestlib
from . import mounts as mountslib
from . import pax as paxlib
from . import retention as retentionlib
from . import schedule as schedulelib

LIST_FILES_DEFAULT_CAP = 10000

WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ---------- argument parsing ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="timetraveller-backup",
        description="Run, inspect, and maintain TimeTraveller backups.",
    )
    p.add_argument("--plan", required=True,
                   help="Plan name (e.g., home, system). Loads config from "
                        "~/.config/timetraveller/<plan>.yaml or /etc/timetraveller/<plan>.yaml.")
    p.add_argument("--config", type=Path, default=None,
                   help="Use this config file instead of the default lookup.")

    # --kind is a clarifier, not an action. It picks full vs incr for whatever
    # operation runs (backup, dry-run, list-files). Actions below are mutually
    # exclusive among themselves.
    p.add_argument("--kind", choices=("full", "incr", "auto"), default=None,
                   help="What kind of backup to take/simulate. 'auto' decides "
                        "from today's date vs the schedule.")

    actions = p.add_mutually_exclusive_group()
    actions.add_argument("--dry-run", action="store_true",
                         help="Resolve sources and excludes and print the pax command, but don't run.")
    actions.add_argument("--show-mounts", action="store_true",
                         help="Print every mount classified as local/nfs/cifs/removable/pseudo/destination.")
    actions.add_argument("--list-files", action="store_true",
                         help=f"Walk the source tree and print files that would be archived. "
                              f"Caps at {LIST_FILES_DEFAULT_CAP} entries unless --list-files-all is given.")
    actions.add_argument("--list-archives", action="store_true",
                         help="Print archives recorded in the local manifest mirror for this plan. "
                              "Does not touch the backup mount. Pair with --refresh-from-mount on "
                              "first use, or any time the mirror may be stale.")
    actions.add_argument("--reindex", nargs="?", const="*", default=None,
                         help="Regenerate .idx.zst sidecar(s). With no arg, fixes missing sidecars. "
                              "Pass an archive filename to force-regenerate one.")
    actions.add_argument("--finalize-archive", metavar="FILE", default=None,
                         help="Finalize the manifest entry for an archive whose run crashed "
                              "between archive-write and manifest-save. Backfills status, "
                              "date_finished, size_bytes, has_sidecar, has_frames from what's "
                              "on disk. Run --reindex first if the sidecar also needs regenerating.")
    actions.add_argument("--recover-failed", metavar="FILE", default=None,
                         help="Recover a backup marked 'failed' whose archive stream is "
                              "actually intact (e.g. a file vanished mid-walk -> tar exit 2). "
                              "Renames <FILE>.failed back to <FILE>, builds the .idx.zst "
                              "sidecar (which streams the whole archive and so doubles as an "
                              "integrity check), and finalizes the manifest entry to "
                              "'ok-with-warnings'. If the stream is truncated or corrupt the "
                              "archive is re-quarantined and the entry stays 'failed'.")
    actions.add_argument("--verify", type=str, default=None,
                         help="Stream the named archive through pax -r to /dev/null to check integrity.")
    actions.add_argument("--extract", type=str, default=None, metavar="ARCHIVE",
                         help="Restore files or subtrees from the named archive into --into "
                              "(default: cwd). Positional args are paths within the archive; "
                              "a trailing slash means subtree. Uses .idx.zst + .frames.json "
                              "sidecars when available for fast random-access extraction; "
                              "falls back to whole-archive scan otherwise.")
    actions.add_argument("--prune", action="store_true",
                         help="Apply retention without taking a new backup.")
    actions.add_argument("--remove-plan", action="store_true",
                         help="Uninstall the plan's schedule, delete its config file, "
                              "and clear its local mirror state. By default, archive "
                              "files on the backup mount are kept; pass --remove-backups "
                              "to delete them too.")
    actions.add_argument("--switch-to-archive", action="store_true",
                         help="Convert this plan to an Archive plan: prune all "
                              "cycles except the newest, then set schedule.mode=archive "
                              "and retention.policy=keep_all. Destructive; cannot be undone.")
    actions.add_argument("--switch-to-active", action="store_true",
                         help="Convert this plan from Archive to Active: set "
                              "schedule.mode=weekly with default cadence and "
                              "retention.policy=max_cycles. Existing cycles are kept.")
    actions.add_argument("--show-schedule", action="store_true",
                         help="Render the cron block for this plan to stdout (no install).")
    actions.add_argument("--install-schedule", action="store_true",
                         help="Install the cron block. For plan=home: user crontab. "
                              "For plan=system: invokes pkexec to update root's crontab.")
    actions.add_argument("--uninstall-schedule", action="store_true",
                         help="Remove the managed cron block for this plan.")
    actions.add_argument("--suspend-schedule", action="store_true",
                         help="Comment out the cron entries for this plan (block stays installed).")
    actions.add_argument("--resume-schedule", action="store_true",
                         help="Uncomment previously-suspended cron entries for this plan.")

    p.add_argument("--list-files-all", action="store_true",
                   help="With --list-files, remove the cap.")
    p.add_argument("--refresh-from-mount", action="store_true",
                   help="With --list-archives: re-read the on-mount manifest "
                        "and overwrite the local mirror before printing. "
                        "Touches the backup mount.")
    p.add_argument("--check-orphans", action="store_true",
                   help="With --list-archives: also enumerate archive files "
                        "on the mount that are absent from the manifest. "
                        "Touches the backup mount.")
    p.add_argument("--remove-backups", action="store_true",
                   help="With --remove-plan: also unlink every archive file "
                        "and sidecar under the plan's archive directory. "
                        "Destructive; cannot be undone.")
    p.add_argument("--binary-path", type=str, default=None,
                   help="Path to timetraveller-backup to use in cron entries. "
                        "Defaults to /usr/local/bin/timetraveller-backup, or the "
                        "currently-running script if --dev-binary-path is set.")
    p.add_argument("--dev-binary-path", action="store_true",
                   help="Use the running script's resolved path in cron entries "
                        "(home plan only; the pkexec helper rejects non-/usr/local paths).")

    # Per-run overrides.
    p.add_argument("--source", action="append", default=[],
                   help="Add a source path for this run (repeatable).")
    p.add_argument("--exclude", action="append", default=[],
                   help="Add an exclude pattern for this run (repeatable).")
    p.add_argument("--include-mount", action="append", default=[],
                   help="Force-include this mountpoint for this run (repeatable).")
    p.add_argument("--exclude-mount", action="append", default=[],
                   help="Force-exclude this mountpoint for this run (repeatable).")
    p.add_argument("--include-removable", action="store_true",
                   help="Include all removable mounts under sources for this run.")
    p.add_argument("--include-nfs", action="store_true",
                   help="Include NFS mounts under sources for this run.")
    p.add_argument("--include-cifs", action="store_true",
                   help="Include CIFS mounts (other than the destination) for this run.")
    p.add_argument("--destination", type=str, default=None,
                   help="Override the config destination for this run.")
    p.add_argument("--no-retention", action="store_true",
                   help="Skip the retention pass after this run.")
    p.add_argument("--no-framed", action="store_true",
                   help="Disable framed-zstd output for this run. Single-file restore "
                        "from the resulting archive will require a full archive read.")
    p.add_argument("--status", choices=("ok", "ok-with-warnings", "failed"),
                   default="ok-with-warnings",
                   help="With --finalize-archive: status to record on the entry. Default "
                        "'ok-with-warnings' is conservative for crash recovery — pax may "
                        "have emitted non-fatal warnings whose result is no longer available.")
    p.add_argument("--force", action="store_true",
                   help="With --finalize-archive: allow overwriting an entry that already "
                        "has a terminal status (ok / ok-with-warnings / failed / empty). "
                        "With --recover-failed: allow recovering an entry whose status "
                        "isn't 'failed'.")
    p.add_argument("--manual", action="store_true",
                   help="Mark this as a manual (not scheduled) run; uses HHMMSS in the filename "
                        "to avoid colliding with same-day scheduled runs.")
    p.add_argument("--log-file", type=Path, default=None,
                   help="Append pax/zstd stderr to this file.")
    p.add_argument("--into", type=Path, default=None,
                   help="With --extract: destination directory. Defaults to cwd.")
    p.add_argument("paths", nargs="*",
                   help="With --extract: archive paths to restore. "
                        "`./etc/fstab` extracts one file; `./etc/` extracts a subtree.")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


# ---------- helpers ----------

def _load_plan(args: argparse.Namespace) -> configlib.PlanConfig:
    if args.config:
        return configlib.load(args.config)
    path = configlib.resolve_config_path(args.plan)
    return configlib.load(path)


def _effective_plan(args: argparse.Namespace, plan: configlib.PlanConfig) -> configlib.PlanConfig:
    """Apply per-run overrides to a copy of the plan config (additive)."""
    if args.source:
        plan.sources = list(plan.sources) + list(args.source)
    if args.exclude:
        plan.excludes = list(plan.excludes) + list(args.exclude)
    if args.include_mount:
        plan.include_mounts = list(plan.include_mounts) + list(args.include_mount)
    if args.exclude_mount:
        plan.exclude_mounts = list(plan.exclude_mounts) + list(args.exclude_mount)
    if args.include_removable:
        plan.include_removable = True
    if args.include_nfs:
        plan.include_nfs = True
    if args.include_cifs:
        plan.include_cifs = True
    if args.destination:
        plan.destination = args.destination
    return plan


def _is_full_day(plan: configlib.PlanConfig, now: datetime) -> bool:
    """Would today fire a scheduled full backup for this plan?"""
    sch = plan.schedule
    if sch.mode == "weekly":
        return now.strftime("%a").lower()[:3] in sch.full.days
    if sch.mode == "monthly":
        return now.day == sch.full.day_of_month
    return False


def _is_incr_day(plan: configlib.PlanConfig, now: datetime) -> bool:
    """Would today fire a scheduled incremental backup for this plan?"""
    sch = plan.schedule
    weekday = now.strftime("%a").lower()[:3]
    if sch.incr.mode == "disabled":
        return False
    if sch.mode == "weekly":
        if sch.incr.mode == "except_full":
            return weekday not in set(sch.full.days)
        if sch.incr.mode == "weekdays":
            return weekday in sch.incr.days
        return False
    if sch.mode == "monthly":
        if sch.incr.mode == "weekdays":
            return weekday in sch.incr.days
        if sch.incr.mode == "every_n_days":
            n = max(2, int(sch.incr.every_n_days))
            return ((now.day - 1) % n) == 0
        return False
    return False


def _resolve_kind(kind: str | None, plan: configlib.PlanConfig, now: datetime) -> str:
    """Decide full vs incr when --kind=auto (or no kind given). Full wins on
    a day where both schedules would fire."""
    if kind in ("full", "incr"):
        return kind
    if _is_full_day(plan, now):
        return "full"
    if _is_incr_day(plan, now):
        return "incr"
    raise SystemExit(
        f"--kind=auto but today ({now.strftime('%a').lower()}, "
        f"day {now.day}) is not scheduled. Pass --kind full or --kind incr to force."
    )


def _incremental_window(manifest: manifestlib.Manifest,
                        now: datetime) -> tuple[datetime, datetime]:
    """Find the time window for an incremental: last successful backup → now.

    Falls back to yesterday-00:00 → now if no prior successful backup exists.
    """
    # "empty" counts as a successful run for the purposes of windowing: there
    # was simply nothing to archive, but we did check.
    successes = [a for a in manifest.archives
                 if a.status in ("ok", "ok-with-warnings", "empty") and a.date_finished]
    if successes:
        last = max(successes, key=lambda a: a.date_finished)
        frm = datetime.fromisoformat(last.date_finished)
        if frm.tzinfo is None:
            frm = frm.replace(tzinfo=timezone.utc)
        return frm, now
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return yesterday, now


def _log(args: argparse.Namespace, msg: str) -> None:
    if not args.quiet:
        print(msg, flush=True)


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _save_manifest(m: manifestlib.Manifest, archive_dir: Path, plan_name: str) -> None:
    """Persist the manifest to the on-mount location and the local mirror.

    The on-mount write is authoritative — any failure there propagates. The
    mirror write is best-effort: a failure (e.g. XDG_STATE_HOME unwritable)
    is logged but never fails the backup, since the mirror is purely a
    browse-path optimization.
    """
    manifestlib.save(m, manifestlib.manifest_path(archive_dir))
    try:
        manifestlib.save(m, manifestlib.mirror_manifest_path(plan_name))
    except OSError as e:
        print(f"WARNING: manifest mirror write failed: {e}", file=sys.stderr)


def _mirror_sidecar(plan_name: str, source_sidecar: Path,
                    archive_filename: str) -> None:
    """Best-effort copy of an on-mount sidecar into the local mirror.

    Mirror failure is logged but never propagated — the on-mount sidecar
    remains authoritative for restore; the mirror is only for offline browse.
    """
    try:
        indexlib.copy_sidecar_to_mirror(plan_name, source_sidecar, archive_filename)
    except OSError as e:
        print(f"WARNING: sidecar mirror copy failed: {e}", file=sys.stderr)


# ---------- action handlers ----------

def action_show_mounts(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    report = mountslib.filter_sources(
        plan.sources, plan.destination,
        include_removable=plan.include_removable,
        include_nfs=plan.include_nfs,
        include_cifs=plan.include_cifs,
        include_mounts=plan.include_mounts,
        exclude_mounts=plan.exclude_mounts,
    )
    print(mountslib.format_report(report))
    return 0


def action_list_archives(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    archive_dir = plan.archive_dir()

    if args.refresh_from_mount:
        on_mount = manifestlib.load(manifestlib.manifest_path(archive_dir))
        # Backfill has_sidecar / has_frames from disk and seed the local sidecar
        # mirror while we're already touching the mount.
        for entry in on_mount.archives:
            sc = indexlib.sidecar_path(archive_dir / entry.filename)
            entry.has_sidecar = sc.exists()
            if entry.has_sidecar:
                _mirror_sidecar(plan.plan_name, sc, entry.filename)
            entry.has_frames = framewriter.sidecar_path(archive_dir / entry.filename).exists()
        # Try to persist backfilled flags to the on-mount manifest so other
        # machines / users running --refresh-from-mount see them. For a system
        # plan whose archive_dir is owned by root, this write will fail when
        # the GUI user runs without sudo — that's OK, the local mirror is
        # what the GUI actually reads. Skip the on-mount write in that case
        # and update the mirror directly.
        try:
            _save_manifest(on_mount, archive_dir, plan.plan_name)
        except PermissionError:
            print(f"NOTE: on-mount manifest not writable as this user; "
                  f"updating local mirror only.", file=sys.stderr)
            try:
                manifestlib.save(on_mount, manifestlib.mirror_manifest_path(plan.plan_name))
            except OSError as e:
                print(f"WARNING: local mirror update failed: {e}", file=sys.stderr)

    mirror = manifestlib.mirror_manifest_path(plan.plan_name)
    if not mirror.exists():
        print(
            f"No local manifest mirror for plan {plan.plan_name!r} at {mirror}.\n"
            f"Run with --refresh-from-mount once to populate it from the on-mount manifest.",
            file=sys.stderr,
        )
        return 1

    listing = archivelib.list_from_manifest(plan.plan_name, archive_dir)

    if not listing.cycles:
        print(f"No archives recorded for plan {plan.plan_name!r} at {archive_dir}.")
    else:
        total_archives = sum(len(c.archives) for c in listing.cycles)
        print(f"Plan: {plan.plan_name}   Destination: {archive_dir}")
        print(f"Cycles: {len(listing.cycles)}   Archives: {total_archives}")
        for c in listing.cycles:
            status = "complete" if c.is_complete else "INCOMPLETE"
            print(f"\n  Cycle {c.cycle_id} [{status}]  total {c.total_size/1024**2:.1f} MiB")
            for a in c.archives:
                tag = a.status if a.status != "ok" else ""
                tag = f"  [{tag}]" if tag else ""
                print(f"    {a.kind:5s}  {a.filename:50s}  {a.size_bytes/1024**2:9.1f} MiB{tag}")

    if args.check_orphans:
        orphans = archivelib.discover_orphans(archive_dir)
        if orphans:
            print(f"\nOrphans on mount ({len(orphans)} archive(s) not in manifest):")
            for o in orphans:
                print(f"    {o.kind:5s}  {o.filename:50s}  {o.size_bytes/1024**2:9.1f} MiB")
        else:
            print("\nNo orphans on mount.")

    return 0


def action_dry_run(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    now = datetime.now(timezone.utc)
    kind = _resolve_kind(args.kind, plan, now)

    archive_dir = plan.archive_dir()
    fname = paxlib.archive_filename(dt=now, kind=kind, manual=args.manual)
    archive_path = archive_dir / fname

    report = mountslib.filter_sources(
        plan.sources, plan.destination,
        include_removable=plan.include_removable,
        include_nfs=plan.include_nfs,
        include_cifs=plan.include_cifs,
        include_mounts=plan.include_mounts,
        exclude_mounts=plan.exclude_mounts,
    )

    incr_window = None
    if kind == "incr":
        mirror = manifestlib.mirror_manifest_path(plan.plan_name)
        if not mirror.exists():
            print(
                f"No local manifest mirror for plan {plan.plan_name!r} at {mirror}.\n"
                f"Run `--list-archives --refresh-from-mount` once to populate it "
                f"before dry-running an incremental.",
                file=sys.stderr,
            )
            return 1
        m = manifestlib.load(mirror)
        incr_window = _incremental_window(m, now)

    sources_rel, chdir = _relative_sources(plan.sources)

    inv = paxlib.PaxInvocation(
        sources=sources_rel,
        chdir=chdir,
        archive_path=archive_path,
        excludes=plan.excludes,
        extra_mount_excludes=report.additional_excludes,
        incr_window=incr_window,
        compression=plan.compression,
        one_filesystem=True,
        extra_pax_flags=plan.extra_pax_flags,
        framed=plan.framed and not args.no_framed,
    )

    print(f"Plan:        {plan.plan_name}")
    print(f"Kind:        {kind}")
    print(f"Archive:     {archive_path}")
    if incr_window:
        print(f"Incr window: {incr_window[0].isoformat()} .. {incr_window[1].isoformat()}")
    print(f"chdir:       {chdir}")
    print(f"Sources:     {sources_rel}")
    print(f"\nMount filter:")
    print(mountslib.format_report(report))
    print(f"\npax command (would run from cwd={chdir}):")
    print("  " + " ".join(_shell_quote(x) for x in inv.pax_argv()))
    print(f"\nzstd command:")
    print("  " + " ".join(_shell_quote(x) for x in inv.zstd_argv()))
    return 0


def action_list_files(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    # Walk the effective source tree honoring -X (no crossing mount boundaries)
    # and excludes, printing files we'd archive.
    cap = None if args.list_files_all else LIST_FILES_DEFAULT_CAP

    report = mountslib.filter_sources(
        plan.sources, plan.destination,
        include_removable=plan.include_removable,
        include_nfs=plan.include_nfs,
        include_cifs=plan.include_cifs,
        include_mounts=plan.include_mounts,
        exclude_mounts=plan.exclude_mounts,
    )
    skip_targets = set(report.additional_excludes)

    # Compile excludes once. glob_to_regexes returns 1-2 patterns per glob.
    import re as _re
    patterns: list = []
    for g in plan.excludes:
        for rx in paxlib.glob_to_regexes(g):
            patterns.append(_re.compile(rx))

    def excluded(path: str) -> bool:
        for pat in patterns:
            if pat.match(path):
                return True
        return False

    count = 0
    for source in plan.sources:
        src = Path(source).resolve()
        try:
            src_dev = src.stat().st_dev
        except OSError:
            continue
        for root, dirs, files in os.walk(src, followlinks=False):
            # Prune mount-boundary crossings and explicit excludes.
            dirs[:] = sorted(d for d in dirs if not _should_prune(
                root, d, src_dev, skip_targets, excluded))
            for fname in sorted(files):
                full = os.path.join(root, fname)
                rel = "./" + os.path.relpath(full, "/")
                if excluded(rel) or excluded(full):
                    continue
                print(full)
                count += 1
                if cap is not None and count >= cap:
                    print(f"[capped at {cap}; pass --list-files-all to see everything]",
                          file=sys.stderr)
                    return 0
    return 0


def _should_prune(root: str, d: str, src_dev: int, skip_targets: set[str],
                  excluded_fn) -> bool:
    full = os.path.join(root, d)
    if full in skip_targets:
        return True
    try:
        st = os.lstat(full)
    except OSError:
        return True
    if st.st_dev != src_dev:
        return True
    rel = "./" + os.path.relpath(full, "/")
    if excluded_fn(rel) or excluded_fn(full):
        return True
    return False


def action_reindex(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    archive_dir = plan.archive_dir()
    target = args.reindex
    if target == "*":
        archives = sorted(p for p in archive_dir.glob("*.pax.zst"))
        archives = [a for a in archives if not indexlib.sidecar_path(a).exists()]
    else:
        archives = [archive_dir / target]
    if not archives:
        _log(args, "reindex: nothing to do")
        return 0

    m = manifestlib.load(manifestlib.manifest_path(archive_dir))
    by_filename = {a.filename: a for a in m.archives}
    for a in archives:
        _log(args, f"reindex: {a.name}")
        indexlib.write_sidecar(a)
        entry = by_filename.get(a.name)
        if entry is not None:
            entry.has_sidecar = True
        _mirror_sidecar(plan.plan_name, indexlib.sidecar_path(a), a.name)
    _save_manifest(m, archive_dir, plan.plan_name)
    return 0


def action_finalize_archive(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    archive_dir = plan.archive_dir()
    archive_path = archive_dir / args.finalize_archive
    if not archive_path.exists():
        print(f"ERROR: archive not found: {archive_path}", file=sys.stderr)
        return 1

    m = manifestlib.load(manifestlib.manifest_path(archive_dir))
    entry = next((a for a in m.archives if a.filename == archive_path.name), None)
    if entry is None:
        print(f"ERROR: no manifest entry for {archive_path.name}. "
              f"--finalize-archive updates an existing row; it does not create one.",
              file=sys.stderr)
        return 1

    terminal = ("ok", "ok-with-warnings", "failed", "empty")
    if entry.status in terminal and not args.force:
        print(f"ERROR: entry status is already {entry.status!r}. "
              f"Pass --force to overwrite.", file=sys.stderr)
        return 1

    sidecar = indexlib.sidecar_path(archive_path)
    frames = framewriter.sidecar_path(archive_path)

    entry.status = args.status
    entry.date_finished = datetime.now(timezone.utc).isoformat()
    entry.size_bytes = archive_path.stat().st_size
    entry.has_sidecar = sidecar.exists()
    entry.has_frames = frames.exists()

    if entry.has_sidecar:
        _mirror_sidecar(plan.plan_name, sidecar, archive_path.name)

    _save_manifest(m, archive_dir, plan.plan_name)

    _log(args, f"finalize: {archive_path.name}")
    _log(args, f"  status={entry.status}  size={entry.size_bytes/1024**2:.1f} MiB  "
               f"date_finished={entry.date_finished}")
    _log(args, f"  has_sidecar={entry.has_sidecar}  has_frames={entry.has_frames}")
    return 0


def action_recover_failed(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    """Recover a failed backup whose archive stream is actually intact.

    A backup marked 'failed' has its archive renamed to '<name>.failed' on
    disk (see action_backup) while the manifest keeps the bare filename. Some
    failures leave a structurally valid stream — most commonly a file vanishing
    mid-walk, which GNU tar reports as exit 2 even though it skips the member
    and the archive stays sound (the same benign race v1.0.5's status
    classification now tolerates for new runs). Others (truncation, zstd
    corruption) do not.

    We build the .idx.zst sidecar straight off the archive. write_sidecar
    streams the whole thing through tarfile, so a clean completion *is* the
    integrity proof; any read/parse error means the stream is unusable. Only on
    success do we un-quarantine the file and finalize the entry. On failure the
    archive is left (or put back) at its .failed name so the manifest's
    failed-status <-> .failed-on-disk invariant holds for a later retry.
    """
    archive_dir = plan.archive_dir()
    fname = args.recover_failed
    m = manifestlib.load(manifestlib.manifest_path(archive_dir))
    entry = next((a for a in m.archives if a.filename == fname), None)
    if entry is None:
        print(f"ERROR: no manifest entry for {fname!r}. --recover-failed updates "
              f"an existing row; it does not create one.", file=sys.stderr)
        return 1
    if entry.status != "failed" and not args.force:
        print(f"ERROR: entry status is {entry.status!r}, not 'failed'. "
              f"Pass --force to recover anyway.", file=sys.stderr)
        return 1

    bare = archive_dir / fname
    failed = bare.with_suffix(bare.suffix + ".failed")
    # Normally the archive is at <name>.failed; accept a bare file too, so a
    # prior recovery that renamed then crashed before finalize re-runs cleanly.
    if failed.exists():
        failed.rename(bare)
    elif not bare.exists():
        print(f"ERROR: archive not found at {failed} or {bare}.", file=sys.stderr)
        return 1

    _log(args, f"recover: {fname} — verifying + indexing…")
    try:
        indexlib.write_sidecar(bare)
    except Exception as e:  # noqa: BLE001 - any read/parse error means unrecoverable
        # Re-quarantine so manifest(failed) <-> disk(.failed) stays consistent.
        try:
            if bare.exists():
                bare.rename(failed)
        except OSError:
            pass
        print(f"ERROR: {fname} is not recoverable — archive stream is truncated "
              f"or corrupt: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    sidecar = indexlib.sidecar_path(bare)
    _mirror_sidecar(plan.plan_name, sidecar, fname)

    entry.status = "ok-with-warnings"
    entry.date_finished = datetime.now(timezone.utc).isoformat()
    entry.size_bytes = bare.stat().st_size
    entry.has_sidecar = True
    entry.has_frames = framewriter.sidecar_path(bare).exists()
    _save_manifest(m, archive_dir, plan.plan_name)

    _log(args, f"recover: {fname} -> {entry.status}")
    _log(args, f"  size={entry.size_bytes/1024**2:.1f} MiB  "
               f"has_sidecar={entry.has_sidecar}  has_frames={entry.has_frames}")
    return 0


def action_extract(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    archive = plan.archive_dir() / args.extract
    if not archive.exists():
        print(f"archive not found: {archive}", file=sys.stderr)
        return 1
    if not args.paths:
        print("--extract: at least one path argument is required.\n"
              "  Examples:\n"
              "    --extract ARCHIVE ./etc/fstab           # single file\n"
              "    --extract ARCHIVE ./etc/                # subtree\n"
              "    --extract ARCHIVE --into /tmp/r ./var/log/syslog",
              file=sys.stderr)
        return 1
    into = args.into if args.into is not None else Path.cwd()

    stats = extractlib.extract_files(archive, list(args.paths), into=into)

    if stats.matched_files + stats.matched_dirs + stats.matched_symlinks == 0:
        print(f"--extract: no matching entries in {archive.name}", file=sys.stderr)
        return 1

    mode = "naive (whole-archive scan)" if stats.fallback_naive else "fast (sidecar-based)"
    _log(args, f"--extract mode: {mode}")
    _log(args, f"  patterns: {stats.requested_patterns}, "
               f"files: {stats.matched_files}, dirs: {stats.matched_dirs}, "
               f"symlinks: {stats.matched_symlinks}"
               + (f", hardlinks skipped: {stats.matched_hardlinks}"
                  if stats.matched_hardlinks else ""))
    if not stats.fallback_naive:
        _log(args, f"  frames read: {stats.frames_read} "
                   f"({stats.nfs_bytes_read/1024**2:.1f} MiB from archive)")
    _log(args, f"  bytes written: {stats.bytes_written:,}")
    _log(args, f"  elapsed: {_hms(stats.seconds_total)}")
    return 0


def action_verify(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    import subprocess
    archive = plan.archive_dir() / args.verify
    if not archive.exists():
        print(f"archive not found: {archive}", file=sys.stderr)
        return 1
    # Stream the archive through `tar -tf` (list mode) so tar reads every
    # entry header end-to-end without extracting. Any corruption shows up
    # as a non-zero exit code.
    zstdcat = subprocess.Popen(["zstdcat", str(archive)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    tar = subprocess.Popen(["tar", "-tf", "-"], stdin=zstdcat.stdout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    zstdcat.stdout.close()  # type: ignore[union-attr]
    _, err = tar.communicate()
    zstdcat.wait()
    if tar.returncode != 0:
        print(f"verify: archive {archive.name} FAILED", file=sys.stderr)
        if err:
            print(err.decode(errors="replace"), file=sys.stderr)
        return 1
    print(f"verify: archive {archive.name} OK")
    return 0


def action_prune(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    archive_dir = plan.archive_dir()
    mpath = manifestlib.manifest_path(archive_dir)
    m = manifestlib.load(mpath)
    if not m.plan_name:
        m.plan_name = plan.plan_name
    plan_obj = retentionlib.apply(
        m,
        policy=plan.retention.policy,
        max_cycles=plan.retention.max_cycles,
        max_age_days=plan.retention.max_age_days,
        max_size_gb=plan.retention.max_size_gb,
    )
    print(retentionlib.format_plan(plan_obj))
    _delete_cycles(archive_dir, plan.plan_name, plan_obj.delete, m, log=lambda msg: _log(args, msg))
    return 0


def _delete_cycles(archive_dir: Path, plan_name: str,
                   cycles_to_delete: list, manifest: manifestlib.Manifest,
                   log=None) -> None:
    """Delete the given cycles' archive files + sidecars and update the manifest.

    Shared by action_prune and prune_to_newest_cycle. Saves the manifest only
    if at least one cycle was deleted.
    """
    if not cycles_to_delete:
        return
    log = log or (lambda _msg: None)
    for cycle in cycles_to_delete:
        for a in cycle.archives:
            apath = archive_dir / a.filename
            spath = indexlib.sidecar_path(apath)
            for p in (apath, spath):
                if p.exists():
                    log(f"prune: rm {p}")
                    p.unlink()
            indexlib.delete_sidecar_mirror(plan_name, a.filename)
            manifest.remove(a.filename)
    _save_manifest(manifest, archive_dir, plan_name)


def _config_path_for_args(args: argparse.Namespace) -> Path:
    """Resolve the YAML path we should write back to. Mirrors _load_plan."""
    if args.config:
        return Path(args.config)
    return configlib.resolve_config_path(args.plan)


def action_switch_to_archive(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    """Convert an Active plan to an Archive plan.

    Steps:
      1. Prune all cycles except the newest complete one.
      2. Set schedule.mode=archive and retention.policy=keep_all.
      3. Save the YAML back to its source path.

    Destructive — older cycles are physically deleted from disk.
    """
    if plan.schedule.mode == "archive":
        print(f"plan {plan.plan_name!r} is already an Archive plan; nothing to do.")
        return 0

    path = _config_path_for_args(args)
    print(f"switching plan {plan.plan_name!r} to Archive (config: {path})")

    deleted = prune_to_newest_cycle(plan, log=lambda msg: _log(args, msg))
    if deleted:
        n_archives = sum(len(c.archives) for c in deleted)
        print(f"pruned {len(deleted)} older cycle(s) ({n_archives} archive(s)).")
    else:
        print("no older cycles to prune.")

    plan.schedule = configlib.Schedule(mode="archive")
    plan.retention = configlib.Retention(policy="keep_all")
    plan.validate()
    configlib.save(plan, path)
    print(f"saved {path}: schedule.mode=archive, retention.policy=keep_all")
    return 0


def action_switch_to_active(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    """Convert an Archive plan to an Active plan.

    Sets schedule.mode=weekly with the standard defaults and retention.policy=
    max_cycles (max_cycles=4). Existing cycles on disk are preserved; retention
    starts applying from the next prune.
    """
    if plan.schedule.mode != "archive":
        print(f"plan {plan.plan_name!r} is not an Archive plan; nothing to do.")
        return 0

    path = _config_path_for_args(args)
    print(f"switching plan {plan.plan_name!r} to Active (config: {path})")

    plan.schedule = configlib.Schedule()  # defaults: weekly, Sundays 02:00, except_full incrs
    plan.retention = configlib.Retention()  # defaults: max_cycles=4
    plan.validate()
    configlib.save(plan, path)
    print(f"saved {path}: schedule.mode=weekly, retention.policy=max_cycles")
    print("existing cycles preserved. Run --prune or wait for the next scheduled "
          "prune to apply retention.")
    return 0


def action_remove_plan(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    """Remove a plan: uninstall its schedule, clear local state, delete its YAML.

    With --remove-backups, also unlink every archive file + sidecar under the
    plan's archive directory. Without that flag, the on-mount archives are
    preserved (the user can browse to them manually if they reconsider).

    Best-effort throughout: a failure in one step is reported but doesn't stop
    the next step. The function returns 0 if every step succeeded, 1 otherwise.
    """
    import shutil
    plan_name = plan.plan_name
    path = _config_path_for_args(args)
    print(f"removing plan {plan_name!r} (config: {path})")
    if args.remove_backups:
        print("WARNING: --remove-backups is set; archive files will be deleted.")

    errors: list[str] = []

    # 1. Uninstall the schedule. action_uninstall_schedule is a no-op when no
    #    managed block exists, so this is safe even if the user never installed.
    try:
        rc = action_uninstall_schedule(args, plan)
        if rc != 0:
            errors.append(f"schedule uninstall returned exit {rc}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"schedule uninstall: {e}")

    # 2. Optionally delete the on-mount archive files. Touches the backup mount.
    if args.remove_backups:
        archive_dir = plan.archive_dir()
        if archive_dir.exists():
            try:
                n_files = 0
                for child in sorted(archive_dir.iterdir()):
                    if child.is_file():
                        child.unlink()
                        n_files += 1
                # Remove the now-empty plan dir. We do NOT bubble up to the host
                # dir — other plans may still live under it.
                try:
                    archive_dir.rmdir()
                except OSError as e:
                    print(f"note: could not remove {archive_dir}: {e}", file=sys.stderr)
                print(f"deleted {n_files} archive file(s) from {archive_dir}")
            except OSError as e:
                errors.append(f"archive deletion: {e}")
        else:
            print(f"no archive directory at {archive_dir}; nothing to delete.")

    # 3. Clear the local mirror state (manifest mirror, sidecar mirror, log).
    mirror_dir = manifestlib.mirror_manifest_path(plan_name).parent
    if mirror_dir.exists():
        try:
            shutil.rmtree(mirror_dir)
            print(f"cleared local mirror state at {mirror_dir}")
        except OSError as e:
            errors.append(f"mirror cleanup: {e}")

    log_path = _default_log_path(plan_name)
    if log_path.exists():
        try:
            log_path.unlink()
            print(f"removed log file {log_path}")
        except OSError as e:
            errors.append(f"log removal: {e}")

    # 4. Delete the YAML config last — once it's gone, the plan is "removed"
    #    from the GUI's perspective. Earlier steps reference plan fields, so
    #    keeping the YAML around through them simplifies recovery if something
    #    fails partway.
    if path.exists():
        try:
            path.unlink()
            print(f"removed config file {path}")
        except OSError as e:
            errors.append(f"config removal: {e}")
    else:
        print(f"config file {path} already gone.")

    if errors:
        print(f"\nremove-plan finished with {len(errors)} issue(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"\nplan {plan_name!r} removed.")
    return 0


def prune_to_newest_cycle(plan: configlib.PlanConfig, log=None) -> list:
    """Prune every cycle except the newest complete one. Used by Active→Archive.

    Returns the list of deleted Cycles (each carries its archives list, so the
    caller can summarise what was removed). Incomplete cycles follow the same
    retention rule as normal pruning: they are kept (see retention.py docstring).
    """
    archive_dir = plan.archive_dir()
    mpath = manifestlib.manifest_path(archive_dir)
    m = manifestlib.load(mpath)
    if not m.plan_name:
        m.plan_name = plan.plan_name
    rplan = retentionlib.apply(m, policy="max_cycles", max_cycles=1)
    _delete_cycles(archive_dir, plan.plan_name, rplan.delete, m, log=log)
    return rplan.delete


# Canonical paths where a real timetraveller-backup shim lives after install.
# Both are accepted by the pkexec helper's allowlist; whichever exists is the
# right thing to embed in cron entries.
#   /usr/bin/timetraveller-backup        installed by the .deb package
#   /usr/local/bin/timetraveller-backup  installed by install.sh (dev)
_INSTALLED_BINARY_CANDIDATES = (
    "/usr/bin/timetraveller-backup",
    "/usr/local/bin/timetraveller-backup",
)
PKEXEC_HELPER_PATH = "/usr/libexec/timetraveller-install-system-cron"


def _default_installed_binary() -> str:
    """Return whichever canonical install location actually exists on disk."""
    for path in _INSTALLED_BINARY_CANDIDATES:
        if os.path.exists(path):
            return path
    # Fall back to the deb path even if missing — the helper will reject and
    # the user gets a clear error rather than a vague KeyError.
    return _INSTALLED_BINARY_CANDIDATES[0]


def _binary_path_for_cron(args: argparse.Namespace, plan: configlib.PlanConfig) -> str:
    """Pick which timetraveller-backup path to embed in cron entries."""
    if args.binary_path:
        return args.binary_path
    if args.dev_binary_path:
        if plan.plan_name in configlib.SYSTEM_PLAN_NAMES:
            raise SystemExit(
                f"ERROR: --dev-binary-path is not allowed for the {plan.plan_name!r} "
                "plan. The pkexec helper rejects non-canonical paths for security. "
                "Install via install.sh or the .deb for system-level plans."
            )
        return os.path.realpath(sys.argv[0])
    return _default_installed_binary()


def _read_user_crontab() -> str:
    """Return the current user's crontab text, or '' if none."""
    import subprocess
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    # crontab exits 1 with "no crontab for X" when empty; treat as empty.
    if r.returncode != 0 and "no crontab" not in (r.stderr or ""):
        # Unexpected error — propagate.
        sys.stderr.write(r.stderr)
        raise SystemExit(2)
    return r.stdout or ""


def _write_user_crontab(text: str) -> None:
    import subprocess
    r = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(2)


def action_show_schedule(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    bin_path = _binary_path_for_cron(args, plan)
    block = schedulelib.render_block(plan, bin_path)
    errors = schedulelib.validate_block(block, plan.plan_name)
    if errors:
        # This would be a bug in our renderer — emit to stderr but still print.
        for e in errors:
            print(f"validation: {e}", file=sys.stderr)
    print(block, end="")
    return 0 if not errors else 1


def action_install_schedule(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    bin_path = _binary_path_for_cron(args, plan)
    block = schedulelib.render_block(plan, bin_path)

    if plan.plan_name in configlib.SYSTEM_PLAN_NAMES:
        # Delegate to pkexec helper. The helper reads root's crontab, swaps
        # the plan's managed block, validates, and writes back.
        import subprocess
        _log(args, f"Installing {plan.plan_name} schedule via pkexec {PKEXEC_HELPER_PATH}")
        r = subprocess.run(
            ["pkexec", PKEXEC_HELPER_PATH, "install", plan.plan_name],
            input=block, text=True, capture_output=True,
        )
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr or "")
            print(f"\nERROR: helper exited {r.returncode}", file=sys.stderr)
            return r.returncode
        return 0

    # User crontab path: do it directly, no pkexec.
    current = _read_user_crontab()
    new = schedulelib.replace_block(current, plan.plan_name, block)
    # Validate the resulting managed block (defense in depth).
    extracted = schedulelib.find_block(new, plan.plan_name) or ""
    errors = schedulelib.validate_block(extracted, plan.plan_name)
    if errors:
        for e in errors:
            print(f"validation: {e}", file=sys.stderr)
        return 1
    _write_user_crontab(new)
    _log(args, f"Schedule installed in user crontab for plan {plan.plan_name!r}.")
    _log(args, "Inspect with: crontab -l")
    return 0


def _toggle_schedule(args: argparse.Namespace, plan: configlib.PlanConfig,
                     mode: str) -> int:
    """Shared implementation for suspend and resume."""
    assert mode in ("suspend", "resume")
    if plan.plan_name in configlib.SYSTEM_PLAN_NAMES:
        import subprocess
        _log(args, f"{mode} {plan.plan_name} schedule via pkexec {PKEXEC_HELPER_PATH}")
        r = subprocess.run(
            ["pkexec", PKEXEC_HELPER_PATH, mode, plan.plan_name],
            text=True, capture_output=True,
        )
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr or "")
            return r.returncode
        return 0

    # User-crontab plans: edit user crontab directly.
    current = _read_user_crontab()
    if schedulelib.find_block(current, plan.plan_name) is None:
        print(f"No managed block for plan {plan.plan_name!r}; nothing to {mode}.",
              file=sys.stderr)
        return 1
    state = schedulelib.is_block_suspended(current, plan.plan_name)
    if mode == "suspend" and state is True:
        _log(args, "Already suspended; nothing to do.")
        return 0
    if mode == "resume" and state is False:
        _log(args, "Already active; nothing to do.")
        return 0
    if mode == "suspend":
        new = schedulelib.suspend_block(current, plan.plan_name)
    else:
        new = schedulelib.resume_block(current, plan.plan_name)
    _write_user_crontab(new)
    past = "suspended" if mode == "suspend" else "resumed"
    _log(args, f"Schedule {past} for plan {plan.plan_name!r}.")
    return 0


def action_suspend_schedule(args, plan):
    return _toggle_schedule(args, plan, "suspend")


def action_resume_schedule(args, plan):
    return _toggle_schedule(args, plan, "resume")


def action_uninstall_schedule(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    if plan.plan_name in configlib.SYSTEM_PLAN_NAMES:
        import subprocess
        _log(args, f"Removing {plan.plan_name} schedule via pkexec {PKEXEC_HELPER_PATH}")
        r = subprocess.run(
            ["pkexec", PKEXEC_HELPER_PATH, "uninstall", plan.plan_name],
            text=True, capture_output=True,
        )
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr or "")
            return r.returncode
        return 0

    current = _read_user_crontab()
    if schedulelib.find_block(current, plan.plan_name) is None:
        _log(args, f"No managed block for plan {plan.plan_name!r}; nothing to do.")
        return 0
    new = schedulelib.remove_block(current, plan.plan_name)
    _write_user_crontab(new)
    _log(args, f"Schedule removed from user crontab for plan {plan.plan_name!r}.")
    return 0


def _lock_path(plan_name: str) -> Path:
    """Per-plan lock file path."""
    if os.geteuid() == 0:
        base = Path("/var/lock/timetraveller")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "timetraveller" / "locks"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{plan_name}.lock"


def _acquire_plan_lock(plan_name: str):
    """Try to take a non-blocking exclusive lock on the plan. Returns the
    open file (caller keeps it alive); raises SystemExit on contention.
    """
    import fcntl
    path = _lock_path(plan_name)
    fp = open(path, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        print(f"Another timetraveller-backup is already running for plan "
              f"{plan_name!r} (lock held at {path}). Exiting.", file=sys.stderr)
        raise SystemExit(0)
    return fp


def action_backup(args: argparse.Namespace, plan: configlib.PlanConfig) -> int:
    now = datetime.now(timezone.utc)
    kind = _resolve_kind(args.kind, plan, now)

    # If a SCHEDULED incremental coincides with a scheduled full day, defer:
    # cron will run --kind full separately. Manual invocations are not
    # deferred — the user asked for it explicitly.
    if kind == "incr" and not args.manual and _is_full_day(plan, now):
        _log(args, f"Deferring incremental: today is a scheduled full-backup day for plan "
                   f"{plan.plan_name!r}; the full run will cover it.")
        return 0

    lock_fp = _acquire_plan_lock(plan.plan_name)  # noqa: F841 (held for the duration)

    archive_dir = plan.archive_dir()
    fname = paxlib.archive_filename(dt=now, kind=kind, manual=args.manual)
    archive_path = archive_dir / fname
    mpath = manifestlib.manifest_path(archive_dir)
    m = manifestlib.load(mpath)
    if not m.plan_name:
        m.plan_name = plan.plan_name

    report = mountslib.filter_sources(
        plan.sources, plan.destination,
        include_removable=plan.include_removable,
        include_nfs=plan.include_nfs,
        include_cifs=plan.include_cifs,
        include_mounts=plan.include_mounts,
        exclude_mounts=plan.exclude_mounts,
    )

    incr_window = None
    incr_file_list: list[str] = []
    cycle_id: str
    if kind == "incr":
        # Attach to current (most recent successful full).
        cs = manifestlib.cycles(m)
        complete = [c for c in cs if c.is_complete]
        if not complete:
            print("ERROR: incremental requested but no successful full exists in this plan. "
                  "Run --kind full first.", file=sys.stderr)
            return 2
        cycle_id = complete[-1].cycle_id
        incr_window = _incremental_window(m, now)

        # Compute the file list in Python. We can't use pax -T because it
        # exits 1 when the source operand's own mtime is outside the window,
        # even when files under it qualify. Build the list ourselves and feed
        # it to pax via stdin.
        sources_abs = [str(Path(s).resolve()) for s in plan.sources]
        excludes_re: list[str] = []
        for g in plan.excludes:
            excludes_re.extend(paxlib.glob_to_regexes(g))
        incr_file_list = paxlib.list_changes_in_window(
            sources_abs, excludes_re, report.additional_excludes,
            incr_window[0], incr_window[1],
        )
        if not incr_file_list:
            _log(args, f"No files changed in window "
                       f"{incr_window[0].isoformat()} .. {incr_window[1].isoformat()}. "
                       f"Recording empty incremental.")
            entry = manifestlib.ArchiveEntry(
                filename=fname,
                kind=kind,
                cycle_id=cycle_id,
                date_started=now.isoformat(),
                date_finished=datetime.now(timezone.utc).isoformat(),
                size_bytes=0,
                status="empty",
                hostname=socket.gethostname(),
                plan_name=plan.plan_name,
                incr_window_from=incr_window[0].isoformat(),
                incr_window_to=incr_window[1].isoformat(),
                notes="No files changed in window; no archive written.",
            )
            m.append(entry)
            _save_manifest(m, archive_dir, plan.plan_name)
            if not args.no_retention:
                action_prune(args, plan)
            return 0
    else:
        # Use the archive filename's date component as the cycle_id. For
        # scheduled runs that's just YYYY-MM-DD; for manual runs it includes
        # the time, so multiple same-day fulls get distinct cycle ids.
        parsed = paxlib.parse_filename(fname)
        cycle_id = parsed[0] if parsed else now.strftime("%Y-%m-%d")

    sources_rel, chdir = _relative_sources(plan.sources)

    framed = plan.framed and not args.no_framed
    if not framed:
        print("WARNING: framing disabled — single-file restore from this archive "
              "will require a full archive read (can be many hours on large archives).",
              file=sys.stderr)

    inv = paxlib.PaxInvocation(
        sources=sources_rel,
        chdir=chdir,
        archive_path=archive_path,
        excludes=plan.excludes,
        extra_mount_excludes=report.additional_excludes,
        incr_window=incr_window,
        compression=plan.compression,
        one_filesystem=True,
        extra_pax_flags=plan.extra_pax_flags,
        framed=framed,
    )

    hostname = socket.gethostname()
    entry = manifestlib.ArchiveEntry(
        filename=fname,
        kind=kind,
        cycle_id=cycle_id,
        date_started=now.isoformat(),
        date_finished="",
        size_bytes=0,
        status="in-progress",
        hostname=hostname,
        plan_name=plan.plan_name,
        incr_window_from=incr_window[0].isoformat() if incr_window else "",
        incr_window_to=incr_window[1].isoformat() if incr_window else "",
    )
    m.append(entry)
    _save_manifest(m, archive_dir, plan.plan_name)

    log_path = args.log_file or _default_log_path(plan.plan_name)
    _log(args, f"Running {kind} backup → {archive_path}")

    sources_abs = [str(Path(s).resolve()) for s in plan.sources]
    excludes_re: list[str] = []
    for g in plan.excludes:
        excludes_re.extend(paxlib.glob_to_regexes(g))

    if kind == "incr":
        _log(args, f"  ({len(incr_file_list)} changed files)")
        result = paxlib.run_with_file_list(inv, incr_file_list, log_file=log_path)
    else:
        # Stream the source trees through pax. We use the same file-list
        # pipeline as incrementals so we can pre-filter sockets/FIFOs/device
        # nodes (pax can't archive them and would otherwise exit 1).
        file_iter = paxlib.iter_archivable_files(
            sources_abs, excludes_re, report.additional_excludes,
            mtime_window=None,         # no time filter for fulls
            include_dirs=True,         # preserve directory metadata on restore
            one_filesystem=True,       # mirror pax -X behaviour
            skip_special=True,         # drop sockets/FIFOs/devices
        )
        result = paxlib.run_with_file_list(inv, file_iter, log_file=log_path)
    finished = datetime.now(timezone.utc)

    status = result.status

    if status == "failed":
        # Record the failure immediately so the manifest reflects the on-disk
        # state (renamed-to-.failed) even if the worker is killed right after.
        m.update_status(
            fname,
            status=status,
            date_finished=finished.isoformat(),
            size_bytes=result.archive_size,
        )
        _save_manifest(m, archive_dir, plan.plan_name)
        print(f"ERROR: pax={result.pax_returncode} zstd={result.zstd_returncode}; "
              f"see {log_path}", file=sys.stderr)
        # Leave a marker file so the GUI surfaces it.
        try:
            archive_path.rename(archive_path.with_suffix(archive_path.suffix + ".failed"))
        except OSError:
            pass
        return 1

    if status == "ok-with-warnings":
        print(f"WARNING: pax={result.pax_returncode} (non-fatal — archive is trustworthy); "
              f"see {log_path}", file=sys.stderr)

    has_sidecar = False
    if result.index_built and indexlib.sidecar_path(archive_path).exists():
        # Framed run already built the .idx.zst inline off the write stream —
        # no second pass over the archive (the win on the NFS target).
        has_sidecar = True
        _log(args, f"Archive written: {result.archive_size/1024**2:.1f} MiB "
                   f"in {_hms(result.duration_seconds)} (sidecar built inline)")
        _mirror_sidecar(plan.plan_name, indexlib.sidecar_path(archive_path), fname)
    else:
        # --no-framed runs, or an inline-index failure: fall back to the
        # post-write re-read pass.
        _log(args, f"Archive written: {result.archive_size/1024**2:.1f} MiB "
                   f"in {_hms(result.duration_seconds)} (sidecar pending)")
        try:
            indexlib.write_sidecar(archive_path)
            has_sidecar = True
            _log(args, "Sidecar index written.")
            _mirror_sidecar(plan.plan_name, indexlib.sidecar_path(archive_path), fname)
        except Exception as e:  # noqa: BLE001 - sidecar failure shouldn't fail the backup
            print(f"WARNING: sidecar generation failed: {e}", file=sys.stderr)

    if not args.no_retention:
        prune_args = argparse.Namespace(**vars(args))
        action_prune(prune_args, plan)

    # Persist final state with the true completion time, now that the sidecar
    # pass and retention are done. `date_finished` reflects the moment the
    # backup is genuinely complete, not just the archive-write phase.
    m.update_status(
        fname,
        status=status,
        date_finished=datetime.now(timezone.utc).isoformat(),
        size_bytes=result.archive_size,
    )
    for entry in m.archives:
        if entry.filename == fname:
            if result.frame_count > 0:
                entry.has_frames = True
            if has_sidecar:
                entry.has_sidecar = True
            break
    _save_manifest(m, archive_dir, plan.plan_name)

    total = (datetime.now(timezone.utc) - now).total_seconds()
    _log(args, f"Backup complete: {_hms(total)} total.")
    return 0


def _relative_sources(sources: list[str]) -> tuple[list[str], str]:
    """Translate absolute sources into relative paths under a common chdir.

    We cd to / and emit paths like './home' or '.' — the leading `./` makes
    pax emit archive members with the `./` prefix that our exclude regexes
    target. (Compare: passing 'home' gets members 'home/...' without prefix.)
    """
    rel = []
    for s in sources:
        rs = Path(s).resolve()
        if str(rs) == "/":
            rel.append(".")
        else:
            rel.append("./" + str(rs.relative_to("/")))
    return rel, "/"


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in ' \t\n"\'\\$`'):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def _default_log_path(plan_name: str) -> Path:
    if os.geteuid() == 0:
        base = Path("/var/log/timetraveller")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "timetraveller"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{plan_name}.log"


# ---------- entry point ----------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        plan = _load_plan(args)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        return 2

    plan = _effective_plan(args, plan)

    if args.show_mounts:
        return action_show_mounts(args, plan)
    if args.list_archives:
        return action_list_archives(args, plan)
    if args.dry_run:
        return action_dry_run(args, plan)
    if args.list_files:
        return action_list_files(args, plan)
    if args.reindex is not None:
        return action_reindex(args, plan)
    if args.finalize_archive:
        return action_finalize_archive(args, plan)
    if args.recover_failed:
        return action_recover_failed(args, plan)
    if args.verify:
        return action_verify(args, plan)
    if args.extract:
        return action_extract(args, plan)
    if args.prune:
        return action_prune(args, plan)
    if args.remove_plan:
        return action_remove_plan(args, plan)
    if args.switch_to_archive:
        return action_switch_to_archive(args, plan)
    if args.switch_to_active:
        return action_switch_to_active(args, plan)
    if args.show_schedule:
        return action_show_schedule(args, plan)
    if args.install_schedule:
        return action_install_schedule(args, plan)
    if args.uninstall_schedule:
        return action_uninstall_schedule(args, plan)
    if args.suspend_schedule:
        return action_suspend_schedule(args, plan)
    if args.resume_schedule:
        return action_resume_schedule(args, plan)

    # Default action: take a backup.
    if args.kind is None:
        args.kind = "auto"
    return action_backup(args, plan)


if __name__ == "__main__":
    sys.exit(main())
