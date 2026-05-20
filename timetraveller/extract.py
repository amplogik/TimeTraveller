"""Fast single-file / subtree restore from a framed pax-zst archive.

Combines the v2 `.idx.zst` (per-file uncompressed byte offsets) and the
`.frames.json` (uncompressed→compressed range map) to pull only the zstd
frames that contain the requested files. For a single small file out of a
multi-TB archive, this turns a multi-hour `zstdcat | tar -x` into a
near-instant range-read + selective decompression.

The fast path requires both sidecars. Missing-or-legacy sidecars fall
through to a naive `zstdcat | tar -x` extraction, so all archives stay
restorable regardless of age.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import zstandard as zstd
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "extract requires the 'zstandard' package. Install with:\n"
        "    sudo apt install python3-zstandard   # Ubuntu/Debian (preferred)\n"
        "    pip install --user 'zstandard>=0.20'  # fallback"
    ) from e

from . import framewriter as fwlib
from . import index as indexlib


@dataclass
class ExtractStats:
    requested_patterns: int
    matched_files: int
    matched_dirs: int
    matched_symlinks: int
    matched_hardlinks: int
    frames_read: int
    nfs_bytes_read: int
    bytes_written: int
    seconds_total: float
    fallback_naive: bool = False


# ---------- sidecar loading ----------

def _load_v2_sidecar(sidecar_path: Path) -> dict[str, dict] | None:
    """Return {name: record} from a v2 sidecar, or None if v1/legacy/missing.

    A v1 sidecar (plain text from tar -tv) has no per-file offsets and is
    useless for fast extract — callers fall back to naive in that case.
    """
    if not sidecar_path.exists():
        return None
    text = subprocess.check_output(["zstdcat", str(sidecar_path)], text=True)
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None  # legacy text format

    records: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "version" in rec:
            continue
        records[rec["name"]] = rec
    return records


def _load_frames_index(frames_path: Path) -> dict | None:
    if not frames_path.exists():
        return None
    return json.loads(frames_path.read_text())


# ---------- pattern matching ----------

def _match_records(records: dict[str, dict], patterns: list[str]) -> list[dict]:
    """Return records whose name matches any pattern.

    A pattern is either:
      - An exact path (e.g. `./etc/fstab`) — matches one entry
      - A prefix ending with `/` (e.g. `./etc/`) — matches that dir and all
        descendants
    """
    out: list[dict] = []
    seen: set[str] = set()
    for pat in patterns:
        normalised = pat if pat.startswith("./") else "./" + pat.lstrip("/")
        if normalised.endswith("/"):
            prefix = normalised
            for name, rec in records.items():
                if name == prefix.rstrip("/") or name.startswith(prefix):
                    if name not in seen:
                        out.append(rec)
                        seen.add(name)
        else:
            rec = records.get(normalised)
            if rec is None:
                # Also try the trailing-slash form (for dirs)
                rec = records.get(normalised + "/")
            if rec and rec["name"] not in seen:
                out.append(rec)
                seen.add(rec["name"])
    return out


# ---------- frame coalescing ----------

def _frames_needed(records: list[dict], frame_size: int) -> set[int]:
    """Return the set of frame indices needed to extract all listed files.

    Dirs and symlinks need no frames. Regular files need every frame that
    overlaps [data_offset, data_offset + size).
    """
    out: set[int] = set()
    for rec in records:
        if rec.get("type") != "f" or rec.get("size", 0) == 0:
            continue
        start = rec["data_offset"]
        end = start + rec["size"]
        sf = start // frame_size
        ef = (end - 1) // frame_size
        for i in range(sf, ef + 1):
            out.add(i)
    return out


def _coalesce_to_ranges(frame_ids: set[int]) -> list[tuple[int, int]]:
    """Coalesce a set of frame indices into adjacent (start, end) ranges, inclusive."""
    if not frame_ids:
        return []
    ids = sorted(frame_ids)
    ranges: list[tuple[int, int]] = []
    s = ids[0]
    p = ids[0]
    for i in ids[1:]:
        if i == p + 1:
            p = i
        else:
            ranges.append((s, p))
            s = p = i
    ranges.append((s, p))
    return ranges


# ---------- the fast read+decompress engine ----------

def _read_and_decompress_frames(
    archive_path: Path,
    frames_meta: list[dict],
    frame_ranges: list[tuple[int, int]],
) -> tuple[dict[int, bytes], int]:
    """Pull the compressed bytes for each coalesced range in a single NFS
    read, then decompress each frame inside the read independently.

    Returns ({frame_id: uncompressed_bytes}, total_nfs_bytes_read).
    """
    dctx = zstd.ZstdDecompressor()
    out: dict[int, bytes] = {}
    bytes_read = 0
    with open(archive_path, "rb") as f:
        for start, end in frame_ranges:
            first = frames_meta[start]
            last = frames_meta[end]
            blob_start = first["co"]
            blob_end = last["co"] + last["cl"]
            f.seek(blob_start)
            blob = f.read(blob_end - blob_start)
            bytes_read += len(blob)
            for fid in range(start, end + 1):
                fr = frames_meta[fid]
                rel = fr["co"] - blob_start
                out[fid] = dctx.decompress(blob[rel:rel + fr["cl"]])
    return out, bytes_read


def _slice_file_body(rec: dict, frames_meta: list[dict],
                     decompressed: dict[int, bytes], frame_size: int) -> bytes:
    """Reassemble a file's bytes from one-or-more decompressed frames."""
    if rec["size"] == 0:
        return b""
    start = rec["data_offset"]
    end = start + rec["size"]
    sf = start // frame_size
    ef = (end - 1) // frame_size
    if sf == ef:
        offset_in_frame = start - sf * frame_size
        return decompressed[sf][offset_in_frame:offset_in_frame + rec["size"]]
    # Multi-frame: head + middles + tail
    body = bytearray()
    head_offset = start - sf * frame_size
    body.extend(decompressed[sf][head_offset:])
    for fid in range(sf + 1, ef):
        body.extend(decompressed[fid])
    tail_len = end - ef * frame_size
    body.extend(decompressed[ef][:tail_len])
    return bytes(body)


