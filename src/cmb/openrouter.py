"""OpenRouter helpers — currently just per-generation cost lookup."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def fetch_generation(generation_id: str, *, retries: int = 3, timeout: float = 10.0) -> dict[str, Any] | None:
    """Look up token usage and total_cost for a single generation. OpenRouter
    keeps these around for ~30 seconds; we retry briefly because the row can
    lag the chat-completions response."""

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not generation_id or not api_key:
        return None

    url = f"{BASE_URL}/generation"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"id": generation_id}

    for attempt in range(retries):
        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=timeout)
        except httpx.HTTPError:
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code == 200:
            data = resp.json().get("data") or {}
            return {
                "total_cost": float(data.get("total_cost") or 0.0),
                "tokens_prompt": int(data.get("tokens_prompt") or 0),
                "tokens_completion": int(data.get("tokens_completion") or 0),
                "native_tokens_prompt": int(data.get("native_tokens_prompt") or 0),
                "native_tokens_completion": int(data.get("native_tokens_completion") or 0),
                "raw": data,
            }
        if resp.status_code in (404, 425):
            time.sleep(0.6 * (attempt + 1))
            continue
        return None
    return None
