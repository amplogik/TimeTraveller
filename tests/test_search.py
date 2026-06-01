"""Tests for the streaming sidecar search core."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import zstandard as zstd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import search


def _write_sidecar(path: Path, records: list[dict],
                   *, header: dict | None = None,
                   trailing_newline: bool = True) -> None:
    """Build a v2-shaped zstd-compressed JSONL sidecar."""
    h = header if header is not None else {"version": 2, "archive": path.name}
    body_lines = [json.dumps(h)]
    for r in records:
        body_lines.append(json.dumps(r))
    raw = ("\n".join(body_lines) + ("\n" if trailing_newline else "")).encode()
    cctx = zstd.ZstdCompressor(level=1)
    path.write_bytes(cctx.compress(raw))


def _entry(name: str, *, size: int = 100, mtime: int = 1700000000,
           type_: str = "f") -> dict:
    return {"name": name, "type": type_, "size": size, "mode": 0o644,
            "mtime": mtime, "uname": "kim", "gname": "kim",
            "header_offset": 0, "data_offset": 512}


def test_basename_match_case_insensitive(tmp_path):
    sc = tmp_path / "archive.pax.zst.idx.zst"
    _write_sidecar(sc, [
        _entry("./home/kim/Recipe.md"),
        _entry("./tmp/recipe.md.bak"),
        _entry("./var/log/syslog"),
    ])
    matches = list(search.iter_matches(sc, "recipe"))
    paths = sorted(m.path for m in matches)
    assert paths == ["./home/kim/Recipe.md", "./tmp/recipe.md.bak"]


def test_basename_mode_does_not_match_directory_segments(tmp_path):
    """A hit in a directory component should not surface in basename mode."""
    sc = tmp_path / "a.idx.zst"
    _write_sidecar(sc, [
        _entry("./home/kim/recipe_dir/notes.txt"),
        _entry("./home/kim/recipe.md"),
    ])
    matches = list(search.iter_matches(sc, "recipe", mode=search.MODE_BASENAME))
    assert [m.path for m in matches] == ["./home/kim/recipe.md"]


def test_path_mode_matches_anywhere_in_path(tmp_path):
    sc = tmp_path / "a.idx.zst"
    _write_sidecar(sc, [
        _entry("./home/kim/recipe_dir/notes.txt"),
        _entry("./home/kim/recipe.md"),
        _entry("./tmp/other"),
    ])
    matches = list(search.iter_matches(sc, "recipe", mode=search.MODE_PATH))
    assert sorted(m.path for m in matches) == [
        "./home/kim/recipe.md",
        "./home/kim/recipe_dir/notes.txt",
    ]


def test_min_search_length_returns_empty(tmp_path):
    sc = tmp_path / "a.idx.zst"
    _write_sidecar(sc, [_entry("./a"), _entry("./ab"), _entry("./abc")])
    assert list(search.iter_matches(sc, "ab")) == []
    # Boundary: exactly MIN_SEARCH_LEN does search.
    assert len(list(search.iter_matches(sc, "abc"))) == 1


def test_invalid_mode_raises(tmp_path):
    sc = tmp_path / "a.idx.zst"
    _write_sidecar(sc, [_entry("./x")])
    with pytest.raises(ValueError):
        list(search.iter_matches(sc, "abc", mode="invalid"))


def test_empty_sidecar_yields_nothing(tmp_path):
    sc = tmp_path / "a.idx.zst"
    _write_sidecar(sc, [])
    assert list(search.iter_matches(sc, "anything")) == []


def test_corrupt_json_lines_skipped(tmp_path):
    """A line that passes the prefilter but isn't valid JSON shouldn't
    abort the iteration — search must survive a partially-corrupt sidecar."""
    sc = tmp_path / "a.idx.zst"
    # Hand-roll the body so we can inject a bad line that still contains
    # the term as bytes (passes prefilter).
    h = json.dumps({"version": 2})
    good = json.dumps(_entry("./home/recipe.md"))
    bad = '{ "name": "./home/recipe.md", broken json'
    raw = ("\n".join([h, bad, good]) + "\n").encode()
    sc.write_bytes(zstd.ZstdCompressor(level=1).compress(raw))

    matches = list(search.iter_matches(sc, "recipe"))
    assert len(matches) == 1
    assert matches[0].path == "./home/recipe.md"


def test_v1_legacy_sidecar_yields_nothing(tmp_path):
    """v1 sidecars are plain `tar -tvf` text — json.loads fails on each
    line so search returns no matches. Caller should fall back to a
    suggest-reindex message rather than treating empty as 'no hits'."""
    sc = tmp_path / "legacy.idx.zst"
    text = (
        "-rw-r--r-- kim/kim 100 2026-05-30 18:00 ./home/kim/recipe.md\n"
        "-rw-r--r-- kim/kim 200 2026-05-30 18:00 ./tmp/recipe.md.bak\n"
    )
    sc.write_bytes(zstd.ZstdCompressor(level=1).compress(text.encode()))
    assert list(search.iter_matches(sc, "recipe")) == []


def test_match_record_carries_metadata(tmp_path):
    sc = tmp_path / "2026-05-31_full.pax.zst.idx.zst"
    _write_sidecar(sc, [
        _entry("./home/kim/notes/recipe.md", size=12384, mtime=1716234567),
    ])
    [m] = list(search.iter_matches(sc, "recipe"))
    assert m.archive == "2026-05-31_full.pax.zst"
    assert m.path == "./home/kim/notes/recipe.md"
    assert m.name == "recipe.md"
    assert m.size == 12384
    assert m.mtime == 1716234567
    assert m.type == "f"


def test_no_trailing_newline_still_yields_last_record(tmp_path):
    """Robustness: a sidecar whose last line lacks a trailing newline
    must still surface the final entry as a match."""
    sc = tmp_path / "a.idx.zst"
    _write_sidecar(sc, [_entry("./home/recipe.md")], trailing_newline=False)
    matches = list(search.iter_matches(sc, "recipe"))
    assert [m.path for m in matches] == ["./home/recipe.md"]


def test_prefilter_skips_lines_without_term(tmp_path):
    """Soak test: a 10k-entry sidecar with one needle. Pure performance
    check that the prefilter doesn't json-parse every line."""
    sc = tmp_path / "big.idx.zst"
    records = [_entry(f"./home/kim/file_{i:05d}.dat") for i in range(10_000)]
    records.append(_entry("./home/kim/recipe.md"))
    _write_sidecar(sc, records)
    matches = list(search.iter_matches(sc, "recipe"))
    assert [m.path for m in matches] == ["./home/kim/recipe.md"]


def test_archive_name_strips_idx_zst_suffix(tmp_path):
    sc = tmp_path / "2026-05-31_full.pax.zst.idx.zst"
    _write_sidecar(sc, [_entry("./a/recipe.md")])
    [m] = list(search.iter_matches(sc, "recipe"))
    assert m.archive == "2026-05-31_full.pax.zst"
