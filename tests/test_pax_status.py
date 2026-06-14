"""Tri-state RunResult.status tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller.pax import RunResult


def _result(pax_rc: int, zstd_rc: int, pax_stderr: str | None = None) -> RunResult:
    return RunResult(
        pax_returncode=pax_rc,
        zstd_returncode=zstd_rc,
        pax_stderr=pax_stderr,
        archive_path=Path("/tmp/unused.pax.zst"),
        archive_size=0,
        duration_seconds=0.0,
    )


# The real failing run, verbatim from ~/.local/state/timetraveller/home.log.
_BRAVE_RACE_STDERR = """\
tar: ./home/kim/.config/BraveSoftware/Brave-Browser/Safe Browsing/UrlBilling.store.4_13425813062472980: Cannot stat: No such file or directory
tar: ./home/kim/.config/BraveSoftware/Brave-Browser/Safe Browsing/UrlMalBin.store.4_13425813062473064: Cannot stat: No such file or directory
tar: Exiting with failure status due to previous errors
"""


def test_pax_zero_zstd_zero_is_ok():
    r = _result(0, 0)
    assert r.status == "ok"
    assert r.ok is True


def test_pax_one_zstd_zero_is_warnings():
    """pax exit 1 is non-fatal: 'file changed as we read it' style warnings.
    The pax stream is structurally valid, so the archive is trustworthy."""
    r = _result(1, 0)
    assert r.status == "ok-with-warnings"
    assert r.ok is True


def test_pax_two_zstd_zero_no_stderr_is_failed():
    """pax exit >=2 with no captured stderr stays failed — we can't certify the
    archive without seeing why pax bailed."""
    r = _result(2, 0)
    assert r.status == "failed"
    assert r.ok is False


def test_pax_two_empty_stderr_is_failed():
    """Exit >=2 but no diagnostics we recognise: don't trust it."""
    r = _result(2, 0, pax_stderr="")
    assert r.status == "failed"
    assert r.ok is False


def test_pax_two_vanished_files_is_warnings():
    """Exit 2 caused only by files vanishing mid-walk (ENOENT) is the same
    benign race as exit 1 — the stream is valid, missing members are skipped."""
    r = _result(2, 0, pax_stderr=_BRAVE_RACE_STDERR)
    assert r.status == "ok-with-warnings"
    assert r.ok is True


def test_pax_two_cannot_open_enoent_is_warnings():
    """The open-phase wording for a vanished file is equally benign."""
    stderr = ("tar: ./home/kim/cache/x: Cannot open: No such file or directory\n"
              "tar: Exiting with failure status due to previous errors\n")
    r = _result(2, 0, pax_stderr=stderr)
    assert r.status == "ok-with-warnings"


# Shard 3 of the 2026-06-14 home full, verbatim from home.s3of4.log: a vanished
# leveldb file (drives exit to 2) co-occurring with a "file changed as we read
# it" warning on a rotating log. Each line is benign on its own; the bug was
# that their *combination* on the exit-2 path tipped the shard — and thus the
# whole run — to "failed".
_MIXED_RACE_STDERR = """\
tar: ./home/kim/.Eigenlabs/3.0.2-beta-3/Log/eigend.0.log: file changed as we read it
tar: ./home/kim/.config/BraveSoftware/Brave-Browser/Default/IndexedDB/https_www.youtube.com_0.indexeddb.leveldb/001943.log: Cannot stat: No such file or directory
tar: Exiting with failure status due to previous errors
"""


def test_pax_two_file_changed_with_vanished_is_warnings():
    """Exit 2 from a vanished file, alongside a 'file changed as we read it'
    warning, is still benign — both are point-in-time races on volatile files
    and the stream stays valid. Regression for the 2026-06-14 false failure."""
    r = _result(2, 0, pax_stderr=_MIXED_RACE_STDERR)
    assert r.status == "ok-with-warnings"
    assert r.ok is True


def test_pax_two_file_changed_only_is_warnings():
    """'file changed as we read it' as the sole diagnostic on an exit-2 run is
    benign on its own too."""
    stderr = ("tar: ./home/kim/.xsession-errors: file changed as we read it\n"
              "tar: Exiting with failure status due to previous errors\n")
    r = _result(2, 0, pax_stderr=stderr)
    assert r.status == "ok-with-warnings"


def test_pax_two_enospc_is_failed():
    """A genuine fatal line among the vanished-file lines keeps it failed."""
    stderr = ("tar: ./home/kim/x: Cannot stat: No such file or directory\n"
              "tar: /mnt/Backups/a.pax.zst: Cannot write: No space left on device\n"
              "tar: Error is not recoverable: exiting now\n")
    r = _result(2, 0, pax_stderr=stderr)
    assert r.status == "failed"
    assert r.ok is False


def test_pax_two_permission_denied_is_failed():
    """Permission denied is not the vanished-file race; stay failed."""
    stderr = ("tar: ./home/kim/.ssh/id_ed25519: Cannot open: Permission denied\n"
              "tar: Exiting with failure status due to previous errors\n")
    r = _result(2, 0, pax_stderr=stderr)
    assert r.status == "failed"


def test_pax_two_benign_stderr_but_zstd_failed_is_failed():
    """A zstd failure dominates even when tar's stderr is all-benign."""
    r = _result(2, 7, pax_stderr=_BRAVE_RACE_STDERR)
    assert r.status == "failed"
    assert r.ok is False


def test_pax_zero_zstd_nonzero_is_failed():
    """zstd failure means the compressed stream is corrupt regardless of pax."""
    r = _result(0, 1)
    assert r.status == "failed"
    assert r.ok is False


def test_pax_one_zstd_nonzero_is_failed():
    """zstd failure dominates pax warnings."""
    r = _result(1, 1)
    assert r.status == "failed"
    assert r.ok is False
