"""Outputs: reconciliation report, audit trail, metrics table, improvement chart.

Markdown + CSV only -- no chart library, so the repo has zero dependencies and
the report renders on GitHub and in the Devpost write-up.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .eval import baseline_manual_estimate, improvement_table


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def reconciliation_report(run: dict[str, Any], evaluation: dict[str, Any] | None = None) -> str:
    m = run["metrics"]
    lines: list[str] = []
    a = lines.append
    a(f"# Bank Reconciliation Report - {run['run_id']}")
    a("")
    a(f"*Round {run['round_no']} | generated {run['created_at']} | bank file `{run['bank_file']}` | GL file `{run['ledger_file']}`*")
    a("")
    a("## 1. Summary")
    a("")
    a("| Metric | Value |")
    a("|---|---|")
    a(f"| Bank statement rows | {m['bank_rows']} |")
    a(f"| GL / ledger rows | {m['ledger_rows']} |")
    a(f"| Deterministic matches | {m['matches']} ({m['multi_line_matches']} multi-line) |")
    a(f"| Auto-match rate (bank rows) | {_fmt_pct(m['auto_match_rate_bank'])} |")
    a(f"| GL lines matched | {m['ledger_lines_matched']} / {m['ledger_rows']} ({_fmt_pct(m['auto_match_rate_ledger'])}) |")
    a(f"| Exceptions raised | {m['exceptions']} |")
    a(f"| Auto-resolved from approved rules | {m['auto_resolved']} ({_fmt_pct(m['auto_resolve_rate'])}) |")
    a(f"| Patterns recognised but gated to a human | {m.get('rule_hits_gated', 0)} "
      f"(recognition rate {_fmt_pct(m.get('rule_hit_rate', 0.0))}) |")
    a(f"| Human review queue | {m['needs_review']} ({_fmt_pct(m['review_queue_rate'])}) |")
    a(f"| Journal entries posted | {m['posted_entries']} |")
    a(f"| Unattended auto-posts | **{len(run.get('posted', []))}** (policy: {run['config']['policy']['allow_auto_post']}) |")
    a(f"| Rules in memory | {m['rules_in_memory']} |")
    a(f"| LLM calls / tokens | {m['llm_calls']} / {m['llm_total_tokens']:,} |")
    a(f"| Run time | {m['elapsed_seconds']}s |")
    a("")
    a("## 2. Deterministic match breakdown")
    a("")
    a("| Pass | Matches |")
    a("|---|---|")
    for k, v in m["match_steps"].items():
        a(f"| {k} | {v} |")
    a("")
    a("## 3. Exceptions by category")
    a("")
    a("| Category | Count |")
    a("|---|---|")
    for k, v in m["exceptions_by_category"].items():
        a(f"| {k} | {v} |")
    a("")

    a("## 4. Reconciling items (no posting required)")
    a("")
    timing = [e for e in run["exceptions"] if e["evidence"].get("resolution") == "reconciling_item"]
    if not timing:
        a("_None._")
    else:
        a("| Date | Description | Amount | Category |")
        a("|---|---|---:|---|")
        for e in timing:
            a(f"| {e['date']} | {e['description'][:52]} | {e['amount']:,.2f} | {e['category']} |")
    a("")

    a("## 5. Review queue")
    a("")
    queue = [e for e in run["exceptions"] if e["needs_review"]]
    if not queue:
        a("_Queue is empty._")
    else:
        a("| Exception | Date | Amount | Category | Conf | Reason it needs a human |")
        a("|---|---|---:|---|---:|---|")
        for e in queue[:40]:
            reason = e["evidence"].get("review_reason", "")
            a(f"| {e['exception_id']} | {e['date']} | {e['amount']:,.2f} | {e['category']} | {e['confidence']:.2f} | {reason[:70]} |")
        if len(queue) > 40:
            a(f"| ... | | | | | {len(queue) - 40} more |")
    a("")

    a("## 6. Proposed journal entries")
    a("")
    if not run["proposals"]:
        a("_None._")
    else:
        for p in run["proposals"][:25]:
            a(f"**{p['proposal_id']}** - {p['status']} - source `{p['source']}` - critic `{p['critic_verdict']}`")
            a("")
            a("| Account | Debit | Credit |")
            a("|---|---:|---:|")
            for l in p["lines"]:
                a(f"| {l['account_code']} {l['account_name']} | {l['debit']:,.2f} | {l['credit']:,.2f} |")
            a("")
            a(f"> {p['rationale'][:300]}")
            a("")
        if len(run["proposals"]) > 25:
            a(f"_{len(run['proposals']) - 25} further proposals omitted._")
            a("")

    a("## 7. Guardrails applied")
    a("")
    pol = run["config"]["policy"]
    a(f"- Materiality limit: {pol['materiality_limit']:,.2f} (anything above always goes to a human)")
    a(f"- Confidence threshold: {pol['confidence_threshold']}")
    a(f"- Auto-resolve cap: {pol['auto_resolve_cap']:,.2f}, only on an exact approved-rule signature")
    a(f"- Auto-post enabled: {pol['allow_auto_post']} (cap {pol['auto_post_cap']:,.2f})")
    a(f"- Never auto-resolved: {', '.join(pol['never_auto_categories'])}")
    a(f"- Never auto-posted accounts: {', '.join(pol['never_auto_post_accounts'])}")
    a(f"- Unbalanced proposals reaching a human: {m['unbalanced_proposals']}")
    a("")

    if evaluation:
        a("## 8. Accuracy vs planted ground truth")
        a("")
        a("| Metric | Value |")
        a("|---|---|")
        a(f"| Match precision | {_fmt_pct(evaluation['match_precision'])} |")
        a(f"| Match recall | {_fmt_pct(evaluation['match_recall'])} |")
        a(f"| Match F1 | {evaluation['match_f1']} |")
        a(f"| Classification accuracy | {_fmt_pct(evaluation['classification_precision'])} |")
        a(f"| Classification macro-F1 | {evaluation['classification_macro_f1']} |")
        a(f"| False auto-posts | **{evaluation['false_auto_posts']}** (rate {_fmt_pct(evaluation['false_auto_post_rate'])}) |")
        a("")
        a("| Category | TP | FP | FN | Precision | Recall | F1 |")
        a("|---|---:|---:|---:|---:|---:|---:|")
        for cat, s in evaluation["per_category"].items():
            a(f"| {cat} | {s['tp']} | {s['fp']} | {s['fn']} | {s['precision']} | {s['recall']} | {s['f1']} |")
        a("")
        base = baseline_manual_estimate(evaluation["exceptions_raised"])
        a(f"Manual baseline for {evaluation['exceptions_raised']} exceptions at "
          f"{base['minutes_per_exception_assumption']} min each: "
          f"**{base['manual_hours_estimate']} hours** of controller time, vs "
          f"{run['metrics']['elapsed_seconds']} seconds of compute plus review of "
          f"{run['metrics']['needs_review']} queued items.")
        a("")

    a("## 9. Audit trail")
    a("")
    a("Full machine-readable trail in `audit_trail.json` for this run. Sample:")
    a("")
    a("```")
    for e in run.get("audit_sample", [])[:12]:
        a(f"{e['ts']}  {e['actor']:<26} {e['action']:<18} {e['entity_id']}")
    a("```")
    a("")
    return "\n".join(lines)


def improvement_markdown(rows: list[dict[str, Any]]) -> str:
    table = improvement_table(rows)
    if not table:
        return "_No runs recorded._\n"
    headers = list(table[0].keys())
    out = ["# Improvement across runs", "", "Each round: run the reconciliation, work the review queue, re-run.", ""]
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "---|" * len(headers))
    for row in table:
        out.append("| " + " | ".join("" if row[h] is None else str(row[h]) for h in headers) + " |")
    out.append("")
    first, last = table[0], table[-1]
    if len(table) > 1:
        out.append("## What changed")
        out.append("")
        out.append(f"- Pattern recognition from memory: **{_fmt_pct(first.get('rule_hit_rate') or 0)} -> {_fmt_pct(last.get('rule_hit_rate') or 0)}**")
        out.append(f"- Resolved with no human at all: **{_fmt_pct(first['auto_resolve_rate'] or 0)} -> {_fmt_pct(last['auto_resolve_rate'] or 0)}** "
                   f"(capped by policy at {first.get('auto_resolve_cap', 'n/a')})")
        out.append(f"- Human review queue: **{first['needs_review']} -> {last['needs_review']} items**")
        out.append(f"- Approved rules in memory: **{first['rules_in_memory']} -> {last['rules_in_memory']}**")
        out.append(f"- Classification accuracy: **{_fmt_pct(first['classification_precision'] or 0)} -> {_fmt_pct(last['classification_precision'] or 0)}**")
        out.append(f"- False auto-posts: **{first['false_auto_posts']} -> {last['false_auto_posts']}** (must stay 0)")
        out.append("")
    return "\n".join(out)


def exceptions_csv(run: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["exception_id", "date", "amount", "category", "confidence", "needs_review",
                    "resolution", "resolved_by", "explanation"])
        for e in run["exceptions"]:
            w.writerow([e["exception_id"], e["date"], e["amount"], e["category"], e["confidence"],
                        e["needs_review"], e["resolution"], e["resolved_by"], e["explanation"]])
    return p


def write_run_artifacts(run: dict[str, Any], outdir: str | Path, evaluation: dict[str, Any] | None = None,
                        audit: list[dict[str, Any]] | None = None) -> dict[str, Path]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rid = run["run_id"]
    run = dict(run)
    run["audit_sample"] = (audit or [])[:12]
    paths = {
        "report": out / f"{rid}_reconciliation_report.md",
        "run_json": out / f"{rid}_run.json",
        "exceptions": out / f"{rid}_exceptions.csv",
        "audit": out / f"{rid}_audit_trail.json",
    }
    paths["report"].write_text(reconciliation_report(run, evaluation))
    paths["run_json"].write_text(json.dumps(run, indent=2, default=str))
    exceptions_csv(run, paths["exceptions"])
    paths["audit"].write_text(json.dumps(audit or [], indent=2, default=str))
    if evaluation is not None:
        paths["evaluation"] = out / f"{rid}_evaluation.json"
        paths["evaluation"].write_text(json.dumps(evaluation, indent=2, default=str))
    return paths
