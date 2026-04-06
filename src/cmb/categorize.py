"""File categorizer that mirrors the vendored hook's logic so the dataset
filter and the hook agree on what counts as source / test / docs / lock /
generated. Keep the rules in lockstep with hooks/bin/ai-commit-message.
"""

from __future__ import annotations

import os
import re

LOCK_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.lock",
    "composer.lock",
    "poetry.lock",
    "gemfile.lock",
    "pipfile.lock",
}

CC_PREFIX = re.compile(
    r"^(feat|fix|refactor|docs|style|test|chore|perf|ci|build|revert)(\([^)]*\))?!?:",
    re.IGNORECASE,
)


def categorize(path: str) -> str:
    lower = path.lower()
    base = os.path.basename(lower)

    if base in LOCK_NAMES or lower.endswith(".lock"):
        return "lock"
    if "/__snapshots__/" in lower or lower.endswith(".snap"):
        return "snapshot"
    if (
        "/dist/" in lower
        or "/build/" in lower
        or "/coverage/" in lower
        or "/generated/" in lower
        or "/gen/" in lower
        or lower.endswith(".min.js")
        or lower.endswith(".map")
    ):
        return "generated"
    if (
        lower.startswith("docs/")
        or "/docs/" in lower
        or base in {"readme.md", "changelog.md", "license", "license.md"}
        or lower.endswith(".md")
        or lower.endswith(".rst")
        or lower.endswith(".adoc")
        or lower.endswith(".txt")
    ):
        return "docs"
    if (
        "/test/" in lower
        or "/tests/" in lower
        or "/spec/" in lower
        or "/__tests__/" in lower
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base.endswith(".spec.ts")
        or base.endswith(".spec.tsx")
        or base.endswith(".spec.js")
        or base.endswith(".test.ts")
        or base.endswith(".test.tsx")
        or base.endswith(".test.js")
        or lower.endswith(".feature")
    ):
        return "test"
    if (
        base in {
            "package.json",
            "tsconfig.json",
            "dockerfile",
            ".gitignore",
            ".gitattributes",
            "makefile",
        }
        or lower.endswith(".json")
        or lower.endswith(".yaml")
        or lower.endswith(".yml")
        or lower.endswith(".toml")
        or lower.endswith(".ini")
        or lower.endswith(".cfg")
        or lower.endswith(".conf")
    ):
        return "config"
    return "source"


def infer_change_type(message: str, files: list[str]) -> str:
    """Infer a Conventional-Commits-style category from the original message,
    falling back to a file-mix heuristic. Returns one of feat/fix/refactor/
    docs/test/chore."""

    match = CC_PREFIX.match(message.strip())
    if match:
        prefix = match.group(1).lower()
        return {
            "feat": "feat",
            "fix": "fix",
            "refactor": "refactor",
            "perf": "refactor",
            "docs": "docs",
            "test": "test",
            "style": "chore",
            "chore": "chore",
            "ci": "chore",
            "build": "chore",
            "revert": "chore",
        }.get(prefix, "chore")

    cats = [categorize(p) for p in files]
    if cats and all(c == "docs" for c in cats):
        return "docs"
    if cats and all(c == "test" for c in cats):
        return "test"

    head = message.lower().splitlines()[0] if message else ""
    if any(k in head for k in ("fix", "bug", "patch", "hotfix", "regression")):
        return "fix"
    if any(k in head for k in ("add ", "introduce", "implement", "support ", "new ")):
        return "feat"
    if any(k in head for k in ("refactor", "cleanup", "rename", "extract", "simplif")):
        return "refactor"
    return "chore"
