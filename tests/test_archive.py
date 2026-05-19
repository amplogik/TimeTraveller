"""Tests for archive.parse_index — index sidecar parsing."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller.archive import build_extract_argv, parse_index


REAL_SIDECAR_TEXT = """\
drwxrwxr-x kim/kim           0 2026-05-17 20:50 ./tmp/tt-source/
drwxrwxr-x kim/kim           0 2026-05-17 20:50 ./tmp/tt-source/b/
-rw-rw-r-- kim/kim           5 2026-05-17 20:50 ./tmp/tt-source/b/beta.txt
drwxrwxr-x kim/kim           0 2026-05-17 20:50 ./tmp/tt-source/a/
-rw-rw-r-- kim/kim           6 2026-05-17 20:50 ./tmp/tt-source/a/alpha.txt
"""


def test_parse_real_sidecar():
    root = parse_index(REAL_SIDECAR_TEXT)
    assert root.is_dir
    tmp = root.children["tmp"]
    assert tmp.is_dir
    assert tmp.full_path == "./tmp"
    src = tmp.children["tt-source"]
    assert src.is_dir
    assert "a" in src.children and "b" in src.children
    alpha = src.children["a"].children["alpha.txt"]
    assert not alpha.is_dir
    assert alpha.size == 6
    assert alpha.full_path == "./tmp/tt-source/a/alpha.txt"
    assert alpha.owner == "kim"


def test_tool_diagnostic_lines_skipped():
    """tar/pax warnings shouldn't be parsed as file entries."""
    text = (
        "drwxrwxr-x kim/kim 0 2026-05-17 20:50 ./tmp/foo/\n"
        "tar: ./tmp/foo/sock: socket ignored\n"
        "pax: anything else weird\n"
    )
    root = parse_index(text)
    for name in root.children:
        assert "tar" not in name and "pax" not in name


def test_intermediate_dir_synthesized():
    """If a file appears before its parent directory entry, the parent is created."""
    text = "-rw-r--r-- u/g 1 2026-05-17 20:50 ./deep/nested/file.txt\n"
    root = parse_index(text)
    deep = root.children["deep"]
    assert deep.is_dir
    nested = deep.children["nested"]
    assert nested.is_dir
    f = nested.children["file.txt"]
    assert not f.is_dir
    assert f.size == 1


def test_symlink_target_stripped():
    text = "lrwxrwxr-x u/g 0 2026-05-17 20:50 ./link -> ./target\n"
    root = parse_index(text)
    link = root.children["link"]
    assert not link.is_dir
    assert link.full_path == "./link"
    assert link.symlink_target == "./target"


def test_path_with_spaces():
    text = "-rw-r--r-- u/g 5 2026-05-17 20:50 ./My Documents/notes.txt\n"
    root = parse_index(text)
    docs = root.children["My Documents"]
    assert docs.is_dir
    note = docs.children["notes.txt"]
    assert note.size == 5
    assert note.full_path == "./My Documents/notes.txt"


def test_setuid_permissions_preserved():
    text = "-rwsr-x--- root/daemon 60 2026-05-17 21:15 ./tmp/important.txt\n"
    root = parse_index(text)
    f = root.children["tmp"].children["important.txt"]
    assert f.perms == "-rwsr-x---"
    assert f.owner == "root"
    assert f.group == "daemon"


def test_directory_trailing_slash_stripped():
    """tar -tv lists directories with a trailing slash; we normalise it off."""
    text = "drwxrwxr-x kim/kim 0 2026-05-17 20:50 ./tmp/something/\n"
    root = parse_index(text)
    s = root.children["tmp"].children["something"]
    assert s.is_dir
    assert s.full_path == "./tmp/something"  # no trailing slash


def test_large_file_size_parsed():
    """Files larger than 8 GB (ustar limit) parse correctly with pax format."""
    text = "-rw-rw-r-- kim/kim 17179869184 2026-05-17 20:50 ./tmp/big.img\n"
    root = parse_index(text)
    f = root.children["tmp"].children["big.img"]
    assert f.size == 17179869184  # 16 GiB


def test_sorted_children_dirs_first():
    text = (
        "-rw-r--r-- u/g 1 2026-05-17 20:50 ./zfile.txt\n"
        "drwxrwxr-x u/g 0 2026-05-17 20:50 ./adir/\n"
        "-rw-r--r-- u/g 1 2026-05-17 20:50 ./bfile.txt\n"
    )
    root = parse_index(text)
    names = [n.name for n in root.sorted_children()]
    assert names == ["adir", "bfile.txt", "zfile.txt"]


def test_total_entries_counts_recursively():
    text = (
        "drwx------ u/g 0 2026-05-17 20:50 ./a/\n"
        "-rw------- u/g 1 2026-05-17 20:50 ./a/x\n"
        "-rw------- u/g 1 2026-05-17 20:50 ./a/y\n"
        "-rw------- u/g 1 2026-05-17 20:50 ./b\n"
    )
    root = parse_index(text)
    assert root.total_entries() == 5


def test_build_extract_argv_rejects_option_like_path():
    from pathlib import Path
    with pytest.raises(ValueError):
        build_extract_argv(Path("/tmp/archive.pax.zst"), ["--malicious"])


def test_build_extract_argv_normal_case():
    from pathlib import Path
    zstdcat, tar = build_extract_argv(
        Path("/tmp/archive.pax.zst"),
        ["./home/kim/foo.txt", "./etc/passwd"],
    )
    assert zstdcat == ["zstdcat", "/tmp/archive.pax.zst"]
    # tar -xf - -p [-- paths]
    assert tar[:3] == ["tar", "-xf", "-"]
    assert "-p" in tar
    assert "./home/kim/foo.txt" in tar
    # "--" must come before paths for safety.
    assert tar.index("--") < tar.index("./home/kim/foo.txt")
