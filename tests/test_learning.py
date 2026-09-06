from outlier.learning import build_learning_summary


def test_learning_summary_shows_growth_and_preserves_safety():
    def run(run_id, round_no, recognition, queue, accuracy, rules, tokens):
        return {
            "run_id": run_id,
            "round_no": round_no,
            "created_at": f"2026-09-0{round_no}",
            "config": {"policy": {"allow_auto_post": False}},
            "metrics": {
                "rule_hit_rate": recognition,
                "auto_resolve_rate": 0.0 if round_no == 1 else 0.25,
                "needs_review": queue,
                "classification_precision": accuracy,
                "false_auto_posts": 0,
                "rules_in_memory": rules,
                "llm_total_tokens": tokens,
            },
        }

    summary = build_learning_summary([
        run("R1", 1, 0.0, 10, 0.9, 0, 100),
        run("R2", 2, 0.7, 7, 0.9, 3, 90),
    ], [{"payload": {"category": "fee"}, "hits": 2}])
    assert summary["delta"] == {
        "recognition_rate": 0.7,
        "needs_review": -3,
        "classification_accuracy": 0.0,
        "auto_resolve_rate": 0.25,
    }
    assert summary["controls"]["accuracy_non_regressed"] is True
    assert summary["controls"]["accuracy_evaluated"] is True
    assert summary["memory"]["categories"] == {"fee": 1}
    assert "Memory is earning its place" in {r["title"] for r in summary["reflections"]}


def _run(run_id, round_no, metrics):
    return {
        "run_id": run_id,
        "round_no": round_no,
        "created_at": f"2026-09-0{round_no}",
        "config": {"policy": {"allow_auto_post": False}},
        "metrics": metrics,
    }


def _base_metrics(**over):
    m = {
        "rule_hit_rate": 0.5,
        "auto_resolve_rate": 0.2,
        "needs_review": 8,
        "classification_precision": 0.9,
        "false_auto_posts": 0,
        "rules_in_memory": 2,
        "llm_total_tokens": 100,
    }
    m.update(over)
    return m


def test_learning_summary_flags_an_accuracy_regression():
    summary = build_learning_summary([
        _run("R1", 1, _base_metrics(classification_precision=0.9)),
        _run("R2", 2, _base_metrics(classification_precision=0.85)),
    ], [])
    assert summary["controls"]["accuracy_non_regressed"] is False
    assert summary["controls"]["accuracy_evaluated"] is True
    assert summary["delta"]["classification_accuracy"] == -0.05
    safety = next(r for r in summary["reflections"] if r["kind"] == "safety")
    assert "needs investigation" in safety["finding"]


def test_learning_summary_with_no_runs_is_empty_but_honest():
    summary = build_learning_summary([], [])
    assert summary["has_runs"] is False
    assert summary["run_count"] == 0
    assert summary["reflections"] == []
    assert summary["controls"]["accuracy_evaluated"] is False
    assert summary["controls"]["accuracy_non_regressed"] is None


def test_learning_summary_marks_unevaluated_accuracy_as_unknown():
    metrics = _base_metrics()
    del metrics["classification_precision"]
    summary = build_learning_summary([_run("R1", 1, metrics)], [])
    assert summary["rounds"][0]["classification_accuracy"] is None
    assert summary["delta"]["classification_accuracy"] is None
    assert summary["controls"]["accuracy_evaluated"] is False
    assert summary["controls"]["accuracy_non_regressed"] is None
    safety = next(r for r in summary["reflections"] if r["kind"] == "safety")
    assert "not been evaluated" in safety["finding"]
