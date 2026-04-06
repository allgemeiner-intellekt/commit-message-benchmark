"""Run the vendored hook against one (commit, model) cell."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .openrouter import fetch_generation
from .replay import ReplayedRepo


@dataclass
class GenerationResult:
    commit_id: str
    model: str
    hook_version: str
    ok: bool
    message: str
    stage_calls: list[dict[str, Any]]
    total_cost_usd: float
    total_latency_ms: int
    stderr: str
    exit_code: int


def cache_path(commit_id: str, model: str, hook_version: str) -> Path:
    safe_model = model.replace("/", "__").replace(":", "_")
    return paths.GENERATION_CACHE_DIR / f"{commit_id}__{safe_model}__{hook_version}.json"


def load_cached(commit_id: str, model: str, hook_version: str) -> GenerationResult | None:
    p = cache_path(commit_id, model, hook_version)
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return GenerationResult(**d)


def _save(result: GenerationResult) -> None:
    p = cache_path(result.commit_id, result.model, result.hook_version)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result.__dict__, indent=2), encoding="utf-8")


def generate(
    commit_id: str,
    model: str,
    replayed: ReplayedRepo,
    hook_version: str,
    *,
    api_url: str = "https://openrouter.ai/api/v1/chat/completions",
    api_key_env: str = "OPENROUTER_API_KEY",
    extra_env: dict[str, str] | None = None,
) -> GenerationResult:
    """Invoke `hooks/bin/ai-commit-message` cwd=replayed.workdir, capture the
    generated commit message + the usage sidecar, then look up per-call cost
    via OpenRouter and persist the result."""

    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"environment variable {api_key_env} is not set")

    sidecar = Path(tempfile.mkstemp(prefix="usage_", suffix=".jsonl")[1])
    sidecar.unlink()  # the hook will (re)create on first append

    env = os.environ.copy()
    env.update(
        {
            "AI_COMMIT_API_URL": api_url,
            "AI_COMMIT_API_KEY": api_key,
            "AI_COMMIT_MODEL": model,
            "AI_COMMIT_USAGE_SIDECAR": str(sidecar),
            "AI_COMMIT_DEBUG": env.get("AI_COMMIT_DEBUG", "false"),
            # Reasoning models (gpt-5-mini, gpt-5-nano, o-series) burn the
            # default 500-token budget on reasoning_tokens. We bump to 2000 AND
            # tell OpenRouter to keep reasoning effort low and exclude the
            # reasoning trace from the response — non-reasoning providers
            # ignore the parameter, so it's safe to set globally.
            "AI_COMMIT_MAX_TOKENS": env.get("AI_COMMIT_MAX_TOKENS", "2000"),
            "AI_COMMIT_REASONING_EFFORT": env.get("AI_COMMIT_REASONING_EFFORT", "low"),
            # Some reasoning models still take >60s end-to-end on a real diff.
            "AI_COMMIT_TIMEOUT_SECONDS": env.get("AI_COMMIT_TIMEOUT_SECONDS", "180"),
        }
    )
    if extra_env:
        env.update(extra_env)

    start = time.time()
    proc = subprocess.run(
        [str(paths.HOOK_BIN)],
        cwd=str(replayed.workdir),
        env=env,
        capture_output=True,
        text=True,
    )
    total_latency_ms = int((time.time() - start) * 1000)

    message = proc.stdout.strip()
    stderr = proc.stderr

    stage_calls: list[dict[str, Any]] = []
    if sidecar.exists():
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                stage_calls.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass

    # Cost comes straight from the chat-completions response usage block,
    # captured into the sidecar by the hook. The legacy /generation lookup
    # endpoint always 404s now, so we no longer rely on it.
    total_cost = sum(float(c.get("cost_usd") or 0.0) for c in stage_calls)

    result = GenerationResult(
        commit_id=commit_id,
        model=model,
        hook_version=hook_version,
        ok=(proc.returncode == 0 and bool(message)),
        message=message,
        stage_calls=stage_calls,
        total_cost_usd=total_cost,
        total_latency_ms=total_latency_ms,
        stderr=stderr[-4000:],
        exit_code=proc.returncode,
    )
    _save(result)
    return result
