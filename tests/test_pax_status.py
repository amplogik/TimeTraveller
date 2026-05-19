"""Tri-state RunResult.status tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller.pax import RunResult


def _result(pax_rc: int, zstd_rc: int) -> RunResult:
    return RunResult(
        pax_returncode=pax_rc,
        zstd_returncode=zstd_rc,
        archive_path=Path("/tmp/unused.pax.zst"),
        archive_size=0,
        duration_seconds=0.0,
    )


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


def test_pax_two_zstd_zero_is_failed():
    """pax exit >=2 is a fatal walk error — archive may be incomplete."""
    r = _result(2, 0)
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
