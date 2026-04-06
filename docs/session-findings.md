# Session findings — first end-to-end benchmark sweep

> **Run details**: hook v1.1.1 · rubric v1.0.0 · 14 cherry-picked commits across 7 OSS repos × 10 candidate models = 134 graded cells (134 / 140 succeeded). Judging done by 7 Opus subagents in parallel inside this Claude Code session. Date: 2026-04-06.

This document captures the **non-obvious** things I learned while building and running the pipeline. Code-level facts (file paths, function signatures) live in the source — they're not repeated here. This is the stuff you want to know before doing another run.

---

## 1. Headline result (small dataset, treat top 3-4 as roughly tied)

| rank | model | weighted score | format ✓ | $/commit | mean latency | failure rate |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `google/gemini-2.5-flash` | **4.64** | 100% | $0.00194 | 3.7s | 0% |
| 2 | `openai/gpt-5-mini` | 4.57 | 79% | $0.00128 | 8.0s | 0% |
| 3 | `deepseek/deepseek-v3.2` | 4.54 | 91% | $0.00136 | **59s** ⚠ | 21% ⚠ |
| 4 | `anthropic/claude-haiku-4.5` | 4.46 | 79% | $0.00591 | 9.4s | 0% |
| 5 | `openai/gpt-oss-120b` | 4.39 | 64% ⚠ | $0.00023 | 19.8s | 0% |
| 6 | `qwen/qwen3-coder-30b-a3b-instruct` | 4.24 | 100% | $0.00019 | **2.7s** | 0% |
| 7 | `google/gemini-2.5-flash-lite` | 4.18 | 93% | $0.00043 | 4.7s | 0% |
| 8 | `z-ai/glm-4.5-air` | 4.08 | 73% | $0.00097 | 11.9s | 21% ⚠ |
| 9 | `mistralai/mistral-small-3.2-24b-instruct` | 4.01 | 100% | $0.00019 | 2.3s | 0% |
| 10 | `openai/gpt-5-nano` | 3.99 | 93% | $0.00040 | 6.7s | 0% |

**Working picks:**
- **Default** → `google/gemini-2.5-flash` (highest quality, only one combining 100% format compliance with sub-4s latency at <$0.002/commit).
- **Budget** → `qwen/qwen3-coder-30b-a3b-instruct` (4.24 quality, 100% format, **2.7s, $0.00019**) — ~10× cheaper than the winner with comparable speed and perfect format.

n=14 is too small for a defensible final answer. The score CIs in the HTML report overlap heavily for the top 4. Rerun on the full ~100-commit dataset before committing to a long-term hook config.

---

## 2. The DeepSeek V3.2 latency mystery (and why it's not fixable from the hook)

DeepSeek-v3.2 came in 3rd by quality but its mean latency was **59 seconds** (one call hit 116s) with 21% upstream failures. That's 10-30× slower than every other model in the pool.

**Root cause**: DeepSeek V3.2-Exp introduced an internal reasoning/thinking mode that V3.1 did not have. Per-call evidence from `results/cache/*deepseek*`:

| commit | stage1 latency | tokens_in | tokens_out |
| --- | --- | --- | --- |
| python__cpython__c447d1bc146b | 116.4s | 692 | 1161 |
| rails__rails__312e11528d60 | 81.7s | 1222 | **2500** (= our max_tokens cap) |
| golang__go__388c41c412c2 | 73.7s | 3667 | 2155 |

A normal commit-message stage1 response is 50-200 completion tokens. DeepSeek V3.2 is returning **800-2500** completion tokens per call, often hitting our 2000-token cap. Those are thinking tokens that the provider counts as completion tokens.

**Why our reasoning suppression didn't help**: hook v1.1.1 sends OpenRouter's `reasoning: {effort: "low", exclude: true}` parameter on every payload. That field is honored by OpenAI's GPT-5 family and Anthropic's Claude 4.x family, which is why gpt-5-mini and gpt-5-nano were fast in this run. **DeepSeek's V3.2 thinking is not gated by that field** — the model thinks regardless. There's no OpenRouter knob today that turns it off.

**Practical implication for the hook**: V3.2 is unusable in a per-commit hook regardless of how good its content scores look. If you want a DeepSeek model in the pool, switch back to `deepseek/deepseek-chat-v3.1` (non-thinking) or wait for a `deepseek-v3.2-instruct` / `:exact` variant that exposes a thinking-off switch. Note that v3.1 was deprecated/renamed at some point on OpenRouter — the slug churns; verify against `/api/v1/models` before adding.

