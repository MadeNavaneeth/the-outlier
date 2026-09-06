"""Exception analyst agent.

Called ONLY for residuals the deterministic matcher could not resolve. For
each unmatched item it:

1. gathers evidence with tools (vendor history, chart of accounts, prior
   human decisions, fuzzy candidates),
2. asks the model for a category + confidence + plain-English explanation,
3. drafts a journal entry (or a reconciling item) using a tool-derived prior
   for the account,
4. returns everything in a fixed JSON schema the critic agent can audit.

The prompt is deliberately narrow: the model may only choose from the
enumerated categories and only use account codes present in the chart of
accounts. Anything outside that is coerced back into range by the code, not
by hoping the model behaves.
"""

from __future__ import annotations

import json
from typing import Any

from ..ledger import ACCOUNT_BY_CODE
from ..llm import BaseProvider, MockProvider
from ..models import (
    ALL_CATEGORIES,
    DUPLICATE,
    FEE,
    FRAUD_SUSPECT,
    FX,
    MISSING_ENTRY,
    TIMING,
    UNKNOWN,
    BankTxn,
    JournalLine,
    LedgerEntry,
    ProposedEntry,
    money,
)
from ..matcher import normalize_ref
from .tools import ToolRegistry, infer_expense_account, signature_for

SYSTEM_PROMPT = """You are the exception analyst on an autonomous bank reconciliation.
A deterministic matcher already resolved every clean, unambiguous pairing. You are
looking ONLY at residuals, so assume the easy cases are gone.

Decide, for one unmatched bank or ledger item:
  1. category      -- exactly one of: {categories}
  2. confidence    -- 0.0 to 1.0, calibrated. If the evidence is thin, say so with a LOW number.
  3. explanation   -- one or two sentences a controller can read in five seconds.
  4. account_code  -- from the chart of accounts ONLY. Use the suspense account when unsure.
  5. resolution    -- "journal_entry" (needs posting), "reconciling_item" (report only, no posting) or "investigate" (no action, flag for fraud/audit).
  6. memo          -- short journal memo.

Hard rules:
- Do not invent account codes.
- Do not propose a journal entry for a timing difference; timing differences are reconciling items.
- If the item looks like an unexplained third-party debit, category is fraud_suspect and resolution is investigate.
- Reply with JSON only, matching the schema in the user message.
""".format(categories=", ".join(ALL_CATEGORIES))

OUTPUT_SCHEMA = {
    "category": "one of: " + " | ".join(ALL_CATEGORIES),
    "confidence": "float 0..1",
    "explanation": "string, plain English, <= 2 sentences",
    "account_code": "string, from chart of accounts",
    "resolution": "journal_entry | reconciling_item | investigate",
    "memo": "string",
}


