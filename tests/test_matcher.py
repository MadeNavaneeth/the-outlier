"""Deterministic matcher tests.

These are the tests that matter most: if the matcher claims a pairing that is
not real, everything downstream is fiction.
"""

import csv
from datetime import date

from closeloop.ingest import load_bank, load_ledger
from closeloop.matcher import Matcher, normalize_ref, refs_in
from closeloop.models import BankTxn, LedgerEntry


def mk_ledger(entry_id, amount, d=date(2026, 8, 5), ref="", vendor="", desc="", status="OPEN"):
    return LedgerEntry(entry_id=entry_id, date=d, amount=amount, account_code="6000",
                       account_name="Software & Subscriptions", description=desc or f"{vendor} - {ref}",
                       reference=ref, vendor=vendor, status=status)


def mk_bank(txn_id, amount, d=date(2026, 8, 7), ref="", cp="", desc="", code=""):
    return BankTxn(txn_id=txn_id, date=d, amount=amount, description=desc or f"{cp} PAYMENT {ref}",
                   reference=ref, counterparty=cp, bank_code=code)


def test_exact_match_is_taken_and_consumed():
    m = Matcher([mk_bank("B1", -100.0)], [mk_ledger("L1", -100.0)])
    rep = m.run()
    assert len(rep.matches) == 1
    assert rep.matches[0].match_type == "exact"
    assert rep.unmatched_bank == [] and rep.unmatched_ledger == []


def test_ambiguous_amount_is_not_guessed():
    """Two identical GL lines and one payment: the matcher must not pick one at random."""
    m = Matcher([mk_bank("B1", -100.0)], [mk_ledger("L1", -100.0), mk_ledger("L2", -100.0)])
    rep = m.run()
    assert rep.matches == [], "an ambiguous 1:1 must not be auto-matched"
    assert len(rep.unmatched_ledger) == 2


def test_reference_match_requires_amount_tie_out():
    """Same invoice, different amount is an FX variance, not a clean match."""
    m = Matcher([mk_bank("B1", -103.0, ref="INV-1001")], [mk_ledger("L1", -100.0, ref="INV-1001", vendor="Acme")])
    rep = m.run()
    assert len(rep.matches) == 1
    assert rep.matches[0].match_type == "fx_variance"
    assert rep.matches[0].residual == -3.0
    assert rep.matches[0].requires_review is True


def test_variance_beyond_the_fx_ceiling_is_not_matched():
    m = Matcher([mk_bank("B1", -200.0, ref="INV-1001")], [mk_ledger("L1", -100.0, ref="INV-1001", vendor="Acme")])
    rep = m.run()
    assert rep.matches == []
    assert len(rep.unmatched_bank) == 1


def test_split_payment_sums_the_right_lines():
    """A customer remittance pays several invoices at once.

    The description deliberately does NOT contain a settlement keyword: that is
    what separates a remittance (split pass) from a processor payout (batch
    pass), and an earlier version of this test used "BATCH REMITTANCE", which
    let the batch pass answer a question it should not have been asked.
    """
    bank = [mk_bank("B1", 300.0, cp="Acme", desc="Acme Corp REMITTANCE")]
    ledger = [
        mk_ledger("L1", 100.0, ref="INV-1", vendor="Acme"),
        mk_ledger("L2", 80.0, ref="INV-2", vendor="Acme"),
        mk_ledger("L3", 120.0, ref="INV-3", vendor="Acme"),
        mk_ledger("L4", 55.0, ref="INV-9", vendor="Other"),
    ]
    rep = Matcher(bank, ledger).run()
    assert len(rep.matches) == 1
    assert sorted(rep.matches[0].ledger_entry_ids) == ["L1", "L2", "L3"]
    assert rep.matches[0].match_type == "split"


def test_split_does_not_reuse_a_line():
    bank = [mk_bank("B1", 150.0, cp="Acme", desc="Acme Corp REMITTANCE"),
            mk_bank("B2", 150.0, cp="Acme", desc="Acme Corp REMITTANCE")]
    ledger = [mk_ledger("L1", 100.0, vendor="Acme"), mk_ledger("L2", 50.0, vendor="Acme")]
    rep = Matcher(bank, ledger).run()
    assert len(rep.matches) == 1, "the second bank row cannot reuse the same GL lines"


def test_duplicate_gl_lines_match_earliest_and_flag_the_rest():
    bank = [mk_bank("B1", -250.0, ref="INV-7001", cp="Acme")]
    ledger = [
        mk_ledger("L1", -250.0, d=date(2026, 8, 2), ref="INV-7001", vendor="Acme"),
        mk_ledger("L2", -250.0, d=date(2026, 8, 4), ref="INV-7001", vendor="", desc="INV-7001 (DUPLICATE ENTRY)"),
    ]
    rep = Matcher(bank, ledger).run()
    assert len(rep.matches) == 1
    assert rep.matches[0].match_type == "duplicate_aware"
    assert rep.matches[0].ledger_entry_ids == ["L1"], "must take the earliest copy"
    assert rep.duplicate_candidates == ["L2"]
    assert [e.entry_id for e in rep.unmatched_ledger] == ["L2"]


