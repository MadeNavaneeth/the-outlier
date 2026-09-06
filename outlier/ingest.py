"""Ingest: bank statement + GL export -> typed objects.

Handles CSV (QuickBooks / Xero / generic bank export shapes) and a naive
text-PDF fallback. PDF bank statements in the wild are a mess; the real-world
story is "OCR/PDF parse upstream, reconcile here", so the PDF path extracts
the table lines it can and hands the rest to the exception desk as ``unknown``
rather than silently dropping rows.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from .models import BankTxn, LedgerEntry, money, parse_date

BANK_ALIASES = {
    "txn_id": ["txn_id", "id", "transactionid", "transaction_id", "uniqueid"],
    "date": ["date", "txndate", "postingdate", "posteddate", "valuedate", "transaction date"],
    "amount": ["amount", "amt", "netamount", "transactionamount", "amount (usd)"],
    "credit": ["credit", "credits", "deposit", "credit amount"],
    "debit": ["debit", "debits", "withdrawal", "debit amount"],
    "currency": ["currency", "ccy", "currency code"],
    "description": ["description", "details", "narrative", "memo", "particulars", "transaction description"],
    "reference": ["reference", "ref", "refno", "invoiceno", "invoice_no", "chequeno", "check_no"],
    "counterparty": ["counterparty", "payee", "vendor", "name", "merchant"],
    "bank_code": ["bank_code", "code", "type", "trxtype", "category"],
}

LEDGER_ALIASES = {
    "entry_id": ["entry_id", "id", "txnid", "journalid", "journal_id", "unique id"],
    "date": ["date", "txndate", "postingdate", "transaction date"],
    "amount": ["amount", "amt", "netamount", "amount (usd)"],
    "currency": ["currency", "ccy", "currency code"],
    "account_code": ["account_code", "account", "accountcode", "account code", "code"],
    "account_name": ["account_name", "account name", "accountname"],
    "description": ["description", "memo", "narrative", "details"],
    "reference": ["reference", "ref", "invoiceno", "invoice_no", "bill no", "billno"],
    "vendor": ["vendor", "payee", "customer", "name", "supplier"],
}


def _map_headers(headers: list[str], aliases: dict[str, list[str]]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in headers]
    out: dict[str, int] = {}
    for field_name, names in aliases.items():
        for name in names:
            if name in lowered:
                out[field_name] = lowered.index(name)
                break
    return out


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return money(value)
    s = str(value).strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[(),\s]", "", s)
    s = re.sub(r"^[A-Za-z$€£₹]+", "", s)
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return money(-v if neg else v)


def _parsed_num(value: Any) -> tuple[float, bool]:
    """Return a number and whether a non-empty value parsed successfully."""
    if value is None or not str(value).strip():
        return 0.0, True
    if isinstance(value, (int, float)):
        return money(value), True
    raw = str(value).strip()
    neg = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[(),\s]", "", raw)
    cleaned = re.sub(r"^[A-Za-z$€£₹]+", "", cleaned)
    try:
        parsed = float(cleaned)
    except ValueError:
        return 0.0, False
    return money(-parsed if neg else parsed), True


def _parse_error_bank(path: Path, row_no: int, txn_id: str, reason: str) -> BankTxn:
    return BankTxn(
        txn_id=txn_id or f"BTX-{row_no:05d}",
        date=parse_date("1970-01-01"),  # type: ignore[arg-type]
        amount=0.0,
        description=f"CSV row {row_no} could not be parsed: {reason}",
        bank_code="PARSE_ERROR",
        source_file=path.name,
    )


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    headers, data = rows[0], rows[1:]
    return [dict(zip(headers, r + [""] * (len(headers) - len(r)))) for r in data]


# --------------------------------------------------------------------------
# bank statements
# --------------------------------------------------------------------------


def load_bank(path: str | Path) -> list[BankTxn]:
    p = Path(path)
    if p.suffix.lower() in {".txt", ".pdf"}:
        return _load_bank_text(p)
    rows = read_csv(p)
    if not rows:
        return []
    cols = _map_headers(list(rows[0].keys()), BANK_ALIASES)
    out: list[BankTxn] = []
    for i, row in enumerate(rows):
        vals = list(row.values())

        def get(field_name: str) -> str:
            idx = cols.get(field_name)
            return (vals[idx] if idx is not None and idx < len(vals) else "") or ""

        d = parse_date(get("date"))
        amount, amount_ok = _parsed_num(get("amount"))
        if not get("amount").strip():
            # some exports split debit/credit columns
            credit, credit_ok = _parsed_num(get("credit"))
            debit, debit_ok = _parsed_num(get("debit"))
            amount = credit - debit
            amount_ok = credit_ok and debit_ok
        if d is None or not amount_ok:
            out.append(_parse_error_bank(p, i + 2, get("txn_id").strip(), "invalid date" if d is None else "invalid amount"))
            continue
        out.append(
            BankTxn(
                txn_id=get("txn_id").strip() or f"BTX-{i + 1:05d}",
                date=d,
                amount=amount,
                currency=get("currency").strip().upper() or "USD",
                description=get("description").strip(),
                reference=get("reference").strip(),
                counterparty=get("counterparty").strip(),
                bank_code=get("bank_code").strip(),
                source_file=p.name,
            )
        )
    return out


_PDF_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<amount>-?[\d,]+\.\d{2})\s*$"
)


def _load_bank_text(p: Path) -> list[BankTxn]:
    """Best-effort parse of a text/PDF bank statement export.

    Rows we cannot parse are returned as zero-amount ``unknown`` lines so the
    reconciliation report can say "N statement lines could not be parsed" --
    which is the honest answer, and exactly what a controller wants to see.
    """
    raw = p.read_text(encoding="utf-8", errors="replace")
    out: list[BankTxn] = []
    unparsed = 0
    for i, line in enumerate(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _PDF_LINE.match(line)
        if not m:
            if len(line) > 12 and any(ch.isdigit() for ch in line):
                unparsed += 1
            continue
        d = parse_date(m.group("date"))
        if d is None:
            unparsed += 1
            continue
        out.append(
            BankTxn(
                txn_id=f"{p.stem}-{i + 1:04d}",
                date=d,
                amount=_num(m.group("amount")),
                description=m.group("desc").strip(),
                source_file=p.name,
            )
        )
    if unparsed:
        out.append(
            BankTxn(
                txn_id=f"{p.stem}-UNPARSED",
                date=parse_date("1970-01-01"),  # type: ignore[arg-type]
                amount=0.0,
                description=f"{unparsed} statement line(s) could not be parsed",
                bank_code="PARSE_ERROR",
                source_file=p.name,
            )
        )
    return out


# --------------------------------------------------------------------------
# ledger exports
# --------------------------------------------------------------------------


def load_ledger(path: str | Path) -> list[LedgerEntry]:
    p = Path(path)
    rows = read_csv(p)
    if not rows:
        return []
    cols = _map_headers(list(rows[0].keys()), LEDGER_ALIASES)
    out: list[LedgerEntry] = []
    for i, row in enumerate(rows):
        vals = list(row.values())

        def get(field_name: str) -> str:
            idx = cols.get(field_name)
            return (vals[idx] if idx is not None and idx < len(vals) else "") or ""

        d = parse_date(get("date"))
        amount, amount_ok = _parsed_num(get("amount"))
        if d is None or not amount_ok:
            out.append(
                LedgerEntry(
                    entry_id=get("entry_id").strip() or f"GL-{i + 1:05d}",
                    date=parse_date("1970-01-01"),  # type: ignore[arg-type]
                    amount=0.0,
                    account_code="2300",
                    account_name="Suspense / Needs Review",
                    description=f"CSV row {i + 2} could not be parsed: {'invalid date' if d is None else 'invalid amount'}",
                    status="PARSE_ERROR",
                    source_file=p.name,
                )
            )
            continue
        code = get("account_code").strip() or "6800"
        out.append(
            LedgerEntry(
                entry_id=get("entry_id").strip() or f"GL-{i + 1:05d}",
                date=d,
                amount=amount,
                account_code=code,
                currency=get("currency").strip().upper() or "USD",
                account_name=get("account_name").strip(),
                description=get("description").strip(),
                reference=get("reference").strip(),
                vendor=get("vendor").strip(),
                source_file=p.name,
            )
        )
    return out
