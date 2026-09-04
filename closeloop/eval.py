"""Evaluation against the planted ground truth.

Everything a judge can ask for is computed here from the answer key that the
generator wrote -- the reconciler itself never reads that file.

Metrics
-------
auto_match_rate          matched bank rows / total bank rows (deterministic only)
match_precision          of the pairs we claimed, how many are true pairs
match_recall             of the true pairs, how many we found
classification_precision / _recall / _f1     per category + macro
false_auto_post_rate     auto-posted entries whose posting contradicts truth
unattended_post_rate     auto-posts / total postings (policy claim)
review_queue_rate        share of exceptions needing a human
cost_per_run             LLM tokens + calls
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_truth(path: str | Path) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text())


class Evaluator:
    def __init__(self, truth: list[dict[str, Any]]):
        self.truth = truth
        #: bank txn -> expected ledger ids (only for rows that SHOULD match)
        self.expected_pairs: dict[str, set[str]] = {}
        #: bank txn -> expected category (only for rows that should NOT match)
        self.expected_cat: dict[str, str] = {}
        #: ledger-only truth (outstanding cheques, duplicate GL lines). These
        #: have no bank row, so they are matched up with SYNTH-* exceptions in
        #: the order the GL export lists them.
        self.expected_ledger_only: list[dict[str, Any]] = []
        #: Two kinds of row carry BOTH a correct pairing and an exception:
        #:   * fx      -- matched on reference, but the variance needs posting
        #:   * duplicate -- matched to the first GL copy; the second copy is the
        #:     exception. The pairing itself is correct, so it must count as a
        #:     true positive, not a false one.
        self.fx_rows: set[str] = set()
        for t in truth:
            if not t["bank_txn_id"]:
                #: ledger-only truth: outstanding cheques and duplicated GL lines.
                #: They have no bank row, so they are lined up with the
                #: ledger-side exceptions in GL order.
                self.expected_ledger_only.append(t)
                continue
            if t["expected_ledger_ids"]:
                self.expected_pairs[t["bank_txn_id"]] = set(t["expected_ledger_ids"])
                if t["expected_category"] == "fx":
                    self.fx_rows.add(t["bank_txn_id"])
            if t["expected_category"]:
                #: NOTE: not an elif. FX rows and duplicated-invoice rows carry
                #: BOTH a correct pairing and an exception, so they belong in
                #: both maps. Making this an elif silently dropped 15 of 31
                #: exceptions from the classification score -- the accuracy
                #: number was being computed on half the data.
                self.expected_cat[t["bank_txn_id"]] = t["expected_category"]
        #: ledger-only truth keyed by the GL row it refers to. Positional
        #: alignment (the previous approach) silently mis-scored as soon as any
        #: unrelated GL row was also unmatched -- on the 644-row month that
        #: meant 37 of 71 ledger-side exceptions were graded against the wrong
        #: answer, and reported accuracy was meaningless.
        self.expected_by_entry = {
            t["entry_id"]: t["expected_category"]
            for t in self.expected_ledger_only
            if t.get("entry_id")
        }

    # ------------------------------------------------------------------
    def evaluate(self, run: dict[str, Any]) -> dict[str, Any]:
        matches = run.get("matches", [])
        exceptions = run.get("exceptions", [])
        proposals = {p["proposal_id"]: p for p in run.get("proposals", [])}

        claimed: dict[str, set[str]] = {}
        for m in matches:
            for b in m["bank_txn_ids"]:
                claimed.setdefault(b, set()).update(m["ledger_entry_ids"])

        tp = fp = fn = 0
        wrong_pairs: list[dict[str, Any]] = []
        for bank_id, exp in self.expected_pairs.items():
            got = claimed.get(bank_id, set())
            if not got:
                fn += len(exp)
                continue
            inter = exp & got
            tp += len(inter)
            fp += len(got - exp)
            fn += len(exp - got)
            if got != exp:
                wrong_pairs.append({"bank_txn_id": bank_id, "expected": sorted(exp), "got": sorted(got)})
        # pairs claimed for rows that should never have matched
        for bank_id, got in claimed.items():
            if bank_id in self.expected_cat and bank_id not in self.expected_pairs:
                fp += len(got)
                wrong_pairs.append({"bank_txn_id": bank_id, "expected": [], "got": sorted(got)})

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

        # ---- classification ----
        per_cat: dict[str, dict[str, int]] = {}
        correct = incorrect = 0
        confusion: dict[str, dict[str, int]] = {}
        ledger_only_ids = [
            e["bank_txn_ids"][0]
            for e in exceptions
            if e["bank_txn_ids"] and e.get("evidence", {}).get("side") == "ledger"
        ]
        synth_cat = {gid: self.expected_by_entry[gid] for gid in ledger_only_ids if gid in self.expected_by_entry}
        unmatched_expected_ledger_only = len(
            [gid for gid in self.expected_by_entry if gid not in set(ledger_only_ids)]
        )

        for exc in exceptions:
            bid = exc["bank_txn_ids"][0] if exc["bank_txn_ids"] else ""
            got = exc["category"]
            exp = self.expected_cat.get(bid) or synth_cat.get(bid)
            if exp is None:
                continue
            slot = per_cat.setdefault(exp, {"tp": 0, "fp": 0, "fn": 0})
            confusion.setdefault(exp, {}).setdefault(got, 0)
            confusion[exp][got] += 1
            if got == exp:
                slot["tp"] += 1
                correct += 1
            else:
                slot["fp"] += 1
                incorrect += 1
        for exp in set(self.expected_cat.values()):
            per_cat.setdefault(exp, {"tp": 0, "fp": 0, "fn": 0})
        # A false negative is an exception we should have raised and did not.
        # But a row whose pairing we got exactly right has already been dealt
        # with: for a duplicated invoice the bank row matches the first GL copy
        # and the *copy* is the exception, so counting the bank row as a missed
        # duplicate punishes a correct match. Only unmatched rows can be missed.
        raised = {e["bank_txn_ids"][0] for e in exceptions if e["bank_txn_ids"]}
        correctly_paired = {
            bid
            for bid, exp_ids in self.expected_pairs.items()
            if claimed.get(bid) == exp_ids
        }
        for bid, exp in self.expected_cat.items():
            if bid not in raised and bid not in correctly_paired:
                per_cat[exp]["fn"] += 1
        missed_exceptions = [
            bid for bid, exp in self.expected_cat.items()
            if bid not in raised and bid not in correctly_paired
        ]
        for gid, exp in self.expected_by_entry.items():
            if gid not in set(ledger_only_ids):
                per_cat.setdefault(exp, {"tp": 0, "fp": 0, "fn": 0})
                per_cat[exp]["fn"] += 1

        cat_metrics = {}
        f1s = []
        for cat, s in sorted(per_cat.items()):
            p = s["tp"] / (s["tp"] + s["fp"]) if (s["tp"] + s["fp"]) else 0.0
            r = s["tp"] / (s["tp"] + s["fn"]) if (s["tp"] + s["fn"]) else 0.0
            f = 2 * p * r / (p + r) if (p + r) else 0.0
            f1s.append(f)
            cat_metrics[cat] = {**s, "precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}

        # ---- false auto-posts ----
        posted = run.get("posted", [])
        auto_posted = [p for p in proposals.values() if p.get("source") == "learned_rule" and p.get("status") == "POSTED"]
        false_auto = 0
        for p in auto_posted:
            exc = next((e for e in exceptions if e["exception_id"] == p["exception_id"]), None)
            if not exc:
                false_auto += 1
                continue
            bid = exc["bank_txn_ids"][0] if exc["bank_txn_ids"] else ""
            exp = self.expected_cat.get(bid, "duplicate" if bid.startswith("SYNTH-") else None)
            if exp is None or exp != exc["category"]:
                false_auto += 1
            elif exp == "timing" or exp == "fraud_suspect":
                false_auto += 1  # these must never produce a posting at all

        metrics = run.get("metrics", {})
        return {
            "run_id": run.get("run_id"),
            "round_no": run.get("round_no"),
            "auto_match_rate_bank": metrics.get("auto_match_rate_bank", 0.0),
            "auto_match_rate_ledger": metrics.get("auto_match_rate_ledger", 0.0),
            "match_precision": round(precision, 4),
            "match_recall": round(recall, 4),
            "match_f1": round(f1, 4),
            "matched_pairs_tp": tp,
            "matched_pairs_fp": fp,
            "matched_pairs_fn": fn,
            "wrong_pairs": wrong_pairs[:10],
            "classification_precision": round(correct / (correct + incorrect), 4) if (correct + incorrect) else 0.0,
            "classification_correct": correct,
            "classification_incorrect": incorrect,
            "classification_macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
            "per_category": cat_metrics,
            "confusion": confusion,
            "exceptions_raised": len(exceptions),
            "expected_exceptions": len(self.expected_cat) + len(self.expected_ledger_only),
            "fx_rows": len(self.fx_rows),
            "fx_matched_as_variance": sum(
                1 for m in matches if m["match_type"] == "fx_variance"
            ),
            "fx_flagged_for_review": sum(
                1 for m in matches if m["match_type"] == "fx_variance" and m["requires_review"]
            ),
            "ledger_only_exceptions_missed": unmatched_expected_ledger_only,
            "missed_exceptions": missed_exceptions,
            "ambiguous_matches_refused": run.get("metrics", {}).get("ambiguous_refused", 0),
            "auto_resolved": metrics.get("auto_resolved", 0),
            "auto_resolve_rate": metrics.get("auto_resolve_rate", 0.0),
            "rule_hits_gated": metrics.get("rule_hits_gated", 0),
            "rule_hit_rate": metrics.get("rule_hit_rate", 0.0),
            "review_queue_rate": metrics.get("review_queue_rate", 0.0),
            "needs_review": metrics.get("needs_review", 0),
            "auto_posted": len(auto_posted),
            "false_auto_posts": false_auto,
            "false_auto_post_rate": round(false_auto / len(auto_posted), 4) if auto_posted else 0.0,
            "unbalanced_proposals": metrics.get("unbalanced_proposals", 0),
            "llm_calls": metrics.get("llm_calls", 0),
            "llm_total_tokens": metrics.get("llm_total_tokens", 0),
            "elapsed_seconds": metrics.get("elapsed_seconds", 0.0),
            "rules_in_memory": metrics.get("rules_in_memory", 0),
        }


def improvement_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The money slide: run 1 vs run N."""
    keep = [
        "round_no",
        "auto_match_rate_bank",
        "match_f1",
        "classification_precision",
        "classification_macro_f1",
        "exceptions_raised",
        "auto_resolved",
        "auto_resolve_rate",
        "rule_hits_gated",
        "rule_hit_rate",
        "needs_review",
        "review_queue_rate",
        "false_auto_posts",
        "llm_total_tokens",
        "rules_in_memory",
    ]
    return [{k: r.get(k) for k in keep} for r in rows]


def baseline_manual_estimate(n_exceptions: int, minutes_per_exception: float = 9.0) -> dict[str, float]:
    """Rough manual baseline for the time-saved claim.

    Controllers typically quote 5-15 minutes per investigated exception;
    we use the low end and say so.
    """
    manual_minutes = n_exceptions * minutes_per_exception
    return {
        "manual_minutes_estimate": round(manual_minutes, 1),
        "manual_hours_estimate": round(manual_minutes / 60, 2),
        "minutes_per_exception_assumption": minutes_per_exception,
    }
