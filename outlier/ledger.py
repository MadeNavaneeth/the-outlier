"""Mock chart of accounts + mock general ledger.

Deliberately NOT a real QuickBooks/Xero integration. A mock ledger API is
enough to prove the workflow end to end and it saves ~6 hours of OAuth pain
(this is stated in the project brief's own "pitfalls to avoid").
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .models import JournalLine, ProposedEntry, money


@dataclass
class Account:
    code: str
    name: str
    type: str  # asset | liability | equity | revenue | expense
    normal_side: str  # debit | credit

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Standard SMB chart of accounts. The exception agent can only propose
#: accounts that exist here (validated by the critic agent).
CHART_OF_ACCOUNTS: list[Account] = [
    Account("1000", "Operating Checking", "asset", "debit"),
    Account("1010", "PayPal / Processor Settlement", "asset", "debit"),
    Account("1100", "Accounts Receivable", "asset", "debit"),
    Account("1200", "Undeposited Funds", "asset", "debit"),
    Account("2000", "Accounts Payable", "liability", "credit"),
    Account("2100", "Accrued Expenses", "liability", "credit"),
    Account("2200", "Sales Tax Payable", "liability", "credit"),
    Account("2300", "Suspense / Needs Review", "liability", "credit"),
    Account("4000", "Product Revenue", "revenue", "credit"),
    Account("4100", "Services Revenue", "revenue", "credit"),
    Account("4200", "Other Income", "revenue", "credit"),
    Account("5000", "Cost of Goods Sold", "expense", "debit"),
    Account("6000", "Software & Subscriptions", "expense", "debit"),
    Account("6050", "Cloud & Hosting", "expense", "debit"),
    Account("6100", "Bank Charges & Fees", "expense", "debit"),
    Account("6150", "Payment Processing Fees", "expense", "debit"),
    Account("6200", "Advertising & Marketing", "expense", "debit"),
    Account("6300", "Professional Fees", "expense", "debit"),
    Account("6400", "Rent & Facilities", "expense", "debit"),
    Account("6500", "Travel & Meals", "expense", "debit"),
    Account("6600", "Payroll & Contractors", "expense", "debit"),
    Account("6700", "FX Loss / Gain", "expense", "debit"),
    Account("6800", "Miscellaneous Expense", "expense", "debit"),
]

ACCOUNT_BY_CODE = {a.code: a for a in CHART_OF_ACCOUNTS}
ACCOUNT_BY_NAME = {a.name.lower(): a for a in CHART_OF_ACCOUNTS}


def get_account(code: str) -> Account | None:
    return ACCOUNT_BY_CODE.get(code)


def chart_of_accounts() -> list[dict[str, Any]]:
    return [a.to_dict() for a in CHART_OF_ACCOUNTS]


# --------------------------------------------------------------------------
# Journal entry construction helpers (keep debit/credit bookkeeping in ONE place)
# --------------------------------------------------------------------------


def build_entry(
    amount: float,
    debit_code: str,
    credit_code: str,
    memo: str = "",
) -> list[JournalLine]:
    """Two-line entry. ``amount`` is signed in bank terms:

    * amount < 0 (money left the bank) -> debit expense/AP, credit 1000
    * amount > 0 (money entered bank)  -> debit 1000, credit revenue/AR
    """
    amt = abs(money(amount))
    d = ACCOUNT_BY_CODE[debit_code]
    c = ACCOUNT_BY_CODE[credit_code]
    if amount < 0:
        return [
            JournalLine(debit_code, d.name, debit=amt, credit=0.0, memo=memo),
            JournalLine(credit_code, c.name, debit=0.0, credit=amt, memo=memo),
        ]
    return [
        JournalLine(debit_code, d.name, debit=amt, credit=0.0, memo=memo),
        JournalLine(credit_code, c.name, debit=0.0, credit=amt, memo=memo),
    ]


def bank_fee_entry(amount: float, memo: str) -> list[JournalLine]:
    return build_entry(-abs(amount), "6100", "1000", memo)


def processing_fee_entry(amount: float, memo: str) -> list[JournalLine]:
    return build_entry(-abs(amount), "6150", "1000", memo)


def missing_expense_entry(amount: float, expense_code: str, memo: str) -> list[JournalLine]:
    return build_entry(-abs(amount), expense_code, "1000", memo)


def missing_receipt_entry(amount: float, revenue_code: str, memo: str) -> list[JournalLine]:
    return build_entry(abs(amount), "1000", revenue_code, memo)


def fx_variance_entry(bank_amount: float, book_amount: float, memo: str) -> list[JournalLine]:
    """Bank amount differs from book amount because of FX. Post the difference
    to 6700 FX Loss / Gain so the bank reconciles."""
    diff = money(bank_amount - book_amount)
    if abs(diff) < 0.01:
        return []
    if diff < 0:  # bank gave us less than booked -> FX loss
        return build_entry(diff, "6700", "1000", memo)
    return build_entry(diff, "1000", "6700", memo)


def duplicate_reversal_entry(original: "JournalLine | None", amount: float, memo: str) -> list[JournalLine]:
    """Reverse a duplicated ledger entry (money left the bank once, booked twice)."""
    return build_entry(abs(amount), "1000", "2000", memo)


# --------------------------------------------------------------------------
# Mock GL (persistent, append-only)
# --------------------------------------------------------------------------


@dataclass
class Ledger:
    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        p = Path(path)
        entries: list[dict[str, Any]] = []
        if p.exists():
            entries = json.loads(p.read_text())
        return cls(path=p, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2))

    def post(self, proposal: ProposedEntry, run_id: str, actor: str) -> dict[str, Any]:
        if not proposal.balanced:
            raise ValueError(f"refusing to post unbalanced proposal {proposal.proposal_id}")
        existing = self.find_by_proposal(proposal.proposal_id)
        if existing is not None:
            return existing
        je = {
            "je_id": f"JE-{len(self.entries) + 1:05d}",
            "date": date.today().isoformat(),
            "run_id": run_id,
            "posted_by": actor,
            "proposal_id": proposal.proposal_id,
            "exception_id": proposal.exception_id,
            "rationale": proposal.rationale,
            "lines": [l.to_dict() for l in proposal.lines],
        }
        self.entries.append(je)
        self.save()
        return je

    def find_by_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Return an existing journal entry for a proposal, if it was posted."""
        return next((entry for entry in self.entries if entry.get("proposal_id") == proposal_id), None)

    def balance(self, code: str = "1000") -> float:
        total = 0.0
        for je in self.entries:
            for line in je["lines"]:
                if line["account_code"] == code:
                    total += line["debit"] - line["credit"]
        return money(total)

    def count(self) -> int:
        return len(self.entries)
