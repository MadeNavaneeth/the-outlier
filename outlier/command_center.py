"""Deterministic controller priorities derived from a reconciliation run.

The command center is intentionally separate from the agent workflow. It turns
the run's existing evidence into an ordered close plan without making a new
classification or posting decision.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def _as_date(value: str | None) -> date:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                pass
    return date.today()


def _owner(category: str, critic: str) -> str:
    if category == "fraud_suspect":
        return "fraud"
    if category in {"fee", "missing_entry", "duplicate"}:
        return "AP"
    if category == "timing":
        return "treasury"
    if critic in {"FAIL", "ESCALATE"} or category in {"fx", "unknown"}:
        return "controllership"
    return "controllership"


def _recommendation(category: str, resolution: str, critic: str) -> str:
    if category == "fraud_suspect":
        return "Investigate counterparty and preserve supporting evidence before close."
    if critic in {"FAIL", "ESCALATE"}:
        return "Resolve the critic exception and obtain controller sign-off."
    if resolution == "journal_entry":
        return "Review the proposed journal entry, support it, and approve or re-code."
    if category == "timing":
        return "Confirm the clearing date and document the reconciling item."
    return "Confirm the classification and document the close decision."


def _action_for(exception: dict[str, Any], policy: dict[str, Any], as_of: date) -> dict[str, Any]:
    category = str(exception.get("category", "unknown"))
    evidence = exception.get("evidence") or {}
    critic = str((evidence.get("critic") or {}).get("verdict", ""))
    amount = abs(float(exception.get("amount", 0.0)))
    materiality = float(policy.get("materiality_limit", 0.0))
    exception_date = _as_date(str(exception.get("date", "")))
    age_days = max(0, (as_of - exception_date).days)
    factors: list[dict[str, Any]] = []
    score = 20 if exception.get("needs_review", True) else 0
    if exception.get("needs_review", True):
        factors.append({"code": "unresolved", "points": 20, "reason": "requires human review"})
    if category in {"fraud_suspect", "unknown"}:
        score += 35
        factors.append({"code": "control_risk", "points": 35, "reason": f"{category} is not safe to automate"})
    if critic in {"FAIL", "ESCALATE"}:
        score += 25
        factors.append({"code": "critic", "points": 25, "reason": f"critic verdict is {critic}"})
    if exception.get("materiality_breach") or (materiality and amount > materiality):
        score += 25
        factors.append({"code": "materiality", "points": 25, "reason": f"amount exceeds {materiality:.2f} materiality limit"})
    amount_points = min(15, round((amount / materiality) * 15, 1)) if materiality else 0
    if amount_points:
        score += amount_points
        factors.append({"code": "amount", "points": amount_points, "reason": f"absolute amount is {amount:.2f}"})
    age_points = min(15, age_days)
    if age_points:
        score += age_points
        factors.append({"code": "age", "points": age_points, "reason": f"open item is {age_days} day(s) old"})
    score = min(100, round(score, 1))
    severity = "critical" if score >= 80 else "high" if score >= 55 else "medium" if score >= 30 else "low"
    owner = _owner(category, critic)
    due_days = {"fraud": 1, "controllership": 2, "AP": 4, "treasury": 5}[owner]
    due_date = as_of + timedelta(days=due_days)
    return {
        "action_id": f"ACTION-{exception.get('exception_id', 'UNKNOWN')}",
        "exception_id": exception.get("exception_id", ""),
        "priority": 0,
        "severity": severity,
        "risk_score": score,
        "owner_lane": owner,
        "suggested_due_date": due_date.isoformat(),
        "status": "open" if exception.get("needs_review", True) else "resolved",
        "title": f"Resolve {category.replace('_', ' ')} exception",
        "recommendation": _recommendation(category, str(evidence.get("resolution", "")), critic),
        "evidence": {
            "exception_id": exception.get("exception_id", ""),
            "category": category,
            "amount": exception.get("amount", 0.0),
            "date": exception.get("date", ""),
            "description": exception.get("description", ""),
            "critic_verdict": critic or None,
            "review_reason": evidence.get("review_reason", ""),
            "factors": factors,
        },
    }


def build_command_center(run: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic close plan from one persisted run payload."""
    policy = (run.get("config") or {}).get("policy") or {}
    as_of = _as_date(run.get("created_at"))
    actions = [_action_for(e, policy, as_of) for e in run.get("exceptions", [])]
    actions.sort(key=lambda a: (-a["risk_score"], a["suggested_due_date"], a["exception_id"]))
    for index, action in enumerate(actions, start=1):
        action["priority"] = index
    open_actions = [a for a in actions if a["status"] == "open"]
    return {
        "run_id": run.get("run_id"),
        "as_of": as_of.isoformat(),
        "generated_from": "reconciliation_run",
        "method": "deterministic risk scoring; no new model decision",
        "summary": {
            "actions": len(actions),
            "open_actions": len(open_actions),
            "critical": sum(a["severity"] == "critical" for a in open_actions),
            "high": sum(a["severity"] == "high" for a in open_actions),
            "medium": sum(a["severity"] == "medium" for a in open_actions),
            "low": sum(a["severity"] == "low" for a in open_actions),
            "amount_at_risk": round(sum(abs(float(a["evidence"]["amount"])) for a in open_actions), 2),
        },
        "controls": {
            "auto_post_enabled": bool(policy.get("allow_auto_post", False)),
            "materiality_limit": policy.get("materiality_limit"),
            "human_review_required_for": policy.get("never_auto_categories", []),
            "posting_unchanged": True,
        },
        "actions": actions,
    }


def command_center_markdown(center: dict[str, Any]) -> str:
    """Render the command center for a controller or auditor."""
    s = center["summary"]
    lines = [
        f"# Close Command Center - {center['run_id']}",
        "",
        f"*As of {center['as_of']} | {center['method']}*",
        "",
        "## Controller snapshot",
        "",
        f"- Open actions: **{s['open_actions']}** ({s['critical']} critical, {s['high']} high)",
        f"- Amount represented by open exceptions: **${s['amount_at_risk']:,.2f}**",
        f"- Auto-posting remains **{'enabled' if center['controls']['auto_post_enabled'] else 'disabled'}**",
        "",
        "## Prioritized actions",
        "",
        "| Priority | Severity | Owner | Due | Exception | Score | Recommendation |",
        "|---:|---|---|---|---|---:|---|",
    ]
    for action in center["actions"]:
        if action["status"] != "open":
            continue
        lines.append(
            f"| {action['priority']} | {action['severity']} | {action['owner_lane']} | "
            f"{action['suggested_due_date']} | {action['exception_id']} | {action['risk_score']} | {action['recommendation']} |"
        )
    lines.extend(["", "## Scoring and controls", "", "Each score is deterministic and links back to the exception evidence.", ""])
    for action in center["actions"]:
        reasons = "; ".join(f"{f['code']} +{f['points']}" for f in action["evidence"]["factors"]) or "resolved"
        lines.append(f"- `{action['exception_id']}`: {action['risk_score']} ({reasons})")
    return "\n".join(lines) + "\n"
