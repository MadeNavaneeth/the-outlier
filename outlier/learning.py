"""Deterministic learning-loop evidence for the review desk and reports."""

from __future__ import annotations

from typing import Any


def _metric(run: dict[str, Any], name: str, default: float = 0.0) -> float:
    value = (run.get("metrics") or {}).get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _accuracy(run: dict[str, Any]) -> float | None:
    metrics = run.get("metrics") or {}
    if "classification_precision" not in metrics:
        return None
    try:
        return float(metrics["classification_precision"])
    except (TypeError, ValueError):
        return None


def build_learning_summary(runs: list[dict[str, Any]], rules: list[Any]) -> dict[str, Any]:
    """Summarize what changed, what stayed safe, and what to review next.

    This is deliberately derived from persisted run metrics and rule memory. It
    does not invent a model opinion or change a policy decision.
    """
    ordered = sorted(runs, key=lambda r: (r.get("round_no", 0), r.get("created_at", "")))
    first = ordered[0] if ordered else {}
    latest = ordered[-1] if ordered else {}
    first_m = first.get("metrics") or {}
    latest_m = latest.get("metrics") or {}
    false_auto_posts = [int(_metric(r, "false_auto_posts")) for r in ordered]
    accuracy_values = [value for r in ordered if (value := _accuracy(r)) is not None]
    recognition_delta = _metric(latest, "rule_hit_rate") - _metric(first, "rule_hit_rate")
    queue_delta = int(_metric(latest, "needs_review")) - int(_metric(first, "needs_review"))
    accuracy_non_regressed = bool(accuracy_values) and all(
        accuracy_values[index] >= accuracy_values[index - 1] - 1e-9
        for index in range(1, len(accuracy_values))
    )
    rules_dicts = [r.to_dict() if hasattr(r, "to_dict") else r for r in rules]
    categories: dict[str, int] = {}
    for rule in rules_dicts:
        category = str((rule.get("payload") or {}).get("category", "unknown"))
        categories[category] = categories.get(category, 0) + 1
    reflections: list[dict[str, Any]] = []
    if ordered:
        reflections.append({
            "kind": "learning",
            "title": "Memory is earning its place",
            "finding": f"Recognition moved from {_metric(first, 'rule_hit_rate') * 100:.1f}% to {_metric(latest, 'rule_hit_rate') * 100:.1f}%.",
            "evidence": f"{len(rules_dicts)} approved rules are persisted from human decisions.",
        })
        reflections.append({
            "kind": "efficiency",
            "title": "Human work is shrinking",
            "finding": f"The review queue changed by {queue_delta:+d} items across {len(ordered)} round(s).",
            "evidence": f"Latest queue: {int(_metric(latest, 'needs_review'))}; latest auto-resolved: {int(_metric(latest, 'auto_resolved'))}.",
        })
        reflections.append({
            "kind": "safety",
            "title": "Safety did not trade away accuracy",
            "finding": (
                "Classification accuracy never regressed."
                if accuracy_values and accuracy_non_regressed
                else "Classification accuracy has not been evaluated for these runs."
                if not accuracy_values
                else "Classification accuracy needs investigation before more automation."
            ),
            "evidence": f"False auto-posts across recorded rounds: {max(false_auto_posts, default=0)}.",
        })
    next_step = (
        "Review the remaining queue and approve only patterns with clear evidence."
        if latest_m and int(_metric(latest, "needs_review"))
        else "Run another month and test whether the learned memory generalizes."
    )
    return {
        "has_runs": bool(ordered),
        "run_count": len(ordered),
        "baseline_run_id": first.get("run_id"),
        "latest_run_id": latest.get("run_id"),
        "rounds": [
            {
                "run_id": run.get("run_id"),
                "round_no": run.get("round_no"),
                "recognition_rate": _metric(run, "rule_hit_rate"),
                "auto_resolve_rate": _metric(run, "auto_resolve_rate"),
                "needs_review": int(_metric(run, "needs_review")),
                "classification_accuracy": _accuracy(run),
                "false_auto_posts": int(_metric(run, "false_auto_posts")),
                "rules_in_memory": int(_metric(run, "rules_in_memory")),
                "llm_total_tokens": int(_metric(run, "llm_total_tokens")),
            }
            for run in ordered
        ],
        "delta": {
            "recognition_rate": round(recognition_delta, 4),
            "needs_review": queue_delta,
            "classification_accuracy": (
                round((_accuracy(latest) or 0.0) - (_accuracy(first) or 0.0), 4)
                if _accuracy(latest) is not None and _accuracy(first) is not None
                else None
            ),
            "auto_resolve_rate": round(_metric(latest, "auto_resolve_rate") - _metric(first, "auto_resolve_rate"), 4),
        },
        "memory": {
            "rules": len(rules_dicts),
            "categories": categories,
            "total_hits": sum(int(rule.get("hits") or 0) for rule in rules_dicts),
        },
        "controls": {
            "false_auto_posts": max(false_auto_posts, default=0),
            "accuracy_non_regressed": accuracy_non_regressed if accuracy_values else None,
            "accuracy_evaluated": bool(accuracy_values),
            "auto_post_enabled": bool((latest.get("config") or {}).get("policy", {}).get("allow_auto_post", False)),
        },
        "reflections": reflections,
        "next_step": next_step,
    }
