"""Tests for the GUI reindex tracker — pure-logic and adoption decisions.

The QObject-based ReindexHandle/ReindexTracker need a QCoreApplication to
deliver signals; we set one up at module scope so signal-based assertions
work. The actual subprocess launching (`tracker.start`) is exercised via
a fake-shell wrapper that completes quickly, so we can observe a full
launch → poll → finished cycle without waiting on a real reindex.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer

from timetraveller.gui import reindex_tracker as rt


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    yield tmp_path


def _spin_until(predicate, timeout_s=5.0) -> bool:
    """Run the Qt event loop until predicate() is true or timeout elapses."""
    deadline = time.monotonic() + timeout_s
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(loop.quit)
    timer.start()
    try:
        while time.monotonic() < deadline:
            if predicate():
                return True
            loop.exec()
        return predicate()
    finally:
        timer.stop()


def test_state_dir_creates_under_xdg(xdg):
    d = rt.state_dir("home")
    assert d == xdg / "timetraveller" / "home" / "reindex"
    assert d.is_dir()


def test_state_files_paths(xdg):
    p = rt.pid_file("home", "2026-05-31_full.pax.zst")
    l = rt.log_file("home", "2026-05-31_full.pax.zst")
    d = rt.done_file("home", "2026-05-31_full.pax.zst")
    base = xdg / "timetraveller" / "home" / "reindex"
    assert p == base / "2026-05-31_full.pax.zst.pid"
    assert l == base / "2026-05-31_full.pax.zst.log"
    assert d == base / "2026-05-31_full.pax.zst.done"


def test_alive_for_current_process():
    assert rt._alive(os.getpid()) is True


def test_alive_for_dead_pid():
    # Spawn a child that exits immediately, then check its (now-dead) pid.
    p = subprocess.Popen(["true"])
    p.wait()
    # The pid may have been reaped; if so os.kill returns ProcessLookupError
    # which _alive correctly reports as False. (If still in a brief zombie
    # state, os.kill(0) returns success — we then loop briefly.)
    for _ in range(20):
        if not rt._alive(p.pid):
            break
        time.sleep(0.05)
    assert rt._alive(p.pid) is False


def test_pid_is_reindex_matches_cmdline(tmp_path):
    # Launch a bash that sleeps with a synthesized --reindex argv so we can
    # inspect /proc/<pid>/cmdline and confirm the matcher's substring logic.
    archive = "2026-05-31_full.pax.zst"
    p = subprocess.Popen(
        ["bash", "-c", f"exec -a 'timetraveller-backup --reindex {archive}' "
                       f"sleep 5"],
    )
    try:
        # Give it a moment to exec.
        time.sleep(0.1)
        assert rt._pid_is_reindex(p.pid, archive) is True
        assert rt._pid_is_reindex(p.pid, "other.pax.zst") is False
    finally:
        p.terminate()
        p.wait()


def test_pid_is_reindex_false_for_unrelated_process():
    # The test process itself isn't a reindex.
    assert rt._pid_is_reindex(os.getpid(), "anything") is False


def test_pid_is_reindex_false_for_dead_pid():
    assert rt._pid_is_reindex(2**22, "anything") is False  # almost certainly unused


def test_read_log_tail_handles_missing(tmp_path):
    assert rt._read_log_tail(tmp_path / "nope") == ""


def test_read_log_tail_truncates_long(tmp_path):
    log = tmp_path / "log"
    log.write_text("A" * (rt.LOG_TAIL_BYTES * 3))
    out = rt._read_log_tail(log)
    assert len(out) == rt.LOG_TAIL_BYTES
    assert out.startswith("A") and out.endswith("A")


# ---------- tracker integration: launch a fast fake-reindex --------------


class _FakeReindexHarness:
    """Patches subprocess.Popen so tracker.start() spawns a fast no-op that
    still touches the same .log/.done files (so the rest of the tracker
    behaves identically). The archive name is preserved in the wrapper text
    as a `# --reindex <archive>` comment so the outer bash's
    /proc/<pid>/cmdline still satisfies `_pid_is_reindex`.
    """

    def __init__(self, tmp_path: Path, monkeypatch, *, exit_code: int = 0,
                 sleep_s: float = 0.0, stdout_text: str = "fake reindex done"):
        self.exit_code = exit_code
        self.sleep_s = sleep_s
        self.stdout_text = stdout_text
        self._real_popen = subprocess.Popen
        monkeypatch.setattr(rt.subprocess, "Popen", self._fake_popen)

    def _fake_popen(self, argv, **kwargs):
        import re as _re
        import shlex as _shlex
        assert argv[0] == "bash" and argv[1] == "-c"
        orig = argv[2]
        log_path = _re.search(r" >(\S+) 2>&1", orig).group(1)
        done_path = _re.search(r"echo \$ec >(\S+);", orig).group(1)
        archive = _re.search(r"--reindex (\S+)", orig).group(1).strip("'\"")
        new_wrapper = (
            f"# --reindex {archive}\n"
            f"echo {_shlex.quote(self.stdout_text)} >{log_path} 2>&1\n"
            f"sleep {self.sleep_s}\n"
            f"echo {self.exit_code} >{done_path}\n"
            f"exit {self.exit_code}"
        )
        return self._real_popen(["bash", "-c", new_wrapper], **kwargs)


def test_tracker_start_creates_pid_file_and_emits_started(xdg, qapp, monkeypatch):
    _FakeReindexHarness(xdg, monkeypatch, exit_code=0, sleep_s=0.3)
    tracker = rt.ReindexTracker()
    started: list[tuple[str, str]] = []
    tracker.started.connect(lambda p, a: started.append((p, a)))

    handle = tracker.start("home", "2026-05-31_full.pax.zst")
    assert handle is not None
    assert rt.pid_file("home", "2026-05-31_full.pax.zst").exists()
    assert started == [("home", "2026-05-31_full.pax.zst")]
    assert tracker.running_for("home", "2026-05-31_full.pax.zst") is handle

    # Wait for completion so the spawned bash doesn't outlive the test.
    finished: list[tuple] = []
    tracker.finished.connect(lambda p, a, ok, log: finished.append((p, a, ok)))
    _spin_until(lambda: bool(finished), timeout_s=5.0)
    assert finished and finished[0] == ("home", "2026-05-31_full.pax.zst", True)
    assert not rt.pid_file("home", "2026-05-31_full.pax.zst").exists()
    assert not rt.done_file("home", "2026-05-31_full.pax.zst").exists()
    # Log stays for diagnostics.
    assert rt.log_file("home", "2026-05-31_full.pax.zst").exists()


def test_tracker_start_returns_existing_handle_for_same_archive(xdg, qapp, monkeypatch):
    _FakeReindexHarness(xdg, monkeypatch, exit_code=0, sleep_s=0.5)
    tracker = rt.ReindexTracker()
    h1 = tracker.start("home", "A.pax.zst")
    h2 = tracker.start("home", "A.pax.zst")
    assert h1 is h2
    finished: list = []
    tracker.finished.connect(lambda *a: finished.append(a))
    _spin_until(lambda: bool(finished), timeout_s=5.0)


def test_tracker_emits_failed_on_nonzero_exit(xdg, qapp, monkeypatch):
    _FakeReindexHarness(xdg, monkeypatch, exit_code=2, sleep_s=0.2,
                        stdout_text="simulated failure")
    tracker = rt.ReindexTracker()
    finished: list = []
    tracker.finished.connect(lambda p, a, ok, log: finished.append((ok, log)))
    tracker.start("home", "B.pax.zst")
    _spin_until(lambda: bool(finished), timeout_s=5.0)
    assert finished
    ok, log = finished[0]
    assert ok is False
    assert "simulated failure" in log


def test_adopt_drops_stale_pid_files(xdg, qapp):
    # Seed a pid file for a long-dead pid; adopt() should clean it up.
    d = rt.state_dir("home")
    (d / "ghost.pax.zst.pid").write_text("2147483")  # impossibly high pid
    tracker = rt.ReindexTracker()
    adopted = tracker.adopt("home")
    assert adopted == []
    assert not (d / "ghost.pax.zst.pid").exists()


def test_adopt_picks_up_live_reindex(xdg, qapp):
    # Launch a real fake-reindex outside the tracker, then adopt(). The
    # comment line below stays in the outer bash's argv[2] so its cmdline
    # carries "--reindex <archive>" for the adoption guard.
    archive = "C.pax.zst"
    log_path = rt.log_file("home", archive)
    done_path = rt.done_file("home", archive)
    pid_path = rt.pid_file("home", archive)
    wrapper = (
        f"# --reindex {archive}\n"
        f"echo hello >{log_path} 2>&1\n"
        f"sleep 1\n"
        f"echo 0 >{done_path}\n"
        f"exit 0"
    )
    proc = subprocess.Popen(["bash", "-c", wrapper], start_new_session=True)
    pid_path.write_text(str(proc.pid))
    try:
        tracker = rt.ReindexTracker()
        finished: list = []
        tracker.finished.connect(lambda *a: finished.append(a))
        adopted = tracker.adopt("home")
        assert len(adopted) == 1
        assert tracker.running_for("home", archive) is adopted[0]
        _spin_until(lambda: bool(finished), timeout_s=5.0)
        assert finished and finished[0][:3] == ("home", archive, True)
    finally:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