class ExceptionAnalyst:
    def __init__(self, provider: BaseProvider, tools: ToolRegistry):
        self.provider = provider
        self.tools = tools
        if isinstance(provider, MockProvider):
            provider.register("classify_exception", lambda payload: self._mock_classify(payload))

    # ------------------------------------------------------------------
    def build_context(self, item: dict[str, Any]) -> dict[str, Any]:
        """Tool calls happen here, in code, so they are auditable and testable."""
        vendor = item.get("vendor") or item.get("counterparty") or ""
        desc = item.get("description", "")
        sig = item.get("signature", "")
        duplicates = self.tools.call(
            "find_duplicates",
            entry_id=item.get("entry_id", ""),
            amount=item.get("amount", 0.0),
            reference=item.get("reference", ""),
        )
        return {
            "purpose": "classify_exception",
            "item": item,
            "signature": sig,
            "duplicate_candidates": duplicates,
            "fx_candidate": self._fx_candidate(item, duplicates),
            "vendor_history": self.tools.call("get_vendor_history", vendor=vendor, limit=5),
            "chart_of_accounts": self.tools.call("get_chart_of_accounts"),
            "prior_decisions": self.tools.call("lookup_prior_decisions", signature=sig, vendor=vendor),
            "fuzzy_candidates": self.tools.call("fuzzy_match", txn_id=item.get("txn_id", ""), tol_pct=0.03)
            if item.get("txn_id")
            else [],
            "similar_ledger": self.tools.call("search_ledger", query=vendor or desc[:20], limit=5),
            "account_prior": infer_expense_account(desc, item.get("bank_code", "")),
        }

    # ------------------------------------------------------------------
    def analyze(
        self,
        txn: BankTxn | None,
        ledger_entry: LedgerEntry | None,
        exception_id: str,
        proposal_id: str,
    ) -> tuple[dict[str, Any], ProposedEntry | None]:
        item = self._item_from(txn, ledger_entry)
        ctx = self.build_context(item)
        fallback = {
            "category": UNKNOWN,
            "confidence": 0.2,
            "explanation": "No confident classification; routed to the review queue.",
            "account_code": "2300",
            "resolution": "investigate",
            "memo": "Unreconciled item - needs review",
        }
        out = self.provider.complete_json(
            SYSTEM_PROMPT,
            json.dumps(ctx, indent=2, default=str),
            purpose="classify_exception",
            fallback=fallback,
        )
        out = self._coerce(out, ctx)
        proposal = self._draft(out, item, exception_id, proposal_id)
        return {**out, "context": ctx, "item": item}, proposal

    # ------------------------------------------------------------------
    def _item_from(self, txn: BankTxn | None, ledger_entry: LedgerEntry | None) -> dict[str, Any]:
        if txn is not None:
            return {
                "side": "bank",
                "txn_id": txn.txn_id,
                "date": txn.date.isoformat(),
                "amount": txn.amount,
                "description": txn.description,
                "reference": txn.reference,
                "counterparty": txn.counterparty,
                "vendor": txn.counterparty,
                "bank_code": txn.bank_code,
                "signature": signature_for(txn),
            }
        assert ledger_entry is not None
        pseudo = BankTxn(
            txn_id=f"SYNTH-{ledger_entry.entry_id}",
            date=ledger_entry.date,
            amount=ledger_entry.amount,
            description=ledger_entry.description,
            reference=ledger_entry.reference,
            counterparty=ledger_entry.vendor,
            bank_code="LEDGER_ONLY",
        )
        return {
            "side": "ledger",
            "entry_id": ledger_entry.entry_id,
            "txn_id": "",
            "date": ledger_entry.date.isoformat(),
            "amount": ledger_entry.amount,
            "description": ledger_entry.description,
            "reference": ledger_entry.reference,
            "counterparty": ledger_entry.vendor,
            "vendor": ledger_entry.vendor,
            "bank_code": "LEDGER_ONLY",
            "account_code": ledger_entry.account_code,
            "signature": signature_for(pseudo),
        }

    # ------------------------------------------------------------------
    def _coerce(self, out: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        """Never trust the model. Clamp to the allowed value space."""
        cat = str(out.get("category", UNKNOWN)).lower()
        if cat not in ALL_CATEGORIES:
            cat = UNKNOWN
        try:
            conf = max(0.0, min(1.0, float(out.get("confidence", 0.3))))
        except (TypeError, ValueError):
            conf = 0.3
        code = str(out.get("account_code", "2300")).strip()
        if code not in ACCOUNT_BY_CODE:
            code = ctx["account_prior"][0]
        res = str(out.get("resolution", "")).lower()
        if res not in {"journal_entry", "reconciling_item", "investigate"}:
            res = "investigate"
        if cat == TIMING and res == "journal_entry":
            res = "reconciling_item"  # hard rule from the prompt, enforced in code too
        if cat == FRAUD_SUSPECT:
            res = "investigate"
        return {
            "category": cat,
            "confidence": round(conf, 2),
            "explanation": str(out.get("explanation", ""))[:600],
            "account_code": code,
            "resolution": res,
            "memo": str(out.get("memo", ""))[:160] or "Reconciliation adjustment",
            "degraded": bool(out.get("degraded", False)),
        }

    # ------------------------------------------------------------------
    def _draft(
        self, out: dict[str, Any], item: dict[str, Any], exception_id: str, proposal_id: str
    ) -> ProposedEntry | None:
        if out["resolution"] != "journal_entry":
            return None
        amt = money(item["amount"])
        code = out["account_code"]
        acct = ACCOUNT_BY_CODE[code]
        memo = out["memo"]
        if amt < 0:  # money left the bank and was never booked
            lines = [
                JournalLine(code, acct.name, debit=abs(amt), credit=0.0, memo=memo),
                JournalLine("1000", "Operating Checking", debit=0.0, credit=abs(amt), memo=memo),
            ]
        else:  # money arrived and was never booked
            if acct.type == "revenue":
                lines = [
                    JournalLine("1000", "Operating Checking", debit=amt, credit=0.0, memo=memo),
                    JournalLine(code, acct.name, debit=0.0, credit=amt, memo=memo),
                ]
            else:
                lines = [
                    JournalLine("1000", "Operating Checking", debit=amt, credit=0.0, memo=memo),
                    JournalLine("2300", "Suspense / Needs Review", debit=0.0, credit=amt, memo=memo),
                ]
        rationale = (
            f"{out['category']}: {out['explanation']} "
            f"Account {code} {acct.name} chosen from {out.get('_account_reason', 'vendor history / description prior')}."
        )
        return ProposedEntry(
            proposal_id=proposal_id,
            exception_id=exception_id,
            lines=lines,
            rationale=rationale[:800],
            amount=amt,
            account_code=code,
            status="DRAFT",
            source="agent",
        )

    # ------------------------------------------------------------------
    def _fx_candidate(self, item: dict[str, Any], duplicates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """A GL entry that carries the SAME reference but a different amount.

        That is the only evidence strong enough to call something an FX
        variance; "a nearby amount" is not. Entries that ``find_duplicates``
        already identified as double-bookings are excluded, otherwise a
        duplicated invoice looks like an FX variance -- which is exactly the
        misclassification this guard was added for.
        """
        ref = normalize_ref(item.get("reference") or "")
        if not ref:
            return None
        dup_ids = {d["entry_id"] for d in duplicates}
        amount = float(item.get("amount", 0.0))
        best = None
        for c in self.tools.call("search_ledger", query=ref, limit=20, amount=amount, tol=max(1.0, abs(amount) * 0.05)):
            if c["entry_id"] in dup_ids:
                continue
            if c.get("status") == "MATCHED":
                # already consumed by a deterministic match: it cannot also be
                # the other side of an FX variance
                continue
            if abs(float(c["amount"]) - amount) < 0.02:
                continue
            if best is None or abs(float(c["amount"]) - amount) < abs(float(best["amount"]) - amount):
                best = c
        return best

    # ------------------------------------------------------------------
    # offline stand-in for the model (transparent scoring, not canned output)
    # ------------------------------------------------------------------
    def _mock_classify(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = payload["item"]
        desc = (item.get("description") or "").upper()
        code = (item.get("bank_code") or "").upper()
        amt = float(item.get("amount", 0.0))
        side = item.get("side", "bank")
        prior_code, prior_reason, prior_conf = payload["account_prior"]
        history = payload["vendor_history"]
        cands = payload.get("fuzzy_candidates") or []

        dup_hits = [d for d in (payload.get("duplicate_candidates") or []) if d.get("same_reference")]

        if code == "AMBIGUOUS_MULTI":
            return {
                "category": UNKNOWN,
                "confidence": 0.25,
                "explanation": "More than one exact combination of GL lines ties out to this bank amount, so the "
                "matcher refused to guess. A human has to pick the right set.",
                "account_code": "2300",
                "resolution": "investigate",
                "memo": "Ambiguous multi-line match",
            }

        if side == "ledger" and dup_hits:
            return {
                "category": DUPLICATE,
                "confidence": 0.91,
                "explanation": f"Ledger entry {item.get('entry_id')} duplicates {dup_hits[0]['entry_id']} "
                f"(same reference {item.get('reference') or 'n/a'}, same amount). The bank only paid once.",
                "account_code": "2000",
                "resolution": "journal_entry",
                "memo": f"Reverse duplicate booking {item.get('reference') or ''}".strip(),
            }

        if code == "LEDGER_ONLY":
            if dup_hits:
                return {
                    "category": DUPLICATE,
                    "confidence": 0.91,
                    "explanation": f"Ledger entry {item.get('entry_id')} duplicates {dup_hits[0]['entry_id']} "
                    f"(same reference {item.get('reference') or 'n/a'}, same amount). The bank only paid once.",
                    "account_code": "2000",
                    "resolution": "journal_entry",
                    "memo": f"Reverse duplicate booking {item.get('reference') or ''}".strip(),
                }
            if amt < 0:
                return {
                    "category": TIMING,
                    "confidence": 0.8,
                    "explanation": "Payment is booked in the GL but has not cleared the bank. "
                    "Treat as an outstanding cheque on the reconciliation report; no entry needed.",
                    "account_code": item.get("account_code", "2000"),
                    "resolution": "reconciling_item",
                    "memo": "Outstanding cheque",
                }
            return {
                "category": UNKNOWN,
                "confidence": 0.3,
                "explanation": "Ledger-only item with no bank activity and no clear cause.",
                "account_code": "2300",
                "resolution": "investigate",
                "memo": "Ledger-only item - investigate",
            }

        fx_c = payload.get("fx_candidate")
        if fx_c:
            drift = money(amt - fx_c["amount"])
            return {
                "category": FX,
                "confidence": 0.83,
                "explanation": f"GL entry {fx_c['entry_id']} carries the same reference and is booked at "
                f"{fx_c['amount']:.2f}, but the bank settled {amt:.2f}. The {drift:+.2f} difference is an FX / "
                "conversion variance and should be posted to 6700 so the bank clears.",
                "account_code": "6700",
                "resolution": "journal_entry",
                "memo": f"FX variance on {item.get('reference') or item.get('counterparty')}".strip(),
            }

        if code == "FEE" or any(k in desc for k in ("FEE", "CHARGE", "MARKUP")):
            return {
                "category": FEE,
                "confidence": 0.86,
                "explanation": f"Bank-initiated charge ({item.get('description')}) that never hits the GL "
                "until month-end. Book it to the fee account.",
                "account_code": "6150" if "MERCHANT" in desc or "PROCESSING" in desc else "6100",
                "resolution": "journal_entry",
                "memo": item.get("description", "Bank charge")[:80],
            }

        if code == "DEP" or "DEPOSIT" in desc:
            return {
                "category": TIMING,
                "confidence": 0.82,
                "explanation": "Cash received at the bank near period end that is not yet recorded in the GL. "
                "Deposit in transit; it clears next period.",
                "account_code": "1200",
                "resolution": "reconciling_item",
                "memo": "Deposit in transit",
            }

        if code == "POS" or "UNKNOWNMERCHANT" in desc or "CRYPTO" in desc or "ATM WITHDRAWAL" in desc:
            return {
                "category": FRAUD_SUSPECT,
                "confidence": 0.79,
                "explanation": f"Unexplained third-party debit with no vendor, no reference and no GL activity. "
                "Escalate to the card team before anything is posted.",
                "account_code": "2300",
                "resolution": "investigate",
                "memo": "Suspect transaction - escalate",
            }

        if code == "INT" or "INTEREST" in desc:
            return {
                "category": MISSING_ENTRY,
                "confidence": 0.84,
                "explanation": "Bank-credited interest income that was never booked.",
                "account_code": "4200",
                "resolution": "journal_entry",
                "memo": "Bank interest income",
            }

        if history.get("usual_account_code") and prior_conf < 0.5:
            return {
                "category": MISSING_ENTRY,
                "confidence": 0.68,
                "explanation": f"Vendor {item.get('vendor')} is historically coded to "
                f"{history['usual_account_code']} {history.get('usual_account_name') or ''}; "
                "this payment cleared the bank but was never booked.",
                "account_code": history["usual_account_code"],
                "resolution": "journal_entry",
                "memo": f"{item.get('vendor', 'Vendor')} payment not booked"[:80],
            }

        return {
            "category": MISSING_ENTRY if prior_conf >= 0.5 else UNKNOWN,
            "confidence": round(prior_conf, 2),
            "explanation": f"{prior_reason.capitalize()}. Bank item {item.get('txn_id')} has no corresponding "
            "GL entry, so it is proposed as a missing entry.",
            "account_code": prior_code,
            "resolution": "journal_entry" if prior_conf >= 0.5 else "investigate",
            "memo": item.get("description", "Unbooked bank item")[:80],
        }
