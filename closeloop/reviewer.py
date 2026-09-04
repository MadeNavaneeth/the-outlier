"""Simulated reviewer.

Two purposes:

1. Lets the CLI reproduce the whole human-in-the-loop loop offline, which is
   how the run-1 -> run-3 improvement chart gets generated in one command.
2. Lets us *test* the learn-from-review machinery without a human clicking
   through 40 exceptions.

The simulator is intentionally imperfect: it agrees with a correct
classification, overrides a wrong one with the ground truth, and rejects a
slice of low-confidence items outright (so the system sees rejections too).
Every decision it makes is written to the audit trail exactly like a real
reviewer's, tagged ``human:simulated``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .ledger import ACCOUNT_BY_CODE
from .models import ApprovedRule, next_id
from .store import Store

TIMING = "timing"


class SimulatedReviewer:
    def __init__(self, store: Store, truth_path: str | Path, seed: int = 99, reject_rate: float = 0.08):
        self.store = store
        self.truth = json.loads(Path(truth_path).read_text())
        self.rng = random.Random(seed)
        self.reject_rate = reject_rate
        self.cat_by_bank = {t["bank_txn_id"]: t["expected_category"] for t in self.truth if t["bank_txn_id"] and t["expected_category"]}
        self.cat_by_entry = {t["entry_id"]: t["expected_category"] for t in self.truth if t.get("entry_id")}

    # ------------------------------------------------------------------
    def ground_truth_category(self, bank_id: str) -> str | None:
        if bank_id in self.cat_by_bank:
            return self.cat_by_bank[bank_id]
        return self.cat_by_entry.get(bank_id)

    # ------------------------------------------------------------------
    def review_run(self, run: dict[str, Any], run_id: str) -> dict[str, Any]:
        """Walk the review queue for one run and apply decisions."""
        proposals = {p["proposal_id"]: p for p in run["proposals"]}
        decisions = {"approved": 0, "edited": 0, "rejected": 0, "auto": 0, "rules_created": 0, "posted": 0}

        for exc in run["exceptions"]:
            bid = exc["bank_txn_ids"][0] if exc["bank_txn_ids"] else ""
            if not exc["needs_review"]:
                decisions["auto"] += 1
                continue
            gt = self.ground_truth_category(bid)
            prop = proposals.get(exc["proposal_id"]) if exc.get("proposal_id") else None

            # hard rejection slice
            if self.rng.random() < self.reject_rate or exc["category"] == "unknown":
                self._decide(run_id, exc, prop, "REJECTED", decisions)
                continue

            if gt and exc["category"] != gt:
                # reviewer overrides the category
                fixed = self._fix_proposal(exc, prop, gt)
                self._decide(run_id, exc, fixed, "EDITED", decisions)
                self._learn(run_id, exc, fixed, gt, decisions)
                continue

            self._decide(run_id, exc, prop, "APPROVED", decisions)
            self._learn(run_id, exc, prop, exc["category"], decisions)
        return decisions

    # ------------------------------------------------------------------
    def _fix_proposal(self, exc: dict[str, Any], prop: dict[str, Any] | None, gt: str) -> dict[str, Any] | None:
        if gt == TIMING:
            return None  # reviewer says: reconciling item, do not post
        if prop is None:
            return None
        amt = abs(float(exc["amount"]))
        if gt == "fee":
            code = "6150" if "MERCHANT" in exc["description"].upper() or "PROCESSING" in exc["description"].upper() else "6100"
        elif gt == "duplicate":
            code = "2000"
        elif gt == "fx":
            code = "6700"
        else:
            code = prop.get("account_code") if prop.get("account_code") in ACCOUNT_BY_CODE else "2300"
        if float(exc["amount"]) < 0:
            lines = [
                {"account_code": code, "account_name": ACCOUNT_BY_CODE[code].name, "debit": amt, "credit": 0.0,
                 "memo": "reviewer-corrected"},
                {"account_code": "1000", "account_name": "Operating Checking", "debit": 0.0, "credit": amt,
                 "memo": "reviewer-corrected"},
            ]
        else:
            lines = [
                {"account_code": "1000", "account_name": "Operating Checking", "debit": amt, "credit": 0.0,
                 "memo": "reviewer-corrected"},
                {"account_code": code if ACCOUNT_BY_CODE[code].type == "revenue" else "2300",
                 "account_name": ACCOUNT_BY_CODE[code if ACCOUNT_BY_CODE[code].type == "revenue" else "2300"].name,
                 "debit": 0.0, "credit": amt, "memo": "reviewer-corrected"},
            ]
        fixed = dict(prop)
        fixed["lines"] = lines
        fixed["account_code"] = code
        fixed["status"] = "EDITED"
        fixed["rationale"] = "Reviewer corrected the coding before approval."
        return fixed

    # ------------------------------------------------------------------
    def _decide(self, run_id: str, exc: dict[str, Any], prop: dict[str, Any] | None, status: str, decisions: dict) -> None:
        key = status.lower()
        decisions[key] = decisions.get(key, 0) + 1
        self.store.save_decision(run_id, exc["exception_id"], {"status": status, "proposal": prop, "by": "human:simulated"})
        self.store.audit(run_id, "human:simulated", f"review_{key}", "exception", exc["exception_id"],
                         category=exc["category"], amount=exc["amount"],
                         proposal=prop.get("proposal_id") if prop else None,
                         account_code=prop.get("account_code") if prop else None)

    # ------------------------------------------------------------------
    def _learn(self, run_id: str, exc: dict[str, Any], prop: dict[str, Any] | None, category: str, decisions: dict) -> None:
        """Store the human decision as an approved rule for next run."""
        sig = exc.get("evidence", {}).get("signature")
        if not sig:
            return
        if category in ("fraud_suspect", "unknown"):
            # never automate these; a human looks at every one, every time
            return
        existing = self.store.get_rule(sig)
        if existing is not None:
            return
        rule = ApprovedRule(
            rule_id=next_id("RULE"),
            kind="classification",
            signature=sig,
            payload={
                "category": category,
                "confidence": 0.95,
                "explanation": exc.get("explanation", "")[:300],
                "resolution": "journal_entry" if prop else "reconciling_item",
                "account_code": prop.get("account_code", "") if prop else "",
                "lines": prop.get("lines") if prop else [],
                "vendor": exc.get("description", "")[:40],
                "approved_status": "human_approved",
                #: the rule only ever fires at or below the amount a human
                #: actually looked at
                "amount_cap": abs(float(exc.get("amount", 0.0))),
            },
            created_from="human:simulated",
            exception_id=exc["exception_id"],
        )
        self.store.add_rule(rule)
        decisions["rules_created"] += 1
        self.store.audit(run_id, "human:simulated", "rule_learned", "rule", rule.rule_id,
                         signature=sig, category=category)
