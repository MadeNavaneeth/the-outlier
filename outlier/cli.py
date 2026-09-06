"""Command line interface.

    python3 -m outlier generate --out sample
    python3 -m outlier run --bank ... --ledger ... --truth ...
    python3 -m outlier review --run RUN-ID            # simulated reviewer
    python3 -m outlier improve --rounds 3             # the money slide
    python3 -m outlier serve --port 8000              # review desk UI
  python3 -m outlier ask --question "..."           # ask a configured model directly
    python3 -m outlier report / audit / learning / tools / policy
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Policy
from .eval import Evaluator, improvement_table, load_truth
from .ledger import Ledger, chart_of_accounts
from .llm import MockProvider, get_provider
from .matcher import MatcherConfig
from .agents.orchestrator import Orchestrator
from .agents.tools import build_tools
from .reporter import improvement_markdown, write_run_artifacts
from .reviewer import SimulatedReviewer
from .store import Store
from .synthetic import GeneratorConfig, write_dataset
from .ingest import load_bank, load_ledger
from .learning import build_learning_summary


# ----------------------------------------------------------------------
def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _policy(args: argparse.Namespace) -> Policy:
    return Policy(
        materiality_limit=args.materiality,
        confidence_threshold=args.confidence,
        auto_resolve_cap=args.auto_resolve_cap,
        allow_auto_post=args.allow_auto_post,
        auto_post_cap=args.auto_post_cap,
        date_window_days=args.window,
    )


def _print_metric_block(ev: dict[str, Any]) -> None:
    rows = [
        ("auto-match rate (bank)", f"{ev['auto_match_rate_bank'] * 100:.1f}%"),
        ("match precision / recall / F1", f"{ev['match_precision']:.3f} / {ev['match_recall']:.3f} / {ev['match_f1']:.3f}"),
        ("classification accuracy", f"{ev['classification_precision'] * 100:.1f}%"),
        ("classification macro-F1", f"{ev['classification_macro_f1']:.3f}"),
        ("exceptions raised", ev["exceptions_raised"]),
        ("auto-resolved by rules", f"{ev['auto_resolved']} ({ev['auto_resolve_rate'] * 100:.1f}%)"),
        ("human review queue", f"{ev['needs_review']} ({ev['review_queue_rate'] * 100:.1f}%)"),
        ("false auto-posts", ev["false_auto_posts"]),
        ("unbalanced proposals", ev["unbalanced_proposals"]),
        ("rules in memory", ev["rules_in_memory"]),
        ("llm calls / tokens", f"{ev['llm_calls']} / {ev['llm_total_tokens']:,}"),
        ("elapsed", f"{ev['elapsed_seconds']}s"),
    ]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k.ljust(width)} : {v}")


def _persist_evaluation(store: Store, run_id: str, evaluation: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the learning-relevant evaluation fields with the persisted run."""
    fields = (
        "classification_precision",
        "false_auto_posts",
        "auto_resolve_rate",
        "rule_hit_rate",
        "needs_review",
        "rules_in_memory",
        "llm_total_tokens",
    )
    return store.update_run_metrics(run_id, {key: evaluation[key] for key in fields if key in evaluation})


# ----------------------------------------------------------------------
def cmd_generate(args: argparse.Namespace) -> int:
    cfg = GeneratorConfig(seed=args.seed)
    if args.size == "small":
        cfg = GeneratorConfig(n_clean=30, n_split=3, n_batch=2, n_timing_out=2, n_timing_in=2,
                              n_fee=3, n_fx=2, n_duplicate=2, n_fraud=1, n_chatter=1, seed=args.seed)
    elif args.size == "large":
        cfg = GeneratorConfig(n_clean=400, n_split=25, n_batch=12, n_timing_out=20, n_timing_in=14,
                              n_fee=22, n_fx=18, n_duplicate=14, n_fraud=5, n_chatter=8, seed=args.seed)
    data = write_dataset(args.out, cfg)
    print(f"wrote {args.out}/")
    print(f"  bank rows      : {len(data['bank'])}")
    print(f"  ledger rows    : {len(data['ledger'])}")
    print(f"  truth items    : {len(data['truth'])}")
    print(f"  planted anomalies: splits={cfg.n_split} batches={cfg.n_batch} timing={cfg.n_timing_in + cfg.n_timing_out} "
          f"fees={cfg.n_fee} fx={cfg.n_fx} duplicates={cfg.n_duplicate} fraud={cfg.n_fraud}")
    return 0


