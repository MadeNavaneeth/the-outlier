from outlier.agents.orchestrator import Orchestrator
from outlier.command_center import build_command_center, command_center_markdown
from outlier.config import Policy
from outlier.llm import MockProvider
from outlier.store import Store
from outlier.web.server import Api


def test_command_center_prioritizes_control_risk_and_exposes_factors():
    run = {
        "run_id": "RUN-CMD",
        "created_at": "2026-09-05T12:00:00+00:00",
        "config": {"policy": Policy(materiality_limit=1000).to_dict()},
        "exceptions": [
            {"exception_id": "EXC-FRAUD", "category": "fraud_suspect", "amount": -50,
             "date": "2026-09-04", "needs_review": True, "evidence": {"review_reason": "unexplained"}},
            {"exception_id": "EXC-FEE", "category": "fee", "amount": -120,
             "date": "2026-09-05", "needs_review": True, "evidence": {"resolution": "journal_entry"}},
        ],
    }
    center = build_command_center(run)
    assert center["actions"][0]["exception_id"] == "EXC-FRAUD"
    assert center["actions"][0]["owner_lane"] == "fraud"
    assert any(f["code"] == "control_risk" for f in center["actions"][0]["evidence"]["factors"])
    assert center["controls"]["posting_unchanged"] is True


def test_command_center_keeps_resolved_items_out_of_open_snapshot():
    run = {"run_id": "RUN-CMD", "created_at": "2026-09-05", "config": {"policy": Policy().to_dict()},
           "exceptions": [{"exception_id": "EXC-1", "category": "fee", "amount": -10, "date": "2026-09-05",
                            "needs_review": False, "evidence": {}}]}
    center = build_command_center(run)
    assert center["summary"]["open_actions"] == 0
    assert center["actions"][0]["status"] == "resolved"
    assert "Prioritized actions" in command_center_markdown(center)


def test_api_command_center_is_audited_once_across_restart(dataset, tmp_path):
    store = Store(tmp_path / "outlier.db")
    Orchestrator(MockProvider(), store, policy=Policy()).run(
        dataset / "bank_statement.csv", dataset / "ledger_export.csv", run_id="RUN-CMD-API", round_no=1
    )
    api = Api(tmp_path / "outlier.db")
    first = api.command_center()
    second = api.command_center()
    restarted = Api(tmp_path / "outlier.db")
    third = restarted.command_center()
    assert first["run_id"] == second["run_id"] == third["run_id"] == "RUN-CMD-API"
    events = [e for e in api.audit(limit=500) if e["action"] == "generated" and e["actor"] == "command_center"]
    assert len(events) == 1
