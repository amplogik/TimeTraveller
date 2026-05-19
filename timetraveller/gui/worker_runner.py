"""Run the timetraveller-backup worker as a subprocess from the GUI.

We invoke the worker via `python -m timetraveller.worker` so the GUI works
without needing /usr/local/bin to be populated (the install.sh step). When
the project is properly installed, the same QProcess machinery still works —
swap the program for `timetraveller-backup` if you want.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QProcess, QProcessEnvironment


WORKER_MODULE = "timetraveller.worker"


def build_qprocess(parent=None) -> QProcess:
    """Create a QProcess set up to run the worker with the project on PYTHONPATH."""
    proc = QProcess(parent)
    # Merge stderr into stdout so the dialog shows everything in order.
    proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

    env = QProcessEnvironment.systemEnvironment()
    # Ensure the running checkout is importable when not installed.
    repo_root = str(Path(__file__).resolve().parents[2])
    existing = env.value("PYTHONPATH", "")
    env.insert("PYTHONPATH", f"{repo_root}{os.pathsep}{existing}" if existing else repo_root)
    proc.setProcessEnvironment(env)

    return proc


def worker_program() -> str:
    return sys.executable


def worker_args(extra: list[str]) -> list[str]:
    return ["-m", WORKER_MODULE, *extra]