# ----------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    store = _store(args)
    provider = get_provider(args.provider)
    orch = Orchestrator(provider, store, policy=_policy(args),
                        matcher_cfg=MatcherConfig(date_window_days=args.window))
    result = orch.run(
        args.bank,
        args.ledger,
        run_id=args.run_id,
        round_no=args.round,
        auto_apply_rules=not args.no_rules,
        post_approved=args.post_approved,
    )
    run = result.to_dict()
    audit = store.audit_trail_json(result.run_id)

    ev = None
    if args.truth and Path(args.truth).exists():
        ev = Evaluator(load_truth(args.truth)).evaluate(run)
        persisted = _persist_evaluation(store, result.run_id, ev)
        if persisted is not None:
            run = persisted

    paths = write_run_artifacts(run, args.reports, ev, audit,
                                build_learning_summary(store.all_runs(), store.all_rules()))
    print(f"run {result.run_id}  round {result.round_no}  provider={provider.name}/{provider.model}")
    m = run["metrics"]
    print(f"  bank rows {m['bank_rows']}  ledger rows {m['ledger_rows']}")
    print(f"  matches {m['matches']}  auto-match rate {m['auto_match_rate_bank'] * 100:.1f}%  "
          f"exceptions {m['exceptions']}  queue {m['needs_review']}")
    if ev:
        print("  -- vs planted ground truth --")
        _print_metric_block(ev)
    for k, p in paths.items():
        print(f"  {k:<10} -> {p}")
    return 0


# ----------------------------------------------------------------------
def cmd_review(args: argparse.Namespace) -> int:
    store = _store(args)
    run = store.get_run(args.run) or store.latest_run()
    if not run:
        print("no run found; run `python3 -m outlier run` first", file=sys.stderr)
        return 1
    truth_path = args.truth
    if not truth_path:
        truth_path = str(Path(run["bank_file"]).parent / "ground_truth.json")
    reviewer = SimulatedReviewer(store, truth_path, seed=args.seed)
    decisions = reviewer.review_run(run, run["run_id"])
    print(f"reviewed run {run['run_id']}")
    for k, v in decisions.items():
        print(f"  {k:<14}: {v}")
    return 0


# ----------------------------------------------------------------------
def cmd_post(args: argparse.Namespace) -> int:
    """Post the approved journal entries for a run to the mock GL."""
    store = _store(args)
    run = store.get_run(args.run) or store.latest_run()
    if not run:
        print("no run found", file=sys.stderr)
        return 1
    ledger = Ledger.load(Path(args.db).parent / "ledger.json")
    decisions = store.decisions(run["run_id"])
    posted = 0
    skipped = 0
    for exc in run["exceptions"]:
        d = decisions.get(exc["exception_id"])
        if not d or d["status"] in {"REJECTED"}:
            continue
        prop = d.get("proposal")
        if not prop:
            continue
        from .models import JournalLine, ProposedEntry

        pe = ProposedEntry(
            proposal_id=prop["proposal_id"],
            exception_id=exc["exception_id"],
            lines=[JournalLine(**l) for l in prop["lines"]],
            rationale=prop.get("rationale", ""),
            amount=prop.get("amount", 0.0),
            account_code=prop.get("account_code", ""),
            source="human:simulated",
        )
        existing = ledger.find_by_proposal(pe.proposal_id)
        if existing is not None:
            skipped += 1
            store.audit(run["run_id"], "guardrail", "post_skipped_duplicate", "proposal",
                        pe.proposal_id, existing_je=existing["je_id"])
            continue
        je = ledger.post(pe, run["run_id"], actor="human:simulated")
        posted += 1
        store.audit(run["run_id"], "human:simulated", "posted_je", "journal_entry", je["je_id"],
                    proposal=pe.proposal_id, amount=pe.amount)
    print(f"posted {posted} journal entries; skipped {skipped} already-posted entries; "
          f"GL now has {ledger.count()} entries, "
          f"1000 balance {ledger.balance('1000'):,.2f}")
    return 0