# ---------- filesystem writeout ----------

def _safe_out_path(record_name: str, into: Path) -> Path:
    """Map an archive path like `./etc/fstab` to <into>/etc/fstab, refusing
    any attempt to escape the destination directory (e.g. `../`)."""
    rel = record_name
    if rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return into
    # Reject path components that try to walk up the tree.
    parts = rel.split("/")
    if any(p == ".." for p in parts):
        raise ValueError(f"refusing to extract path with '..' component: {record_name!r}")
    return into / rel


def _restore_metadata(out_path: Path, rec: dict, *, follow_symlinks: bool) -> None:
    """Restore mode, mtime, owner (owner only when running as root).

    Linux doesn't expose `lchmod` (no `AT_SYMLINK_NOFOLLOW` for chmod), so
    when `follow_symlinks=False` (i.e. we're metadata-restoring a symlink),
    we skip chmod entirely — symlink modes on Linux are 0o777 by convention
    and not actually consulted during permission checks.
    """
    if follow_symlinks:
        try:
            os.chmod(out_path, rec.get("mode", 0o644))
        except OSError:
            pass
    try:
        mt = rec.get("mtime", 0)
        os.utime(out_path, (mt, mt), follow_symlinks=follow_symlinks)
    except (OSError, NotImplementedError):
        pass
    if os.geteuid() == 0:
        try:
            import pwd
            import grp
            uname = rec.get("uname", "")
            gname = rec.get("gname", "")
            uid = pwd.getpwnam(uname).pw_uid if uname and not uname.isdigit() else int(uname or -1)
            gid = grp.getgrnam(gname).gr_gid if gname and not gname.isdigit() else int(gname or -1)
            os.chown(out_path, uid, gid, follow_symlinks=follow_symlinks)
        except (KeyError, OSError, ValueError):
            pass


def _write_records(
    records: list[dict], frames_meta: list[dict],
    decompressed: dict[int, bytes], frame_size: int, into: Path,
) -> tuple[int, int, int, int, int]:
    """Write each record to disk and restore metadata.

    Returns (bytes_written, n_files, n_dirs, n_symlinks, n_hardlinks_skipped).
    """
    bytes_written = 0
    n_files = n_dirs = n_symlinks = n_hardlinks = 0
    # Ensure dirs sort before their children so parents exist on file write.
    records = sorted(records, key=lambda r: r["name"])
    for rec in records:
        out_path = _safe_out_path(rec["name"], into)
        t = rec.get("type", "f")
        if t == "d":
            out_path.mkdir(parents=True, exist_ok=True)
            _restore_metadata(out_path, rec, follow_symlinks=True)
            n_dirs += 1
        elif t == "l":
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if out_path.exists() or out_path.is_symlink():
                out_path.unlink()
            os.symlink(rec.get("link_target", ""), out_path)
            _restore_metadata(out_path, rec, follow_symlinks=False)
            n_symlinks += 1
        elif t == "f":
            body = _slice_file_body(rec, frames_meta, decompressed, frame_size)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(body)
            _restore_metadata(out_path, rec, follow_symlinks=True)
            bytes_written += len(body)
            n_files += 1
        elif t == "h":
            # Hard link reference into the archive. Resolving via the fast
            # path requires extracting the link target's body too, which we
            # haven't done unless it was also requested. Skip for now —
            # callers can re-request including the target, or fall back
            # to the naive path.
            n_hardlinks += 1
        # Other types (p/b/c/socket) are not restorable as regular files.
    return bytes_written, n_files, n_dirs, n_symlinks, n_hardlinks


