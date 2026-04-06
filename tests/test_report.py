from cmb.report import ModelStats, render_html, _pareto_pick


def _stats(model: str, score: float, cost: float) -> ModelStats:
    return ModelStats(
        model=model,
        n=10,
        weighted_score=score,
        score_ci=(score - 0.1, score + 0.1),
        format_pass_rate=0.9,
        cost_mean=cost,
        cost_p50=cost,
        cost_p95=cost * 1.2,
        latency_mean=2.0,
        latency_p50=1.8,
        latency_p95=3.5,
        failure_rate=0.0,
        per_commit=[],
    )


def test_pareto_pick_picks_best_score_and_best_budget():
    stats = [
        _stats("a/expensive", score=4.6, cost=0.01),
        _stats("b/cheap", score=4.2, cost=0.0001),
        _stats("c/mid", score=4.4, cost=0.001),
    ]
    best, budget = _pareto_pick(stats)
    assert best.model == "a/expensive"
    assert budget.model == "b/cheap"


def test_render_html_contains_model_anchors_and_table():
    stats = [
        _stats("openai/gpt-5-mini", score=4.3, cost=0.0008),
        _stats("anthropic/claude-haiku-4.5", score=4.5, cost=0.002),
    ]
    meta = {"hook_version": "1.0.0", "rubric_version": "1.0.0", "n_commits": 12, "weights": {}}
    html = render_html(stats, meta)
    assert "<table class='lb'>" in html
    assert "openai/gpt-5-mini" in html
    assert "id='model-anthropic_claude-haiku-4.5'" in html
    assert "Recommended" in html
    assert "<svg" in html  # pareto plot rendered
