"""Shared test fixtures."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import heal as heallib


@pytest.fixture(autouse=True)
def _no_deferred_wait(monkeypatch):
    """verify_frame_checksums pauses before re-checking suspect frames; record
    the pause instead of waiting it out (it would add minutes to the suite)."""
    slept: list[float] = []
    monkeypatch.setattr(heallib, "_sleep", slept.append)
    return slept
