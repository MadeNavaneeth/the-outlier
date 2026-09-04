# CloseLoop — autonomous bank reconciliation with a human exception desk

**Track 2 · Autonomous Office of the CFO** — *Syndicate by Maximor / Agent Orchestrator*

Month-end bank reconciliation, automated end to end: ingest the bank statement and
the GL export, match what can be matched **deterministically**, send only the
residual to agents, classify every exception with evidence, draft the journal
entry, put the risky ones in front of a human, learn from what the human decides,
and produce an audit trail an auditor can follow.

> **Headline numbers (127 bank rows / 166 GL rows, planted anomalies, offline model):**
>
> | | |
> |---|---|
> | Auto-match rate (deterministic, no LLM) | **87.4 %** |
> | Match precision / recall / F1 vs ground truth | **1.000 / 1.000 / 1.000** |
> | Exception classification accuracy | **93.8 %** (macro-F1 0.981) |
> | Unbalanced journal entries reaching a human | **0** |
> | **False auto-posts** | **0** |
> | After one review pass: patterns recognised from memory | **0 % → 77.4 %** |
> | After one review pass: resolved with no human at all | **0 % → 25.8 %** (policy-capped) |
> | Human review queue | **31 → 23 items** |
>
> Reproduce with `make demo`.

---

## Why this workflow

Reconciliation is the close-blocker. A finance team pulls two files, matches
transactions, chases the 5–15 % that don't tie out, proposes adjusting entries,
and documents all of it for the auditors. It is tedious, it is error-prone, and
nothing else in the close can finish until it is done.

Two design decisions drive everything else:

1. **The LLM never does the easy matches.** Exact, reference, split, batch and
   duplicate-aware matching are deterministic code with an audit trail. Only the
   residual reaches an agent. That is what makes the match numbers defensible
   instead of "the model said so".
2. **Nothing posts without a human, by default.** `allow_auto_post` ships `False`.
   Auto-*resolution* exists, but only for an exact signature a human already
   approved, only below the amount that human actually looked at, and never for a
   fraud suspect. Recognition and authority are deliberately separate.

---

## Run it

Zero dependencies. Python 3.11+ and the standard library, nothing to install.

```bash
git clone <this repo> && cd closeloop

make demo          # generate data -> run -> review -> run -> run -> report
# or step by step:
python3 closeloop.py generate --out sample
python3 closeloop.py run                      # one reconciliation, scored vs ground truth
python3 closeloop.py improve --rounds 3       # the learning curve
python3 closeloop.py serve                    # the human review desk on :8000
python3 -m pytest -q                          # 81 tests
```

`make demo` prints the improvement table and writes `reports/`.

### Using a real model

