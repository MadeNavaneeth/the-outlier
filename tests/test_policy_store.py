"""Guardrails and the learned-rule store."""

from closeloop.config import Policy
from closeloop.models import ApprovedRule
from closeloop.store import Store


def rule(sig="FEE|FEE|BANK", category="fee", cap=500.0, status="human_approved"):
    return ApprovedRule(
        rule_id="RULE-X", kind="classification", signature=sig,
        payload={"category": category, "amount_cap": cap, "approved_status": status,
                 "resolution": "journal_entry", "account_code": "6100", "lines": []},
        created_from="human:desk", exception_id="EXC-1",
    )


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

def test_auto_resolve_needs_a_rule():
    ok, why = Policy().allows_auto_resolve("fee", 10.0, None)
    assert not ok and "no approved rule" in why


def test_auto_resolve_blocks_fraud_even_with_a_rule():
    ok, why = Policy().allows_auto_resolve("fraud_suspect", 10.0, rule(category="fraud_suspect"))
    assert not ok and "never-auto" in why


def test_auto_resolve_blocks_amounts_above_the_reviewed_cap():
    ok, why = Policy().allows_auto_resolve("fee", 900.0, rule(cap=500.0))
    assert not ok and "exceeds" in why


def test_auto_resolve_uses_the_narrower_of_policy_and_rule_cap():
    p = Policy(auto_resolve_cap=5000.0)
    ok, _ = p.allows_auto_resolve("fee", 900.0, rule(cap=500.0))
    assert not ok, "a rule may never widen the cap a human actually reviewed"


def test_auto_resolve_blocks_unapproved_rules():
    ok, why = Policy().allows_auto_resolve("fee", 10.0, rule(status="agent_guess"))
    assert not ok and "human approval" in why


def test_auto_resolve_blocks_category_disagreement():
    ok, why = Policy().allows_auto_resolve("timing", 10.0, rule(category="duplicate"))
    assert not ok and "disagreement" in why


def test_auto_post_is_off_by_default():
    ok, why = Policy().allows_auto_post(["6100", "1000"], 10.0, "learned_rule")
    assert not ok and "disabled" in why


def test_auto_post_respects_the_never_auto_post_accounts():
    p = Policy(allow_auto_post=True, auto_post_cap=1000.0)
    ok, why = p.allows_auto_post(["2300", "1000"], 10.0, "learned_rule")
    assert not ok and "never-auto-post" in why
    ok, _ = p.allows_auto_post(["6100", "1000"], 10.0, "learned_rule")
    assert ok


def test_auto_post_refuses_non_rule_sources():
    p = Policy(allow_auto_post=True, auto_post_cap=1000.0)
    ok, why = p.allows_auto_post(["6100", "1000"], 10.0, "agent")
    assert not ok and "learned rules" in why


def test_materiality_always_forces_review():
    needs, why = Policy(materiality_limit=2500.0).needs_review("fee", 0.99, 9000.0, True)
    assert needs and "materiality" in why


def test_low_confidence_forces_review():
    needs, why = Policy().needs_review("fee", 0.4, 10.0, False)
    assert needs and "confidence" in why


def test_first_time_patterns_always_need_review():
    needs, why = Policy().needs_review("fee", 0.95, 10.0, False)
    assert needs and "first-time" in why


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def test_rule_lookup_is_category_aware(tmp_path):
    """Regression: digit-stripped signatures collide. A 'duplicate' rule must
    not be returned for an item the system predicts as 'timing'."""
    s = Store(tmp_path / "t.db")
    s.add_rule(rule(sig="INVDUPLICATEENTRY|LEDGERONLY|", category="duplicate"))
    assert s.get_rule("INVDUPLICATEENTRY|LEDGERONLY|", category="duplicate") is not None
    assert s.get_rule("INVDUPLICATEENTRY|LEDGERONLY|", category="timing") is None
    assert s.get_rule("INVDUPLICATEENTRY|LEDGERONLY|") is not None  # no category filter


def test_rule_hits_are_counted(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_rule(rule())
    s.bump_rule("RULE-X")
    s.bump_rule("RULE-X")
    assert s.get_rule("FEE|FEE|BANK").hits == 2


def test_audit_trail_is_ordered_and_filterable(tmp_path):
    s = Store(tmp_path / "t.db")
    s.audit("R1", "agent:critic", "reviewed", "exception", "E1", verdict="PASS")
    s.audit("R1", "human:desk", "review_approved", "exception", "E1")
    s.audit("R2", "orchestrator", "run_started", "run", "R2")
    assert len(s.audit_events("R1")) == 2
    assert len(s.audit_events("R2")) == 1
    assert [e.seq for e in s.audit_events("R1")] == sorted(e.seq for e in s.audit_events("R1"))
    trail = s.audit_trail_json("R1")
    assert trail[0]["detail"]["verdict"] == "PASS"


def test_decisions_round_trip(tmp_path):
    s = Store(tmp_path / "t.db")
    s.save_decision("R1", "E1", {"status": "APPROVED", "by": "human:desk"})
    assert s.decisions("R1")["E1"]["status"] == "APPROVED"
    assert s.decisions("R2") == {}
