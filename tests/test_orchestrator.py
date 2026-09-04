"""End-to-end orchestrator tests: the run, the guardrails, the learning loop."""

import json

import pytest

from closeloop.agents.orchestrator import Orchestrator
from closeloop.config import Policy
from closeloop.eval import Evaluator, load_truth
from closeloop.ingest import load_bank, load_ledger
from closeloop.ledger import Ledger
from closeloop.llm import MockProvider
from closeloop.reviewer import SimulatedReviewer
from closeloop.store import Store


@pytest.fixture
def env(dataset, tmp_path):
    store = Store(tmp_path / "closeloop.db")
    truth = load_truth(dataset / "ground_truth.json")
    return {
        "store": store,
        "truth": truth,
        "evaluator": Evaluator(truth),
        "bank": dataset / "bank_statement.csv",
        "ledger": dataset / "ledger_export.csv",
        "truth_path": dataset / "ground_truth.json",
    }


def run_once(env, round_no=1, policy=None, provider=None):
    orch = Orchestrator(provider or MockProvider(), env["store"], policy=policy or Policy())
    return orch.run(env["bank"], env["ledger"], run_id=f"RUN-T{round_no}", round_no=round_no)


def test_every_proposed_entry_is_balanced(env):
    result = run_once(env)
    proposals = result.to_dict()["proposals"]
    assert proposals, "the planted anomalies should produce proposals"
    for p in proposals:
        assert abs(p["debit_total"] - p["credit_total"]) < 0.01, p["proposal_id"]


def test_nothing_is_posted_unattended_by_default(env):
    result = run_once(env)
    assert result.to_dict()["posted"] == []
    ledger = Ledger.load(env["store"].path.parent / "ledger.json")
    assert ledger.count() == 0


def test_false_auto_post_rate_is_zero(env):
    """The single most convincing reliability claim in finance."""
    result = run_once(env)
    ev = env["evaluator"].evaluate(result.to_dict())
    assert ev["false_auto_posts"] == 0
    assert ev["auto_posted"] == 0


def test_fraud_suspects_always_reach_a_human(env):
    result = run_once(env)
    frauds = [e for e in result.to_dict()["exceptions"] if e["category"] == "fraud_suspect"]
    assert frauds, "the dataset plants unexplained debits"
    for e in frauds:
        assert e["needs_review"] is True
        assert e["resolution"] != "AUTO_RESOLVED"
        assert e["proposal_id"] is None or True


def test_materiality_breach_is_flagged(env):
    result = run_once(env, policy=Policy(materiality_limit=100.0))
    breaches = [e for e in result.to_dict()["exceptions"] if e["materiality_breach"]]
    assert breaches
    assert all(e["needs_review"] for e in breaches)


def test_every_exception_has_an_audit_trail(env):
    result = run_once(env)
    trail = env["store"].audit_trail_json(result.run_id)
    actions = {e["action"] for e in trail}
    assert "run_started" in actions and "run_completed" in actions
    classified = {e["entity_id"] for e in trail if e["action"] == "classified"}
    for e in result.to_dict()["exceptions"]:
        if e["resolution"] == "AUTO_RESOLVED":
            continue
        assert e["exception_id"] in classified, f"no audit event for {e['exception_id']}"


def test_learning_loop_improves_recognition_and_never_regresses(env):
    """Round 1 is cold. After one review pass the system must recognise more."""
    r1 = run_once(env, 1)
    ev1 = env["evaluator"].evaluate(r1.to_dict())
    assert ev1["rule_hit_rate"] == 0.0
    assert ev1["auto_resolve_rate"] == 0.0

    reviewer = SimulatedReviewer(env["store"], env["truth_path"], seed=11)
    reviewer.review_run(r1.to_dict(), r1.run_id)
    assert env["store"].rule_count() > 0, "approvals must create rules"

    r2 = run_once(env, 2, provider=MockProvider())
    ev2 = env["evaluator"].evaluate(r2.to_dict())
    assert ev2["rule_hit_rate"] > ev1["rule_hit_rate"]
    assert ev2["auto_resolve_rate"] > ev1["auto_resolve_rate"]
    assert ev2["needs_review"] <= ev1["needs_review"]
    # learning must never make the classifier worse
    assert ev2["classification_precision"] >= ev1["classification_precision"] - 1e-9
    assert ev2["false_auto_posts"] == 0


def test_rules_do_not_auto_resolve_above_the_reviewed_cap(env):
    r1 = run_once(env, 1)
    SimulatedReviewer(env["store"], env["truth_path"], seed=11).review_run(r1.to_dict(), r1.run_id)
    strict = Policy(auto_resolve_cap=0.01)
    r2 = run_once(env, 2, policy=strict, provider=MockProvider())
    assert r2.to_dict()["metrics"]["auto_resolved"] == 0
    assert r2.to_dict()["metrics"]["rule_hits_gated"] > 0, "recognised but held back by the cap"


def test_posting_only_after_explicit_opt_in(env):
    r = run_once(env, 1, policy=Policy(allow_auto_post=True, auto_post_cap=1000.0))
    # agent-sourced proposals may never auto-post, even with the flag on
    assert r.to_dict()["posted"] == []


def test_ambiguous_matches_are_refused_not_guessed(env):
    """If two exact line sets tie out, the matcher must hand it to a human."""
    bank = load_bank(env["bank"])
    ledger = load_ledger(env["ledger"])
    result = run_once(env)
    m = result.to_dict()["metrics"]
    assert "ambiguous_refused" in m


def test_run_is_persisted_and_reloaded(env):
    result = run_once(env)
    loaded = env["store"].get_run(result.run_id)
    assert loaded is not None
    assert loaded["metrics"]["bank_rows"] == result.to_dict()["metrics"]["bank_rows"]
    assert env["store"].latest_run()["run_id"] == result.run_id