# ----------------------------------------------------------------------
def cmd_improve(args: argparse.Namespace) -> int:
    """The demo loop: run -> human review -> run -> ... and chart the gains."""
    store = _store(args)
    policy = _policy(args)
    if getattr(args, "high_trust", False):
        policy.auto_resolve_cap = 50000.0
        print("high-trust mode: auto-resolve cap raised to 50,000 (a controller dial, not a default)")
    truth = load_truth(args.truth)
    evaluator = Evaluator(truth)
    rows: list[dict[str, Any]] = []
    reports: list[Path] = []

    for rnd in range(1, args.rounds + 1):
        # a fresh provider per round keeps the simulated model's behaviour
        # stable, so any change in the numbers comes from the learned rules
        provider = get_provider(args.provider)
        orch = Orchestrator(provider, store, policy=policy,
                            matcher_cfg=MatcherConfig(date_window_days=args.window))
        run = orch.run(args.bank, args.ledger, run_id=f"RUN-R{rnd}", round_no=rnd).to_dict()
        audit = store.audit_trail_json(run["run_id"])
        ev = evaluator.evaluate(run)
        persisted = _persist_evaluation(store, run["run_id"], ev)
        if persisted is not None:
            run = persisted
        paths = write_run_artifacts(run, args.reports, ev, audit,
                                    build_learning_summary(store.all_runs(), store.all_rules()))
        reports.append(paths["report"])
        rows.append(ev)
        print(f"round {rnd}: recognised {ev['rule_hit_rate'] * 100:5.1f}%  "
              f"auto-resolved {ev['auto_resolve_rate'] * 100:5.1f}%  queue {ev['needs_review']:3d}  "
              f"accuracy {ev['classification_precision'] * 100:5.1f}%  rules {ev['rules_in_memory']:3d}  "
              f"tokens {ev['llm_total_tokens']:7d}  false-auto-posts {ev['false_auto_posts']}")
        if rnd < args.rounds:
            reviewer = SimulatedReviewer(store, args.truth, seed=args.seed + rnd)
            decisions = reviewer.review_run(run, run["run_id"])
            print(f"          reviewer: approved {decisions['approved']}  edited {decisions['edited']}  "
                  f"rejected {decisions['rejected']}  rules learned {decisions['rules_created']}")

    out = Path(args.reports) / "IMPROVEMENT.md"
    out.write_text(improvement_markdown(rows))
    (Path(args.reports) / "improvement.json").write_text(json.dumps(improvement_table(rows), indent=2))
    print(f"\nwrote {out}")
    return 0


# ----------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    store = _store(args)
    run = store.get_run(args.run) or store.latest_run()
    if not run:
        print("no run found", file=sys.stderr)
        return 1
    audit = store.audit_trail_json(run["run_id"])
    ev = Evaluator(load_truth(args.truth)).evaluate(run) if args.truth and Path(args.truth).exists() else None
    paths = write_run_artifacts(run, args.reports, ev, audit,
                                build_learning_summary(store.all_runs(), store.all_rules()))
    print(paths["report"].read_text())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = _store(args)
    events = store.audit_events(args.run, limit=args.limit)
    for e in events:
        print(f"{e.seq:>5}  {e.ts}  {e.actor:<26} {e.action:<20} {e.entity_type:<14} {e.entity_id}")
        if args.verbose and e.detail:
            print(f"       {json.dumps(e.detail, default=str)[:160]}")
    return 0


