"""Streaming search across v2 `.idx.zst` sidecars.

Given an archive's sidecar and a search term, iterate matching entries
without materializing the full sidecar in memory. The fast path is a
bytes-level case-folded substring prefilter: lines that can't possibly
match are skipped without invoking json.loads. Lines that pass the
prefilter are parsed and re-checked against the precise mode rule
(basename-only vs. anywhere in the path).

For a million-entry sidecar (typical home-plan full), the prefilter is
sub-second on modern hardware because json parsing only runs on the
handful of lines that contain the search term as bytes.

The case-insensitivity is ASCII-folded (Python `bytes.lower()` doesn't
touch multi-byte UTF-8 sequences). Practical for the GUI use case
("recipe.md", "passwords.txt"); see search_widget for the search UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import zstandard as zstd
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "search requires the 'zstandard' package. Install with:\n"
        "    sudo apt install python3-zstandard"
    ) from e


MIN_SEARCH_LEN = 3

MODE_BASENAME = "basename"
MODE_PATH = "path"


@dataclass(frozen=True)
class Match:
    """One file entry that matched a search.

    `archive` is the archive filename (e.g. "2026-05-31_full.pax.zst");
    callers set this to identify which archive the match came from.
    `path` is the full archive path including the leading "./".
    `mtime` is the file's mtime as recorded in the tar header — i.e.
    the file's modification time *at the moment of backup*, which is
    what the user uses to pick the right version to restore.
    """
    archive: str
    path: str
    name: str
    size: int
    mtime: int
    type: str


def iter_matches(sidecar_path: Path, term: str,
                 *, mode: str = MODE_BASENAME) -> Iterator[Match]:
    """Yield matches from a single sidecar.

    `term` shorter than MIN_SEARCH_LEN yields nothing — the caller is
    expected to gate on the same minimum so the GUI doesn't kick off a
    sweeping scan on every keystroke.

    Robustness: bad JSON lines (corrupted sidecar, format mismatch) are
    silently skipped rather than aborting the iteration. A v1 sidecar
    (plain `tar -tvf` text) will produce zero matches because its lines
    don't survive json.loads.
    """
    if len(term) < MIN_SEARCH_LEN:
        return
    if mode not in (MODE_BASENAME, MODE_PATH):
        raise ValueError(f"unknown search mode: {mode!r}")

    term_lower = term.lower()
    term_bytes = term_lower.encode("utf-8")
    archive_name = sidecar_path.name.removesuffix(".idx.zst")

    dctx = zstd.ZstdDecompressor()
    buf = b""
    header_consumed = False
    with open(sidecar_path, "rb") as raw_in:
        with dctx.stream_reader(raw_in) as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line, buf = buf[:nl], buf[nl + 1:]
                    if not header_consumed:
                        header_consumed = True
                        # Header object — no file payload to match.
                        continue
                    if not line:
                        continue
                    if term_bytes not in line.lower():
                        continue
                    m = _line_to_match(line, term_lower, mode, archive_name)
                    if m is not None:
                        yield m
    if buf:
        m = _line_to_match(buf, term_lower, mode, archive_name)
        if m is not None:
            yield m


def _line_to_match(line: bytes, term_lower: str, mode: str,
                   archive_name: str) -> Match | None:
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    name = rec.get("name")
    if not isinstance(name, str):
        return None
    basename = name.rsplit("/", 1)[-1]
    hay = basename if mode == MODE_BASENAME else name
    if term_lower not in hay.lower():
        return None
    return Match(
        archive=archive_name,
        path=name,
        name=basename,
        size=int(rec.get("size") or 0),
        mtime=int(rec.get("mtime") or 0),
        type=str(rec.get("type") or "?"),
    )
