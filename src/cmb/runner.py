"""Synchronous runner that iterates the (commit × model) cartesian product
and skips cells that are already cached. Concurrency intentionally kept simple
— a small ThreadPoolExecutor per provider gives us enough parallelism without
the async-everywhere overhead."""

from __future__ import annotations

import concurrent.futures as cf
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import yaml

from . import paths
from .dataset import CommitRecord, load_dataset
from .generator import GenerationResult, generate, load_cached
from .replay import replay


def load_models(free_only: bool = False, only: list[str] | None = None) -> list[dict]:
    cfg = yaml.safe_load(paths.MODELS_YAML.read_text(encoding="utf-8"))
    models: list[dict] = []
    for entry in cfg.get("free", []) or []:
        models.append({**entry, "tier": "free"})
    if not free_only:
        for entry in cfg.get("paid", []) or []:
            models.append({**entry, "tier": "paid"})
    if only:
        wanted = set(only)
        models = [m for m in models if m["slug"] in wanted]
    return models


def _provider(slug: str) -> str:
    return slug.split("/", 1)[0] if "/" in slug else slug


def run(
    *,
    free_only: bool = False,
    only_models: list[str] | None = None,
    limit_commits: int | None = None,
) -> dict:
    paths.ensure_dirs()
    commits = load_dataset()
    if limit_commits is not None:
        commits = commits[:limit_commits]
    if not commits:
        raise RuntimeError("no commits in data/commits.jsonl — run `bench dataset build` first")

    models = load_models(free_only=free_only, only=only_models)
    if not models:
        raise RuntimeError("no models selected (check config/models.yaml)")

    hook_version = paths.hook_version()
    summary = {"generated": 0, "cached": 0, "failed": 0}

    # Resolve source repo per commit (for replay).
    repo_dirs: dict[str, Path] = {}
    for c in commits:
        owner, repo = c.repo.split("/", 1)
        repo_dirs[c.id] = paths.REPOS_DIR / f"{owner}__{repo}"

    # Group cells by provider so we bound concurrency per provider.
    by_provider: dict[str, list[tuple[CommitRecord, dict]]] = defaultdict(list)
    for model in models:
        for c in commits:
            if load_cached(c.id, model["slug"], hook_version) is not None:
                summary["cached"] += 1
                continue
            by_provider[_provider(model["slug"])].append((c, model))

    def _do_one(commit: CommitRecord, model: dict) -> str:
        try:
            replayed = replay(repo_dirs[commit.id], commit.sha, commit.parent_sha)
        except Exception as e:
            print(f"[runner] replay failed for {commit.id}: {e}")
            return "failed"
        try:
            res = generate(
                commit_id=commit.id,
                model=model["slug"],
                replayed=replayed,
                hook_version=hook_version,
            )
        except Exception as e:
            print(f"[runner] generate failed for {commit.id} / {model['slug']}: {e}")
            return "failed"
        return "generated" if res.ok else "failed"

    cfg_defaults = (yaml.safe_load(paths.MODELS_YAML.read_text(encoding="utf-8")).get("defaults") or {})
    default_concurrency = int(cfg_defaults.get("concurrency", 4))

    for provider, cells in by_provider.items():
        if not cells:
            continue
        print(f"[runner] {provider}: {len(cells)} cells")
        with cf.ThreadPoolExecutor(max_workers=default_concurrency) as ex:
            futures = [ex.submit(_do_one, c, m) for c, m in cells]
            for f in cf.as_completed(futures):
                summary[f.result()] += 1

    return summary
