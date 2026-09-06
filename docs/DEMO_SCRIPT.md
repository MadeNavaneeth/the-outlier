# Demo video script — 3 minutes

Record the terminal at 14pt+ and the browser side by side. Everything below is
one command; nothing is staged.

---

## 0:00 – 0:20 · The problem

> "Month-end bank reconciliation. Two files, a few hundred rows, and the 5 to 15
> percent that don't tie out. A controller spends hours chasing them, drafting
> adjusting entries, and writing it all down for the auditors. Nothing else in the
> close can finish until it's done.
>
> This is The Outlier. It matches what can be matched deterministically, sends only
> the residual to agents, puts the risky items in front of a human, learns from
> what the human decides — and it never posts anything by itself."

**On screen:** `reports/RUN-COLD_reconciliation_report.md` scrolling.

---

## 0:20 – 0:50 · AO build timeline

> "The repository was finalized through Agent Orchestrator. The earlier build
> sessions were not captured, so I will not claim they happened inside AO. The
> verified sessions created isolated worktrees for finalization and audit; the
> working system and its evidence are visible here in the repository."

**On screen:** `AO_LOG.md`, then the AO session list. Say the session IDs out loud.

---

## 0:50 – 1:50 · Live run

```bash
python3 outlier.py generate --out sample
python3 outlier.py run --run-id RUN-COLD --provider mock
```

`--provider mock` pins the deterministic offline model so the demo numbers are
reproducible even on a machine with live API keys set (otherwise `auto` picks
up any ambient key and hits the network).

> "127 bank rows, 166 GL rows, and I planted the anomalies — splits, batch
> settlements, outstanding cheques, bank fees, FX variances, duplicated GL lines,
> and two unexplained debits. The answer key is written to a file the reconciler
> never reads."

Read the numbers off the terminal:

> "87.4 percent matched with no model involved. Match precision and recall against
> the answer key: 1.000 and 1.000. Zero false pairings. 31 exceptions, every one
> classified, 93.5 percent correct. And the number I care about most: **zero
> false auto-posts** — nothing was posted, because auto-posting is off."

Then open the desk:

```bash
python3 outlier.py serve
```

Click through **one of each**:

1. **Approve** a bank fee → "Approved, and that just wrote a rule."
2. **Re-code** an item to a different account → "I disagree with the coding, so I
   re-code it. The correction is what gets stored."
3. **Reject** one → "Rejected. No rule, nothing posted."
4. Open **Matches needing sign-off** → "The matcher matched these, but a
   multi-line set or an FX variance always gets a human glance."
5. Open **Audit trail** → "Every one of those clicks, plus every agent call and
   every guardrail that said no."
6. Open **Close room** → "These objects are shortcuts into the real workflow: the
   desk, the memory shelf, the controls, and the matching wall. It is tactile
   presentation, not a fake game layer over accounting decisions."

---

## 1:50 – 2:20 · The learning curve

```bash
python3 outlier.py improve --rounds 4 --provider mock
```

> "Same month, four rounds, with a reviewer working the queue between them.
> Recognition goes from zero to 77 percent. Items resolved with no human at all go
> from zero to 26 percent. The queue drops from 31 to 23. And accuracy never goes
> down — which matters, because an earlier version of this *did* go down. A rule
> learned for a duplicate started overriding a correct timing classification. The
> fix is in the repo, and there's a test for it."

**On screen:** `reports/IMPROVEMENT.md`, then the desk's **Learning lab** tab.

> "This is the part I want a judge to inspect. The lab shows the memory growing,
> the queue shrinking, token usage per round, and the safety check that accuracy
> never regressed. The agent can improve, but the controller still owns the
> decision boundary."

If the browser is not available, use the read-only equivalent:

```bash
python3 outlier.py learning
```

Then the dial:

```bash
python3 outlier.py improve --rounds 3 --high-trust --reports reports/hightrust --provider mock
```

> "That's the same run with the auto-resolve cap raised to fifty thousand. The
> queue collapses. That gap between 26 percent and this is the guardrail, and it's
> a controller's decision, not a hardcoded behaviour."

---

## 2:20 – 2:40 · Traces: a bug we actually found

> "Here's the debugging story. The reference pass used to treat a shared invoice
> number as enough to match — so five FX variances got silently matched at the
> wrong amount and disappeared from the exception desk. We caught it because the
> eval said five expected exceptions were missing, not because anything looked
> broken. That's the trace."

**On screen:** `python3 outlier.py audit --run RUN-COLD --verbose | head -40`,
then `docs/ARCHITECTURE.md` § "Things we got wrong first".

---

## 2:40 – 3:00 · Architecture and guardrails

> "Six deterministic passes, an analyst agent that classifies and drafts, a critic
> agent whose only job is to break the proposal, and an orchestrator that is the
> only component allowed to write to the ledger. Everything risky is a policy
> number you can read: materiality 2500, confidence floor 0.70, auto-resolve only
> on an exact approved signature under 500, fraud suspects never automated,
> auto-posting off. Zero runtime dependencies — 120 tests, all passing."

**On screen:** `python3 outlier.py policy`, then `python3 -m pytest -q`.

---

## Optional · Live model (only if time allows)

If `EXPLABS_API_KEY` is set, show the same agents running on `gpt-6-astra`
instead of the offline stand-in — same prompts, same schema, same guardrails:

```bash
python3 outlier.py ask --provider explabs --question "What is a suspense account?"
python3 outlier.py ask --provider explabs --format json --question "Classify: bank fee -12.50 never booked"
```

> "Same pipeline, real model. Token usage lands in the report next to every
> other metric, so the cost claim is measured, not asserted."

---

## Shot list

| # | Shot | Command |
|---|---|---|
| 1 | Report scrolling | open `reports/RUN-COLD_reconciliation_report.md` |
| 2 | AO sessions | AO UI + `AO_LOG.md` |
| 3 | Cold run numbers | `python3 outlier.py run --run-id RUN-COLD --provider mock` |
| 4 | Desk: approve / re-code / reject | `python3 outlier.py serve` |
| 5 | Matches needing sign-off | desk tab |
| 6 | Audit trail | desk tab |
| 7 | Improvement table | `python3 outlier.py improve --rounds 4 --provider mock` |
| 8 | High-trust dial | `… improve --rounds 3 --high-trust` |
| 9 | Trace / bug story | `python3 outlier.py audit --run RUN-COLD --verbose` |
| 10 | Guardrails + tests | `outlier.py policy` · `python3 -m pytest -q` |
| 11 | (opt) live model | `outlier.py ask --provider explabs` |
| 12 | Learning lab + close room | `outlier.py learning` · desk tabs |

## Prep

```bash
make clean && make demo     # verifies everything, warms reports/ so nothing is slow
# Now reset to the exact on-camera starting state: the desk always shows the
# LATEST run, so it must open on RUN-COLD (31 queue, 0 rules) — not on RUN-R4.
rm -f data/outlier.db       # Windows: del data\outlier.db
python3 outlier.py generate --out sample
python3 outlier.py run --run-id RUN-COLD --provider mock
python3 outlier.py serve    # leave it running
```

Record the desk shots (approve / re-code / reject) BEFORE running `improve` —
`improve` adds rounds 1–4 and the desk will switch to showing RUN-R4.

Record at 1080p, terminal font 14pt+, browser zoom 110 %. Submit 30+ minutes
before the deadline — Devpost uploads fail at the wire.
