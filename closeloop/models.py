"""Core data models for CloseLoop.

Sign convention (everywhere in the codebase, no exceptions):
    amount > 0  -> money INTO  the company bank account (credit / receipt)
    amount < 0  -> money OUT of the company bank account (debit / payment)

Ledger entries use the same sign convention so that matching is a plain
equality test. Journal *lines* produced for posting use classic debit/credit
columns (debit > 0, credit > 0) -- see :mod:`closeloop.ledger`.

Everything is a frozen-ish dataclass with ``to_dict`` so it round-trips
through JSON, SQLite and the web API without an ORM.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Categories for unmatched bank items
# --------------------------------------------------------------------------

TIMING = "timing"
MISSING_ENTRY = "missing_entry"
DUPLICATE = "duplicate"
FEE = "fee"
FX = "fx"
FRAUD_SUSPECT = "fraud_suspect"
UNKNOWN = "unknown"

ALL_CATEGORIES = (
    TIMING,
    MISSING_ENTRY,
    DUPLICATE,
    FEE,
    FX,
    FRAUD_SUSPECT,
    UNKNOWN,
)

#: Categories that need a journal entry to resolve.
POSTABLE = (MISSING_ENTRY, DUPLICATE, FEE, FX)
#: Categories that resolve as a reconciling item on the report (no posting).
RECONCILING_ONLY = (TIMING,)


def iso(d: date | str | None) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)


def parse_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class BankTxn:
    """One row of a bank statement (CSV or parsed PDF)."""

    txn_id: str
    date: date
    amount: float
    currency: str = "USD"
    description: str = ""
    reference: str = ""
    counterparty: str = ""
    bank_code: str = ""
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = iso(self.date)
        return d

    @property
    def direction(self) -> str:
        return "IN" if self.amount > 0 else "OUT"

    @property
    def key(self) -> str:
        return self.txn_id


@dataclass
class LedgerEntry:
    """One row of the GL / sub-ledger export (QuickBooks/Xero style CSV)."""

    entry_id: str
    date: date
    amount: float
    account_code: str
    account_name: str = ""
    description: str = ""
    reference: str = ""
    vendor: str = ""
    status: str = "OPEN"  # OPEN | MATCHED
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = iso(self.date)
        return d

    @property
    def key(self) -> str:
        return self.entry_id


@dataclass
class Match:
    """A resolved pairing between one bank transaction and 1..n ledger entries."""

    match_id: str
    bank_txn_ids: list[str]
    ledger_entry_ids: list[str]
    match_type: str  # exact | reference | fuzzy_amount | split | batch | learned
    confidence: float
    method: str  # deterministic | learned_rule | agent
    residual: float = 0.0
    evidence: list[str] = field(default_factory=list)
    auto: bool = True
    requires_review: bool = False
    notes: str = ""

    @property
    def is_multi(self) -> bool:
        return len(self.ledger_entry_ids) > 1 or len(self.bank_txn_ids) > 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JournalLine:
    account_code: str
    account_name: str
    debit: float = 0.0
    credit: float = 0.0
    memo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProposedEntry:
    """A journal entry the exception agent drafted for a human to approve."""

    proposal_id: str
    exception_id: str
    lines: list[JournalLine]
    rationale: str = ""
    amount: float = 0.0
    account_code: str = ""
    status: str = "DRAFT"  # DRAFT | APPROVED | EDITED | REJECTED | POSTED
    source: str = "agent"  # agent | learned_rule | human
    critic_verdict: str = "PENDING"  # PASS | FAIL | ESCALATE | PENDING
    critic_notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def debit_total(self) -> float:
        return round(sum(l.debit for l in self.lines), 2)

    @property
    def credit_total(self) -> float:
        return round(sum(l.credit for l in self.lines), 2)

    @property
    def balanced(self) -> bool:
        return abs(self.debit_total - self.credit_total) < 0.01

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["lines"] = [l.to_dict() for l in self.lines]
        d["debit_total"] = self.debit_total
        d["credit_total"] = self.credit_total
        d["balanced"] = self.balanced
        return d


@dataclass
class ExceptionRecord:
    """One unmatched bank item, classified with evidence + a proposed fix."""

    exception_id: str
    bank_txn_ids: list[str]
    category: str
    confidence: float
    explanation: str
    amount: float
    date: str
    description: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    proposal_id: str | None = None
    resolution: str = "OPEN"  # OPEN | AUTO_RESOLVED | APPROVED | EDITED | REJECTED
    resolved_by: str = ""  # "" | deterministic | learned_rule | agent | human:<name>
    needs_review: bool = True
    materiality_breach: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovedRule:
    """A pattern a human approved. Used to auto-resolve the same case later.

    This is the "learn from reviewer decisions" loop: rules are created ONLY
    from an explicit human approval, and they only ever fire on an exact
    signature match, which keeps the false-auto-post rate at zero.
    """

    rule_id: str
    kind: str  # classification | posting | match
    signature: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_from: str = ""
    exception_id: str = ""
    hits: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditEvent:
    """Append-only audit trail entry. Every decision lands here."""

    seq: int
    ts: str
    run_id: str
    actor: str  # deterministic | agent:<name> | rule:<id> | human:<name> | guardrail
    action: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    """Everything one reconciliation run produced. Persisted as JSON."""

    run_id: str
    created_at: str
    round_no: int
    bank_file: str
    ledger_file: str
    config: dict[str, Any]
    metrics: dict[str, Any]
    matches: list[dict[str, Any]] = field(default_factory=list)
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    rules_used: list[str] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    posted: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def n_matches(self) -> int:
        return len(self.matches)

    @property
    def n_exceptions(self) -> int:
        return len(self.exceptions)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_id_counter = itertools.count(1)


def next_id(prefix: str) -> str:
    return f"{prefix}-{next(_id_counter):05d}"


def money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def pairs(items: Iterable[Any], n: int) -> Iterable[tuple]:
    return itertools.combinations(items, n)