---

## 3. Reasoning models broke the hook by default — the fix matters globally

When I first ran the sweep on hook v1.0.0:

- `openai/gpt-5-mini` failed **14/14 cells** with empty stage1 responses.
- `openai/gpt-5-nano` failed **14/14 cells**, half via curl 60s timeouts.

The hook's old config was `max_tokens=500`, no `reasoning` parameter. GPT-5-mini/nano are reasoning models — they spend their *entire* completion budget on reasoning_tokens by default and return zero `content`. A direct probe showed `reasoning_tokens: 192 / completion_tokens: 269` with `max_tokens=500` and a trivial prompt, so a real 5KB hook prompt left nothing for the actual answer.

**Fix shipped in hook v1.1.0 / v1.1.1** (`hooks/bin/ai-commit-message`):
- Bumped default `max_tokens` from 500 → 2000.
- Bumped default curl timeout from 60s → 180s.
- Added a new env var `AI_COMMIT_REASONING_EFFORT` (default `low`) which the payload builder translates to OpenRouter's `reasoning: {effort: <value>, exclude: true}`. Providers that don't support the field ignore it (verified — no harm to non-reasoning models).

After the fix, gpt-5-mini went 0/14 → 14/14 and gpt-5-nano went 0/14 → 14/14. **Both are now viable for the hook** — gpt-5-mini even came 2nd overall.

**Important**: this is a behavior change to the hook itself. Bumping `hooks/VERSION` is what enabled clean cache invalidation. Any future hook prompt or payload tweak must bump the version too, otherwise the runner happily returns stale generations.

---

## 4. OpenRouter cost lookup endpoint is broken — use the response body instead

The original generator looked up per-call cost via `GET /api/v1/generation?id=<gen_id>`. **That endpoint always returns 404 now**, even immediately after the chat-completions call. I confirmed it live: a fresh `gen-1775495677-...` from 2 seconds prior returned `{"error": {"message": "Generation ... not found", "code": 404}}`.

**Working alternative**: OpenRouter includes `usage.cost` (and `usage.prompt_tokens` / `usage.completion_tokens`) directly in the chat-completions response body. Hook v1.1.1 now extracts those into the sidecar JSON line, and `generator.py` reads them with no HTTP roundtrip. The legacy `openrouter.py::fetch_generation` is dead code — leaving it for now in case the endpoint comes back, but nothing calls it.

If this breaks again in the future: look at `body_file` inside `call_ai_api` (in the vendored hook), the `usage` object is right there.

---

## 5. OpenRouter free-tier slugs churn — never hard-code without verifying

My original `config/models.yaml` had 5 hand-picked free slugs (`deepseek-chat-v3.1:free`, `gemini-2.0-flash-exp:free`, etc.). **Every single one was either gone or rate-limiting hard** when I ran them. The first sweep against the original list returned 70/70 failures (all 404s and 429s).

**Process to use when adding models**:

1. Hit `https://openrouter.ai/api/v1/models` and filter live.
2. Filter to `:free`-suffix slugs OR slugs you specifically want.
3. Sanity-check with a 1-message probe before putting it in the YAML.
4. Treat free-tier 429s as "this slug is unreliable today, try again or pick another".

I removed the free/paid distinction entirely after that — `config/models.yaml` is now a single flat `models:` list. It's just an editable list of slugs; nothing else cares about provenance.

---

## 6. Subagent batching needs commit-aligned batches, not flat slicing

First grading pass: `bench judge-prep --batch-size 15` sliced the queue flat across cell_ids. Several commits got chopped between adjacent batches, and at least two subagents read the existing-files heuristic wrong and skipped ~30 cells thinking they were already graded by an earlier batch. Result: `bench judge-collect` reported 107/137 graded.

**Fix shipped in `judge_io.write_queue`**: batches are now aligned on commit boundaries — every cell for one commit goes into the same batch. `batch_size` is now interpreted as *commits per batch*, not cells. With 14 commits and `--batch-size 2` we get 7 commit-aligned batches of ~20 cells each, no overlap, no skipped cells. Second pass: 134/134 with 0 invalid.

