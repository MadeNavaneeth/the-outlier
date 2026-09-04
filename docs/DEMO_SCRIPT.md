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
> This is CloseLoop. It matches what can be matched deterministically, sends only
> the residual to agents, puts the risky items in front of a human, learns from
> what the human decides — and it never posts anything by itself."

**On screen:** `reports/RUN-COLD_reconciliation_report.md` scrolling.

---

## 0:20 – 0:50 · AO build timeline

> "Everything was built inside AO. Session 1 at kickoff scaffolded the repo and
> the synthetic data generator. Session 2 built the deterministic matcher. Session
> 3 wired the agents and the tool registry. Session 4 was failure analysis — we
> found the matcher was manufacturing matches, and this is where that got fixed.
> Session 5 added the review desk."

**On screen:** `AO_LOG.md`, then the AO session list. Say the session IDs out loud.

---

## 0:50 – 1:50 · Live run

```bash
python3 closeloop.py generate --out sample
python3 closeloop.py run --run-id RUN-COLD
```

> "127 bank rows, 166 GL rows, and I planted the anomalies — splits, batch
> settlements, outstanding cheques, bank fees, FX variances, duplicated GL lines,
> and two unexplained debits. The answer key is written to a file the reconciler
> never reads."

Read the numbers off the terminal:

> "87.4 percent matched with no model involved. Match precision and recall against
> the answer key: 1.000 and 1.000. Zero false pairings. 31 exceptions, every one
> classified, 93.8 percent correct. And the number I care about most: **zero
> false auto-posts** — nothing was posted, because auto-posting is off."

Then open the desk:

```bash
python3 closeloop.py serve
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

---

## 1:50 – 2:20 · The learning curve

```bash
python3 closeloop.py improve --rounds 4
```

> "Same month, four rounds, with a reviewer working the queue between them.
> Recognition goes from zero to 77 percent. Items resolved with no human at all go
> from zero to 26 percent. The queue drops from 31 to 23. And accuracy never goes
> down — which matters, because an earlier version of this *did* go down. A rule
> learned for a duplicate started overriding a correct timing classification. The
> fix is in the repo, and there's a test for it."

**On screen:** `reports/IMPROVEMENT.md`.

Then the dial:

```bash
python3 closeloop.py improve --rounds 3 --high-trust --reports reports/hightrust
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

**On screen:** `python3 closeloop.py audit --run RUN-COLD --verbose | head -40`,
then `docs/ARCHITECTURE.md` § "Things we got wrong first".

---

## 2:40 – 3:00 · Architecture and guardrails

> "Six deterministic passes, an analyst agent that classifies and drafts, a critic
> agent whose only job is to break the proposal, and an orchestrator that is the
> only component allowed to write to the ledger. Everything risky is a policy
> number you can read: materiality 2500, confidence floor 0.70, auto-resolve only
> on an exact approved signature under 500, fraud suspects never automated,
> auto-posting off. Zero runtime dependencies — 63 tests, all passing."

**On screen:** `python3 closeloop.py policy`, then `python3 -m pytest -q`.

---

## Shot list

| # | Shot | Command |
|---|---|---|
| 1 | Report scrolling | open `reports/RUN-COLD_reconciliation_report.md` |
| 2 | AO sessions | AO UI + `AO_LOG.md` |
| 3 | Cold run numbers | `python3 closeloop.py run --run-id RUN-COLD` |
| 4 | Desk: approve / re-code / reject | `python3 closeloop.py serve` |
| 5 | Matches needing sign-off | desk tab |
| 6 | Audit trail | desk tab |
| 7 | Improvement table | `python3 closeloop.py improve --rounds 4` |
| 8 | High-trust dial | `… improve --rounds 3 --high-trust` |
| 9 | Trace / bug story | `python3 closeloop.py audit --run RUN-COLD --verbose` |
| 10 | Guardrails + tests | `closeloop.py policy` · `python3 -m pytest -q` |

## Prep

```bash
make clean && make demo     # warms data/ and reports/ so nothing is slow on camera
python3 closeloop.py serve  # leave it running
```

Record at 1080p, terminal font 14pt+, browser zoom 110 %. Submit 30+ minutes
before the deadline — Devpost uploads fail at the wire.
