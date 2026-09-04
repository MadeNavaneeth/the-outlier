"""Tool definitions handed to the agents.

Tools are plain Python functions with a JSON-schema-ish signature so the same
objects can be (a) called directly by the orchestrator, (b) serialised into an
AO / OpenAI tool spec, and (c) unit-tested. Every tool is read-only -- agents
get no way to touch the ledger directly.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from ..ledger import ACCOUNT_BY_CODE, CHART_OF_ACCOUNTS
from ..matcher import normalize_ref
from ..models import BankTxn, LedgerEntry


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(self, name: str, description: str, params: dict[str, str], fn: Callable[..., Any]) -> None:
        self._tools[name] = {"name": name, "description": description, "params": params, "fn": fn}

    def call(self, name: str, **kwargs: Any) -> Any:
        if name not in self._tools:
            raise KeyError(f"unknown tool {name!r}")
        return self._tools[name]["fn"](**kwargs)

    def spec(self) -> list[dict[str, Any]]:
        """OpenAI / AO-compatible tool schema."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {k: {"type": "string", "description": k} for k in t["params"]},
                    },
                },
            }
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools)


def build_tools(
    bank: Sequence[BankTxn],
    ledger: Sequence[LedgerEntry],
    rules_lookup: Callable[..., dict[str, Any]],
    coa: Sequence[Any] = CHART_OF_ACCOUNTS,
) -> ToolRegistry:
    reg = ToolRegistry()
    bank_by_id = {t.txn_id: t for t in bank}
    ledger_by_id = {e.entry_id: e for e in ledger}

    def search_ledger(query: str = "", limit: int = 10, amount: float | None = None, tol: float = 1.0) -> list[dict]:
        """Search GL entries by vendor/reference text and/or amount."""
        q = normalize_ref(query)
        out = []
        for e in ledger:
            hay = normalize_ref(f"{e.vendor} {e.description} {e.reference}")
            text_hit = bool(q) and q in hay
            amt_hit = amount is not None and abs(e.amount - float(amount)) <= tol
            if text_hit or amt_hit:
                out.append(
                    {
                        "entry_id": e.entry_id,
                        "date": e.date.isoformat(),
                        "amount": e.amount,
                        "account_code": e.account_code,
                        "account_name": e.account_name,
                        "vendor": e.vendor,
                        "reference": e.reference,
                        "description": e.description,
                        "status": e.status,
                    }
                )
            if len(out) >= int(limit):
                break
        return out

    def search_bank(query: str = "", limit: int = 10) -> list[dict]:
        q = normalize_ref(query)
        out = []
        for t in bank:
            hay = normalize_ref(f"{t.counterparty} {t.description} {t.reference}")
            if not q or q in hay:
                out.append(
                    {
                        "txn_id": t.txn_id,
                        "date": t.date.isoformat(),
                        "amount": t.amount,
                        "description": t.description,
                        "counterparty": t.counterparty,
                        "reference": t.reference,
                    }
                )
            if len(out) >= int(limit):
                break
        return out

    def fuzzy_match(txn_id: str, tol_pct: float = 0.03) -> list[dict]:
        """Nearest ledger candidates for a bank row the deterministic passes missed."""
        t = bank_by_id.get(txn_id)
        if t is None:
            return []
        tol = max(0.02, abs(t.amount) * float(tol_pct))
        cands = []
        for e in ledger:
            if abs(e.amount - t.amount) <= tol:
                score = 1.0 - abs(e.amount - t.amount) / tol
                if normalize_ref(t.reference) and normalize_ref(t.reference) in normalize_ref(e.description + e.reference):
                    score += 0.5
                if t.counterparty and t.counterparty.lower() in e.vendor.lower():
                    score += 0.4
                score -= min(0.3, abs((t.date - e.date).days) * 0.02)
                cands.append(
                    {
                        "entry_id": e.entry_id,
                        "amount": e.amount,
                        "date": e.date.isoformat(),
                        "vendor": e.vendor,
                        "account_code": e.account_code,
                        "score": round(score, 3),
                    }
                )
        cands.sort(key=lambda c: -c["score"])
        return cands[:5]

    def find_duplicates(
        entry_id: str = "", amount: float | None = None, reference: str = "", window_days: int = 5
    ) -> list[dict]:
        """Every GL entry that looks like a double-booking of this one.

        Searches the WHOLE ledger including already-matched lines, because a
        duplicate is by definition a second copy of something that did match.
        """
        base = ledger_by_id.get(entry_id)
        ref = normalize_ref(reference or (base.reference if base else ""))
        amt = round(float(amount if amount is not None else (base.amount if base else 0.0)), 2)
        out = []
        for e in ledger:
            if base is not None and e.entry_id == base.entry_id:
                continue
            if abs(e.amount - amt) > 0.02:
                continue
            same_ref = bool(ref) and (ref in normalize_ref(e.reference) or ref in normalize_ref(e.description))
            same_vendor = bool(base) and bool(base.vendor) and e.vendor == base.vendor
            close_date = base is not None and abs((e.date - base.date).days) <= int(window_days)
            if same_ref or (same_vendor and close_date):
                out.append(
                    {
                        "entry_id": e.entry_id,
                        "date": e.date.isoformat(),
                        "amount": e.amount,
                        "account_code": e.account_code,
                        "vendor": e.vendor,
                        "reference": e.reference,
                        "matched_to_bank": e.status == "MATCHED",
                        "same_reference": bool(same_ref),
                    }
                )
        return out

    def get_vendor_history(vendor: str = "", limit: int = 5) -> dict:
        v = (vendor or "").lower()
        entries = [e for e in ledger if v and v.split()[0] in (e.vendor or "").lower()]
        codes: dict[str, int] = {}
        for e in entries:
            codes[e.account_code] = codes.get(e.account_code, 0) + 1
        top = sorted(codes.items(), key=lambda kv: -kv[1])
        return {
            "vendor": vendor,
            "n_entries": len(entries),
            "account_code_distribution": dict(top),
            "usual_account_code": top[0][0] if top else None,
            "usual_account_name": ACCOUNT_BY_CODE[top[0][0]].name if top and top[0][0] in ACCOUNT_BY_CODE else None,
            "recent": [
                {"date": e.date.isoformat(), "amount": e.amount, "account_code": e.account_code}
                for e in entries[-int(limit):]
            ],
        }

    def get_chart_of_accounts() -> list[dict]:
        return [{"code": a.code, "name": a.name, "type": a.type, "normal_side": a.normal_side} for a in coa]

    def lookup_prior_decisions(signature: str = "", vendor: str = "", category: str = "") -> dict:
        """Return any human-approved rule that exactly matches this signature."""
        return rules_lookup(signature, vendor, category or None)

    def get_ledger_entry(entry_id: str = "") -> dict:
        e = ledger_by_id.get(entry_id)
        return e.to_dict() if e else {}

    def get_bank_txn(txn_id: str = "") -> dict:
        t = bank_by_id.get(txn_id)
        return t.to_dict() if t else {}

    reg.register("search_ledger", "Search GL entries by vendor/reference text and/or amount.",
                 {"query": "free text", "limit": "max rows", "amount": "optional amount filter"}, search_ledger)
    reg.register("search_bank", "Search bank transactions by free text.",
                 {"query": "free text", "limit": "max rows"}, search_bank)
    reg.register("fuzzy_match", "Return the best ledger candidates for an unmatched bank transaction.",
                 {"txn_id": "bank txn id", "tol_pct": "amount tolerance as a fraction"}, fuzzy_match)
    reg.register("find_duplicates",
                 "Find GL entries that look like a double-booking of this one, including already-matched lines.",
                 {"entry_id": "GL id", "amount": "amount", "reference": "reference", "window_days": "date window"},
                 find_duplicates)
    reg.register("get_vendor_history", "Historical GL coding for a vendor: usual account code + recent entries.",
                 {"vendor": "vendor name", "limit": "recent rows"}, get_vendor_history)
    reg.register("get_chart_of_accounts", "The company chart of accounts. Only these codes may be used.",
                 {}, get_chart_of_accounts)
    reg.register("lookup_prior_decisions", "Look up a human-approved rule for an exception signature.",
                 {"signature": "exception signature", "vendor": "vendor", "category": "predicted category"},
                 lookup_prior_decisions)
    reg.register("get_ledger_entry", "Fetch one GL entry by id.", {"entry_id": "GL id"}, get_ledger_entry)
    reg.register("get_bank_txn", "Fetch one bank transaction by id.", {"txn_id": "bank txn id"}, get_bank_txn)
    return reg


