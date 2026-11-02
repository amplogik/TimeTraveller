"""A file tar can't read (permission denied) must cost that one file, loudly --
not quarantine the whole archive. Regression for 2026-11-01: a root:root 0600
file under ~/Desktop sent a ~70 GB shard of every weekly full to .failed for
seven weeks."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as manifestlib
from timetraveller import pax as paxlib
from timetraveller.worker import _finalize_one

from tests.test_finalize_archive import _entry, _plan

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads everything; can't make a file unreadable")


def _unreadable_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "keep").mkdir(parents=True)
    (src / "keep" / "a.txt").write_text("hello\n")
    secret = src / "creds.properties"
    secret.write_text("x=1\n")
    secret.chmod(0)
    return src


def test_real_tar_permission_denied_is_warning_and_named(tmp_path):
    src = _unreadable_tree(tmp_path)
    out = tmp_path / "mount" / "a.pax.zst"
    inv = paxlib.PaxInvocation(sources=["."], chdir=str(src), archive_path=out,
                               excludes=[], extra_mount_excludes=[])
    try:
        r = paxlib.run(inv, log_file=tmp_path / "run.log")
    finally:
        (src / "creds.properties").chmod(0o600)

    # pax exits 1 here (GNU tar, the shard path, exits 2 -- covered in
    # test_pax_status); either way: warning, archive kept, file named.
    assert r.pax_returncode in (1, 2)
    assert r.status == "ok-with-warnings"
    assert r.skipped_unreadable == ["./creds.properties"]
    assert out.exists()
    # The readable file is in the archive; the stream is intact.
    listing = subprocess.run(f"zstd -dc {out} | tar -t", shell=True, check=True,
                             capture_output=True, text=True).stdout
    assert "./keep/a.txt" in listing
    assert "./creds.properties" not in listing


def test_finalize_keeps_archive_and_records_skips(tmp_path, capsys):
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    fname = "2026-11-01_full.s5of8.pax.zst"
    archive = archive_dir / fname
    subprocess.run(f"tar -cf - -C {tmp_path} --files-from /dev/null | zstd -q -o {archive}",
                   shell=True, check=True)

    m = manifestlib.Manifest(plan_name="testplan", archives=[_entry(fname)])
    result = paxlib.RunResult(
        pax_returncode=2, zstd_returncode=0, archive_path=archive,
        archive_size=archive.stat().st_size, duration_seconds=0.0,
        pax_stderr=("tar: ./home/kim/Desktop/root-owned.properties: Cannot open: "
                    "Permission denied\n"
                    "tar: Exiting with failure status due to previous errors\n"))
    log = tmp_path / "shard.log"
    args = argparse.Namespace(quiet=True)

    status = _finalize_one(m, archive, fname, result, plan, args, log)

    assert status == "ok-with-warnings"
    assert archive.exists()
    assert not archive.with_name(fname + ".failed").exists()
    entry = m.archives[0]
    assert entry.status == "ok-with-warnings"
    assert "skipped 1 unreadable file(s)" in entry.notes
    assert "./home/kim/Desktop/root-owned.properties" in entry.notes
    err = capsys.readouterr().err
    assert "NOT in this backup" in err
    assert "SKIPPED-UNREADABLE" in log.read_text()


def test_real_tar_file_list_path_exit2_is_warning(tmp_path):
    """The shard path (run_with_file_list -> GNU tar) is the one that exited 2
    and quarantined the shard in production."""
    src = _unreadable_tree(tmp_path)
    out = tmp_path / "mount" / "b.pax.zst"
    inv = paxlib.PaxInvocation(sources=["."], chdir=str(src), archive_path=out,
                               excludes=[], extra_mount_excludes=[])
    try:
        r = paxlib.run_with_file_list(inv, ["./keep/a.txt", "./creds.properties"],
                                      log_file=tmp_path / "run.log")
    finally:
        (src / "creds.properties").chmod(0o600)
    assert r.pax_returncode == 2
    assert r.status == "ok-with-warnings"
    assert r.skipped_unreadable == ["./creds.properties"]
