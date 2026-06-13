"""Tracks GUI-launched worker jobs (--reindex, --recover-failed) so the
Archives panel can show running state, survive GUI restarts, and surface
failures.

Both jobs stream-read the whole archive over NFS (often 1+ TB for full
backups), which makes them multi-hour background jobs. The tracker spawns the
worker in its own session group so the GUI can exit without killing the run,
and persists pid/log/exit-code files under XDG_STATE_HOME so a fresh GUI
session can adopt a still-running job on startup.

State files (per `(plan, archive)`, under a per-action subdir):

    $XDG_STATE_HOME/timetraveller/<plan>/<subdir>/<archive>.pid    (pid number)
    $XDG_STATE_HOME/timetraveller/<plan>/<subdir>/<archive>.log    (stdout+stderr)
    $XDG_STATE_HOME/timetraveller/<plan>/<subdir>/<archive>.done   (exit code)

<subdir> is `reindex` or `recover`, so the two job kinds never collide for the
same archive. The .done file is written by a tiny bash wrapper around the
worker — the wrapper records `$?` so an adopted handle that polls pid liveness
can recover the exit code after the GUI restart misses the live signal.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


POLL_INTERVAL_MS = 2000
LOG_TAIL_BYTES = 4000


def state_dir(plan_name: str, subdir: str) -> Path:
    """Return (and create) the per-plan, per-action job state directory."""
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = Path(xdg) / "timetraveller" / plan_name / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file(plan_name: str, archive: str, subdir: str) -> Path:
    return state_dir(plan_name, subdir) / f"{archive}.pid"


def log_file(plan_name: str, archive: str, subdir: str) -> Path:
    return state_dir(plan_name, subdir) / f"{archive}.log"


def done_file(plan_name: str, archive: str, subdir: str) -> Path:
    return state_dir(plan_name, subdir) / f"{archive}.done"


def _alive(pid: int) -> bool:
    """True iff the pid names a running (non-zombie) process.

    Reads /proc/<pid>/status so a zombie (State: Z) reads as dead — the
    process has finished its work and just hasn't been reaped yet, which
    is exactly the moment we want to surface as "finished" to the UI.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False
    except PermissionError:
        # /proc/<pid>/status is normally world-readable; fall back to a
        # signal probe if it isn't.
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return line.split()[1] not in ("Z", "X")
    return True


