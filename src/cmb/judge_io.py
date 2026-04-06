"""Prepare judge prompts for Opus subagents and parse their JSON results.

The CLI never calls a judge API. Instead `judge-prep` writes one self-contained
prompt file per ungraded (commit, model, hook_version, rubric_version) cell to
`results/judge-queue/`, plus a `manifest.json` listing them in batches. The
parent Claude session is then expected to dispatch Opus subagents in parallel
to fill `results/judgements/`. `judge-collect` validates the results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .dataset import CommitRecord, load_dataset
from .generator import GenerationResult


CC_FORMAT_RE = re.compile(
    r"^(feat|fix|refactor|docs|style|test|chore|perf|ci|build|revert)(\([^)]+\))?!?: \S",
)


@dataclass
class CellId:
    commit_id: str
    model: str
    hook_version: str
    rubric_version: str

    @property
    def slug(self) -> str:
        m = self.model.replace("/", "__").replace(":", "_")
        return f"{self.commit_id}__{m}__h{self.hook_version}__r{self.rubric_version}"


def format_pass(message: str) -> bool:
    if not message:
        return False
    first = message.splitlines()[0]
    if len(first) > 72:
        return False
    if first.endswith("."):
        return False
    return bool(CC_FORMAT_RE.match(first))


def load_rubric() -> dict[str, Any]:
    return yaml.safe_load(paths.RUBRIC_YAML.read_text(encoding="utf-8"))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]\n"


def build_prompt(commit: CommitRecord, generation: GenerationResult, rubric: dict) -> str:
    diff_summary = (
        f"files changed: {len(commit.files)}\n"
        f"additions: {commit.additions}\n"
        f"deletions: {commit.deletions}\n"
        f"category: {commit.category}\n"
        f"changed files:\n"
        + "\n".join(f"  - {p}" for p in commit.files[:50])
    )

    instructions = rubric["judge_instructions"]
    dim_lines = []
    for name, spec in rubric["dimensions"].items():
        dim_lines.append(f"### {name} (weight {spec['weight']})\n{spec['description'].strip()}")
    rubric_text = "\n\n".join(dim_lines)

    fmt_pass = format_pass(generation.message)

    return f"""# Commit message judgement

{instructions}

## Rubric

{rubric_text}

## Diff summary

{diff_summary}

## Original human commit message (NOT ground truth)

```
{_truncate(commit.original_message, 2000)}
```

## Candidate AI commit message (the one to score)

```
{generation.message}
```

## Deterministic checks already computed

- Conventional Commits subject regex match (≤72 chars, no trailing period): {fmt_pass}
- Hook version: {generation.hook_version}
- Model: {generation.model}

## Output

