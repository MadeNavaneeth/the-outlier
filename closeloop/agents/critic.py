"""Critic agent -- an independent re-check of everything the analyst proposes.

Separate agent, separate prompt, separate "opinion". It does not get to see
the analyst's chain of thought, only the claim: category, account, amount and
the drafted entry. It answers three questions:

* is the entry mechanically valid?      (debits = credits, real accounts,
                                         correct side for the account type)
* is the account plausible?             (agrees with vendor history + CoA)
* is the explanation consistent?        (does the story match the numbers)

Verdicts: PASS, FAIL (drop the proposal, send to a human), or ESCALATE
(disagreement between critic and analyst -> human decides).
"""

from __future__ import annotations

import json
from typing import Any

from ..ledger import ACCOUNT_BY_CODE
from ..llm import BaseProvider, MockProvider
from ..models import (
    DUPLICATE,
    FEE,
    FRAUD_SUSPECT,
    FX,
    MISSING_ENTRY,
    TIMING,
    UNKNOWN,
    ProposedEntry,
)
from .tools import ToolRegistry

SYSTEM_PROMPT = """You are the critic reviewer on an autonomous bank reconciliation.
Another agent classified an unmatched item and drafted a journal entry. Your job is to
try to break it. Be skeptical; a wrong posting is far worse than an unnecessary review.

Check:
  1. mechanics  -- do debits equal credits? Are all account codes real? Is the
                   debit/credit side correct for each account type?
  2. plausibility -- does the account match how this vendor/description is
                   historically coded? Is the amount direction sensible?
  3. consistency -- does the stated explanation actually support the category
                   and the entry? Flag hand-waving.

Return JSON only:
{
  "verdict": "PASS | FAIL | ESCALATE",
  "confidence": 0.0-1.0,
  "issues": ["short, specific issues"],
  "suggested_account_code": "only if you disagree with the account, else null",
  "note": "one sentence for the reviewer"
}
"""


