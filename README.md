# commit-message-benchmark

A reproducible pipeline for picking the best LLM to power a `prepare-commit-msg`
hook. The user's existing two-stage hook (vendored under `hooks/`) is run against
many candidate models via OpenRouter, the outputs are graded by Opus subagents
inside a Claude Code session, and the results are rolled up into a single
self-contained HTML leaderboard.

The pipeline is **driven by Claude Code running inside this repo**. The CLI
handles all deterministic work (cloning, replaying commits, calling models,
rendering the report); Claude itself dispatches Opus subagents to do the
LLM-judging step. There is no paid judge API.

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # then fill in OPENROUTER_API_KEY
```

## Workflow

1. **Build the dataset** (once, or whenever you bump the seed / repo list):

   ```bash
   uv run bench dataset build --max-per-repo 2   # smoke (~12 commits)
   uv run bench dataset build                    # full (~100 commits)
   ```

2. **Generate candidate commit messages**. Cells are cached on disk by
   `(commit_sha, model, hook_version)`, so adding a new model later only
   computes the new cells.

   ```bash
   # Phase B — free-tier shakedown:
   uv run bench run --free-only --commits 5

   # Phase C — paid sweep (after adding entries to config/models.yaml):
   uv run bench run
   ```

3. **Prepare the judge queue**. This writes one self-contained prompt file per
   ungraded cell to `results/judge-queue/` plus a `manifest.json`.

   ```bash
   uv run bench judge-prep
   ```

4. **Ask Claude to grade the queue.** In the same Claude Code session, say
   something like:

   > Read `results/judge-queue/manifest.json` and dispatch one Opus subagent per
   > batch in parallel. Each subagent should read its prompt files, follow the
   > rubric instructions inside them, and write the resulting JSON object to
   > the matching `judgement_path`.

   Claude takes it from there.

5. **Collect and validate** the results, then render the report:

   ```bash
   uv run bench judge-collect
   uv run bench report
   open results/report/index.html
   ```

6. **Install the winner** back into your real git hooks directory:

   ```bash
   uv run bench install-hook --model anthropic/claude-haiku-4.5
   # then in your shell rc:
   export AI_COMMIT_API_URL=https://openrouter.ai/api/v1/chat/completions
   export AI_COMMIT_API_KEY=$OPENROUTER_API_KEY
   export AI_COMMIT_MODEL=anthropic/claude-haiku-4.5
   ```

## Adding a new model

Add a line to the `paid:` (or `free:`) section of `config/models.yaml`, then:

```bash
uv run bench run --models the/new-model-slug
uv run bench judge-prep
# ask Claude to grade the new cells (as in step 4)
uv run bench judge-collect
uv run bench report
```

The runner skips every cell that's already cached, so the marginal cost is just
the new model × 100 commits.

## Layout

```
config/    repos.yaml, models.yaml, rubric.yaml — the only knobs you usually touch
hooks/     vendored copy of the live prepare-commit-msg hook (with usage sidecar)
data/      shallow clones + commits.jsonl + replay cache
results/   per-cell generations, judge queue, judgements, leaderboard
src/cmb/   the implementation
tests/     unit tests (run with `uv run pytest`)
```

## Cache invalidation

- Bumping `hooks/VERSION` invalidates all cached generations and judgements.
- Bumping `config/rubric.yaml`'s `version` invalidates only judgements.
- Adding a new model only adds new cells; existing ones are reused.
