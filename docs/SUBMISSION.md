# Devpost submission draft — The Outlier (Track 2)

Copy-paste source for the Devpost entry. Every number below reproduces from
this repo (`make demo` / `make large`, `--provider mock`). Bracketed items
are the only things left to fill at submit time.

## Project name

The Outlier — autonomous bank reconciliation with a human exception desk

## Tagline

Month-end bank rec that matches deterministically, explains every exception,
defers to a human on anything risky, and learns from each decision — with
zero unattended postings.

## Track

Track 2 — Autonomous Office of the CFO

## Problem + target users

Reconciliation is the close-blocker: a finance team pulls the bank statement
and the GL export, matches hundreds of rows, chases the 5–15 % that don't tie
out, drafts adjusting entries, and documents everything for auditors. It is
tedious, error-prone, and nothing else in the close finishes until it is done.
Target users: controllers and accountants at startups/SMBs doing month-end
close with small finance teams.

## What it does

Ingest bank + GL files → deterministic matching (exact, duplicate-aware,
reference, split, batch, near-amount — the LLM never sees easy cases) →
exception analyst agent classifies the residual with evidence and drafts the
journal entry → critic agent tries to break the proposal → policy gate
(materiality, confidence, caps, never-auto lists) → human review desk
(approve / re-code / reject) → explicit posting → approved decisions become
bounded memory rules for next month. Every state change lands in an
append-only audit trail.

## Agent architecture, tools, evaluation

- Analyst + critic agents over a read-only tool registry (ledger/bank search,
  fuzzy candidates, duplicate search, vendor history, chart of accounts,
  prior decisions). Only the orchestrator writes, only through policy.
- All prompts and schemas: `docs/PROMPTS.md`. Design + 13-item failure log:
  `docs/ARCHITECTURE.md`.
- The dataset generator plants anomalies and writes an answer key the
  reconciler never reads, so accuracy is measured, not asserted.

## Measurable results (verified, offline mock provider)

| Metric | small (127 bank / 166 GL) | large (518 bank / 644 GL) |
|---|---|---|
| Auto-match rate (deterministic) | 87.4 % | 90.5 % |
| Match precision / recall / F1 | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 |
| Classification accuracy | 93.5 % | 96.0 % |
| False auto-posts | 0 | 0 |
| Unbalanced proposals | 0 | 0 |

Learning curve (`improve --rounds 4`): recognition 0.0 % → 77.4 %,
auto-resolution 0.0 % → 25.8 % (policy-capped), queue 31 → 23, accuracy flat
at 93.5 % (never regresses — regression-covered). Manual baseline for the
same month: ~4.7 controller-hours vs seconds of compute. Suite: 120 tests,
all passing, zero runtime dependencies (stdlib only).

## AO usage

Built with Agent Orchestrator; see `AO_LOG.md` for the session record.
[UPDATE BEFORE SUBMITTING: list the worker sessions, branches merged, and
total session count shown in the demo video. State only verifiable IDs —
never invent session IDs or recordings.]

## Links

- GitHub: https://github.com/MadeNavaneeth/the-outlier
- Demo video (3–5 min, shows AO dashboard + session count): [VIDEO URL]

## Team

| Name | Devpost |
|---|---|
| Yadamreddy Navaneeth | @NTHDARKRIDER555 |
| [second member or delete this row — every member must register individually] | |