class CriticAgent:
    def __init__(self, provider: BaseProvider, tools: ToolRegistry):
        self.provider = provider
        self.tools = tools
        if isinstance(provider, MockProvider):
            provider.register("critic_review", lambda payload: self._mock_review(payload))

    # ------------------------------------------------------------------
    def review(
        self,
        classification: dict[str, Any],
        proposal: ProposedEntry | None,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        hard = self.hard_checks(classification, proposal, item)
        if hard["verdict"] == "FAIL":
            return hard  # no point spending tokens on a mechanically broken entry

        payload = {
            "purpose": "critic_review",
            "classification": {
                k: v for k, v in classification.items() if k in {"category", "confidence", "explanation", "account_code", "resolution"}
            },
            "item": {k: v for k, v in item.items() if k in {"description", "amount", "date", "vendor", "counterparty", "bank_code", "side"}},
            "proposal": proposal.to_dict() if proposal else None,
            "vendor_history": self.tools.call("get_vendor_history", vendor=item.get("vendor") or item.get("counterparty") or "", limit=3),
            "hard_checks": hard,
        }
        out = self.provider.complete_json(
            SYSTEM_PROMPT,
            json.dumps(payload, indent=2, default=str),
            purpose="critic_review",
            fallback={"verdict": "ESCALATE", "confidence": 0.4, "issues": ["critic unavailable"], "suggested_account_code": None, "note": "Critic call failed; escalating to a human."},
        )
        merged = self._merge(hard, out)
        return merged

    # ------------------------------------------------------------------
    def hard_checks(
        self, classification: dict[str, Any], proposal: ProposedEntry | None, item: dict[str, Any]
    ) -> dict[str, Any]:
        issues: list[str] = []
        cat = classification.get("category")
        res = classification.get("resolution")
        amt = float(item.get("amount", 0.0))

        if res == "journal_entry":
            if proposal is None:
                issues.append("resolution is journal_entry but no entry was drafted")
            else:
                if not proposal.balanced:
                    issues.append(
                        f"unbalanced entry: debits {proposal.debit_total:.2f} != credits {proposal.credit_total:.2f}"
                    )
                for line in proposal.lines:
                    if line.account_code not in ACCOUNT_BY_CODE:
                        issues.append(f"account {line.account_code} is not in the chart of accounts")
                    if line.debit and line.credit:
                        issues.append(f"line on {line.account_code} has both debit and credit")
                if proposal.lines:
                    asset_acct = [l for l in proposal.lines if l.account_code == "1000"]
                    if asset_acct:
                        l = asset_acct[0]
                        if amt < 0 and l.debit > 0:
                            issues.append("money left the bank but 1000 is debited")
                        if amt > 0 and l.credit > 0:
                            issues.append("money entered the bank but 1000 is credited")
                if abs(proposal.amount - amt) > 0.02:
                    issues.append(f"entry amount {proposal.amount:.2f} does not equal bank amount {amt:.2f}")

        if cat == TIMING and res == "journal_entry":
            issues.append("timing differences must be reconciling items, not journal entries")
        if cat == FRAUD_SUSPECT and res == "journal_entry":
            issues.append("fraud suspects must never be posted automatically")
        if cat == UNKNOWN and res == "journal_entry":
            issues.append("cannot post an entry for an item classified unknown")
        if not classification.get("explanation"):
            issues.append("no explanation provided")
        elif len(classification["explanation"]) < 25:
            issues.append("explanation is too thin to audit")

        verdict = "PASS" if not issues else ("FAIL" if any("unbalanced" in i or "not in the chart" in i for i in issues) else "ESCALATE")
        return {"verdict": verdict, "issues": issues, "source": "hard_checks", "confidence": 0.95 if not issues else 0.9}

    # ------------------------------------------------------------------
    def _merge(self, hard: dict[str, Any], soft: dict[str, Any]) -> dict[str, Any]:
        verdict = str(soft.get("verdict", "ESCALATE")).upper()
        if verdict not in {"PASS", "FAIL", "ESCALATE"}:
            verdict = "ESCALATE"
        if hard["verdict"] == "FAIL":
            verdict = "FAIL"
        elif hard["verdict"] == "ESCALATE" and verdict == "PASS":
            verdict = "ESCALATE"
        issues = list(dict.fromkeys((hard.get("issues") or []) + list(soft.get("issues") or [])))
        return {
            "verdict": verdict,
            "confidence": round(min(float(soft.get("confidence", 0.5) or 0.5), 1.0), 2),
            "issues": issues[:8],
            "suggested_account_code": soft.get("suggested_account_code"),
            "note": str(soft.get("note", ""))[:300],
            "hard_check_issues": hard.get("issues", []),
        }

    # ------------------------------------------------------------------
    def _mock_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        cls = payload["classification"]
        hist = payload["vendor_history"]
        prop = payload.get("proposal")
        issues: list[str] = []
        suggested = None

        usual = hist.get("usual_account_code")
        if prop and usual and cls.get("category") in {MISSING_ENTRY, FEE, FX} and cls.get("account_code") != usual:
            if cls.get("account_code") not in {"6100", "6150", "6700", "2300", "2000", "1200"}:
                issues.append(
                    f"vendor is historically coded to {usual}; proposal uses {cls.get('account_code')}"
                )
                suggested = usual

        if cls.get("category") == DUPLICATE and prop and "2000" not in [l["account_code"] for l in prop["lines"]]:
            issues.append("duplicate reversals should credit Accounts Payable (2000)")

        if cls.get("confidence", 0) < 0.5:
            issues.append("analyst confidence is low")

        if cls.get("category") == UNKNOWN:
            return {
                "verdict": "FAIL",
                "confidence": 0.9,
                "issues": issues + ["unknown classification cannot be actioned"],
                "suggested_account_code": None,
                "note": "No defensible classification; send to the review queue.",
            }

        if issues:
            return {
                "verdict": "ESCALATE",
                "confidence": 0.7,
                "issues": issues,
                "suggested_account_code": suggested,
                "note": "Analyst and critic disagree; a human should decide.",
            }
        return {
            "verdict": "PASS",
            "confidence": 0.88,
            "issues": [],
            "suggested_account_code": None,
            "note": "Entry is mechanically valid and the account agrees with vendor history.",
        }