def infer_expense_account(description: str, bank_code: str = "") -> tuple[str, str, float]:
    """Cheap deterministic account inference used as a *prior* for the agent.

    Returns ``(account_code, reason, confidence)``. The critic agent still has
    to agree before anything reaches a human.
    """
    d = (description or "").upper()
    code = (bank_code or "").upper()
    table = [
        (("FEE", "CHARGE", "SERVICE CHARGE", "WIRE", "ACH RETURN"), "6100", 0.72),
        (("MERCHANT FEE", "PROCESSING", "STRIPE FEE", "PAYPAL FEE"), "6150", 0.75),
        (("INTEREST",), "4200", 0.6),
        (("AWS", "AMAZON WEB", "GOOGLE CLOUD", "SNOWFLAKE", "HOSTING"), "6050", 0.68),
        (("ADS", "ADVERTIS", "LINKEDIN", "GOOGLE ADS", "META"), "6200", 0.65),
        (("RENT", "WEWORK", "UTILIT", "ELECTRIC"), "6400", 0.65),
        (("PAYROLL", "GUSTO", "SALARY"), "6600", 0.7),
        (("AIR", "DELTA", "UNITED", "MARRIOTT", "UBER", "HOTEL"), "6500", 0.65),
    ]
    for keys, acct, conf in table:
        if any(k in d for k in keys):
            return acct, f"description matched keyword set {keys[0]!r}", conf
    if code == "FEE":
        return "6100", "bank transaction code FEE", 0.7
    if code == "INT":
        return "4200", "bank transaction code INT (interest)", 0.6
    return "2300", "no confident account mapping; routed to Suspense / Needs Review", 0.3


def signature_for(txn: BankTxn) -> str:
    """Stable signature for the learn-from-review loop.

    Built from the *pattern*, not the amount: a normalised description, the
    bank transaction code and the counterparty. Keying on the amount -- the
    obvious move -- makes rules useless, because a $45 bank fee and a $38 bank
    fee would never share a rule and nothing would ever generalise.

    The category is deliberately NOT part of the signature. If the model
    predicts a different category this month than the human approved last
    month, that disagreement is exactly when a human should look again, so the
    orchestrator compares them separately instead of hiding it in the key.

    Safety comes from the guardrails: a rule only fires below the cap a human
    actually reviewed, and never for fraud suspects.
    """
    desc = re.sub(r"[0-9]+", "#", (txn.description or "").upper()).strip()
    return "|".join(
        [
            normalize_ref(desc)[:28],
            normalize_ref(txn.bank_code),
            normalize_ref(txn.counterparty)[:16],
        ]
    )
