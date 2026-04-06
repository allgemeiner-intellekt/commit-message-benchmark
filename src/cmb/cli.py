"""`bench …` CLI. Each command is a thin wrapper around a module function."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from . import paths
from .dataset import build_dataset, load_dataset
from .judge_io import collect, write_queue
from .report import write_report
from .runner import load_models, run

load_dotenv()

app = typer.Typer(add_completion=False, no_args_is_help=True)
dataset_app = typer.Typer(no_args_is_help=True, help="Build / inspect the benchmark dataset.")
app.add_typer(dataset_app, name="dataset")


@dataset_app.command("build")
def dataset_build(max_per_repo: Optional[int] = typer.Option(None, "--max-per-repo")):
    """Clone source repos and write data/commits.jsonl."""
    build_dataset(max_per_repo=max_per_repo)


@dataset_app.command("show")
def dataset_show(limit: int = 10):
    """Print the first N commits from data/commits.jsonl."""
    rows = load_dataset()
    for r in rows[:limit]:
        typer.echo(f"{r.id}\t{r.category}\t{r.repo}\t{r.sha[:10]}\t{r.original_message.splitlines()[0][:80]}")
    typer.echo(f"... {len(rows)} total")


@app.command("run")
def cmd_run(
    models: Optional[list[str]] = typer.Option(None, "--models", "-m"),
    commits: Optional[int] = typer.Option(None, "--commits", "-n"),
):
    """Generate commit messages for (commit × model) cells, skipping cached ones."""
    summary = run(only_models=models, limit_commits=commits)
    typer.echo(json.dumps(summary, indent=2))


@app.command("judge-prep")
def cmd_judge_prep(batch_size: int = typer.Option(5, "--batch-size")):
    """Write prompts + manifest.json for any ungraded cells."""
    manifest = write_queue(batch_size=batch_size)
    typer.echo(
        f"queued {len(manifest['queued'])} cells in {len(manifest['batches'])} batches → "
        f"{paths.JUDGE_MANIFEST}"
    )
    if manifest["queued"]:
        typer.echo(
            "next: ask Claude (in this session) to read the manifest and dispatch "
            "Opus subagents to grade each batch, then run `bench judge-collect`."
        )


@app.command("judge-collect")
def cmd_judge_collect():
    """Validate subagent JSON outputs and rebuild the judgements index."""
    summary = collect()
    typer.echo(json.dumps(summary, indent=2))


@app.command("report")
def cmd_report():
    """Render results/report/index.html."""
    write_report()


@app.command("status")
def cmd_status():
    """Quick health check: dataset size, cached cells, queued judgements."""
    n_commits = len(load_dataset())
    n_cached = len(list(paths.GENERATION_CACHE_DIR.glob("*.json")))
    n_queued = len(list(paths.JUDGE_QUEUE_DIR.glob("*.prompt.md")))
    n_judged = len(list(paths.JUDGEMENTS_DIR.glob("*.json")))
    typer.echo(
        json.dumps(
            {
                "hook_version": paths.hook_version(),
                "commits": n_commits,
                "cached_generations": n_cached,
                "queued_judgements": n_queued,
                "completed_judgements": n_judged,
            },
            indent=2,
        )
    )


@app.command("install-hook")
def cmd_install_hook(
    model: str = typer.Option(..., "--model", "-m"),
    target: Path = typer.Option(Path.home() / ".git-hooks", "--target"),
):
    """Copy the vendored hook back to ~/.git-hooks/ and set the chosen model."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.HOOKS_DIR / "prepare-commit-msg", target / "prepare-commit-msg")
    shutil.copy2(paths.HOOK_BIN, target / "bin" / "ai-commit-message")
    (target / "VERSION").write_text(paths.hook_version() + "\n", encoding="utf-8")
    typer.echo(
        f"installed hook v{paths.hook_version()} to {target}\n"
        f"set the model in your shell rc:\n"
        f"  export AI_COMMIT_API_URL=https://openrouter.ai/api/v1/chat/completions\n"
        f"  export AI_COMMIT_API_KEY=$OPENROUTER_API_KEY\n"
        f"  export AI_COMMIT_MODEL={model}"
    )


@app.command("models")
def cmd_models():
    """List configured candidate models."""
    for m in load_models():
        typer.echo(m["slug"])


if __name__ == "__main__":
    app()