def test_duplicate_pass_does_not_consume_a_different_amount_with_the_same_reference():
    """Regression: this used to pair the payment with an FX-variance line."""
    bank = [mk_bank("B1", -250.0, ref="INV-7001", cp="Acme")]
    ledger = [
        mk_ledger("L1", -246.0, d=date(2026, 8, 1), ref="INV-7001", vendor="Acme"),  # FX variance
        mk_ledger("L2", -250.0, d=date(2026, 8, 2), ref="INV-7001", vendor="Acme"),
        mk_ledger("L3", -250.0, d=date(2026, 8, 3), ref="INV-7001", vendor="", desc="INV-7001 (DUPLICATE ENTRY)"),
    ]
    rep = Matcher(bank, ledger).run()
    matched = [e for m in rep.matches for e in m.ledger_entry_ids]
    assert "L1" not in matched, "the FX line must not be consumed as a duplicate match"
    assert "L2" in matched


def test_batch_settlement_needs_a_unique_line_set_on_one_date():
    """More lines than the split pass is allowed to take, so only the batch
    pass can resolve it."""
    amounts = [100.0, 90.0, 110.0, 40.0, 60.0]
    bank = [mk_bank("B1", sum(amounts), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts")]
    ledger = [mk_ledger(f"L{i}", v, vendor="Stripe Payouts", desc=f"Order {i}")
              for i, v in enumerate(amounts)]
    rep = Matcher(bank, ledger).run()
    assert len(rep.matches) == 1
    assert rep.matches[0].match_type == "batch"
    assert sorted(rep.matches[0].ledger_entry_ids) == ["L0", "L1", "L2", "L3", "L4"]


def test_settlements_on_one_date_are_solved_jointly():
    """Regression: solved one deposit at a time, the largest settlement grabbed
    a line run that another settlement needed. Every sum still tied out, so
    nothing looked wrong -- match recall just quietly dropped.

    Lines L1..L6 = 25, 25, 25, 25, 25, 35.
      B2 (85) has exactly one contiguous run: L4+L5+L6.
      B1 (75) has three: L1..L3, L2..L4, L3..L5 -- two of them eat B2's lines.
    The only assignment that uses no line twice is B1 = L1..L3, B2 = L4..L6.
    A greedy pass takes B1 = L1..L3 or L2..L4 and B2 never resolves, so this
    also pins the "lock the forced choice first, then backtrack" behaviour.
    """
    day = date(2026, 8, 4)
    ledger = [
        mk_ledger("L1", 25.0, d=day, vendor="Stripe Payouts", desc="Order 1"),
        mk_ledger("L2", 25.0, d=day, vendor="Stripe Payouts", desc="Order 2"),
        mk_ledger("L3", 25.0, d=day, vendor="Stripe Payouts", desc="Order 3"),
        mk_ledger("L4", 25.0, d=day, vendor="Stripe Payouts", desc="Order 4"),
        mk_ledger("L5", 25.0, d=day, vendor="Stripe Payouts", desc="Order 5"),
        mk_ledger("L6", 35.0, d=day, vendor="Stripe Payouts", desc="Order 6"),
    ]
    bank = [
        mk_bank("B1", 75.0, d=date(2026, 8, 5), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts"),
        mk_bank("B2", 85.0, d=date(2026, 8, 5), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts"),
    ]
    rep = Matcher(bank, ledger).run()
    got = {m.bank_txn_ids[0]: sorted(m.ledger_entry_ids) for m in rep.matches}
    assert got == {"B1": ["L1", "L2", "L3"], "B2": ["L4", "L5", "L6"]}, got
    used = [e for ids in got.values() for e in ids]
    assert len(used) == len(set(used)), f"a GL line was used twice: {got}"
    assert [t.txn_id for t in rep.unmatched_bank] == [], [t.txn_id for t in rep.unmatched_bank]


def test_contending_settlements_get_the_only_consistent_assignment():
    """Two identical 75.00 settlements against 25 x5 + 35. Both have the same
    three candidate runs (L1..L3, L2..L4, L3..L5) and any two of them overlap,
    so at most one can be explained line by line. Matching both by sharing
    lines would make every sum tie out while double-counting the revenue --
    the exact failure this rewrite exists to prevent. One must match, the
    other must be left for the exception desk.
    """
    day = date(2026, 8, 4)
    ledger = [
        mk_ledger("L1", 25.0, d=day, vendor="Stripe Payouts", desc="Order 1"),
        mk_ledger("L2", 25.0, d=day, vendor="Stripe Payouts", desc="Order 2"),
        mk_ledger("L3", 25.0, d=day, vendor="Stripe Payouts", desc="Order 3"),
        mk_ledger("L4", 25.0, d=day, vendor="Stripe Payouts", desc="Order 4"),
        mk_ledger("L5", 25.0, d=day, vendor="Stripe Payouts", desc="Order 5"),
        mk_ledger("L6", 35.0, d=day, vendor="Stripe Payouts", desc="Order 6"),
    ]
    bank = [
        mk_bank("B1", 75.0, d=date(2026, 8, 5), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts"),
        mk_bank("B2", 75.0, d=date(2026, 8, 5), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts"),
    ]
    rep = Matcher(bank, ledger).run()
    got = {m.bank_txn_ids[0]: sorted(m.ledger_entry_ids) for m in rep.matches}
    assert len(got) == 1, f"exactly one settlement can be explained: {got}"
    ids = next(iter(got.values()))
    assert ids in (["L1", "L2", "L3"], ["L2", "L3", "L4"], ["L3", "L4", "L5"]), got
    leftover = [t.txn_id for t in rep.unmatched_bank]
    assert len(leftover) == 1 and leftover[0] not in got, (got, leftover)


def test_unsolvable_date_degrades_to_one_match_not_a_shared_line():
    """L1..L6 = 40, 30, 20, 10, 25, 25. B1 (100) has one run, L1..L4; B2 (60)
    has two, L2..L4 and L4..L6 -- every one of them overlaps B1's. There is no
    joint answer, so the honest result is one match and one leftover, not two
    matches that both claim L4.
    """
    day = date(2026, 8, 4)
    ledger = [
        mk_ledger("L1", 40.0, d=day, vendor="Stripe Payouts", desc="Order 1"),
        mk_ledger("L2", 30.0, d=day, vendor="Stripe Payouts", desc="Order 2"),
        mk_ledger("L3", 20.0, d=day, vendor="Stripe Payouts", desc="Order 3"),
        mk_ledger("L4", 10.0, d=day, vendor="Stripe Payouts", desc="Order 4"),
        mk_ledger("L5", 25.0, d=day, vendor="Stripe Payouts", desc="Order 5"),
        mk_ledger("L6", 25.0, d=day, vendor="Stripe Payouts", desc="Order 6"),
    ]
    bank = [
        mk_bank("B1", 100.0, d=date(2026, 8, 5), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts"),
        mk_bank("B2", 60.0, d=date(2026, 8, 5), desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts"),
    ]
    rep = Matcher(bank, ledger).run()
    got = {m.bank_txn_ids[0]: sorted(m.ledger_entry_ids) for m in rep.matches}
    assert got == {"B1": ["L1", "L2", "L3", "L4"]}, got
    assert [t.txn_id for t in rep.unmatched_bank] == ["B2"], [t.txn_id for t in rep.unmatched_bank]
    assert [e.entry_id for e in rep.unmatched_ledger] == ["L5", "L6"], [
        e.entry_id for e in rep.unmatched_ledger
    ]



def test_settlement_with_two_possible_line_sets_is_refused():
    """Two exact subsets that both tie out: the matcher must not pick one."""
    bank = [mk_bank("B1", 200.0, desc="STRIPE PAYOUT - DAILY SETTLEMENT", cp="Stripe Payouts")]
    ledger = [
        mk_ledger("L1", 100.0, vendor="Stripe Payouts", desc="Order 1"),
        mk_ledger("L2", 100.0, vendor="Stripe Payouts", desc="Order 2"),
        mk_ledger("L3", 60.0, vendor="Stripe Payouts", desc="Order 3"),
        mk_ledger("L4", 140.0, vendor="Stripe Payouts", desc="Order 4"),
    ]
    rep = Matcher(bank, ledger).run()
    assert rep.matches == []
    assert rep.ambiguous, "the ambiguity must be recorded, not silently dropped"


def test_near_amount_pass_only_accepts_cent_drift():
    ok = Matcher([mk_bank("B1", -100.00)], [mk_ledger("L1", -100.03)]).run()
    assert ok.matches and ok.matches[0].match_type == "near_amount"
    too_far = Matcher([mk_bank("B1", -100.00)], [mk_ledger("L1", -101.00)]).run()
    assert too_far.matches == []


def test_no_false_matches_on_the_generated_month(dataset):
    """The headline reliability claim, measured on planted ground truth."""
    import json

    from closeloop.eval import Evaluator, load_truth

    bank = load_bank(dataset / "bank_statement.csv")
    ledger = load_ledger(dataset / "ledger_export.csv")
    rep = Matcher(bank, ledger).run()
    run = {
        "matches": [m.to_dict() for m in rep.matches],
        "exceptions": [],
        "proposals": [],
        "metrics": {},
        "posted": [],
    }
    ev = Evaluator(load_truth(dataset / "ground_truth.json")).evaluate(run)
    assert ev["match_recall"] >= 0.90
    assert ev["matched_pairs_fp"] == 0, f"false pairings: {ev['wrong_pairs']}"


def test_reference_normalisation():
    assert normalize_ref("INV-12 345") == "INV12345"
    assert refs_in("Payment for INV-99887 and PO-1234") == {"INV99887", "PO1234"}