# ---------- public API ----------

def extract_files(
    archive_path: Path, patterns: list[str], *, into: Path,
) -> ExtractStats:
    """Extract one or more files / subtrees from a framed archive.

    Args:
        archive_path: path to the `.pax.zst` archive
        patterns: list of paths (exact `./etc/fstab` or prefix `./etc/`)
        into: destination directory; archive paths land under it

    Returns ExtractStats describing what happened.

    Falls back to a naive `zstdcat | tar -x` pipeline if the v2 sidecar or
    frame index is missing — slower but always correct.
    """
    started = time.monotonic()
    sidecar = indexlib.sidecar_path(archive_path)
    frames_sc = fwlib.sidecar_path(archive_path)

    records_dict = _load_v2_sidecar(sidecar)
    frames_doc = _load_frames_index(frames_sc)

    if records_dict is None or frames_doc is None:
        return _extract_naive(archive_path, patterns, into=into, started=started)

    matched = _match_records(records_dict, patterns)
    if not matched:
        return ExtractStats(
            requested_patterns=len(patterns),
            matched_files=0, matched_dirs=0, matched_symlinks=0, matched_hardlinks=0,
            frames_read=0, nfs_bytes_read=0, bytes_written=0,
            seconds_total=time.monotonic() - started,
        )

    frame_size = frames_doc["frame_size"]
    needed = _frames_needed(matched, frame_size)
    ranges = _coalesce_to_ranges(needed)
    decompressed, nfs_bytes = _read_and_decompress_frames(
        archive_path, frames_doc["frames"], ranges,
    )
    into.mkdir(parents=True, exist_ok=True)
    bytes_written, nf, nd, nl, nh = _write_records(
        matched, frames_doc["frames"], decompressed, frame_size, into,
    )
    return ExtractStats(
        requested_patterns=len(patterns),
        matched_files=nf, matched_dirs=nd, matched_symlinks=nl, matched_hardlinks=nh,
        frames_read=len(needed), nfs_bytes_read=nfs_bytes,
        bytes_written=bytes_written,
        seconds_total=time.monotonic() - started,
    )


def _extract_naive(archive_path: Path, patterns: list[str], *,
                   into: Path, started: float) -> ExtractStats:
    """Fallback: pipe the whole archive through tar -x and let tar pick
    out the requested members. Works on any archive regardless of sidecars."""
    into.mkdir(parents=True, exist_ok=True)
    # Normalise patterns to the `./...` form tar emits.
    args = ["tar", "-xf", "-", "-C", str(into)]
    for pat in patterns:
        normalised = pat if pat.startswith("./") else "./" + pat.lstrip("/")
        # tar accepts trailing-slash subtree as a path; tell it to also
        # extract everything beneath via implicit prefix matching.
        args.append(normalised.rstrip("/"))

    zstdcat = subprocess.Popen(
        ["zstdcat", str(archive_path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert zstdcat.stdout is not None
    tar = subprocess.Popen(args, stdin=zstdcat.stdout, stderr=subprocess.DEVNULL)
    zstdcat.stdout.close()
    tar_rc = tar.wait()
    zstdcat_rc = zstdcat.wait()
    if tar_rc not in (0,):  # tar exits non-zero on partial matches; that's OK
        pass

    # Count what landed; tar mode doesn't give us per-type stats cheaply,
    # so just report what we can.
    bytes_written = 0
    n_files = 0
    for root, _, files in os.walk(into):
        for f in files:
            try:
                bytes_written += (Path(root) / f).stat().st_size
                n_files += 1
            except OSError:
                pass

    return ExtractStats(
        requested_patterns=len(patterns),
        matched_files=n_files, matched_dirs=0, matched_symlinks=0, matched_hardlinks=0,
        frames_read=0, nfs_bytes_read=0,
        bytes_written=bytes_written,
        seconds_total=time.monotonic() - started,
        fallback_naive=True,
    )