**Practical guidance for future runs**:
- Use `--batch-size 2` (i.e. 2 commits per subagent) for ~10 models. Each subagent ends up with ~20 cells, ~50 KB of prompts, finishes in 2-3 minutes.
- For larger model pools, scale `batch_size` down so cells-per-batch stays under ~25. Opus subagents start to get sloppy past that.
- 7 parallel subagents finished 134 cells in ~3 minutes wall time.

---

## 7. Things that surprised me about specific models

These are first-impression observations from a 14-commit sample. Verify before generalizing.

- **`openai/gpt-oss-120b`** is dirt cheap ($0.0002/commit) and content-accurate (4.39), but only **64% format compliance** — by far the worst. It frequently emits non-Conventional-Commits subjects or capital letters after the colon. Don't use without a post-processing format gate.
- **`anthropic/claude-haiku-4.5`** was the *most expensive* model in the pool (~3-30× the others) and underperformed on format (79%). Worth a re-check: maybe my prompt isn't using Anthropic's strengths well, or maybe Haiku 4.5 just isn't suited to this exact task.
- **`google/gemini-2.5-flash` is the breakout winner**: only model with both top-tier content score AND 100% format AND sub-4s latency. Worth knowing for other structured-output tasks too.
- **`qwen/qwen3-coder-30b-a3b-instruct`** is the unexpectedly excellent budget pick. 100% format, 2.7s, $0.0002. The coder fine-tuning seems to translate directly into following Conventional Commits format reliably.
- **`gpt-5-nano` underperformed `gpt-4.1-nano`-class budget OpenAI options**: came dead last (3.99) and is a reasoning model, so half of every dollar spent goes to thinking that doesn't show up in the final message. Skip for this task.
- **`mistralai/mistral-small-3.2-24b`** has 100% format and is the fastest in the pool (2.3s) but content scores sit at 4.01 — fine for CI/dev tools, weaker for serious commits.
- **`z-ai/glm-4.5-air`** is flaky on this benchmark: 21% upstream failure and the surviving cells have weak format. Avoid until the provider stabilizes.

---

## 8. Operational notes for the next sweep

When you run this benchmark again (e.g. after adding new models):

1. **Always re-check OpenRouter slugs first** — `uv run python -c "import httpx, os; ..."` against `/api/v1/models`. Slugs vanish without warning.
2. **Bump `hooks/VERSION` if the hook payload or prompt changes**. The runner cache only invalidates when the version changes.
3. **The rubric version (`config/rubric.yaml::version`) controls the judgement cache separately**. Bumping it forces re-grading without re-generating.
4. **For a quick smoke run**: `bench dataset build --max-per-repo 2` (≈14 commits) → `bench run` → `bench judge-prep --batch-size 2` → dispatch `ceil(commits/batch_size)` Opus subagents in parallel → `bench judge-collect` → `bench report`. End-to-end ~10 minutes wall time.
5. **For a real run**: `bench dataset build` (no flag, ≈100 commits) → `bench run` → `bench judge-prep --batch-size 5` → ~20 subagents in waves of 6-7 → `bench judge-collect` → `bench report`. Should be ~30 minutes wall time.
6. **Failure rate ≥ 10% on a model is a red flag** — either it's a rate-limit issue (rerun with smaller concurrency in `models.yaml`) or the model is genuinely unreliable.

---

## 9. Open questions for the next iteration

- Is the score gap between gemini-2.5-flash and gpt-5-mini real, or n=14 noise? Need ≥50 commits before trusting it.
- Does the dataset bias toward big-OSS-project commit style? The 7 source repos all use different conventions. A sweep against the user's *own* commit history would tell us which model best matches their personal voice.
- Anthropic Haiku 4.5 underperformed in a way I don't fully understand. Worth A/B testing the system prompt to see if Anthropic-specific prompting (XML tags, explicit role priming) helps.
- The hook's two-stage architecture (stage1 JSON + optional stage2 refine) might be over-engineered for small fast models. A single-call ablation would tell us how much value stage2 actually adds.
- gpt-oss-120b's format failures are interesting — does adding a "respond in the format `type(scope): subject`" few-shot example fix it, or is it a fundamental limitation? Cheap to test.

---

*This document is generated by Claude Code; treat it as a working notebook rather than a polished deliverable. Update it after each new sweep.*
