"""Archive filename and glob-translation tests."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller.pax import archive_filename, glob_to_regexes, parse_filename


def _match_any(pat_list: list[str], s: str) -> bool:
    return any(re.match(rx, s) for rx in pat_list)


def test_scheduled_filename_is_date_only():
    dt = datetime(2026, 5, 17, 2, 0, tzinfo=timezone.utc)
    assert archive_filename(dt=dt, kind="full") == "2026-05-17_full.pax.zst"
    assert archive_filename(dt=dt, kind="incr") == "2026-05-17_incr.pax.zst"


def test_manual_filename_includes_time():
    dt = datetime(2026, 5, 17, 14, 30, 22, tzinfo=timezone.utc)
    assert archive_filename(dt=dt, kind="full", manual=True) == "2026-05-17T143022_full.pax.zst"


def test_parse_filename_roundtrip():
    assert parse_filename("2026-05-17_full.pax.zst") == ("2026-05-17", "full")
    assert parse_filename("2026-05-17T143022_incr.pax.zst") == ("2026-05-17T143022", "incr")
    assert parse_filename("garbage.zst") is None


def test_glob_to_regex_basic():
    # **/.cache/  → match any depth, the dir itself, and its contents
    rxs = glob_to_regexes("**/.cache/")
    assert _match_any(rxs, "./home/kim/.cache")
    assert _match_any(rxs, "./home/kim/.cache/foo/bar")
    # Must not match a similarly-named file deeper.
    assert not _match_any(rxs, "./var/cache/apt/archives")


def test_glob_to_regex_anchored():
    # /home/** → archive members under ./home/
    rxs = glob_to_regexes("/home/**")
    assert _match_any(rxs, "./home/kim/foo.txt")
    # Anchored: must start with `./`; bare `home/...` should NOT match.
    assert not _match_any(rxs, "home/kim/foo.txt")
    # Should not match etc paths.
    assert not _match_any(rxs, "./etc/passwd")


def test_glob_to_regex_node_modules():
    rxs = glob_to_regexes("**/node_modules/")
    assert _match_any(rxs, "./home/kim/proj/node_modules")
    assert _match_any(rxs, "./home/kim/proj/node_modules/foo/index.js")


def test_glob_to_regex_anchored_dir_with_trailing_slash():
    # /tmp/ → the dir itself and its contents
    rxs = glob_to_regexes("/tmp/")
    assert _match_any(rxs, "./tmp")
    assert _match_any(rxs, "./tmp/foo")
    assert not _match_any(rxs, "./var/tmp")
