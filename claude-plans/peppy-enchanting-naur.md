# Commit Message Benchmark — Implementation Plan

## Context

You already have a sophisticated `prepare-commit-msg` hook at `~/.git-hooks/prepare-commit-msg` that delegates to `~/.git-hooks/bin/ai-commit-message`. That generator is a two-stage Bash+Python pipeline: it builds a structured staged-change summary, samples patch excerpts under a budget, asks an LLM (Stage 1) for a Conventional-Commits message + a confidence/`needs_second_pass` JSON, and optionally runs Stage 2 with a focused context to refine the draft. It talks to any OpenAI-compatible chat-completions endpoint via `AI_COMMIT_API_URL` + `AI_COMMIT_MODEL` + `AI_COMMIT_API_KEY`, so swapping in OpenRouter is just a matter of setting those three env vars.

What's missing is a way to **objectively pick the best model** for this hook (quality × cost × latency) and to **rerun that decision when new models ship**. This project builds that pipeline:

1. A pinned, reproducible benchmark dataset of real commits from public repos.
2. A runner that replays each commit's staged state into a temp git workspace and invokes the (vendored) hook against many models via OpenRouter.
3. A judging stage that runs **inside Claude Code** — Opus subagents (and optionally Codex review subagents) score each generated message against a rubric. No paid judge API.
4. A self-contained **HTML** report that combines quality, cost, and latency, with a one-liner to install the winning model into the live hook.

The pipeline is incremental: adding a new model later reuses all existing generations/judgements and only computes the new cells. The whole thing is designed to be **driven by a Claude Code session running inside this repo** — the CLI handles deterministic, scriptable work, and Claude (you, in any future session) handles the LLM-judging step by dispatching subagents.

---

## Operating model: who runs what

| Step | Driven by | Why |
| --- | --- | --- |
| Build dataset | CLI (`bench dataset build`) | Deterministic, network-bound, no LLM. |
| Replay commits | CLI (called from runner) | Pure git. |
| Generate candidate messages | CLI (`bench run`) | Calls the vendored hook against OpenRouter; deterministic glue. |
| Judge candidates | **Claude session** dispatches Opus subagents in parallel | Avoids paying for a judge API; you're already running Claude when you run this benchmark. |
| (Optional) Second-opinion review | **Claude session** dispatches Codex `codex-rescue` subagents | Cross-model sanity check on the top-N. |
| Render report | CLI (`bench report`) | Reads cached generations + judgements, writes HTML. |
| Install winning model into live hook | CLI (`bench install-hook`) | Single mechanical copy. |

The CLI never makes a judge API call. The judging command is `bench judge-prep`, which writes one prompt file per ungraded `(commit, model)` cell into `results/judge-queue/` plus a `manifest.json`. Then this Claude session reads the manifest, launches Opus subagents in parallel (one per batch of cells), and each subagent writes its scored JSON back into `results/judgements/`. `bench judge-collect` validates and indexes those files. This pattern means you can run the benchmark in a fresh Claude session at any time and the workflow is "run `bench run`, then ask me to judge, then run `bench report`".

---

## High-level architecture

```
commit-message-benchmark/
├── README.md                    # quickstart + Claude-driven workflow
├── pyproject.toml               # uv-managed
├── uv.lock
├── .env.example                 # OPENROUTER_API_KEY
├── config/
│   ├── repos.yaml               # source repos + sampling rules
│   ├── models.yaml              # candidate models (free + paid sections)
│   └── rubric.yaml              # judge rubric weights + dimension prompts
├── hooks/                       # vendored copy of the live hook
│   ├── prepare-commit-msg
│   ├── bin/ai-commit-message    # patched to emit usage sidecar
│   └── VERSION
├── data/
│   ├── repos/                   # shallow clones (gitignored)
│   └── commits.jsonl            # pinned benchmark set
├── results/
│   ├── cache/                   # (commit, model, hook_ver) → generation JSON
│   ├── judge-queue/             # prompt files + manifest.json (input to subagents)
│   ├── judgements/              # (commit, model, hook_ver, rubric_ver) → JSON
│   └── report/
│       ├── index.html           # the leaderboard
│       └── assets/              # css + small JS for sortable table
├── src/cmb/
│   ├── dataset.py               # build commits.jsonl
│   ├── replay.py                # materialize a commit's staged state
│   ├── generator.py             # invoke vendored hook against a model
│   ├── openrouter.py            # cost/usage retrieval helpers
│   ├── judge_io.py              # write judge-queue, parse judgements
│   ├── runner.py                # async orchestration with caching
│   ├── report.py                # aggregate → HTML
│   └── cli.py                   # `bench …` Typer entrypoint
├── tests/
│   ├── test_dataset.py
│   ├── test_replay.py
│   ├── test_judge_io.py
│   └── test_report.py
└── scripts/
    └── install-hook             # copies hooks/* back to ~/.git-hooks/
```