def _pid_runs_job(pid: int, archive: str, action_flag: str) -> bool:
    """Best-effort check that /proc/<pid> is actually our worker job.

    Guards against pid recycling: if the OS hands the recorded pid to an
    unrelated process between GUI sessions, we don't want to adopt it.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError):
        return False
    text = cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    return action_flag in text and archive in text


def _read_log_tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    if len(data) > LOG_TAIL_BYTES:
        data = data[-LOG_TAIL_BYTES:]
    return data.decode("utf-8", errors="replace")


class WorkerJobHandle(QObject):
    """One in-flight worker job. Polls the pid for exit and emits `finished`.

    For tracker-launched handles we keep the Popen object so .poll() can
    waitpid() and reap the child — otherwise the child becomes a zombie
    when it exits and our `_alive` probe would keep reporting alive.
    For adopted handles (from a prior GUI session) we have only the pid;
    those processes were reparented to init when the original GUI exited,
    so init handles reaping and we just observe /proc disappearance.
    """

    finished = pyqtSignal(bool, str)  # (ok, log tail)

    def __init__(self, plan_name: str, archive: str, pid: int, subdir: str,
                 *, popen: subprocess.Popen | None = None, parent=None):
        super().__init__(parent)
        self.plan_name = plan_name
        self.archive = archive
        self.pid = pid
        self.subdir = subdir
        self._popen = popen
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        # Fire one poll on the next event-loop tick so an adopted process
        # that died between scan and ctor exit is detected immediately.
        QTimer.singleShot(0, self._poll)

    def _poll(self) -> None:
        if self._popen is not None:
            if self._popen.poll() is None:
                return
        elif _alive(self.pid):
            return
        self._timer.stop()
        done = done_file(self.plan_name, self.archive, self.subdir)
        try:
            ec = int(done.read_text().strip())
        except (FileNotFoundError, ValueError):
            # No .done — adopted process whose wrapper didn't run. Treat as
            # failed conservatively; the log (if any) will explain.
            ec = -1
        log_tail = _read_log_tail(log_file(self.plan_name, self.archive, self.subdir))
        # Pid + done markers are no longer useful; the .log stays for diagnostics.
        pid_file(self.plan_name, self.archive, self.subdir).unlink(missing_ok=True)
        done.unlink(missing_ok=True)
        self.finished.emit(ec == 0, log_tail)


class WorkerJobTracker(QObject):
    """Owns in-flight WorkerJobHandles keyed by (plan, archive).

    Subclasses set ACTION_FLAG (the worker CLI flag that takes the archive
    name) and SUBDIR (the state-file subdirectory). Lifetime: one per GUI
    process, typically on MainWindow.
    """

    ACTION_FLAG: str = ""
    SUBDIR: str = ""

    started = pyqtSignal(str, str)              # (plan, archive)
    finished = pyqtSignal(str, str, bool, str)  # (plan, archive, ok, log tail)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._handles: dict[tuple[str, str], WorkerJobHandle] = {}

    def running_for(self, plan_name: str, archive: str) -> WorkerJobHandle | None:
        return self._handles.get((plan_name, archive))

    def start(self, plan_name: str, archive: str) -> WorkerJobHandle | None:
        """Spawn a detached worker job. Returns the new handle, or the
        existing handle if one is already running for this archive, or
        None if the launch itself failed.
        """
        key = (plan_name, archive)
        existing = self._handles.get(key)
        if existing is not None:
            return existing

        log_path = log_file(plan_name, archive, self.SUBDIR)
        done_path = done_file(plan_name, archive, self.SUBDIR)
        pid_path = pid_file(plan_name, archive, self.SUBDIR)
        # A leftover .done from a previous, untracked run would confuse the
        # next poll; clear it before launch.
        done_path.unlink(missing_ok=True)

        installed = Path("/usr/bin/timetraveller-backup")
        env = dict(os.environ)
        if installed.exists():
            cmd = [str(installed), "--plan", plan_name, self.ACTION_FLAG, archive]
        else:
            # Source-tree fallback: same module the cron entry-point exposes.
            cmd = [sys.executable, "-m", "timetraveller.worker",
                   "--plan", plan_name, self.ACTION_FLAG, archive]
            repo_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        wrapper = (
            f"{cmd_str} >{shlex.quote(str(log_path))} 2>&1; "
            f"ec=$?; echo $ec >{shlex.quote(str(done_path))}; exit $ec"
        )

        try:
            proc = subprocess.Popen(
                ["bash", "-c", wrapper],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except OSError as e:
            log_path.write_text(f"Failed to launch {self.ACTION_FLAG}: {e}\n")
            return None

        pid_path.write_text(str(proc.pid))
        handle = WorkerJobHandle(plan_name, archive, proc.pid, self.SUBDIR,
                                 popen=proc, parent=self)
        self._handles[key] = handle
        handle.finished.connect(
            lambda ok, log, k=key: self._on_finished(k, ok, log)
        )
        self.started.emit(plan_name, archive)
        return handle

    def adopt(self, plan_name: str) -> list[WorkerJobHandle]:
        """Adopt any still-running jobs of this kind for this plan.

        Called when the Archives panel loads a plan so jobs launched by a
        prior GUI session keep showing as in-flight. Stale pid files
        (process dead, or pid recycled) are cleaned up here.
        """
        adopted: list[WorkerJobHandle] = []
        d = state_dir(plan_name, self.SUBDIR)
        for pid_path in sorted(d.glob("*.pid")):
            archive = pid_path.stem
            key = (plan_name, archive)
            if key in self._handles:
                continue
            try:
                pid = int(pid_path.read_text().strip())
            except (FileNotFoundError, ValueError):
                pid_path.unlink(missing_ok=True)
                continue
            if not _alive(pid) or not _pid_runs_job(pid, archive, self.ACTION_FLAG):
                # Dead or recycled; drop the pid file and any orphaned done
                # marker so the next launch starts clean.
                pid_path.unlink(missing_ok=True)
                done_file(plan_name, archive, self.SUBDIR).unlink(missing_ok=True)
                continue
            handle = WorkerJobHandle(plan_name, archive, pid, self.SUBDIR, parent=self)
            self._handles[key] = handle
            handle.finished.connect(
                lambda ok, log, k=key: self._on_finished(k, ok, log)
            )
            self.started.emit(plan_name, archive)
            adopted.append(handle)
        return adopted

    def _on_finished(self, key: tuple[str, str], ok: bool, log: str) -> None:
        handle = self._handles.pop(key, None)
        plan_name, archive = key
        self.finished.emit(plan_name, archive, ok, log)
        if handle is not None:
            handle.deleteLater()


class ReindexTracker(WorkerJobTracker):
    """Tracks `--reindex` jobs (rebuild a missing .idx.zst sidecar)."""

    ACTION_FLAG = "--reindex"
    SUBDIR = "reindex"


class RecoverTracker(WorkerJobTracker):
    """Tracks `--recover-failed` jobs (un-quarantine a failed-but-intact backup)."""

    ACTION_FLAG = "--recover-failed"
    SUBDIR = "recover"


# Backward-compat alias: the handle class was renamed when the tracker was
# generalized from reindex-only to arbitrary worker jobs.
ReindexHandle = WorkerJobHandle
