"""Build the pinned benchmark dataset from `config/repos.yaml`.

The output is a JSONL at `data/commits.jsonl` with one record per selected
commit. The set is reproducible: same seed + same repo refs => same SHAs.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import yaml

from . import paths
from .categorize import categorize, infer_change_type


@dataclass
class CommitRecord:
    id: str
    repo: str
    sha: str
    parent_sha: str
    original_message: str
    files: list[str]
    additions: int
    deletions: int
    category: str


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    res = subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    )
    return res.stdout


def shallow_clone(owner: str, repo: str, ref: str, dest: Path, depth: int = 2000) -> Path:
    """Shallow-clone (or fetch into) a repo. Cached on disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    if not (dest / ".git").exists():
        _run([
            "git",
            "clone",
            "--filter=blob:none",
            "--no-tags",
            "--depth",
            str(depth),
            "--branch",
            ref,
            url,
            str(dest),
        ])
    return dest


def list_candidate_commits(repo_dir: Path, ref: str, limit: int = 1500) -> list[str]:
    out = _run(
        [
            "git",
            "log",
            "--no-merges",
            "--format=%H",
            f"-n{limit}",
            ref,
        ],
        cwd=repo_dir,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_metadata(repo_dir: Path, sha: str) -> dict | None:
    """Return raw commit metadata or None if it has no parent (initial commit)."""
    try:
        parents = _run(
            ["git", "rev-list", "--parents", "-n", "1", sha], cwd=repo_dir
        ).strip().split()
    except subprocess.CalledProcessError:
        return None
    if len(parents) < 2:
        return None  # initial commit, no parent
    parent_sha = parents[1]

    message = _run(
        ["git", "log", "-1", "--format=%B", sha], cwd=repo_dir
    ).strip()

    numstat = _run(
        [
            "git",
            "diff",
            "--numstat",
            "--no-renames",
            f"{parent_sha}..{sha}",
        ],
        cwd=repo_dir,
    )

    files: list[str] = []
    additions = 0
    deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, path = parts[0], parts[1], "\t".join(parts[2:])
        if a == "-" or d == "-":
            # binary file; skip from counts but record path
            files.append(path)
            continue
        try:
            additions += int(a)
            deletions += int(d)
        except ValueError:
            continue
        files.append(path)

    patch = _run(
        [
            "git",
            "diff",
            "--no-color",
            "--no-renames",
            f"{parent_sha}..{sha}",
        ],
        cwd=repo_dir,
    )

    return {
        "sha": sha,
        "parent_sha": parent_sha,
        "message": message,
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "patch_chars": len(patch),
    }


def passes_filters(meta: dict, filters: dict) -> bool:
    if not meta["files"]:
        return False
    if not (filters["min_files"] <= len(meta["files"]) <= filters["max_files"]):
        return False
    if not (filters["min_patch_chars"] <= meta["patch_chars"] <= filters["max_patch_chars"]):
        return False
    if len(meta["message"]) < filters["min_message_chars"]:
        return False
    msg_first_line = meta["message"].splitlines()[0] if meta["message"] else ""
    for pat in filters.get("drop_message_patterns", []):
        if re.search(pat, msg_first_line):
            return False

    cats = {categorize(p) for p in meta["files"]}
    if cats <= {"lock"}:
        return False
    if cats <= {"generated", "snapshot"}:
        return False
    return True


def build_dataset(max_per_repo: int | None = None, write: bool = True) -> list[CommitRecord]:
    cfg = yaml.safe_load(paths.REPOS_YAML.read_text(encoding="utf-8"))
    seed = int(cfg.get("seed", 0))
    filters = cfg["filters"]
    target_total = int(cfg.get("target_total", 100))
    stratify = cfg.get("stratify_categories") or []
    rng = random.Random(seed)

    paths.ensure_dirs()
    records: list[CommitRecord] = []

    for repo_cfg in cfg["repos"]:
        owner = repo_cfg["owner"]
        repo = repo_cfg["repo"]
        ref = repo_cfg["ref"]
        cap = max_per_repo if max_per_repo is not None else int(repo_cfg.get("sample_n", 0))
        if cap <= 0:
            continue

        dest = paths.REPOS_DIR / f"{owner}__{repo}"
        try:
            shallow_clone(owner, repo, ref, dest)
        except subprocess.CalledProcessError as e:
            print(f"[dataset] failed to clone {owner}/{repo}: {e.stderr.strip()}")
            continue

        candidates = list_candidate_commits(dest, ref)
        rng.shuffle(candidates)

        # Bucket by category for stratified sampling.
        buckets: dict[str, list[CommitRecord]] = {c: [] for c in stratify}
        scanned = 0
        for sha in candidates:
            if all(len(buckets[c]) >= max(1, cap // max(1, len(stratify))) for c in buckets) and len(buckets) > 0:
                if sum(len(b) for b in buckets.values()) >= cap:
                    break
            scanned += 1
            if scanned > 800:
                break
            meta = commit_metadata(dest, sha)
            if meta is None or not passes_filters(meta, filters):
                continue
            cat = infer_change_type(meta["message"], meta["files"])
            if cat not in buckets:
                buckets.setdefault(cat, [])
            rec = CommitRecord(
                id=f"{owner}__{repo}__{sha[:12]}",
                repo=f"{owner}/{repo}",
                sha=sha,
                parent_sha=meta["parent_sha"],
                original_message=meta["message"],
                files=meta["files"],
                additions=meta["additions"],
                deletions=meta["deletions"],
                category=cat,
            )
            buckets[cat].append(rec)
            if sum(len(b) for b in buckets.values()) >= cap:
                break

        # Flatten buckets in stratify order, then leftovers.
        flat: list[CommitRecord] = []
        order = list(stratify) + [c for c in buckets if c not in stratify]
        per_cat_cap = max(1, cap // max(1, len(order)))
        for c in order:
            flat.extend(buckets.get(c, [])[:per_cat_cap])
        if len(flat) < cap:
            seen = {r.sha for r in flat}
            for c in order:
                for r in buckets.get(c, []):
                    if r.sha in seen:
                        continue
                    flat.append(r)
                    seen.add(r.sha)
                    if len(flat) >= cap:
                        break
                if len(flat) >= cap:
                    break
        records.extend(flat[:cap])

    # Trim to target_total preserving stratification across repos.
    if max_per_repo is None and len(records) > target_total:
        records = records[:target_total]

    if write:
        with paths.COMMITS_JSONL.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(asdict(r)) + "\n")
        print(f"[dataset] wrote {len(records)} commits → {paths.COMMITS_JSONL}")

    return records


def load_dataset() -> list[CommitRecord]:
    if not paths.COMMITS_JSONL.exists():
        return []
    out: list[CommitRecord] = []
    for line in paths.COMMITS_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(CommitRecord(**d))
    return out