---

## Component design

### 1. Dataset (`src/cmb/dataset.py`, `config/repos.yaml`)

- `repos.yaml` lists ~6–8 well-maintained OSS repos spanning languages and styles, e.g. `python/cpython`, `facebook/react`, `kubernetes/kubernetes`, `rails/rails`, `rust-lang/rust`, `golang/go`, `django/django`, `microsoft/vscode`. Each entry: `{owner, repo, ref, sample_n}`.
- For each repo: shallow clone into `data/repos/<owner>__<repo>` (cached). Walk `git log --no-merges` and apply filters:
  - 1 ≤ files changed ≤ 30
  - 200 ≤ patch chars ≤ 20,000
  - drop lockfile-only / generated-only commits (reuse the categorizer logic from the hook)
  - drop commits whose original message is shorter than 10 chars or matches `^(Merge|Revert|bump version|Bump |dependabot)`
- Stratify by inferred change-type (parse Conventional-Commits prefix from the original message; fall back to file-mix heuristic) so we get a balanced mix of feat/fix/refactor/docs/test/chore.
- Sample to a target of **~100 commits total** with a fixed RNG seed (in `repos.yaml`).
- Write `data/commits.jsonl` with `{id, repo, sha, parent_sha, original_message, files, additions, deletions, category}`. The set is pinned by SHA → bit-for-bit reproducible.

### 2. Replay (`src/cmb/replay.py`)

For one commit:

1. Create a temp dir, `git init -q`.
2. From the source clone, `git archive <parent_sha> | tar -x` into the temp dir to materialize the parent tree.
3. `git add -A && git commit -q -m parent` so the temp repo has a HEAD.
4. Apply the commit's diff to the index only: `git -C <src> show <sha> --binary | git apply --cached` (fall back to `git apply --cached --3way`).
5. Sanity-check: `git diff --cached` in the temp repo equals the original commit diff (modulo whitespace). Skip + log commits that fail to replay.

Replays are cached on disk under `data/replay-cache/<sha>/` so repeated runs are cheap.

### 3. Vendored hook (`hooks/`)

- Copy `~/.git-hooks/prepare-commit-msg` and `~/.git-hooks/bin/ai-commit-message` into `hooks/` and `hooks/bin/` verbatim. Track a `hooks/VERSION` file (semver) — bumping it invalidates the result cache.
- Apply one minimal patch to `hooks/bin/ai-commit-message`: when `AI_COMMIT_USAGE_SIDECAR` is set, append a JSON line per API call to that file with `{stage, model, http_status, latency_ms, openrouter_id, request_chars, response_chars}`. This is the only deviation from the live hook.
- `scripts/install-hook` copies the vendored files back to `~/.git-hooks/` so the winning configuration is one command away from production.

### 4. Generator wrapper (`src/cmb/generator.py`)

For one `(commit, model)` cell:

1. Replay the commit (cached).
2. Set env: `AI_COMMIT_API_URL=https://openrouter.ai/api/v1/chat/completions`, `AI_COMMIT_API_KEY=$OPENROUTER_API_KEY`, `AI_COMMIT_MODEL=<slug>`, `AI_COMMIT_USAGE_SIDECAR=<tmp>.jsonl`, `AI_COMMIT_DEBUG=true`.
3. `cd` into the replayed repo and run `hooks/bin/ai-commit-message`. Capture stdout (the message), stderr (debug log), exit code, wall time.
4. Read the usage sidecar; for each entry, fetch `GET https://openrouter.ai/api/v1/generation?id=<openrouter_id>` to retrieve canonical token counts and `total_cost` (USD). For free-tier models, `total_cost` will be `0`.
5. Persist `{commit_id, model, hook_version, message, stage_calls:[…], total_cost_usd, total_latency_ms, ok}` to `results/cache/<commit_id>__<model_slug>__<hook_version>.json`. The runner consults this cache before generating.

### 5. Judging (`src/cmb/judge_io.py`, `config/rubric.yaml`, **Claude subagents**)

No HTTP judge. The flow is:

1. **`bench judge-prep`** scans `results/cache/` for cells without a matching `results/judgements/` file. For each ungraded cell, it writes:
   - `results/judge-queue/<cell_id>.prompt.md` — a self-contained prompt with the rubric, the diff stat, the truncated diff, the candidate commit message, and the original human message labelled "one possible reference, not gold". The prompt instructs the judge to return strict JSON in a fenced block.
   - One `results/judge-queue/manifest.json` listing all queued cells in batches of ~5 cells per subagent (so each Opus call is meaningful work without blowing context).
2. **Claude session (you/me) reads the manifest and dispatches Opus subagents in parallel** via the Agent tool with `subagent_type=general-purpose` and `model=opus`. Each subagent is told: read these N prompt files, follow the rubric, write each result to `results/judgements/<cell_id>.json`. Concurrency is bounded to whatever the parent session can comfortably manage (start with 4–6 in flight).
3. **`bench judge-collect`** validates every JSON file (schema-checks the rubric dimensions, clamps to 1–5, records `format_pass` from deterministic regex checks) and indexes them into `results/judgements/index.jsonl`. Cells with missing/invalid JSON are re-queued.
4. (Optional) **`bench judge-review --top 3`** writes a different prompt asking a `codex-rescue` subagent to second-opinion the top-3 models on the same dataset, producing a brief written critique stored under `results/reviews/`. This is a sanity check, not part of the headline score.

Rubric (each 1–5, weights in `rubric.yaml`):
- `accuracy` — does the subject reflect what changed? (weight 0.30)
- `completeness` — are all material changes covered, no lies, no omissions? (0.20)
- `conciseness` — no fluff, within length limits? (0.15)
- `format` — Conventional Commits compliance (subject pattern, ≤72 chars, imperative, no trailing period); the deterministic `format_pass` boolean is a hard floor on this dimension. (0.20)
- `body_quality` — explains *why*, not *how*; N/A allowed for trivial commits. (0.15)

Cache key: `(commit_id, model, hook_version, rubric_version)`. Bumping `rubric.yaml` cleanly invalidates only the judgements, not the generations.

### 6. Runner (`src/cmb/runner.py`)

- Async (httpx + asyncio) with bounded concurrency per provider (configurable in `models.yaml`, default 4).
- Iterates the cartesian product of `commits × models`, skipping cells already cached.
- Resumable, Ctrl-C safe, idempotent.
- Adding a new model: `bench run --models <new-slug>`; cache makes it cheap.

### 7. Report (`src/cmb/report.py`) — **HTML output**

Aggregates per model:

- mean rubric score (weighted), with bootstrap 95% CI
- format pass-rate
- mean / p50 / p95 cost per commit (USD)
- mean / p50 / p95 latency (ms)
- $/quality-point above a baseline
- failure rate

Writes a **single self-contained `results/report/index.html`** containing:

- A summary card naming the recommended model and the "best on a budget" pick.
- A sortable, filterable HTML table (vanilla JS, no build step) with one row per model and columns for each metric. Click a column header to sort.
- A small Pareto plot of cost-vs-quality (inline SVG, no chart library). Each point links to a per-model drill-down.
- Per-model drill-down sections (anchor links, no separate pages) showing: score distribution histogram (inline SVG), 3 best/worst example generations side-by-side with the original human message and the judge's rationale.
- A "judges" section listing which Claude subagent runs produced the judgements (timestamps + counts), so the report is reproducible without re-judging.

One file. No external assets. Open in a browser, share by copy.

### 8. CLI (`src/cmb/cli.py`)

```
bench dataset build [--max-per-repo N]
bench run [--models …] [--commits N] [--free-only]
bench judge-prep                       # writes prompts + manifest for Claude to grade
bench judge-collect                    # validates subagent outputs
bench judge-review --top 3             # optional Codex second-opinion pass
bench report                           # writes results/report/index.html
bench install-hook --model SLUG        # writes chosen model into ~/.git-hooks env
bench status                           # show cache hit-rate, queued cells, est. cost
```

Single Typer app, each command is thin glue over the modules above.

---

## Phased rollout (free models first)

Validate the entire pipeline on **OpenRouter free-tier models** before paying a cent.

**Phase A — implement** the CLI, hook vendor, dataset builder, replay, runner, judge I/O, and HTML report.

**Phase B — free-model shakedown.** `config/models.yaml` has a `free` section pre-populated with current free OpenRouter slugs (verify availability at run time — the list churns; representative candidates as of writing include `meta-llama/llama-3.3-70b-instruct:free`, `deepseek/deepseek-chat-v3.1:free`, `google/gemini-2.0-flash-exp:free`, `qwen/qwen-2.5-72b-instruct:free`, `mistralai/mistral-small-3.1-24b-instruct:free`). Run:

