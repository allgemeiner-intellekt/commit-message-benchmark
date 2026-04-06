"""End-to-end replay test against a synthetic git repo."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cmb import paths
from cmb.replay import replay, staged_diff


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _make_source_repo(tmp_path: Path) -> tuple[Path, str, str]:
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    _git(["config", "user.email", "t@t"], src)
    _git(["config", "user.name", "t"], src)
    _git(["config", "commit.gpgsign", "false"], src)

    (src / "a.py").write_text("def hello():\n    return 1\n")
    (src / "README.md").write_text("# project\n")
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "initial"], src)
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()

    (src / "a.py").write_text("def hello():\n    return 2\n\ndef bye():\n    return 0\n")
    (src / "b.py").write_text("X = 1\n")
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "feat: tweak hello + add b"], src)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()

    return src, sha, parent


def test_replay_round_trip(tmp_path, monkeypatch):
    # Redirect the replay cache so this test never touches the real one.
    monkeypatch.setattr(paths, "REPLAY_CACHE_DIR", tmp_path / "replay-cache")
    paths.REPLAY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    src, sha, parent = _make_source_repo(tmp_path)

    replayed = replay(src, sha, parent)
    assert replayed.workdir.exists()
    diff = staged_diff(replayed)
    assert "def hello" in diff
    assert "def bye" in diff
    assert "+X = 1" in diff
    # The parent's README must NOT be in the staged diff (it didn't change).
    assert "README" not in diff

    # Replaying again hits the cache and returns instantly without re-applying.
    replayed2 = replay(src, sha, parent)
    assert replayed2.workdir == replayed.workdir