The demo runs on a deterministic offline "model" so a judge can run it with no
keys. To use a real one (any OpenAI-compatible endpoint, including the
TensorMux inference gateway):

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://...      # optional
export CLOSELOOP_MODEL=gpt-4o-mini      # optional
python3 closeloop.py run --provider openai
```

Same prompts, same JSON schema, same guardrails — `closeloop/agents/*` never
branches on provider. Cost per run is recorded (`llm_total_tokens` in every
report) either way.

---

## What it actually does

```
bank_statement.csv ─┐
                    ├─► ingest ─► deterministic matcher ─┬─► matched (audit-trailed)
ledger_export.csv ──┘                                    │
                                                         └─► residual
                                                              │
                            learned rules (exact signature) ◄─┤
                                                              │
                                            exception analyst agent (tools: vendor
                                            history, chart of accounts, duplicate
                                            search, fuzzy candidates, prior decisions)
                                                              │
                                                    critic agent (independent re-check)
                                                              │
                                              policy gate (materiality, confidence,
                                              never-auto categories, caps)
                                                              │
                                    ┌─────────────────────────┴──────────────────────┐
                                    ▼                                                ▼
                          human review desk                              reconciling items / report
                     approve · re-code · reject
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                   post to mock GL      store approved rule ──► next month auto-resolves
```

### Deterministic passes (in order)

| Pass | What it does | Why it is safe |
|---|---|---|
| `exact` | amount + date window, **only when the pairing is unique** | ambiguity is refused, never guessed |
| `duplicate_aware` | two identical GL lines, one payment → match the earliest, flag the rest | grouped on (reference, amount), so an FX line can't be consumed as a duplicate |
| `reference` | shared invoice/PO number; amount must tie out, or the variance must be inside the FX ceiling | a reference alone is never enough |
| `split` | 1 bank row = sum of 2–4 GL lines (exact subset sum in integer cents) | pairs hashed, triples hashed, 4+ by pruned DFS under a node budget |
| `batch` | processor settlement = many small GL lines **on one GL date** | per-date search stops two settlements tying out against each other's lines |
| `near_amount` | cent drift only (≤ $0.05) | a loose tolerance here manufactures matches |

Two rules keep multi-line matching honest. Settlements sharing a GL date are
solved **jointly**, so no GL line is ever claimed twice (`_assign_disjoint`); and
if a multi-line set still ties out more than one way, the matcher records the
ambiguity and hands it to a human instead of picking one. Before the joint solve
that guardrail cost 9 matches on the 518-row dataset; it now costs 0, and match
recall is 1.000. The refusal path is still there and still tested — it is the
guardrail working, not the matcher failing.

### Agents

* **Exception analyst** — classifies the residual into `timing · missing_entry ·
  duplicate · fee · fx · fraud_suspect · unknown` with a calibrated confidence and
  a plain-English explanation, and drafts the journal entry. Its output is
  **coerced back into the allowed value space in code**: unknown categories become
  `unknown`, unknown accounts fall back to the tool-derived prior, timing
  differences can never produce a posting, fraud suspects can never be actioned.
* **Critic** — a separate agent with a separate prompt that tries to break the
  proposal: debits = credits, real accounts, correct side for the account type,
  amount direction, account agrees with vendor history, explanation actually
  supports the category. `FAIL` drops the entry, `ESCALATE` sends it to a human.
* **Orchestrator** — the only component allowed to write to the ledger, and only
  through the policy gate.

Prompts and tool schemas: [`docs/PROMPTS.md`](docs/PROMPTS.md).
Architecture and failure log: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Guardrails (the part finance actually cares about)

```
materiality_limit        2500.00    anything above always goes to a human
confidence_threshold     0.70       below this -> human review
auto_resolve_cap          500.00    and never above the amount a human actually reviewed
allow_auto_post          False      unattended posting is OFF by default
auto_post_cap             0.00
never_auto_categories    fraud_suspect
never_auto_post_accounts 2300, 6700, 2000   (suspense, FX, AP)
```

`python3 closeloop.py policy` prints the resolved set. Every override is
audit-logged with the reason:

```
guardrail  rule_gated_to_human      amount 6578.86 exceeds the 500.00 cap a human actually reviewed
guardrail  rule_category_disagreement  approved=duplicate predicted=timing
guardrail  auto_post_blocked        allow_auto_post is disabled (default policy)
```

`python3 closeloop.py improve --high-trust` re-runs the same month with the
auto-resolve cap at 50,000 so you can see exactly what the guardrail is holding
back. It is a controller's dial, not a hardcoded behaviour.

---

## Measurable results

The dataset generator plants the anomalies and writes an answer key
(`ground_truth.json`) that the reconciler never reads. So accuracy is measured,
not asserted.

```bash
python3 closeloop.py generate --out sample            # 127 bank / 166 GL rows
python3 closeloop.py generate --out big --size large  # 518 bank / 644 GL rows
```

Planted mix: clean 1:1, split remittances, processor batch settlements,
outstanding cheques, deposits in transit, bank fees, FX variances, duplicated GL
lines, unexplained debits, bank interest and reversals.

| Metric | small (127 rows) | large (518 rows) |
|---|---|---|
| Auto-match rate | 87.4 % | 90.5 % |
| Match precision / recall / F1 | 1.000 / 1.000 / 1.000 | 1.000 / 1.000 / 1.000 |
| Classification accuracy | 93.5 % | 96.0 % |
| Classification macro-F1 | 0.963 | 0.976 |
| Exceptions raised / scored | 31 / 31 | 101 / 101 |
| Recall on every exception class | 1.000 | 1.000 |
| Unbalanced proposals | 0 | 0 |
| **False auto-posts** | **0** | **0** |
| Ambiguous ties refused | 0 | 0 |
| Run time | 0.3 s | 2.2 s |

The remaining 4 % of classification error on the large set is a handful of
`fee`/`timing`/`duplicate` rows argued into a neighbouring category — precision
0.875–0.971, recall 1.000 on all six classes, and `missed_exceptions: []`.
Nothing is silently dropped; every exception reaches the review queue.

### The learning curve

`python3 closeloop.py improve --rounds 4` → `reports/IMPROVEMENT.md`

```
round 1: recognised   0.0%  auto-resolved   0.0%  queue  31  accuracy  93.5%  rules   0
round 2: recognised  74.2%  auto-resolved  25.8%  queue  23  accuracy  93.5%  rules  21
round 3: recognised  77.4%  auto-resolved  25.8%  queue  23  accuracy  93.5%  rules  22
round 4: recognised  77.4%  auto-resolved  25.8%  queue  23  accuracy  93.5%  rules  22
```

Two things to notice. Recognition climbs and the queue shrinks. Accuracy **never
regresses** — an earlier version of this did regress, because a rule learned for
a duplicate overrode a correct timing classification; that bug and its fix are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#things-we-got-wrong-first).

Manual baseline for the same month: 31 exceptions × ~9 min of controller time
≈ **4.7 hours**, versus 0.3 s of compute and 23 items to click through.

---

## The review desk

```bash
python3 closeloop.py serve        # http://localhost:8000
```

Five tabs: **Review queue** (evidence, critic verdict, drafted entry, approve /
re-code / reject), **Auto-resolved** (what the rules handled with no human),
**Matches needing sign-off** (multi-line sets and FX variances the matcher
flagged), **Learned rules** (signature, category, account, cap, hit count, who
approved it), **Audit trail**.

Approving an item writes a rule; re-coding writes the corrected account into the
rule. Rejecting writes nothing. Posting to the GL is a separate, explicit button.

The HTTP layer is `http.server` with inline CSS/JS — no build step, no CDN. The
JSON endpoints (`/api/summary`, `/api/exceptions`, `/api/decide`, `/api/post`,
`/api/rules`, `/api/audit`) are the same ones the CLI's simulated reviewer uses.

---

## Repository

```
closeloop/
  cli.py                  every command
  ingest.py               bank CSV / text-PDF / GL CSV -> typed rows
  synthetic.py            dataset generator + planted ground truth
  matcher.py              the deterministic passes
  config.py               Policy: every guardrail in one place
  ledger.py               chart of accounts, journal entry builders, mock GL
  store.py                SQLite: rules (memory), audit trail, runs, decisions
  eval.py                 scoring against the answer key
  reviewer.py             simulated reviewer (for tests + the improvement chart)
  reporter.py             reconciliation report, improvement table, CSV exports
  agents/
    tools.py              tool registry + signatures + account priors
    exception_analyst.py  classify + draft
    critic.py             independent re-check
    orchestrator.py       the run, and the only writer to the ledger
  web/
    server.py             review-desk API (stdlib http.server)
    static.html           the UI, inline everything
tests/                    81 tests, `python3 -m pytest -q`
docs/
  ARCHITECTURE.md         design + the bugs we found and fixed
  PROMPTS.md              agent prompts and tool schemas
  DEMO_SCRIPT.md          the 3-minute video, shot list included
AO_LOG.md                 AO session log (build-process evidence)
```

## Tests

```bash
python3 -m pytest -q      # 77 passed
```

They cover the things that would be embarrassing to get wrong: an ambiguous 1:1
is never guessed; a duplicate pass can't consume an FX line; a rule can't widen
the cap a human reviewed; a fraud suspect can never auto-resolve; nothing posts
unattended; every proposal balances; learning never regresses accuracy; the
review-desk API validates the chart of accounts; unparseable statement lines are
surfaced rather than dropped.

## Not done / honest limitations

* **No real QuickBooks or Xero integration.** A mock ledger API is the right
  call for a 30-hour build and the brief says so explicitly.
* **PDF statement parsing is naive.** It handles a clean text export and reports
  unparseable lines as a counted exception rather than dropping them. Real OCR is
  a separate project.
* **Multi-line matching is exponential in the worst case**, bounded by a node
  budget and a candidate cap. On the 644-row set it costs 2.2 s; a million-row
  ledger would need a real solver.
* **The offline model is a heuristic stand-in.** It scores the same evidence a
  real model sees and injects seeded errors so the metrics are honest, but the
  numbers above are not a claim about GPT-4o. Run with `--provider openai` to get
  real ones; nothing else changes.

## License / team

See `AO_LOG.md` for team members and the AO session record.
