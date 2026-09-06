"""Ledger bookkeeping, the LLM layer, ingestion and the evaluator itself."""

import json
from pathlib import Path as pathlib_Path

import pytest

from outlier.eval import Evaluator, improvement_table, load_truth
from outlier.ingest import load_bank, load_ledger
from outlier.ledger import (
    ACCOUNT_BY_CODE,
    Ledger,
    bank_fee_entry,
    build_entry,
    fx_variance_entry,
)
from outlier.llm import MockProvider, OpenAIProvider, _extract_json, est_tokens
from outlier.models import JournalLine, ProposedEntry


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def total(lines):
    return round(sum(l.debit for l in lines), 2), round(sum(l.credit for l in lines), 2)


def test_debit_entry_is_balanced_and_hits_the_bank_on_the_credit_side():
    d, c = total(bank_fee_entry(12.5, "monthly fee"))
    assert d == c == 12.5
    lines = bank_fee_entry(12.5, "monthly fee")
    bank = next(l for l in lines if l.account_code == "1000")
    assert bank.credit == 12.5 and bank.debit == 0.0


def test_receipt_entry_debits_the_bank():
    lines = build_entry(500.0, "1000", "4000", "receipt")
    bank = next(l for l in lines if l.account_code == "1000")
    assert bank.debit == 500.0


def test_fx_variance_sign():
    # booked -100, bank settled -103 -> we lost 3 -> FX loss, debit 6700
    loss = fx_variance_entry(bank_amount=-103.0, book_amount=-100.0, memo="fx")
    assert next(l for l in loss if l.account_code == "6700").debit == 3.0
    # booked -100, bank settled -97 -> we kept 3 -> FX gain, credit 6700
    gain = fx_variance_entry(bank_amount=-97.0, book_amount=-100.0, memo="fx")
    assert next(l for l in gain if l.account_code == "6700").credit == 3.0
    assert fx_variance_entry(-100.0, -100.0, "no variance") == []


def test_ledger_refuses_to_post_an_unbalanced_entry(tmp_path):
    ledger = Ledger.load(tmp_path / "ledger.json")
    bad = ProposedEntry(
        proposal_id="P1", exception_id="E1",
        lines=[JournalLine("6100", "Bank Charges & Fees", debit=10.0)],
        amount=10.0,
    )
    with pytest.raises(ValueError):
        ledger.post(bad, "R1", "test")


def test_ledger_balance_tracks_postings(tmp_path):
    ledger = Ledger.load(tmp_path / "ledger.json")
    pe = ProposedEntry(proposal_id="P1", exception_id="E1", lines=bank_fee_entry(12.5, "fee"), amount=-12.5)
    ledger.post(pe, "R1", "test")
    assert ledger.count() == 1
    assert ledger.balance("1000") == -12.5
    reloaded = Ledger.load(tmp_path / "ledger.json")
    assert reloaded.balance("1000") == -12.5


# --------------------------------------------------------------------------
# llm layer
# --------------------------------------------------------------------------

def test_extract_json_handles_fences_and_prose():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Sure! Here you go: {"a": 1} hope that helps') == {"a": 1}
    assert _extract_json('{"a": {"b": 2}, "c": 3}') == {"a": {"b": 2}, "c": 3}


def test_extract_json_raises_on_garbage():
    with pytest.raises(Exception):
        _extract_json("no json here at all")


def test_extract_json_rejects_non_object_json():
    with pytest.raises(Exception):
        _extract_json('["not", "an", "object"]')


def test_provider_falls_back_and_records_the_failure():
    class Broken(OpenAIProvider):
        def __init__(self):
            super().__init__(api_key="")

        def _raw(self, system, user):
            raise RuntimeError("boom")

    p = Broken()
    out = p.complete_json("sys", "user", "test", fallback={"category": "unknown"})
    assert out == {"category": "unknown"}
    assert p.calls[0].ok is False and "boom" in p.calls[0].error
    assert p.usage()["failed_calls"] == 1


def test_mock_provider_is_deterministic_for_the_same_item():
    p1, p2 = MockProvider(), MockProvider()
    for p in (p1, p2):
        p.register("echo", lambda payload: {"category": "fee", "confidence": 0.9,
                                            "item": payload["item"]})
    payload = {"purpose": "echo", "item": {"txn_id": "B1", "description": "MONTHLY SERVICE CHARGE"}}
    a = p1.complete_json("s", json.dumps(payload), "echo", {})
    b = p2.complete_json("s", json.dumps(payload), "echo", {})
    assert a == b, "the same item must get the same treatment across runs"


def test_mock_provider_degrades_some_items():
    p = MockProvider(error_rate=1.0)
    p.register("echo", lambda payload: {"category": "fee", "confidence": 0.9, "item": payload["item"]})
    out = p.complete_json("s", json.dumps({"purpose": "echo", "item": {"txn_id": "B1"}}), "echo", {})
    assert out["category"] != "fee" and out["degraded"] is True


def test_token_estimate_is_positive():
    assert est_tokens("hello world") > 0


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------

