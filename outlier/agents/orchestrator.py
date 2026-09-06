"""The orchestrator.

Owns one reconciliation run end to end:

    ingest -> deterministic match -> learned-rule auto-resolve
           -> exception analyst -> critic -> policy gate -> review queue
           -> (optional) post -> metrics -> audit trail -> persist

The orchestrator is the only component allowed to write to the ledger, and it
will only do so after the policy gate says yes. That single choke point is
what makes the "0 unattended auto-posts" claim checkable rather than a slogan.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Policy
from ..ingest import load_bank, load_ledger
from ..ledger import Ledger, fx_variance_entry
from ..llm import BaseProvider
from ..matcher import Matcher, MatcherConfig
from ..models import (
    ALL_CATEGORIES,
    BankTxn,
    ExceptionRecord,
    LedgerEntry,
    ProposedEntry,
    RunResult,
    money,
    next_id,
)
from ..store import Store
from .critic import CriticAgent
from .exception_analyst import ExceptionAnalyst
from .tools import build_tools, signature_for


class Orchestrator:
    def __init__(
        self,
        provider: BaseProvider,
        store: Store,
        policy: Policy | None = None,
        matcher_cfg: MatcherConfig | None = None,
    ):
        self.provider = provider
        self.store = store
        self.policy = policy or Policy()
        self.matcher_cfg = matcher_cfg or MatcherConfig()

    # ------------------------------------------------------------------
    def run(
        self,
        bank_file: str | Path,
        ledger_file: str | Path,
        run_id: str | None = None,
        round_no: int = 1,
        auto_apply_rules: bool = True,
        post_approved: bool = False,
    ) -> RunResult:
        t0 = time.time()
        run_id = run_id or f"RUN-{datetime.now(UTC):%Y%m%d-%H%M%S}"
        bank = load_bank(bank_file)
        ledger = load_ledger(ledger_file)
        bank = [t for t in bank if t.amount != 0 or t.bank_code == "PARSE_ERROR"]

        self.store.audit(run_id, "orchestrator", "run_started", "run", run_id,
                         bank_file=str(bank_file), ledger_file=str(ledger_file),
                         bank_rows=len(bank), ledger_rows=len(ledger))

        tools = build_tools(bank, ledger, self.store.lookup)
        analyst = ExceptionAnalyst(self.provider, tools)
        critic = CriticAgent(self.provider, tools)

        # ---------------- 1. deterministic matching --------------------
        matcher = Matcher(bank, ledger, self.matcher_cfg)
        report = matcher.run()
        if self.policy.review_multi_line_matches:
            for match in report.matches:
                if match.is_multi:
                    match.requires_review = True
                    self.store.audit(
                        run_id,
                        "guardrail",
                        "multi_line_match_requires_review",
                        "match",
                        match.match_id,
                        bank=match.bank_txn_ids,
                        ledger=match.ledger_entry_ids,
                    )
        for m in report.matches:
            self.store.audit(run_id, "deterministic", f"match_{m.match_type}", "match", m.match_id,
                             bank=m.bank_txn_ids, ledger=m.ledger_entry_ids, confidence=m.confidence)

        # ---------------- 2. exceptions --------------------------------
        exceptions: list[ExceptionRecord] = []
        proposals: list[ProposedEntry] = []
        rules_used: list[str] = []
        auto_resolved = 0
        rule_hits_gated = 0

        # ambiguous multi-line candidates are surfaced, never guessed at
        ambiguous_bank_ids = {a.get("txn_id") for a in report.ambiguous if a.get("txn_id")}
        for txn in report.unmatched_bank:
            if txn.txn_id in ambiguous_bank_ids:
                txn.bank_code = (txn.bank_code + " AMBIGUOUS_MULTI").strip()
        if report.ambiguous:
            self.store.audit(run_id, "guardrail", "ambiguous_match_refused", "match", "-",
                             count=len(report.ambiguous), detail=report.ambiguous[:5])

        work: list[tuple[BankTxn | None, LedgerEntry | None]] = [(t, None) for t in report.unmatched_bank]
        work += [(None, e) for e in report.unmatched_ledger]

        # A match is not always the end of the story. A reference tie-out with a
        # variance still needs a journal entry to clear the bank, so it gets an
        # exception record and a proposed entry like anything else.
        variance_matches = [m for m in report.matches if m.match_type == "fx_variance"]
        bank_by_id = {t.txn_id: t for t in bank}
        ledger_by_id = {e.entry_id: e for e in ledger}
        for m in variance_matches:
            txn = bank_by_id.get(m.bank_txn_ids[0])
            entry = ledger_by_id.get(m.ledger_entry_ids[0])
            if txn is None or entry is None:
                continue
            exc_id, prop_id = next_id("EXC"), next_id("PROP")
            classification, proposal = analyst.analyze(txn, None, exc_id, prop_id)
            classification.pop("context", None)
            classification.pop("item", None)
            if proposal is not None:
                proposal.lines = fx_variance_entry(
                    txn.amount, entry.amount, f"FX variance on {txn.reference or txn.description}"
                )
                proposal.amount = money(m.residual)
                proposal.account_code = "6700"
                proposal.rationale = (
                    f"Bank row {txn.txn_id} ties to GL {entry.entry_id} on reference {txn.reference or 'n/a'} but settled "
                    f"{m.residual:+.2f} away from the booked {entry.amount:.2f}. Posting the variance to 6700 FX Loss / Gain "
                    f"clears the bank line."
                )
            verdict = critic.review(classification, proposal, {"description": txn.description, "amount": m.residual,
                                                               "date": txn.date.isoformat(), "vendor": txn.counterparty,
                                                               "counterparty": txn.counterparty, "bank_code": "FX_VAR", "side": "bank"})
            self.store.audit(run_id, "agent:exception_analyst", "classified", "exception", exc_id,
                             category=classification["category"], confidence=classification["confidence"],
                             account_code=classification["account_code"], resolution=classification["resolution"],
                             note="variance on a deterministic match")
            self.store.audit(run_id, "agent:critic", "reviewed", "exception", exc_id,
                             verdict=verdict["verdict"], issues=verdict["issues"])
            if proposal is not None:
                proposal.critic_verdict = verdict["verdict"]
                proposal.critic_notes = verdict["issues"]
                proposals.append(proposal)
            exceptions.append(
                ExceptionRecord(
                    exception_id=exc_id,
                    bank_txn_ids=[txn.txn_id],
                    category="fx",
                    confidence=0.9,
                    explanation=f"Matched to GL {entry.entry_id} on reference, but the bank settled {m.residual:+.2f} "
                                f"away from the booked amount. Needs an FX variance entry.",
                    amount=m.residual,
                    date=txn.date.isoformat(),
                    description=txn.description,
                    evidence={
                        "signature": signature_for(txn),
                        "side": "bank",
                        "matched": True,
                        "match_id": m.match_id,
                        "matched_ledger": entry.entry_id,
                        "gross_amount": txn.amount,
                        "booked_amount": entry.amount,
                        "resolution": "journal_entry",
                        "critic": verdict,
                        "review_reason": "deterministic match with a variance; posting needs a human",
                    },
                    proposal_id=proposal.proposal_id if proposal else None,
                    resolution="OPEN",
                    needs_review=True,
                )
            )
            self.store.audit(run_id, "deterministic", "variance_needs_entry", "match", m.match_id,
                             residual=m.residual, exception=exc_id)

        for txn, entry in work:
            exc_id = next_id("EXC")
            prop_id = next_id("PROP")
            item_preview = analyst._item_from(txn, entry)
            signature = item_preview["signature"]  # amount-free pattern signature

            # 2a. analyst agent classifies first
            classification, proposal = analyst.analyze(txn, entry, exc_id, prop_id)
            ctx = classification.pop("context", {})
            item = classification.pop("item", item_preview)

            # 2b. critic agent reviews independently
            verdict = critic.review(classification, proposal, item)
            self.store.audit(run_id, "agent:exception_analyst", "classified", "exception", exc_id,
                             category=classification["category"], confidence=classification["confidence"],
                             account_code=classification["account_code"], resolution=classification["resolution"])
            self.store.audit(run_id, "agent:critic", "reviewed", "exception", exc_id,
                             verdict=verdict["verdict"], issues=verdict["issues"])

            if verdict["verdict"] == "FAIL" and proposal is not None:
                proposal = None  # critic vetoed the entry

            # 2c. a rule learned from a human decision, matched on THIS
            #     prediction's category. Running the analyst first is what makes
            #     the comparison possible: if a human approved "duplicate" for
            #     this pattern and the model says "timing" today, that is a
            #     disagreement for a human, not a shortcut.
            if auto_apply_rules:
                rule = self.store.get_rule(signature, category=classification["category"])
                if rule is not None:
                    ok, reason = self.policy.allows_auto_resolve(
                        classification["category"], float(item["amount"]), rule
                    )
                    if ok:
                        exc, prop = self._apply_rule(rule, txn, entry, exc_id, prop_id, run_id)
                        exceptions.append(exc)
                        if prop:
                            proposals.append(prop)
                        rules_used.append(rule.rule_id)
                        self.store.bump_rule(rule.rule_id)
                        auto_resolved += 1
                        continue
                    rule_hits_gated += 1
                    self.store.audit(run_id, "guardrail", "rule_gated_to_human", "rule", rule.rule_id,
                                     signature=signature, reason=reason, amount=float(item["amount"]),
                                     category=classification["category"])
                else:
                    any_rule = self.store.get_rule(signature)
                    if any_rule is not None:
                        rule_hits_gated += 1
                        self.store.audit(
                            run_id, "guardrail", "rule_category_disagreement", "rule", any_rule.rule_id,
                            signature=signature, approved=any_rule.payload.get("category"),
                            predicted=classification["category"],
                        )

            # 2d. policy gate
            needs_review, why = self.policy.needs_review(
                classification["category"], classification["confidence"], float(item["amount"]), False
            )
            if verdict["verdict"] in {"FAIL", "ESCALATE"}:
                needs_review, why = True, f"critic verdict {verdict['verdict']}: " + "; ".join(verdict["issues"][:2])
            if proposal is None and classification["resolution"] == "journal_entry":
                needs_review, why = True, "entry was vetoed by the critic; human must decide"

            exc = ExceptionRecord(
                exception_id=exc_id,
                bank_txn_ids=[item.get("txn_id")] if item.get("txn_id") else [item.get("entry_id", "")],
                category=classification["category"],
                confidence=classification["confidence"],
                explanation=classification["explanation"],
                amount=float(item["amount"]),
                date=item["date"],
                description=item.get("description", ""),
                evidence={
                    "signature": signature,
                    "side": item.get("side"),
                    "account_code": classification["account_code"],
                    "resolution": classification["resolution"],
                    "account_prior": ctx.get("account_prior"),
                    "vendor_history": {k: v for k, v in (ctx.get("vendor_history") or {}).items() if k in {"usual_account_code", "usual_account_name", "n_entries"}},
                    "fuzzy_candidates": (ctx.get("fuzzy_candidates") or [])[:3],
                    "critic": verdict,
                    "review_reason": why,
                    "degraded_model_output": classification.get("degraded", False),
                },
                proposal_id=proposal.proposal_id if proposal else None,
                resolution="OPEN",
                needs_review=needs_review,
                materiality_breach=abs(float(item["amount"])) > self.policy.materiality_limit,
            )
            if proposal is not None:
                proposal.critic_verdict = verdict["verdict"]
                proposal.critic_notes = verdict["issues"]
                if not needs_review:
                    proposal.status = "APPROVED"
                    proposal.source = "agent"
                proposals.append(proposal)
            exceptions.append(exc)

        # ---------------- 3. optional posting --------------------------
        posted: list[dict[str, Any]] = []
        ledger_db = Ledger.load(self.store.path.parent / "ledger.json")
        if post_approved:
            for prop in proposals:
                if prop.status != "APPROVED":
                    continue
                accounts = [l.account_code for l in prop.lines]
                ok, reason = self.policy.allows_auto_post(accounts, prop.amount, prop.source)
                if not ok:
                    self.store.audit(run_id, "guardrail", "auto_post_blocked", "proposal", prop.proposal_id, reason=reason)
                    continue
                je = ledger_db.post(prop, run_id, actor="agent:orchestrator")
                prop.status = "POSTED"
                posted.append(je)
                self.store.audit(run_id, "agent:orchestrator", "posted_je", "journal_entry", je["je_id"],
                                 proposal=prop.proposal_id, amount=prop.amount)

        # ---------------- 4. metrics + persist -------------------------
        metrics = self._metrics(report, exceptions, proposals, auto_resolved, posted, time.time() - t0,
                                rule_hits_gated)
        result = RunResult(
            run_id=run_id,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            round_no=round_no,
            bank_file=str(bank_file),
            ledger_file=str(ledger_file),
            config={
                "policy": self.policy.to_dict(),
                "matcher": {
                    "date_window_days": self.matcher_cfg.date_window_days,
                    "max_split_lines": self.matcher_cfg.max_split_lines,
                },
                "llm": self.provider.usage(),
                "auto_apply_rules": auto_apply_rules,
                "post_approved": post_approved,
            },
            metrics=metrics,
            matches=[m.to_dict() for m in report.matches],
            exceptions=[e.to_dict() for e in exceptions],
            proposals=[p.to_dict() for p in proposals],
            rules_used=rules_used,
            llm_calls=[c.to_dict() for c in self.provider.calls],
            posted=posted,
        )
        self.store.save_run(result)
        self.store.audit(run_id, "orchestrator", "run_completed", "run", run_id, **{k: v for k, v in metrics.items() if not isinstance(v, (dict, list))})
        return result

    # ------------------------------------------------------------------
    def _apply_rule(
        self, rule, txn: BankTxn | None, entry: LedgerEntry | None, exc_id: str, prop_id: str, run_id: str
    ) -> tuple[ExceptionRecord, ProposedEntry | None]:
        payload = rule.payload
        amount = float(txn.amount if txn is not None else entry.amount)
        prop: ProposedEntry | None = None
        if payload.get("resolution") == "journal_entry" and payload.get("lines"):
            from ..models import JournalLine

            source_lines = [JournalLine(**line) for line in payload["lines"]]
            base = max(sum(line.debit for line in source_lines), sum(line.credit for line in source_lines))
            scale = abs(amount) / base if base else 0.0
            lines = [
                JournalLine(
                    line.account_code,
                    line.account_name,
                    money(line.debit * scale),
                    money(line.credit * scale),
                    line.memo,
                )
                for line in source_lines
            ]
            target = money(abs(amount))
            for side in ("debit", "credit"):
                total = money(sum(getattr(line, side) for line in lines))
                delta = money(target - total)
                if abs(delta) > 0.0:
                    candidates = [line for line in lines if getattr(line, side) > 0]
                    if candidates:
                        line = candidates[-1]
                        setattr(line, side, money(getattr(line, side) + delta))
            prop = ProposedEntry(
                proposal_id=prop_id,
                exception_id=exc_id,
                lines=lines,
                rationale=f"Auto-resolved by approved rule {rule.rule_id} (approved by {rule.created_from} "
                f"from {rule.exception_id}).",
                amount=amount,
                account_code=payload.get("account_code", ""),
                status="APPROVED",
                source="learned_rule",
                critic_verdict="PASS",
                critic_notes=["learned rule exact-signature hit"],
            )
        exc = ExceptionRecord(
            exception_id=exc_id,
            bank_txn_ids=[txn.txn_id if txn else entry.entry_id],
            category=payload.get("category", "unknown"),
            confidence=payload.get("confidence", 0.9),
            explanation=f"Auto-resolved by approved rule {rule.rule_id}: {payload.get('explanation', '')}",
            amount=amount,
            date=(txn.date if txn else entry.date).isoformat(),
            description=(txn.description if txn else entry.description),
            evidence={
                "signature": rule.signature,
                "rule_id": rule.rule_id,
                "resolution": payload.get("resolution"),
                #: the evaluator and the review UI both need to know which side
                #: an item came from; omitting it silently misaligned the
                #: ledger-only ground truth (a bug we hit and fixed)
                "side": "bank" if txn is not None else "ledger",
                "account_code": payload.get("account_code", ""),
            },
            proposal_id=prop.proposal_id if prop else None,
            resolution="AUTO_RESOLVED",
            resolved_by=f"learned_rule:{rule.rule_id}",
            needs_review=False,
        )
        self.store.audit(run_id, f"rule:{rule.rule_id}", "auto_resolved", "exception", exc_id,
                         category=exc.category, amount=amount, signature=rule.signature)
        return exc, prop

    # ------------------------------------------------------------------
    def _metrics(
        self, report, exceptions, proposals, auto_resolved, posted, elapsed: float, rule_hits_gated: int = 0
    ) -> dict[str, Any]:
        by_cat: dict[str, int] = {c: 0 for c in ALL_CATEGORIES}
        for e in exceptions:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        usage = self.provider.usage()
        needs_review = [e for e in exceptions if e.needs_review]
        return {
            "elapsed_seconds": round(elapsed, 2),
            "bank_rows": report.n_bank,
            "ledger_rows": report.n_ledger,
            "matches": len(report.matches),
            "multi_line_matches": sum(1 for m in report.matches if m.is_multi),
            "ambiguous_refused": len(report.ambiguous),
            "match_steps": report.step_stats,
            "auto_match_rate_bank": round(report.bank_matched / report.n_bank, 4) if report.n_bank else 0.0,
            "auto_match_rate_ledger": round(report.ledger_matched / report.n_ledger, 4) if report.n_ledger else 0.0,
            "ledger_lines_matched": report.ledger_matched,
            "exceptions": len(exceptions),
            "exceptions_by_category": by_cat,
            "auto_resolved": auto_resolved,
            "auto_resolve_rate": round(auto_resolved / len(exceptions), 4) if exceptions else 0.0,
            #: patterns the system recognised from memory but a guardrail still
            #: sent to a human. Recognition and authority are different things.
            "rule_hits_gated": rule_hits_gated,
            "rule_hit_rate": round((auto_resolved + rule_hits_gated) / len(exceptions), 4) if exceptions else 0.0,
            "needs_review": len(needs_review),
            "review_queue_rate": round(len(needs_review) / len(exceptions), 4) if exceptions else 0.0,
            "proposals": len(proposals),
            "proposals_balanced": sum(1 for p in proposals if p.balanced),
            "unbalanced_proposals": sum(1 for p in proposals if not p.balanced),
            "critic_pass": sum(1 for p in proposals if p.critic_verdict == "PASS"),
            "critic_escalate": sum(1 for p in proposals if p.critic_verdict == "ESCALATE"),
            "posted_entries": len(posted),
            "unattended_auto_posts": len([p for p in posted]),  # see eval.false_auto_post_rate for the ground-truth check
            "llm_calls": usage["calls"],
            "llm_failed_calls": usage["failed_calls"],
            "llm_total_tokens": usage["total_tokens"],
            "rules_in_memory": self.store.rule_count(),
            "rules_used_this_run": auto_resolved,
        }
