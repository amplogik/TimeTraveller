"""Frame-integrity verification and cross-cycle healing.

Two responsibilities, sharing the same frame/index primitives:

  * D1 (verify-after-write) support: `verify_frame_checksums()` re-reads each
    frame's persisted compressed bytes and compares to the digest recorded at
    write time — optionally dropping the client page cache first so it checks
    what actually LANDED on the backup store, not the just-written RAM buffer.

  * D2 (heal-from-redundancy): when a frame is corrupt, `frames_to_files()`
    maps it to the files it holds (the blast radius), and `heal_files()` pulls
    a byte-identical good copy of each damaged file from another cycle's
    archives when one exists. This is the manual "restore that file from an
    older backup" dance, automated.

Both are read-only against the archive store except `heal_files`, which writes
recovered files into a destination directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import extract as extractlib
from . import framewriter as fwlib
from . import index as indexlib


# ---------- frame checksum verification (shared by D1 + D2) ----------

def verify_frame_checksums(
    archive_path: Path, *, drop_cache: bool = False,
) -> tuple[str, int, list[dict]] | None:
    """Re-hash each frame's persisted compressed bytes against its recorded
    `csum`. Returns (algo, frame_count, bad_frames) where each bad frame is the
    full frame record, or None if the archive has no v2 (csum-bearing) sidecar.

    With `drop_cache`, evict this file from the client page cache before reading
    so the check hits the backup store (which serves from its own cache, hot
    right after a write) rather than re-reading the client's own just-written
    buffer — the only way to catch a RAM→NIC→store bit flip.
    """
    sidecar = fwlib.sidecar_path(archive_path)
    if not sidecar.exists():
        return None
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
    frames = meta.get("frames") or []
    if not frames or "csum" not in frames[0]:
        return None  # v1 sidecar: no per-frame digests
    algo = meta.get("csum_algo", "sha256")
    bad: list[dict] = []
    fd = os.open(str(archive_path), os.O_RDONLY)
    try:
        if drop_cache:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        for fr in frames:
            data = os.pread(fd, fr["cl"], fr["co"])
            if len(data) != fr["cl"] or hashlib.new(algo, data).hexdigest() != fr["csum"]:
                bad.append(fr)
    finally:
        os.close(fd)
    return (algo, len(frames), bad)


# ---------- frame → files mapping (the blast radius) ----------

def frames_to_files(archive_path: Path, frame_ids: list[int]) -> list[str]:
    """Return the archive member names whose data lies in any of `frame_ids`.

    Inverts the `.idx.zst` (file → uncompressed offset/size) against the
    `.frames.json` (frame → uncompressed range). Returns [] if either sidecar
    is missing/legacy (no mapping possible). Only regular files with data can
    be affected; dirs/symlinks carry no frame bytes.
    """
    frames_sc = fwlib.sidecar_path(archive_path)
    if not frames_sc.exists():
        return []
    try:
        frames_doc = json.loads(frames_sc.read_text())
    except (OSError, ValueError):
        return []
    frames = frames_doc.get("frames") or []
    ranges: list[tuple[int, int]] = []
    for fid in frame_ids:
        if 0 <= fid < len(frames):
            fr = frames[fid]
            ranges.append((fr["uo"], fr["uo"] + fr["ul"]))
    if not ranges:
        return []

    records = extractlib._load_v2_sidecar(indexlib.sidecar_path(archive_path))
    if records is None:
        return []
    out: set[str] = set()
    for name, rec in records.items():
        if rec.get("type") != "f" or rec.get("size", 0) == 0:
            continue
        s = rec["data_offset"]
        e = s + rec["size"]
        for fs, fe in ranges:
            if s < fe and e > fs:
                out.add(name)
                break
    return sorted(out)


def damaged_files(archive_path: Path, *, drop_cache: bool = False) -> list[str]:
    """Verify `archive_path` and return the member names damaged by any bad
    frame. Empty if the archive is clean (or unverifiable)."""
    res = verify_frame_checksums(archive_path, drop_cache=drop_cache)
    if res is None:
        return []
    _algo, _n, bad = res
    if not bad:
        return []
    return frames_to_files(archive_path, [fr["id"] for fr in bad])


# ---------- D2: heal from redundancy ----------

@dataclass
class HealResult:
    healed: dict[str, str] = field(default_factory=dict)      # member -> source archive name
    unrecoverable: list[str] = field(default_factory=list)    # member with no clean copy anywhere
    bytes_written: int = 0


def _member_is_clean(archive_path: Path, member: str) -> bool:
    """True if `member` exists in `archive_path` and every frame holding its
    data passes its checksum — i.e. this archive holds a trustworthy copy."""
    records = extractlib._load_v2_sidecar(indexlib.sidecar_path(archive_path))
    if not records or member not in records:
        return False
    rec = records[member]
    if rec.get("type") != "f":
        return member in records  # dirs/symlinks carry no frame data → always "clean"
    frames_sc = fwlib.sidecar_path(archive_path)
    if not frames_sc.exists():
        return False
    try:
        frames = json.loads(frames_sc.read_text()).get("frames") or []
    except (OSError, ValueError):
        return False
    if rec.get("size", 0) == 0:
        return True
    start, end = rec["data_offset"], rec["data_offset"] + rec["size"]
    need = [fr for fr in frames if fr["uo"] < end and fr["uo"] + fr["ul"] > start]
    if not need or "csum" not in need[0]:
        return False
    fd = os.open(str(archive_path), os.O_RDONLY)
    try:
        for fr in need:
            data = os.pread(fd, fr["cl"], fr["co"])
            if len(data) != fr["cl"] or hashlib.sha256(data).hexdigest() != fr["csum"]:
                return False
    finally:
        os.close(fd)
    return True


def heal_files(
    members: list[str], *, into: Path, candidate_archives: list[Path],
) -> HealResult:
    """For each damaged member, find the first candidate archive holding a
    clean copy and extract it into `into`. Candidates should be searched
    newest-first by the caller so the freshest good copy wins.

    A member with no clean copy in any candidate is reported unrecoverable —
    the caller decides whether to prompt for more sources.
    """
    result = HealResult()
    for member in members:
        source = next((a for a in candidate_archives if _member_is_clean(a, member)), None)
        if source is None:
            result.unrecoverable.append(member)
            continue
        stats = extractlib.extract_files(source, [member], into=into)
        result.healed[member] = source.name
        result.bytes_written += stats.bytes_written
    return result
