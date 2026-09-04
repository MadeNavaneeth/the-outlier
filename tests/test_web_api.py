"""Review-desk API tests (no HTTP needed: the API object is testable directly)."""

import pytest

from closeloop.agents.orchestrator import Orchestrator
from closeloop.config import Policy
from closeloop.ledger import Ledger
from closeloop.llm import MockProvider
from closeloop.store import Store
from closeloop.web.server import Api


@pytest.fixture
def api(dataset, tmp_path):
    store = Store(tmp_path / "closeloop.db")
    orch = Orchestrator(MockProvider(), store, policy=Policy())
    orch.run(dataset / "bank_statement.csv", dataset / "ledger_export.csv", run_id="RUN-UI", round_no=1)
    return Api(tmp_path / "closeloop.db")


def test_summary_exposes_metrics_and_policy(api):
    s = api.summary()
    assert s["has_run"] and s["run_id"] == "RUN-UI"
    assert s["metrics"]["bank_rows"] > 0
    assert s["policy"]["allow_auto_post"] is False
    assert s["counts"]["queue"] > 0


def test_approving_creates_a_rule_that_survives(api):
    queue = [e for e in api.exceptions() if not e["decision"] and e["proposal"]]
    assert queue
    target = queue[0]
    res = api.decide(target["exception_id"], "approve")
    assert res["ok"] and res["rule_created"] is True
    assert any(r["signature"] == target["evidence"]["signature"] for r in api.rules())


def test_rejecting_creates_no_rule(api):
    queue = [e for e in api.exceptions() if not e["decision"] and e["proposal"]]
    res = api.decide(queue[0]["exception_id"], "reject")
    assert res["ok"] and res["rule_created"] is False
    assert api.summary()["counts"]["decided"] == 1


def test_recode_validates_the_chart_of_accounts(api):
    queue = [e for e in api.exceptions() if not e["decision"] and e["proposal"]]
    bad = api.decide(queue[0]["exception_id"], "edit", account_code="9999")
    assert not bad["ok"] and "chart of accounts" in bad["error"]
    good = api.decide(queue[0]["exception_id"], "edit", account_code="6100")
    assert good["ok"]
    lines = good["decision"]["proposal"]["lines"]
    assert any(l["account_code"] == "6100" for l in lines)
    assert round(sum(l["debit"] for l in lines), 2) == round(sum(l["credit"] for l in lines), 2)


def test_reconciling_items_cannot_be_approved_as_entries(api):
    timing = [e for e in api.exceptions() if not e["proposal"]]
    assert timing, "the dataset plants timing differences"
    res = api.decide(timing[0]["exception_id"], "approve")
    assert not res["ok"] and "no journal entry" in res["error"]


def test_posting_moves_the_approved_entries_to_the_gl(api, tmp_path):
    queue = [e for e in api.exceptions() if not e["decision"] and e["proposal"]]
    for e in queue[:3]:
        assert api.decide(e["exception_id"], "approve")["ok"]
    res = api.post()
    assert res["ok"] and res["posted"] == 3
    assert res["gl_entries"] == 3
    ledger = Ledger.load(tmp_path / "ledger.json")
    for je in ledger.entries:
        d = sum(l["debit"] for l in je["lines"])
        c = sum(l["credit"] for l in je["lines"])
        assert round(d, 2) == round(c, 2)


def test_audit_trail_records_the_human_decisions(api):
    queue = [e for e in api.exceptions() if not e["decision"] and e["proposal"]]
    api.decide(queue[0]["exception_id"], "approve")
    actions = [e["action"] for e in api.audit(limit=200)]
    assert "review_approved" in actions
    assert "rule_learned" in actions
