"""Materialize a benchmark commit's STAGED state in a temp git repo so the
hook can run `git diff --cached` against it as if you'd just `git add`-ed
those changes."""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import paths


class ReplayError(RuntimeError):
    pass


@dataclass
class ReplayedRepo:
    workdir: Path  # the temp git repo with staged changes


def _run(cmd: list[str], cwd: Path | None = None, input_bytes: bytes | None = None) -> bytes:
    res = subprocess.run(cmd, cwd=cwd, input=input_bytes, capture_output=True)
    if res.returncode != 0:
        raise ReplayError(
            f"command failed: {' '.join(cmd)}\nstdout: {res.stdout!r}\nstderr: {res.stderr!r}"
        )
    return res.stdout


def replay(source_repo: Path, sha: str, parent_sha: str) -> ReplayedRepo:
    """Build a temp git repo holding the parent tree as HEAD and the commit's
    diff staged. Cached on disk under `data/replay-cache/<sha>/`."""

    cache = paths.REPLAY_CACHE_DIR / sha
    if (cache / ".git").exists():
        return ReplayedRepo(workdir=cache)

    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)

    # 1. Materialize the parent tree via `git archive | tar -x`.
    archive = subprocess.run(
        ["git", "archive", "--format=tar", parent_sha],
        cwd=source_repo,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise ReplayError(
            f"git archive {parent_sha} failed in {source_repo}: {archive.stderr!r}"
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(cache)],
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    if extract.returncode != 0:
        raise ReplayError(f"tar -x failed: {extract.stderr!r}")

    # 2. Init a fresh git repo and commit the parent tree as HEAD.
    _run(["git", "init", "-q", "-b", "main"], cwd=cache)
    _run(["git", "config", "user.email", "bench@local"], cwd=cache)
    _run(["git", "config", "user.name", "bench"], cwd=cache)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=cache)
    _run(["git", "add", "-A"], cwd=cache)
    _run(["git", "commit", "-q", "--allow-empty", "-m", "parent"], cwd=cache)

    # 3. Apply the commit's diff to the index.
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-color", f"{parent_sha}..{sha}"],
        cwd=source_repo,
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        raise ReplayError(f"git diff {parent_sha}..{sha} failed: {diff.stderr!r}")

    apply = subprocess.run(
        ["git", "apply", "--cached", "--whitespace=nowarn"],
        cwd=cache,
        input=diff.stdout,
        capture_output=True,
    )
    if apply.returncode != 0:
        # Try a 3-way fallback.
        apply3 = subprocess.run(
            ["git", "apply", "--cached", "--3way", "--whitespace=nowarn"],
            cwd=cache,
            input=diff.stdout,
            capture_output=True,
        )
        if apply3.returncode != 0:
            shutil.rmtree(cache, ignore_errors=True)
            raise ReplayError(
                f"git apply --cached failed for {sha}: {apply.stderr!r} / 3way: {apply3.stderr!r}"
            )

    return ReplayedRepo(workdir=cache)


def staged_diff(repo: ReplayedRepo) -> str:
    out = _run(
        [
            "git",
            "diff",
            "--cached",
            "--no-color",
            "--no-ext-diff",
            "--diff-algorithm=minimal",
        ],
        cwd=repo.workdir,
    )
    return out.decode("utf-8", errors="replace")
