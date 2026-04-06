"""Aggregate cached generations + judgements into a single self-contained
HTML leaderboard at results/report/index.html.

No external assets, no chart libs — vanilla JS for the sortable table and
inline SVG for the Pareto plot."""

from __future__ import annotations

import html
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .dataset import load_dataset
from .generator import GenerationResult
from .judge_io import judgement_path, all_cells, load_rubric


@dataclass
class ModelStats:
    model: str
    n: int
    weighted_score: float
    score_ci: tuple[float, float]
    format_pass_rate: float
    cost_mean: float
    cost_p50: float
    cost_p95: float
    latency_mean: float
    latency_p50: float
    latency_p95: float
    failure_rate: float
    per_commit: list[dict[str, Any]]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _bootstrap_ci(values: list[float], iters: int = 500, alpha: float = 0.05) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (values[0], values[0])
    import random
    rng = random.Random(20260406)
    means = []
    n = len(values)
    for _ in range(iters):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(alpha / 2 * iters)]
    hi = means[int((1 - alpha / 2) * iters) - 1]
    return (lo, hi)


def aggregate() -> tuple[list[ModelStats], dict[str, Any]]:
    rubric = load_rubric()
    rubric_version = str(rubric.get("version", "1.0.0"))
    hook_version = paths.hook_version()
    weights = {k: float(v["weight"]) for k, v in rubric["dimensions"].items()}

    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failures: dict[str, int] = defaultdict(int)

    for cell, gen, commit in all_cells(rubric_version, hook_version):
        if not gen.ok:
            failures[gen.model] += 1
            by_model[gen.model].append({"commit": commit, "gen": gen, "scores": None})
            continue
        jp = judgement_path(cell)
        if not jp.exists():
            continue
        try:
            scores = json.loads(jp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        weighted = sum(scores[k] * weights.get(k, 0.0) for k in weights)
        by_model[gen.model].append(
            {
                "commit": commit,
                "gen": gen,
                "scores": scores,
                "weighted": weighted,
            }
        )

    stats_list: list[ModelStats] = []
    for model, rows in by_model.items():
        graded = [r for r in rows if r.get("scores")]
        if not graded:
            continue
        weighted_vals = [r["weighted"] for r in graded]
        cost_vals = [r["gen"].total_cost_usd for r in graded]
        latency_vals = [r["gen"].total_latency_ms / 1000.0 for r in graded]
        format_pass_rate = (
            sum(1 for r in graded if r["scores"].get("format_pass")) / len(graded)
        )
        n_total = len(rows)
        n_failed = failures.get(model, 0)
        stats_list.append(
            ModelStats(
                model=model,
                n=len(graded),
                weighted_score=sum(weighted_vals) / len(weighted_vals),
                score_ci=_bootstrap_ci(weighted_vals),
                format_pass_rate=format_pass_rate,
                cost_mean=sum(cost_vals) / len(cost_vals),
                cost_p50=_percentile(cost_vals, 0.5),
                cost_p95=_percentile(cost_vals, 0.95),
                latency_mean=sum(latency_vals) / len(latency_vals),
                latency_p50=_percentile(latency_vals, 0.5),
                latency_p95=_percentile(latency_vals, 0.95),
                failure_rate=n_failed / n_total if n_total else 0.0,
                per_commit=graded,
            )
        )

    stats_list.sort(key=lambda s: (-s.weighted_score, s.cost_mean))

    meta = {
        "rubric_version": rubric_version,
        "hook_version": hook_version,
        "n_commits": len(load_dataset()),
        "weights": weights,
    }
    return stats_list, meta


def _pareto_pick(stats: list[ModelStats]) -> tuple[ModelStats | None, ModelStats | None]:
    if not stats:
        return None, None
    # Best overall: highest weighted score (ties broken by lower cost).
    best = max(stats, key=lambda s: (s.weighted_score, -s.cost_mean))
    # Best on a budget: highest score among bottom-half cost.
    cost_sorted = sorted(stats, key=lambda s: s.cost_mean)
    cheap_half = cost_sorted[: max(1, len(cost_sorted) // 2)]
    budget = max(cheap_half, key=lambda s: s.weighted_score)
    return best, budget


def _svg_pareto(stats: list[ModelStats]) -> str:
    if not stats:
        return ""
    width, height = 640, 360
    pad_l, pad_b, pad_t, pad_r = 60, 40, 20, 20
    xs = [max(s.cost_mean, 1e-6) for s in stats]
    ys = [s.weighted_score for s in stats]
    x_min = min(xs) / 2 if min(xs) > 0 else 1e-7
    x_max = max(xs) * 2
    y_min = min(ys) - 0.2
    y_max = max(ys) + 0.2

    def lx(x: float) -> float:
        # log scale on cost
        return pad_l + (math.log10(x) - math.log10(x_min)) / (math.log10(x_max) - math.log10(x_min)) * (width - pad_l - pad_r)

    def ly(y: float) -> float:
        return height - pad_b - (y - y_min) / (y_max - y_min) * (height - pad_b - pad_t)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="pareto">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff" stroke="#ddd"/>',
        f'<text x="{width/2}" y="{height-8}" text-anchor="middle" font-size="12">cost per commit (USD, log scale)</text>',
        f'<text x="14" y="{height/2}" text-anchor="middle" font-size="12" transform="rotate(-90 14 {height/2})">weighted score (1-5)</text>',
    ]
    for s in stats:
        cx = lx(max(s.cost_mean, 1e-6))
        cy = ly(s.weighted_score)
        anchor = html.escape(s.model)
        parts.append(
            f'<a href="#model-{html.escape(s.model.replace("/", "_"))}">'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="#3b82f6" opacity="0.8">'
            f'<title>{anchor}\nscore {s.weighted_score:.2f}\n${s.cost_mean:.5f}</title></circle></a>'
        )
        parts.append(
            f'<text x="{cx+8:.1f}" y="{cy-8:.1f}" font-size="10" fill="#333">{anchor}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


_CSS = """
body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; color: #1a1a1a; padding: 0 1rem; }
h1 { margin-bottom: 0.2rem; }
h2 { margin-top: 2.5rem; }
.recommendation { background: #f0f9ff; border-left: 4px solid #0284c7; padding: 1rem 1.2rem; border-radius: 4px; margin: 1rem 0 2rem; }
.recommendation strong { color: #0369a1; }
table.lb { border-collapse: collapse; width: 100%; font-size: 13px; }
table.lb th, table.lb td { border-bottom: 1px solid #eee; padding: 8px 10px; text-align: right; }
table.lb th:first-child, table.lb td:first-child { text-align: left; }
table.lb th { cursor: pointer; background: #fafafa; user-select: none; }
table.lb th:hover { background: #f0f0f0; }
table.lb tbody tr:hover { background: #fafbff; }
.model-card { border: 1px solid #e5e5e5; border-radius: 6px; padding: 1rem 1.2rem; margin: 1rem 0; }
.model-card h3 { margin-top: 0; }
.example { border-top: 1px dashed #ddd; padding-top: 0.6rem; margin-top: 0.6rem; }
.example pre { background: #f6f6f6; padding: 0.6rem; border-radius: 4px; white-space: pre-wrap; font-size: 12px; }
.muted { color: #666; }
.pareto { width: 100%; max-width: 700px; }
"""

_SORT_JS = """
document.querySelectorAll('table.lb').forEach(table => {
  const headers = table.querySelectorAll('th');
  headers.forEach((th, idx) => {
    th.addEventListener('click', () => {
      const tbody = table.tBodies[0];
      const rows = Array.from(tbody.rows);
      const asc = th.dataset.asc !== 'true';
      headers.forEach(h => h.dataset.asc = '');
      th.dataset.asc = asc;
      rows.sort((a, b) => {
        const av = a.cells[idx].dataset.sort ?? a.cells[idx].textContent;
        const bv = b.cells[idx].dataset.sort ?? b.cells[idx].textContent;
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
});
"""


def render_html(stats: list[ModelStats], meta: dict[str, Any]) -> str:
    best, budget = _pareto_pick(stats)
    rec_lines = []
    if best:
        rec_lines.append(
            f"<strong>Recommended:</strong> <code>{html.escape(best.model)}</code> — "
            f"weighted score {best.weighted_score:.2f}, ${best.cost_mean:.5f}/commit, "
            f"format pass {best.format_pass_rate*100:.0f}%."
        )
    if budget and budget is not best:
        rec_lines.append(
            f"<br><strong>Best on a budget:</strong> <code>{html.escape(budget.model)}</code> — "
            f"weighted score {budget.weighted_score:.2f}, ${budget.cost_mean:.5f}/commit."
        )
    rec_html = "<div class='recommendation'>" + "".join(rec_lines or ["No graded models yet."]) + "</div>"

    rows = []
    for s in stats:
        rows.append(
            "<tr>"
            f"<td><a href='#model-{html.escape(s.model.replace('/', '_'))}'>{html.escape(s.model)}</a></td>"
            f"<td data-sort='{s.weighted_score}'>{s.weighted_score:.3f}</td>"
            f"<td data-sort='{s.score_ci[0]}'>[{s.score_ci[0]:.2f}, {s.score_ci[1]:.2f}]</td>"
            f"<td data-sort='{s.format_pass_rate}'>{s.format_pass_rate*100:.0f}%</td>"
            f"<td data-sort='{s.cost_mean}'>${s.cost_mean:.5f}</td>"
            f"<td data-sort='{s.cost_p95}'>${s.cost_p95:.5f}</td>"
            f"<td data-sort='{s.latency_mean}'>{s.latency_mean:.2f}s</td>"
            f"<td data-sort='{s.latency_p95}'>{s.latency_p95:.2f}s</td>"
            f"<td data-sort='{s.failure_rate}'>{s.failure_rate*100:.0f}%</td>"
            f"<td data-sort='{s.n}'>{s.n}</td>"
            "</tr>"
        )
    table_html = (
        "<table class='lb'>"
        "<thead><tr>"
        "<th>model</th><th>score</th><th>95% CI</th><th>format ✓</th>"
        "<th>cost mean</th><th>cost p95</th><th>latency mean</th><th>latency p95</th>"
        "<th>fail rate</th><th>n</th>"
        "</tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )

    cards = []
    for s in stats:
        anchor = s.model.replace("/", "_")
        graded = [r for r in s.per_commit if r.get("scores")]
        graded.sort(key=lambda r: r["weighted"], reverse=True)
        best_examples = graded[:3]
        worst_examples = graded[-3:][::-1]

        def render_example(r):
            commit = r["commit"]
            gen = r["gen"]
            scores = r["scores"]
            return (
                "<div class='example'>"
                f"<div class='muted'>{html.escape(commit.repo)} @ {commit.sha[:10]} — {commit.category} · "
                f"weighted {r['weighted']:.2f} (acc {scores['accuracy']}, comp {scores['completeness']}, "
                f"conc {scores['conciseness']}, fmt {scores['format']}, body {scores['body_quality']})</div>"
                f"<pre><b>candidate:</b>\n{html.escape(gen.message)}</pre>"
                f"<pre><b>original human message:</b>\n{html.escape(commit.original_message[:600])}</pre>"
                f"<div class='muted'>{html.escape(scores.get('rationale',''))}</div>"
                "</div>"
            )

        cards.append(
            f"<div class='model-card' id='model-{html.escape(anchor)}'>"
            f"<h3>{html.escape(s.model)}</h3>"
            f"<div class='muted'>n={s.n} · score {s.weighted_score:.3f} · ${s.cost_mean:.5f}/commit · "
            f"latency p50 {s.latency_p50:.2f}s / p95 {s.latency_p95:.2f}s · format ✓ {s.format_pass_rate*100:.0f}%</div>"
            "<h4>Top 3</h4>"
            + "".join(render_example(r) for r in best_examples)
            + "<h4>Bottom 3</h4>"
            + "".join(render_example(r) for r in worst_examples)
            + "</div>"
        )

    body = (
        f"<h1>Commit message benchmark</h1>"
        f"<div class='muted'>hook v{meta['hook_version']} · rubric v{meta['rubric_version']} · "
        f"{meta['n_commits']} commits in dataset</div>"
        + rec_html
        + "<h2>Leaderboard</h2>"
        + table_html
        + "<h2>Cost vs quality</h2>"
        + _svg_pareto(stats)
        + "<h2>Per-model drill-downs</h2>"
        + "".join(cards)
    )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>commit-message-benchmark</title>"
        f"<style>{_CSS}</style></head><body>"
        + body
        + f"<script>{_SORT_JS}</script>"
        + "</body></html>"
    )


def write_report() -> Path:
    paths.ensure_dirs()
    stats, meta = aggregate()
    html_text = render_html(stats, meta)
    paths.REPORT_HTML.write_text(html_text, encoding="utf-8")
    print(f"[report] wrote {paths.REPORT_HTML} ({len(stats)} models)")
    return paths.REPORT_HTML