```
bench dataset build --max-per-repo 2          # ~12 commits
bench run --free-only                          # ~12 × ~5 free models = 60 cells, $0
# ask Claude (this session) to judge:
bench judge-prep
# → I dispatch Opus subagents to grade the queue
bench judge-collect
bench report
```

This validates: dataset filters, replay correctness, vendored hook + OpenRouter wiring, usage sidecar parsing, the subagent judging loop, the HTML report. Total spend: $0. Success criterion: `report/index.html` opens, all 60 cells have judgements, the leaderboard is sortable and the recommendation paragraph renders.

**Phase C — paid sweep.** Once Phase B is green, expand `config/models.yaml` with paid candidates (Anthropic Haiku/Sonnet/Opus, OpenAI GPT-5/mini/nano, Gemini Flash/Pro, plus a couple of paid open-weight slugs). Re-run the full ~100-commit × ~10-model sweep. Cache means the free-model results are reused.

**Phase D — install the winner.** `bench install-hook --model <slug>` copies the vendored hook back to `~/.git-hooks/` and sets the chosen model as default.

---

## Critical files to create

- `pyproject.toml`, `uv.lock`, `.env.example`
- `config/repos.yaml`, `config/models.yaml` (with `free` and `paid` sections), `config/rubric.yaml`
- `hooks/prepare-commit-msg`, `hooks/bin/ai-commit-message` (vendored + sidecar patch), `hooks/VERSION`
- `src/cmb/{dataset,replay,generator,openrouter,judge_io,runner,report,cli}.py`
- `scripts/install-hook`
- `tests/test_{dataset,replay,judge_io,report}.py`
- `README.md` documenting the **Claude-driven workflow**: `bench run` → "ask Claude to judge" → `bench report`.

## Files to reuse rather than reimplement

- The categorizer and patch sampler from `~/.git-hooks/bin/ai-commit-message` (lines ~147–320): import the same logic into the dataset filter so stratification and the hook agree on what "source/test/docs/lock/generated" mean.
- The two-stage prompt strings (`build_stage1_system_prompt`, `build_stage2_system_prompt`): leave them inside the vendored hook so the benchmark measures the *real* hook end-to-end.

---

## Verification

1. `uv sync && cp .env.example .env` → fill `OPENROUTER_API_KEY`.
2. `bench dataset build --max-per-repo 2` → ~12 commits in `data/commits.jsonl`; eyeball category mix.
3. `python -m pytest -q` → unit tests pass:
   - `test_replay.py` builds a synthetic git repo with a known commit and asserts the replayed `git diff --cached` is byte-equal.
   - `test_dataset.py` checks filters (lockfile-only excluded, merge excluded, size bounds).
   - `test_judge_io.py` round-trips a queue manifest and parses canned judge JSON (including malformed cases that should re-queue).
   - `test_report.py` aggregates a fixture into a known leaderboard and asserts the HTML contains expected ids.
4. `bench run --free-only --commits 5` → ~25 cached generations against free models, $0 spend.
5. `bench judge-prep` → `results/judge-queue/manifest.json` lists ~25 cells in ~5 batches.
6. **Ask Claude (this session) to judge the queue.** Claude reads the manifest and dispatches Opus subagents in parallel; each writes JSON into `results/judgements/`.
7. `bench judge-collect` → all 25 cells validated, none re-queued.
8. `bench report` → `results/report/index.html` opens in a browser, table sorts, Pareto plot renders, per-model drill-downs work.
9. `bench install-hook --model <winner>` → `~/.git-hooks/bin/ai-commit-message` is updated; commit something in a scratch repo to confirm the live hook still works.
10. Add a new model to `config/models.yaml` and rerun `bench run` — confirm only the new cells are computed.

Once green on the free-model shakedown, expand `models.yaml` with paid models and rerun.

---

## Open knobs (sensible defaults, easy to change later)

- Judge subagent: Opus, `subagent_type=general-purpose`, batch size 5 cells, concurrency 4–6.
- Rubric weights: accuracy 0.30, completeness 0.20, conciseness 0.15, format 0.20, body_quality 0.15.
- Temperature: 0.2 for candidates (matches the live hook).
- Concurrency: 4 in-flight OpenRouter requests per provider.
- Hook version starts at `1.0.0`; rubric version at `1.0.0`. Bump either to invalidate the matching cache layer cleanly.
