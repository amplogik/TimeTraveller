"""Build and execute pax commands.

paxmirabilis (Debian/Ubuntu's pax) is the assumed implementation. Excludes are
expressed as substitute-to-empty regexes via `-s ',pattern,,'`, exactly as the
reference perl script did. The archive itself is piped through zstd (paxmirabilis
ships only built-in gzip via `-z`, but we want zstd).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def glob_to_regexes(pattern: str) -> list[str]:
    r"""Translate a config glob to one or more regexes matching archive members.

    Archive members are normalised to start with `./` — we cd / and pass
    sources as `./home`, `.` etc., which makes pax emit member names like
    `./home/kim/foo.txt`. The regexes here target that form.

    To stay compatible with both BRE (pax -s) and Python re (PCRE), the
    output uses only the common subset: no grouping `()`, no alternation `|`,
    no `{n,m}` quantifiers. Patterns with a trailing slash produce TWO
    regexes — one matching the directory exactly, one matching its contents.

    Translation rules:
      - `**` → `.*`
      - `*`  → `[^/]*`
      - `?`  → `[^/]`
      - leading `/` → anchored after the `./` prefix
      - trailing `/` → also match anything below the directory
    """
    p = pattern
    anchored = p.startswith("/")
    if anchored:
        p = p.lstrip("/")
    trailing_slash = p.endswith("/")
    if trailing_slash:
        p = p.rstrip("/")

    out: list[str] = []
    i = 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if i + 1 < len(p) and p[i + 1] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c in ".+^$\\":
            out.append("\\" + c)
        elif c in "(){}|":
            # Escape these in BRE-and-PCRE-safe ways. In BRE they're literal;
            # in PCRE they need escaping. \( works in PCRE; in BRE it would
            # turn into a group, but we don't use it on glob input that
            # contains these characters.
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1

    body = "".join(out)
    prefix = r"\./" if anchored else r"\./.*"

    if trailing_slash:
        # Two regexes: match the directory itself and match its contents.
        return [
            f"^{prefix}{body}$",
            f"^{prefix}{body}/.*$",
        ]
    return [f"^{prefix}{body}$"]


def build_exclude_args(excludes: list[str]) -> list[str]:
    """Turn config excludes into a list of `-s ',re,,'` argv pieces."""
    args: list[str] = []
    for pat in excludes:
        for regex in glob_to_regexes(pat):
            # Comma is the conventional delimiter; switch if the regex
            # contains it.
            delim = "," if "," not in regex else "|"
            args.extend(["-s", f"{delim}{regex}{delim}{delim}"])
    return args


@dataclass
class PaxInvocation:
    """A fully resolved pax|zstd pipeline ready to execute."""
    sources: list[str]       # relative paths (we cd to chdir before invoking)
    chdir: str               # working directory; usually "/"
    archive_path: Path
    excludes: list[str]      # config-style globs
    extra_mount_excludes: list[str]  # mountpoint paths from FilterReport
    incr_window: tuple[datetime, datetime] | None = None
    compression: str = "zstd"
    one_filesystem: bool = True
    extra_pax_flags: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.extra_pax_flags is None:
            self.extra_pax_flags = []

    def pax_argv(self) -> list[str]:
        """Legacy: pax argv for the old "pax walks the tree" mode.

        No longer used by action_backup — we always go through
        run_with_file_list() now so we can pre-filter unsupported file types.
        Kept for ad-hoc CLI testing only.
        """
        argv = ["pax", "-w"]
        if self.one_filesystem:
            argv.append("-X")
        argv.extend(build_exclude_args(self.excludes))
        mount_globs = [str(Path(m).resolve()).rstrip("/") + "/" for m in self.extra_mount_excludes]
        argv.extend(build_exclude_args(mount_globs))
        argv.extend(self.extra_pax_flags)
        argv.extend(self.sources)
        return argv

    def pax_argv_incremental(self) -> list[str]:
        """Archive-write argv: GNU tar in pax format, reading NUL-delimited
        paths from stdin.

        Why GNU tar instead of paxmirabilis: the local pax implementation
        (paxmirabilis 2024) doesn't actually support the POSIX pax format
        despite its name — its `-x` flag only accepts ustar (8 GB max file,
        255-byte max path) and various cpio variants. GNU tar's pax format
        has no such limits and is universally readable, including by
        paxmirabilis on the read side (paxmirabilis lists tar/ustar as a
        readable input format).

        --null --files-from=- : NUL-delimited paths on stdin (same wire
        protocol our Python walker emits).
        --format=pax           : extended-header pax format, no size or
                                 path-length cap.
        --no-recursion         : we feed individual file entries; don't let
                                 tar walk further into directories.
        """
        argv = [
            "tar",
            "--format=pax",
            "--no-recursion",
            "--null",
            "--files-from=-",
            "-c",
        ]
        argv.extend(self.extra_pax_flags)
        return argv

    def zstd_argv(self) -> list[str]:
        # -T0: use all cores. -19 is overkill default; keep zstd at its level
        # 3 default for speed, leave tuning to advanced users via env var.
        level = os.environ.get("TIMETRAVELLER_ZSTD_LEVEL", "3")
        return ["zstd", f"-{level}", "-T0", "-o", str(self.archive_path), "-q"]


def _pax_time(dt: datetime) -> str:
    """pax -T expects [[CC]YY]MMDD[hhmm[.SS]]; emit CCYYMMDDhhmm.SS."""
    return dt.strftime("%Y%m%d%H%M.%S")


def iter_archivable_files(sources: list[str], excludes_re: list[str],
                          extra_excludes: list[str],
                          mtime_window: tuple[datetime, datetime] | None = None,
                          include_dirs: bool = True,
                          one_filesystem: bool = True,
                          skip_special: bool = True):
    """Yield relative archive-member paths under `sources` to feed to pax.

    The walk is the single source of truth for both fulls and incrementals:

      - Full backup:        mtime_window=None, include_dirs=True
      - Incremental backup: mtime_window=(frm, to), include_dirs=False

    Yielded paths are normalised with a `./` prefix matching what pax would
    emit if given `.` as the source under cd /. The walk honors mount
    boundaries (`one_filesystem`), our pax-style exclude regexes, and the
    additional mount-excludes computed by mounts.filter_sources.

    `skip_special=True` filters out sockets, FIFOs, block devices, and
    character devices — pax can't archive those, and including them
    produces "cannot archive a socket" errors that abort the whole run.
    Symlinks, regular files, and directories are kept.
    """
    import os
    import re
    import stat as statmod

    extra_skip = {os.path.normpath(p) for p in extra_excludes}
    patterns = [re.compile(rx) for rx in excludes_re]

    def is_excluded(member_path: str) -> bool:
        return any(pat.match(member_path) for pat in patterns)

    if mtime_window is not None:
        frm_ts = mtime_window[0].timestamp()
        to_ts = mtime_window[1].timestamp()
    else:
        frm_ts = to_ts = 0  # unused when mtime_window is None

    def in_window(mt: float) -> bool:
        return mtime_window is None or (frm_ts < mt <= to_ts)

    for source in sources:
        try:
            sroot = os.path.realpath(source)
            src_dev = os.stat(sroot).st_dev
        except OSError:
            continue

        # Yield the source root itself (full backups need it so pax preserves
        # the dir's permissions/ownership on restore).
        try:
            root_st = os.lstat(sroot)
            rel_root = "./" + os.path.relpath(sroot, "/")
            if include_dirs and not is_excluded(rel_root) and in_window(root_st.st_mtime):
                yield rel_root
        except OSError:
            pass

        for root, dirs, files in os.walk(sroot, followlinks=False):
            new_dirs = []
            for d in dirs:
                full = os.path.join(root, d)
                if full in extra_skip:
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if one_filesystem and st.st_dev != src_dev:
                    continue
                rel = "./" + os.path.relpath(full, "/")
                if is_excluded(rel):
                    continue
                new_dirs.append(d)
                if include_dirs and in_window(st.st_mtime):
                    yield rel
            dirs[:] = sorted(new_dirs)

            for f in sorted(files):
                full = os.path.join(root, f)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if one_filesystem and st.st_dev != src_dev:
                    continue
                if skip_special:
                    m = st.st_mode
                    if not (statmod.S_ISREG(m) or statmod.S_ISLNK(m) or statmod.S_ISDIR(m)):
                        continue  # socket, FIFO, block dev, char dev — pax refuses these
                rel = "./" + os.path.relpath(full, "/")
                if is_excluded(rel):
                    continue
                if not in_window(st.st_mtime):
                    continue
                yield rel


def list_changes_in_window(sources, excludes_re, extra_excludes, frm, to,
                           one_filesystem=True, limit=None):
    """Backward-compat wrapper: returns a list of files modified in (frm, to]."""
    out = []
    for p in iter_archivable_files(
        sources, excludes_re, extra_excludes,
        mtime_window=(frm, to), include_dirs=False,
        one_filesystem=one_filesystem,
    ):
        out.append(p)
        if limit is not None and len(out) >= limit:
            break
    return out


def any_changes_in_window(sources, excludes_re, extra_excludes, frm, to,
                          one_filesystem=True) -> bool:
    """Cheap probe: does any file in (frm, to] exist under `sources`?"""
    return bool(list_changes_in_window(
        sources, excludes_re, extra_excludes, frm, to,
        one_filesystem=one_filesystem, limit=1,
    ))


@dataclass
class RunResult:
    pax_returncode: int
    zstd_returncode: int
    archive_path: Path
    archive_size: int
    duration_seconds: float
    file_count: int = 0   # only meaningful for run_with_file_list

    @property
    def status(self) -> str:
        """Tri-state archive status.

        pax exit 1 is POSIX-specified as a non-fatal warning (e.g. "file
        changed as we read it") — the stream is structurally valid, so the
        archive is trustworthy. Only exit >=2 (fatal) or a zstd failure
        means the archive itself cannot be trusted.
        """
        if self.zstd_returncode != 0 or self.pax_returncode >= 2:
            return "failed"
        if self.pax_returncode == 1:
            return "ok-with-warnings"
        return "ok"

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def run(invocation: PaxInvocation, *, log_file: Path | None = None) -> RunResult:
    """Execute a pax | zstd pipeline for a FULL backup."""
    import time

    invocation.archive_path.parent.mkdir(parents=True, exist_ok=True)
    if invocation.compression != "zstd":
        raise NotImplementedError("only zstd compression is wired up in Phase 1")

    log_fp = open(log_file, "ab") if log_file else None
    started = time.monotonic()
    try:
        if log_fp:
            log_fp.write(f"\n--- pax+zstd run at {datetime.utcnow().isoformat()}Z\n".encode())
            log_fp.write(("CMD: " + " ".join(invocation.pax_argv()) + "\n").encode())
            log_fp.write(("CWD: " + invocation.chdir + "\n").encode())
            log_fp.write(("OUT: " + " ".join(invocation.zstd_argv()) + "\n").encode())
            log_fp.flush()

        pax = subprocess.Popen(
            invocation.pax_argv(),
            cwd=invocation.chdir,
            stdout=subprocess.PIPE,
            stderr=log_fp or subprocess.DEVNULL,
        )
        assert pax.stdout is not None
        zstd = subprocess.Popen(
            invocation.zstd_argv(),
            stdin=pax.stdout,
            stderr=log_fp or subprocess.DEVNULL,
        )
        pax.stdout.close()  # SIGPIPE to pax if zstd dies
        zstd_rc = zstd.wait()
        pax_rc = pax.wait()
    finally:
        if log_fp:
            log_fp.close()
    duration = time.monotonic() - started

    size = invocation.archive_path.stat().st_size if invocation.archive_path.exists() else 0
    return RunResult(
        pax_returncode=pax_rc,
        zstd_returncode=zstd_rc,
        archive_path=invocation.archive_path,
        archive_size=size,
        duration_seconds=duration,
    )


def run_with_file_list(invocation: PaxInvocation, file_iter,
                       *, log_file: Path | None = None) -> RunResult:
    """Run pax | zstd with paths streamed in on pax's stdin (NUL-delimited).

    `file_iter` can be any iterable of relative paths (lists, generators).
    Streaming avoids materialising the full file list in memory, which
    matters for fulls of multi-TB trees with millions of entries.

    This single function handles both full backups (file_iter is the output
    of iter_archivable_files() with no time filter) and incrementals (with a
    time-window filter). pax doesn't get any source operands — it only
    archives what we feed it.
    """
    import time

    invocation.archive_path.parent.mkdir(parents=True, exist_ok=True)
    if invocation.compression != "zstd":
        raise NotImplementedError("only zstd compression is wired up in Phase 1")

    log_fp = open(log_file, "ab") if log_file else None
    started = time.monotonic()
    n = 0
    try:
        if log_fp:
            log_fp.write(f"\n--- pax+zstd run at {datetime.utcnow().isoformat()}Z\n".encode())
            log_fp.write(("CMD: " + " ".join(invocation.pax_argv_incremental()) + "\n").encode())
            log_fp.write(("CWD: " + invocation.chdir + "\n").encode())
            log_fp.write(("OUT: " + " ".join(invocation.zstd_argv()) + "\n").encode())
            log_fp.flush()

        pax = subprocess.Popen(
            invocation.pax_argv_incremental(),
            cwd=invocation.chdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log_fp or subprocess.DEVNULL,
        )
        assert pax.stdin is not None and pax.stdout is not None
        zstd = subprocess.Popen(
            invocation.zstd_argv(),
            stdin=pax.stdout,
            stderr=log_fp or subprocess.DEVNULL,
        )
        pax.stdout.close()
        # Stream filenames NUL-delimited. Python's BufferedWriter handles the
        # internal buffering; pax's pipe buffer (~64K) prevents us from racing
        # ahead too far before pax catches up.
        write = pax.stdin.write
        for path in file_iter:
            write(path.encode("utf-8", errors="surrogateescape"))
            write(b"\0")
            n += 1
        pax.stdin.close()
        zstd_rc = zstd.wait()
        pax_rc = pax.wait()
    finally:
        if log_fp:
            if n > 0:
                with open(log_file, "ab") as lf:
                    lf.write(f"FILES: {n}\n".encode())
            log_fp.close()
    duration = time.monotonic() - started

    size = invocation.archive_path.stat().st_size if invocation.archive_path.exists() else 0
    return RunResult(
        pax_returncode=pax_rc,
        zstd_returncode=zstd_rc,
        archive_path=invocation.archive_path,
        archive_size=size,
        duration_seconds=duration,
        file_count=n,
    )


# Backward-compat alias for any callers/tests that still use the old name.
run_incremental = run_with_file_list


# ---------- archive naming ----------

_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}(?:T\d{6})?)_(?P<kind>full|incr)\.pax\.zst$")


def archive_filename(*, dt: datetime, kind: str, manual: bool = False) -> str:
    """Compute the archive filename per the naming convention.

    Scheduled runs use date only (YYYY-MM-DD). Manual runs include the time
    component (YYYY-MM-DDTHHMMSS) so multiple runs in one day don't collide.
    """
    if kind not in ("full", "incr"):
        raise ValueError(f"kind must be full|incr, got {kind!r}")
    if manual:
        ts = dt.strftime("%Y-%m-%dT%H%M%S")
    else:
        ts = dt.strftime("%Y-%m-%d")
    return f"{ts}_{kind}.pax.zst"


def parse_filename(name: str) -> tuple[str, str] | None:
    """Inverse of archive_filename: return (date_str, kind) or None."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return m.group("date"), m.group("kind")
