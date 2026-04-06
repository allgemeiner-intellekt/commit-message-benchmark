"""Canonical paths for the project. Importing this avoids hard-coding strings."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
REPOS_YAML = CONFIG_DIR / "repos.yaml"
MODELS_YAML = CONFIG_DIR / "models.yaml"
RUBRIC_YAML = CONFIG_DIR / "rubric.yaml"

HOOKS_DIR = ROOT / "hooks"
HOOK_BIN = HOOKS_DIR / "bin" / "ai-commit-message"
HOOK_VERSION_FILE = HOOKS_DIR / "VERSION"

DATA_DIR = ROOT / "data"
REPOS_DIR = DATA_DIR / "repos"
REPLAY_CACHE_DIR = DATA_DIR / "replay-cache"
COMMITS_JSONL = DATA_DIR / "commits.jsonl"

RESULTS_DIR = ROOT / "results"
GENERATION_CACHE_DIR = RESULTS_DIR / "cache"
JUDGE_QUEUE_DIR = RESULTS_DIR / "judge-queue"
JUDGE_MANIFEST = JUDGE_QUEUE_DIR / "manifest.json"
JUDGEMENTS_DIR = RESULTS_DIR / "judgements"
JUDGEMENTS_INDEX = JUDGEMENTS_DIR / "index.jsonl"
REVIEWS_DIR = RESULTS_DIR / "reviews"
REPORT_DIR = RESULTS_DIR / "report"
REPORT_HTML = REPORT_DIR / "index.html"


def hook_version() -> str:
    return HOOK_VERSION_FILE.read_text(encoding="utf-8").strip()


def ensure_dirs() -> None:
    for d in (
        DATA_DIR,
        REPOS_DIR,
        REPLAY_CACHE_DIR,
        RESULTS_DIR,
        GENERATION_CACHE_DIR,
        JUDGE_QUEUE_DIR,
        JUDGEMENTS_DIR,
        REVIEWS_DIR,
        REPORT_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