Return STRICTLY a fenced JSON block as specified in the rubric instructions above. Do not write any prose outside the fenced block.
"""


def all_cells(rubric_version: str, hook_version: str) -> list[tuple[CellId, GenerationResult, CommitRecord]]:
    """Walk results/cache/ and pair each generation with its commit record."""
    commits = {c.id: c for c in load_dataset()}
    out: list[tuple[CellId, GenerationResult, CommitRecord]] = []
    for p in sorted(paths.GENERATION_CACHE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        gen = GenerationResult(**d)
        if gen.hook_version != hook_version:
            continue
        commit = commits.get(gen.commit_id)
        if commit is None:
            continue
        cell = CellId(
            commit_id=gen.commit_id,
            model=gen.model,
            hook_version=gen.hook_version,
            rubric_version=rubric_version,
        )
        out.append((cell, gen, commit))
    return out


def judgement_path(cell: CellId) -> Path:
    return paths.JUDGEMENTS_DIR / f"{cell.slug}.json"


def write_queue(batch_size: int = 10) -> dict[str, Any]:
    """Create prompt files + manifest.json for every ungraded cell.

    Batches are aligned on commit boundaries: every cell for one commit goes
    into the same batch (so a subagent grading commit X never collides with
    another subagent also grading commit X). `batch_size` is interpreted as
    a target number of *commits* per batch, not cells.
    """
    paths.ensure_dirs()
    rubric = load_rubric()
    rubric_version = str(rubric.get("version", "1.0.0"))
    hook_version = paths.hook_version()

    queued: list[dict[str, Any]] = []
    by_commit: dict[str, list[str]] = {}
    for cell, gen, commit in all_cells(rubric_version, hook_version):
        if judgement_path(cell).exists():
            continue
        if not gen.ok or not gen.message:
            continue
        prompt_path = paths.JUDGE_QUEUE_DIR / f"{cell.slug}.prompt.md"
        prompt_path.write_text(build_prompt(commit, gen, rubric), encoding="utf-8")
        queued.append(
            {
                "cell_id": cell.slug,
                "commit_id": cell.commit_id,
                "model": cell.model,
                "hook_version": cell.hook_version,
                "rubric_version": cell.rubric_version,
                "prompt_path": str(prompt_path.relative_to(paths.ROOT)),
                "judgement_path": str(judgement_path(cell).relative_to(paths.ROOT)),
            }
        )
        by_commit.setdefault(cell.commit_id, []).append(cell.slug)

    # Bucket commits into batches; cells from the same commit always travel together.
    commit_ids = sorted(by_commit.keys())
    batches: list[list[str]] = []
    for i in range(0, len(commit_ids), batch_size):
        chunk = commit_ids[i : i + batch_size]
        batch: list[str] = []
        for cid in chunk:
            batch.extend(by_commit[cid])
        if batch:
            batches.append(batch)

    manifest = {
        "rubric_version": rubric_version,
        "hook_version": hook_version,
        "queued": queued,
        "batches": batches,
        "instructions_for_claude": (
            "Read each prompt file, follow the rubric instructions inside it, "
            "and write the resulting JSON (just the JSON object, no fenced block) "
            "to the corresponding judgement_path. Dispatch one Opus subagent per "
            "batch and run several batches in parallel."
        ),
    }
    paths.JUDGE_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_judgement(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    candidate: str | None = None
    m = _FENCE_RE.search(text)
    if m:
        candidate = m.group(1)
    else:
        # Maybe the file is just the JSON object.
        if text.startswith("{") and text.endswith("}"):
            candidate = text
        else:
            # Last-ditch: find the first {...} block.
            brace = re.search(r"\{.*\}", text, re.DOTALL)
            if brace:
                candidate = brace.group(0)
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    required = {"accuracy", "completeness", "conciseness", "format", "body_quality"}
    if not required <= set(obj):
        return None
    out: dict[str, Any] = {}
    for k in required:
        try:
            v = int(obj[k])
        except (TypeError, ValueError):
            return None
        out[k] = max(1, min(5, v))
    out["rationale"] = str(obj.get("rationale") or "").strip()
    return out


def collect() -> dict[str, Any]:
    """Validate every file in results/judgements/ and (re)build index.jsonl.
    Returns a summary {graded, invalid, requeued}."""
    paths.ensure_dirs()
    rubric = load_rubric()
    rubric_version = str(rubric.get("version", "1.0.0"))
    hook_version = paths.hook_version()

    graded = 0
    invalid: list[str] = []
    rows = []

    for cell, gen, commit in all_cells(rubric_version, hook_version):
        jp = judgement_path(cell)
        if not jp.exists():
            continue
        text = jp.read_text(encoding="utf-8")
        parsed = parse_judgement(text)
        if parsed is None:
            invalid.append(cell.slug)
            jp.unlink()
            continue
        parsed["format_pass"] = format_pass(gen.message)
        if not parsed["format_pass"]:
            parsed["format"] = min(parsed["format"], 2)
        # Persist the normalized form back to disk so the index and the file agree.
        jp.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        graded += 1
        rows.append(
            {
                "cell_id": cell.slug,
                "commit_id": cell.commit_id,
                "model": cell.model,
                "hook_version": cell.hook_version,
                "rubric_version": cell.rubric_version,
                "scores": parsed,
            }
        )

    with paths.JUDGEMENTS_INDEX.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    return {"graded": graded, "invalid": invalid}