def cmd_learning(args: argparse.Namespace) -> int:
    """Print the persisted, read-only learning-loop summary."""
    store = _store(args)
    summary = build_learning_summary(store.all_runs(), store.all_rules())
    if args.format == "json":
        print(json.dumps(summary, indent=2, default=str))
        return 0

    if not summary["has_runs"]:
        print("no recorded runs; run `python3 -m outlier improve` first")
        return 0

    print(f"learning rounds: {summary['run_count']}  "
          f"baseline={summary['baseline_run_id']}  latest={summary['latest_run_id']}")
    for row in summary["rounds"]:
        accuracy = (
            f"{row['classification_accuracy'] * 100:5.1f}%"
            if row["classification_accuracy"] is not None else "  n/a"
        )
        print(f"  round {row['round_no']}: recognition {row['recognition_rate'] * 100:5.1f}%  "
              f"auto-resolved {row['auto_resolve_rate'] * 100:5.1f}%  "
              f"queue {row['needs_review']:3d}  accuracy {accuracy}  "
              f"rules {row['rules_in_memory']:3d}  tokens {row['llm_total_tokens']:7d}")
    controls = summary["controls"]
    print(f"  memory rules: {summary['memory']['rules']}  "
          f"false auto-posts: {controls['false_auto_posts']}  "
          f"accuracy non-regressed: {controls['accuracy_non_regressed']}  "
          f"auto-post enabled: {controls['auto_post_enabled']}")
    for reflection in summary["reflections"]:
        print(f"  {reflection['title']}: {reflection['finding']}")
    print(f"  next step: {summary['next_step']}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    bank = load_bank(args.bank) if args.bank else []
    ledger = load_ledger(args.ledger) if args.ledger else []
    store = _store(args)
    reg = build_tools(bank, ledger, store.lookup)
    print(json.dumps(reg.spec(), indent=2))
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    print(json.dumps(_policy(args).to_dict(), indent=2))
    return 0


def cmd_coa(args: argparse.Namespace) -> int:
    for a in chart_of_accounts():
        print(f"{a['code']}  {a['name']:<32} {a['type']:<10} {a['normal_side']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import serve

    return serve(host=args.host, port=args.port, db=args.db, reports=args.reports)


def cmd_ask(args: argparse.Namespace) -> int:
    """Ask a configured live model: question in, answer out.

    Examples:
        python3 outlier.py ask --question "What is a suspense account?"
        python3 outlier.py ask --provider explabs --model gpt-6-astra --reasoning-effort max
        echo "Draft a memo for ..." | python3 outlier.py ask
        python3 outlier.py ask --system "You are a CFO." --question "Explain FX variance"
        python3 outlier.py ask --format json --question "Classify: bank fee -12.50 never booked"
    """
    question = (args.question or "").strip()
    if not question and not sys.stdin.isatty():
        question = sys.stdin.read().strip()
    if not question:
        try:
            question = input("ask gpt-6-astra> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nno question given", file=sys.stderr)
            return 1
    if not question:
        print("no question given; pass --question or pipe via stdin", file=sys.stderr)
        return 1
    provider = get_provider(args.provider)
    if isinstance(provider, MockProvider):
        print("mock provider cannot answer free-form questions; configure a live provider and use --provider ...",
              file=sys.stderr)
        return 1
    # Per-call overrides without touching env.
    if getattr(args, "model", None) and hasattr(provider, "model"):
        provider.model = args.model
    if getattr(args, "reasoning_effort", None) and hasattr(provider, "reasoning_effort"):
        provider.reasoning_effort = args.reasoning_effort
    system = args.system or "You are a helpful finance assistant. Answer concisely."
    if getattr(args, "format", "text") == "json":
        import json as _json

        out = provider.complete_json(
            system + " Reply with JSON only.",
            question,
            purpose="ask",
            fallback={},
        )
        if not out:
            print("ask failed: model returned no usable JSON", file=sys.stderr)
            return 1
        print(_json.dumps(out, indent=2, default=str))
        if getattr(args, "verbose", False):
            print(f"\n[provider={provider.name}/{getattr(provider, 'model', '?')} reasoning_effort="
                  f"{getattr(provider, 'reasoning_effort', '-')}]", file=sys.stderr)
        return 0
    try:
        answer = provider._raw(system, question)  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"ask failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(answer)
    if getattr(args, "verbose", False):
        print(f"\n[provider={provider.name}/{getattr(provider, 'model', '?')} reasoning_effort="
              f"{getattr(provider, 'reasoning_effort', '-')}]", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------
def _add_shared(sp: argparse.ArgumentParser, paths: bool = True, policy: bool = True) -> None:
    """Shared flags are registered on every subcommand.

    They used to live only on the top-level parser, which meant
    ``outlier.py improve --reports out`` failed with "unrecognized arguments"
    unless you knew to write ``outlier.py --reports out improve``. Nobody
    knows that, and it broke a command in our own demo script. Flags now go
    after the subcommand, which is the order everyone types.
    """
    if paths:
        sp.add_argument("--db", default="data/outlier.db", help="SQLite state file")
        sp.add_argument("--reports", default="reports", help="where reports are written")
    if policy:
        sp.add_argument("--materiality", type=float, default=2500.0,
                        help="anything above this always goes to a human")
        sp.add_argument("--confidence", type=float, default=0.70,
                        help="classification confidence floor")
        sp.add_argument("--auto-resolve-cap", type=float, default=500.0,
                        help="max amount a learned rule may resolve with no human")
        sp.add_argument("--allow-auto-post", action="store_true",
                        help="let approved learned rules post to the GL unattended")
        sp.add_argument("--auto-post-cap", type=float, default=0.0)
        sp.add_argument("--window", type=int, default=7, help="matching date window in days")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="outlier", description="Autonomous bank reconciliation + exception desk")
    sub = p.add_subparsers(dest="cmd", required=True)
    provider_choices = [
        "auto", "mock", "openai", "openrouter", "gemini", "anthropic", "claude",
        "custom", "explabs", "astra",
    ]

    g = sub.add_parser("generate", help="generate a synthetic month with planted anomalies")
    g.add_argument("--out", default="sample")
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--size", choices=["small", "default", "large"], default="default")
    _add_shared(g, policy=False)
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("run", help="run one reconciliation")
    r.add_argument("--bank", default="sample/bank_statement.csv")
    r.add_argument("--ledger", default="sample/ledger_export.csv")
    r.add_argument("--truth", default="sample/ground_truth.json")
    r.add_argument("--provider", default="auto", choices=provider_choices)
    r.add_argument("--run-id", default=None)
    r.add_argument("--round", type=int, default=1)
    r.add_argument("--no-rules", action="store_true", help="ignore learned rules (cold start)")
    r.add_argument("--post-approved", action="store_true", help="post agent-approved entries (policy still gates)")
    _add_shared(r)
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("review", help="apply simulated reviewer decisions to a run")
    v.add_argument("--run", default=None)
    v.add_argument("--truth", default=None)
    v.add_argument("--seed", type=int, default=99)
    _add_shared(v, policy=False)
    v.set_defaults(fn=cmd_review)

    po = sub.add_parser("post", help="post approved entries to the mock GL")
    po.add_argument("--run", default=None)
    _add_shared(po, policy=False)
    po.set_defaults(fn=cmd_post)

    im = sub.add_parser("improve", help="run -> review -> run, N rounds, write the improvement chart")
    im.add_argument("--bank", default="sample/bank_statement.csv")
    im.add_argument("--ledger", default="sample/ledger_export.csv")
    im.add_argument("--truth", default="sample/ground_truth.json")
    im.add_argument("--rounds", type=int, default=3)
    im.add_argument("--provider", default="auto", choices=provider_choices)
    im.add_argument("--seed", type=int, default=99)
    im.add_argument("--high-trust", action="store_true",
                    help="raise the auto-resolve cap to 50000 to show what the guardrail is holding back")
    _add_shared(im)
    im.set_defaults(fn=cmd_improve)

    rp = sub.add_parser("report", help="re-render the report for a run")
    rp.add_argument("--run", default=None)
    rp.add_argument("--truth", default=None)
    _add_shared(rp, policy=False)
    rp.set_defaults(fn=cmd_report)

    a = sub.add_parser("audit", help="print the audit trail")
    a.add_argument("--run", default=None)
    a.add_argument("--limit", type=int, default=60)
    a.add_argument("--verbose", action="store_true")
    _add_shared(a, policy=False)
    a.set_defaults(fn=cmd_audit)

    le = sub.add_parser("learning", help="print the persisted learning-loop summary")
    le.add_argument("--format", default="text", choices=["text", "json"],
                     help="text prints a judge-friendly summary; json prints the full contract")
    _add_shared(le, policy=False)
    le.set_defaults(fn=cmd_learning)

    t = sub.add_parser("tools", help="print the agent tool schema")
    t.add_argument("--bank", default=None)
    t.add_argument("--ledger", default=None)
    _add_shared(t, policy=False)
    t.set_defaults(fn=cmd_tools)

    pl = sub.add_parser("policy", help="print the resolved guardrail policy")
    _add_shared(pl, paths=False)
    pl.set_defaults(fn=cmd_policy)

    c = sub.add_parser("coa", help="print the chart of accounts")
    _add_shared(c, paths=False, policy=False)
    c.set_defaults(fn=cmd_coa)

    s = sub.add_parser("serve", help="serve the human review desk")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    _add_shared(s, policy=False)
    s.set_defaults(fn=cmd_serve)

    k = sub.add_parser("ask", help="ask a configured model directly")
    k.add_argument("--question", default=None, help="question text; else reads stdin or prompts")
    k.add_argument("--system", default=None, help="system prompt override")
    k.add_argument("--provider", default="auto", choices=provider_choices)
    k.add_argument("--model", default=None, help="override model, e.g. gpt-6-astra")
    k.add_argument("--reasoning-effort", default=None, help="override, e.g. max")
    k.add_argument("--format", default="text", choices=["text", "json"],
                   help="text prints the raw answer; json parses it through the agent JSON contract")
    k.add_argument("--verbose", action="store_true", help="print provider/model to stderr")
    _add_shared(k, paths=False, policy=False)
    k.set_defaults(fn=cmd_ask)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