def test_generated_files_ingest(dataset):
    bank = load_bank(dataset / "bank_statement.csv")
    ledger = load_ledger(dataset / "ledger_export.csv")
    assert bank and ledger
    assert all(t.txn_id for t in bank)
    assert all(e.account_code in ACCOUNT_BY_CODE for e in ledger)
    assert sum(1 for t in bank if t.amount > 0) > 0
    assert sum(1 for t in bank if t.amount < 0) > 0


def test_unparseable_statement_lines_are_surfaced_not_dropped(tmp_path):
    p = tmp_path / "statement.txt"
    p.write_text(
        "BANK OF TEST - STATEMENT\n"
        "2026-08-01 MONTHLY SERVICE CHARGE -12.50\n"
        "this line is garbage 12345\n"
        "2026-08-02 WIRE TRANSFER FEE -25.00\n"
    )
    rows = load_bank(p)
    parsed = [r for r in rows if r.bank_code != "PARSE_ERROR"]
    assert len(parsed) == 2
    errors = [r for r in rows if r.bank_code == "PARSE_ERROR"]
    assert len(errors) == 1 and "could not be parsed" in errors[0].description


def test_malformed_csv_rows_are_surfaced_not_dropped(tmp_path):
    p = tmp_path / "statement.csv"
    p.write_text(
        "txn_id,date,amount,description\n"
        "B1,2026-08-01,-12.50,valid\n"
        "B2,not-a-date,25.00,bad date\n"
        "B3,2026-08-03,not-a-number,bad amount\n"
    )
    rows = load_bank(p)
    assert [r.txn_id for r in rows] == ["B1", "B2", "B3"]
    assert sum(r.bank_code == "PARSE_ERROR" for r in rows) == 2


# --------------------------------------------------------------------------
# evaluator
# --------------------------------------------------------------------------

def test_evaluator_scores_a_perfect_run():
    truth = [
        {"bank_txn_id": "B1", "expected_ledger_ids": ["L1"], "expected_category": "", "match_type": "exact", "note": "", "side": "bank"},
        {"bank_txn_id": "B2", "expected_ledger_ids": [], "expected_category": "fee", "match_type": "none", "note": "", "side": "bank"},
    ]
    run = {
        "matches": [{"bank_txn_ids": ["B1"], "ledger_entry_ids": ["L1"], "match_type": "exact", "requires_review": False}],
        "exceptions": [{"bank_txn_ids": ["B2"], "category": "fee", "evidence": {"side": "bank"}}],
        "proposals": [], "metrics": {}, "posted": [],
    }
    ev = Evaluator(truth).evaluate(run)
    assert ev["match_precision"] == 1.0 and ev["match_recall"] == 1.0
    assert ev["classification_precision"] == 1.0
    assert ev["false_auto_posts"] == 0


def test_evaluator_counts_a_false_pairing():
    truth = [{"bank_txn_id": "B1", "expected_ledger_ids": [], "expected_category": "fee",
              "match_type": "none", "note": "", "side": "bank"}]
    run = {
        "matches": [{"bank_txn_ids": ["B1"], "ledger_entry_ids": ["L9"], "match_type": "exact", "requires_review": False}],
        "exceptions": [], "proposals": [], "metrics": {}, "posted": [],
    }
    ev = Evaluator(truth).evaluate(run)
    assert ev["matched_pairs_fp"] == 1
    assert ev["match_precision"] == 0.0


def test_every_raised_exception_is_scored(dataset):
    """Regression: rows carrying BOTH a pairing and an exception (FX, duplicated
    invoices) were missing from the answer key, so accuracy was computed on
    roughly half the exceptions and looked better than it was."""
    from outlier.agents.orchestrator import Orchestrator
    from outlier.config import Policy
    from outlier.llm import MockProvider
    from outlier.store import Store
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(pathlib_Path(tmp) / "s.db")
        try:
            result = Orchestrator(MockProvider(), store, policy=Policy()).run(
                dataset / "bank_statement.csv", dataset / "ledger_export.csv", run_id="RUN-EVAL", round_no=1
            )
        finally:
            # Windows cannot delete an open SQLite file on TemporaryDirectory
            # cleanup; the other tests use pytest's tmp_path for the same reason.
            store.close()
    run = result.to_dict()
    ev = Evaluator(load_truth(dataset / "ground_truth.json")).evaluate(run)
    scored = ev["classification_correct"] + ev["classification_incorrect"]
    assert ev["missed_exceptions"] == []
    assert ev["ledger_only_exceptions_missed"] == 0
    assert scored == ev["exceptions_raised"], (
        f"only {scored} of {ev['exceptions_raised']} exceptions were graded"
    )


def test_improvement_table_keeps_the_money_columns():
    rows = improvement_table([{"round_no": 1, "auto_resolve_rate": 0.0, "rule_hit_rate": 0.0},
                              {"round_no": 2, "auto_resolve_rate": 0.2, "rule_hit_rate": 0.7}])
    assert rows[0]["round_no"] == 1
    assert rows[1]["rule_hit_rate"] == 0.7
    assert "auto_resolve_rate" in rows[0]
