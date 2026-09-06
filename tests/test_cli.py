"""CLI smoke tests.

Includes a regression for the flag-ordering trap: shared options used to be
registered only on the top-level parser, so ``outlier.py improve --reports out``
died with "unrecognized arguments" while ``outlier.py --reports out improve``
worked.
"""

import json

import pytest

from outlier.cli import build_parser, main


def test_shared_flags_parse_after_the_subcommand():
    args = build_parser().parse_args(
        ["improve", "--rounds", "2", "--reports", "out", "--db", "x.db", "--materiality", "10"]
    )
    assert args.cmd == "improve" and args.reports == "out" and args.db == "x.db"
    assert args.rounds == 2 and args.materiality == 10.0


def test_a_typo_in_a_subcommand_is_rejected_cleanly():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["recconcile"])
    assert exc.value.code == 2


def test_generate_writes_the_answer_key(tmp_path):
    out = tmp_path / "sample"
    assert main(["generate", "--out", str(out), "--size", "small"]) == 0
    for name in ("bank_statement.csv", "ledger_export.csv", "ground_truth.json", "generation_config.json"):
        assert (out / name).exists(), name
    truth = json.loads((out / "ground_truth.json").read_text())
    assert any(t["expected_category"] == "fraud_suspect" for t in truth)
    assert any(t["side"] == "ledger" for t in truth)


def test_run_then_review_then_report_end_to_end(tmp_path):
    sample = tmp_path / "sample"
    reports = tmp_path / "reports"
    db = str(tmp_path / "state.db")
    base = ["--db", db, "--reports", str(reports)]
    assert main(["generate", "--out", str(sample), "--size", "small", "--db", db]) == 0

    files = ["--bank", str(sample / "bank_statement.csv"),
             "--ledger", str(sample / "ledger_export.csv"),
             "--truth", str(sample / "ground_truth.json")]

    assert main(["run", *base, *files, "--run-id", "RUN-CLI"]) == 0
    assert (reports / "RUN-CLI_reconciliation_report.md").exists()
    assert (reports / "RUN-CLI_command_center.json").exists()
    assert (reports / "RUN-CLI_close_brief.md").exists()
    evaluation = json.loads((reports / "RUN-CLI_evaluation.json").read_text())
    assert evaluation["false_auto_posts"] == 0
    assert evaluation["match_recall"] > 0.8

    assert main(["review", *base, "--run", "RUN-CLI", "--truth", str(sample / "ground_truth.json")]) == 0
    assert main(["run", *base, *files, "--run-id", "RUN-CLI2", "--round", "2"]) == 0
    second = json.loads((reports / "RUN-CLI2_evaluation.json").read_text())
    assert second["auto_resolve_rate"] > evaluation["auto_resolve_rate"]

    assert main(["post", *base, "--run", "RUN-CLI"]) == 0
    assert main(["audit", *base, "--run", "RUN-CLI", "--limit", "10"]) == 0
    assert main(["report", *base, "--run", "RUN-CLI", "--truth", str(sample / "ground_truth.json")]) == 0


def test_improve_writes_the_improvement_chart(tmp_path):
    sample = tmp_path / "sample"
    reports = tmp_path / "reports"
    main(["generate", "--out", str(sample), "--size", "small", "--db", str(tmp_path / "s.db")])
    assert main([
        "improve", "--db", str(tmp_path / "s.db"), "--reports", str(reports),
        "--bank", str(sample / "bank_statement.csv"),
        "--ledger", str(sample / "ledger_export.csv"),
        "--truth", str(sample / "ground_truth.json"),
        "--rounds", "2",
    ]) == 0
    text = (reports / "IMPROVEMENT.md").read_text()
    assert "round_no" in text and "rule_hit_rate" in text
    rows = json.loads((reports / "improvement.json").read_text())
    assert rows[-1]["rule_hit_rate"] > rows[0]["rule_hit_rate"]


def test_learning_command_reads_persisted_summary(tmp_path, capsys):
    sample = tmp_path / "sample"
    reports = tmp_path / "reports"
    db = str(tmp_path / "state.db")
    main(["generate", "--out", str(sample), "--size", "small", "--db", db])
    assert main([
        "improve", "--db", db, "--reports", str(reports),
        "--bank", str(sample / "bank_statement.csv"),
        "--ledger", str(sample / "ledger_export.csv"),
        "--truth", str(sample / "ground_truth.json"),
        "--rounds", "2",
    ]) == 0

    assert main(["learning", "--db", db, "--reports", str(reports)]) == 0
    text = capsys.readouterr().out
    assert "learning rounds: 2" in text
    assert "false auto-posts: 0" in text
    assert "accuracy non-regressed: True" in text
    assert "accuracy " in text and "%" in text

    assert main(["learning", "--db", db, "--reports", str(reports), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_count"] == 2
    assert payload["controls"]["false_auto_posts"] == 0


def test_learning_command_on_an_empty_db_says_so_cleanly(tmp_path, capsys):
    db = str(tmp_path / "empty.db")
    assert main(["learning", "--db", db, "--reports", str(tmp_path / "reports")]) == 0
    assert "no recorded runs" in capsys.readouterr().out


def test_read_commands_fail_cleanly_with_no_run(tmp_path):
    db = str(tmp_path / "empty.db")
    base = ["--db", db, "--reports", str(tmp_path / "reports")]
    assert main(["report", *base]) == 1
    assert main(["review", *base]) == 1
    assert main(["post", *base]) == 1


def test_policy_and_coa_commands_run():
    assert main(["policy", "--materiality", "100"]) == 0
    assert main(["coa"]) == 0


def test_tools_command_prints_a_valid_schema(tmp_path):
    sample = tmp_path / "sample"
    main(["generate", "--out", str(sample), "--size", "small", "--db", str(tmp_path / "s.db")])
    assert main(["tools", "--db", str(tmp_path / "s.db"),
                 "--bank", str(sample / "bank_statement.csv"),
                 "--ledger", str(sample / "ledger_export.csv")]) == 0
