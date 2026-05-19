"""Cached file-listing sidecar for each archive.

After a successful archive write we run `pax -tv` on the archive contents and
store the output as `<archive>.idx.zst`. The GUI reads this sidecar to populate
its tree-view without scanning the (potentially many-GB) archive itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def sidecar_path(archive_path: Path) -> Path:
    return archive_path.with_name(archive_path.name + ".idx.zst")


def sidecar_mirror_path(plan_name: str, archive_filename: str) -> Path:
    """Local-disk mirror path for an archive's sidecar.

    Sidecars are tens of KB compressed, so mirroring all of them locally is
    cheap (~hundreds of KB to a few MB per plan). This is what lets the GUI
    render archive content trees without touching the backup mount.
    """
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return (Path(xdg) / "timetraveller" / plan_name / "sidecars"
            / (archive_filename + ".idx.zst"))


def copy_sidecar_to_mirror(plan_name: str, source_sidecar: Path,
                           archive_filename: str) -> None:
    """Atomically copy an on-mount sidecar to the local mirror.

    Raises OSError on failure — callers that don't care should swallow.
    """
    dst = sidecar_mirror_path(plan_name, archive_filename)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(source_sidecar, tmp)
    tmp.replace(dst)


def delete_sidecar_mirror(plan_name: str, archive_filename: str) -> None:
    """Remove a sidecar from the local mirror. Idempotent — silent if missing."""
    p = sidecar_mirror_path(plan_name, archive_filename)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def write_sidecar(archive_path: Path) -> Path:
    """Generate `<archive>.idx.zst` from the archive.

    Pipeline:  zstdcat archive.pax.zst | tar -tvf - | zstd -o <sidecar>

    We use GNU tar (not paxmirabilis) because the archives are written in
    POSIX pax-extended-header format — paxmirabilis silently truncates
    listings of pax-1.0 archives, missing entries that use extended
    headers (large files, long paths). GNU tar parses them correctly.
    """
    sidecar = sidecar_path(archive_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)

    zstdcat = subprocess.Popen(
        ["zstdcat", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert zstdcat.stdout is not None
    tar = subprocess.Popen(
        ["tar", "-tvf", "-"],
        stdin=zstdcat.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    zstdcat.stdout.close()
    assert tar.stdout is not None
    zstd = subprocess.Popen(
        ["zstd", "-3", "-T0", "-o", str(sidecar), "-q"],
        stdin=tar.stdout,
        stderr=subprocess.DEVNULL,
    )
    tar.stdout.close()
    zstd_rc = zstd.wait()
    tar_rc = tar.wait()
    zstdcat_rc = zstdcat.wait()
    if any(rc != 0 for rc in (zstdcat_rc, tar_rc, zstd_rc)):
        raise RuntimeError(
            f"sidecar generation failed: zstdcat={zstdcat_rc} tar={tar_rc} zstd={zstd_rc}"
        )
    return sidecar


def read_sidecar(sidecar: Path) -> list[str]:
    """Return the decompressed sidecar contents as a list of lines."""
    out = subprocess.run(
        ["zstdcat", str(sidecar)],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.splitlines()
