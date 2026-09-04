# Architecture, and the things we got wrong first

## Components

| Module | Responsibility | Writes anything? |
|---|---|---|
| `ingest.py` | bank CSV / text-PDF / GL CSV → typed rows; unparseable lines become a counted exception | no |
| `matcher.py` | six deterministic passes; records ambiguity instead of guessing | no |
| `agents/tools.py` | read-only tool registry, signatures, account priors | no |
| `agents/exception_analyst.py` | classify + explain + draft an entry; coerces model output into the allowed value space | no |
| `agents/critic.py` | independent re-check; hard checks first, model second | no |
| `agents/orchestrator.py` | sequences the run, applies the policy gate | **yes — the only writer** |
| `config.py` | `Policy`: every guardrail, in one file, printable | no |
| `store.py` | SQLite: rules, audit trail, runs, review decisions | yes (state) |
| `ledger.py` | chart of accounts, entry builders, mock GL | yes (postings) |
| `eval.py` | scoring against the planted answer key | no |
| `reviewer.py` | simulated reviewer: approves, re-codes, rejects, and learns | yes (decisions + rules) |
| `reporter.py` | reconciliation report, improvement table, CSV exports | reports only |
| `web/` | review desk (stdlib `http.server`, inline HTML/CSS/JS) | via `Api` |

## Data flow

```
                    ┌────────────────────────────────────────────────┐
                    │                  Orchestrator                  │
                    │  (only component that can write to the ledger) │
                    └────────────────────────────────────────────────┘
ingest ──► Matcher ──┬─► Match (audit-logged per pass)
                     ├─► variance matches ──────────────┐
                     ├─► ambiguous: REFUSED ────────────┤
                     └─► residual ──┬─► rule hit? ──────┤
                                    │      │            │
                                    │      ├─ policy OK ─► AUTO_RESOLVED (audit: rule:<id>)
                                    │      └─ gated ─────► queue (audit: rule_gated_to_human)
                                    │
                                    └─► ExceptionAnalyst ─► Critic ─► Policy ─┬─► queue
                                                                              └─► approved
                                                                                    │
                                                              human desk ──── approve / re-code / reject
                                                                                    │
                                                                        ┌───────────┴───────────┐
                                                                        ▼                       ▼
                                                                  Ledger.post            ApprovedRule
                                                                                          (next month)
```

Every arrow that changes state writes an `audit` row: actor, action, entity, and
the detail (including the *reason* a guardrail said no).

## Why deterministic-first

An LLM asked to match 166 GL lines against 127 bank rows will produce something
plausible and occasionally wrong, and you will not know which. Deterministic
passes produce a proof: these cents sum to those cents, on this date, with this
reference. The agent only ever sees the residual — 31 items instead of 293 — so
cost, latency and hallucination surface all shrink at once, and the match numbers
can be stated as fact.

## Why a critic agent instead of self-consistency

Asking the same agent to check itself mostly produces agreement. The critic has a
different prompt, a different job ("try to break it"), and — critically — a set of
**hard checks that run before the model is called**. An unbalanced entry or an
invented account code is a `FAIL` in code, not a matter of opinion.

## Why recognition ≠ authority

The improvement story is easy to fake: let rules auto-post everything and the
queue goes to zero. That is not a product, it is a liability. So the system
reports two numbers:

* **recognition rate** — how often a previously approved pattern was recognised
  (the learning)
* **auto-resolve rate** — how often it was allowed to act on it with no human
  (the policy)

On the sample month those are 77.4 % and 25.8 %. The gap *is* the guardrail, and
`improve --high-trust` lets a judge move the dial and watch the queue collapse.

---

## Things we got wrong first

Kept here deliberately — this is the debugging evidence, and each one is a test
now.

### 1. The split pass searched the wrong candidates

Ranked the pool by **largest** amount and took the top 6, then ran subset sum.
The real invoice lines are small, so they were never in the window. Auto-match
rate was 74 %.

*Fix:* group candidates by counterparty, rank by distance from the target, and
search pairs by hash, triples by hash, 4+ by pruned DFS under a node budget.
→ 84 %, and exact on the sample month.

### 2. The batch pass used a greedy sum

Greedy summing strands on overshoot: it takes a line that pushes past the target,
then can never come back. Worse, it will happily assemble a set that ties out
arithmetically but is drawn from two different settlements.

*Fix:* exact subset sum, and the pool is restricted to **one GL date** — a
processor settlement is booked on one day.

### 3. The reference pass matched at any amount

A shared invoice number was treated as sufficient, so FX-variance rows got
silently matched at the wrong amount and the variance disappeared from the
exception desk entirely. Five expected exceptions vanished.

*Fix:* a reference match must tie out exactly, or the variance must be inside the
FX ceiling — and when it is inside the ceiling the match is recorded as
`fx_variance`, flagged for review, **and still raises an exception with a variance
entry**, because the bank line cannot clear without one.

### 4. The near-amount pass manufactured a match

A ±$1.00 tolerance paired a bank fee with an unrelated GL line.

*Fix:* ±$0.05. Cent drift is real; a dollar is not drift.

### 5. The duplicate pass consumed the wrong line

Grouped duplicate candidates by **reference alone**, so a payment got matched to
an FX-variance line that happened to carry the same invoice number — a silently
wrong pairing, and the true duplicate went unflagged.

*Fix:* group on `(reference, amount)`. Vendor is not in the key either, because a
re-keyed duplicate usually loses its vendor name. Both are regression tests.

### 6. Learned rules keyed on the amount

The first signature included the amount bucket, so a $45 bank fee and a $38 bank
fee never shared a rule. Nothing generalised and the improvement chart was flat.

*Fix:* signature = normalised description + bank code + counterparty. Safety moved
into the guardrails (a rule only fires below the amount a human actually reviewed).

### 7. …which then broke classification accuracy

With amount-free signatures, `CDW CHEQUE INV-#####` and `INV-##### (DUPLICATE
ENTRY)` normalise to the same string. A rule learned for a **duplicate** started
overriding a correct **timing** classification, and accuracy *dropped* from 85.7 %
to 80.0 % as the system "learned".

*Fix:* the analyst runs **first**, and rule lookup is category-aware. If a human
approved `duplicate` for a pattern and the model says `timing` today, the run logs
`rule_category_disagreement` and sends it to a human. Accuracy now never
regresses across rounds.

### 8. Two bugs in the measurement, not the system

* Auto-resolved exceptions didn't record which side they came from, so
  ledger-only ground truth misaligned and the evaluator scored a correct
  classification as wrong.
* The evaluator treated a duplicated GL line's bank row as "should never match",
  so a correct `duplicate_aware` pairing was counted as a false positive.

Both fixed; `match_precision` on the sample month is now 1.000. Worth stating
plainly: some of the early "regressions" were the ruler, not the thing being
measured.

### 9. The evaluator scored ledger-only exceptions by position

Ground truth for ledger-side items (a cheque issued but never cashed, a
duplicated GL line) was aligned to raised exceptions **by order**. That is fine
while the counts agree — 10 truth rows and 10 exceptions on the sample month —
and silently wrong the moment they don't. On the 518-row month there were 71
ledger-side exceptions against 34 truth slots, so 37 fell out of the score
entirely and accuracy read 64.4 % while the system was actually right.

*Fix:* the generator now emits the GL `entry_id` on every ledger-only truth row
(`GroundTruthItem.entry_id`) and the evaluator keys on it
(`Evaluator.expected_by_entry`). **Every** raised exception is now scored —
`tests/test_ledger_llm_eval.py::test_every_raised_exception_is_scored` fails if a
truth row is ever dropped again. Accuracy on that month went 64.4 % → 96.0 % with
no change to the matcher.

### 10. The batch pass solved one settlement at a time

Each deposit picked its line run greedily, biggest first. The largest settlement
on a date took lines that belonged to another one — and because both sums still
tied out arithmetically, **nothing looked wrong**. The only symptom was match
recall quietly dropping to 0.933 on the 518-row month, with four settlement rows
(6–12 GL lines each) left unmatched.

*Fix:* per GL date, enumerate contiguous candidate runs for every deposit on that
date and solve them **jointly**, so no GL line is used twice
(`Matcher._assign_disjoint`). Recall 0.933 → 1.000.

### 11. Candidate enumeration consulted the availability map

`_contiguous_runs` filtered on `self.unmatched_ledger`, so which runs were even
*proposed* depended on what earlier passes had already consumed. Two deposits on
the same date then saw different candidate sets for the same lines, and the joint
solve could not reason correctly.

*Fix:* enumeration is a pure function of the pool and the target. Availability is
decided in one place only — the assignment.

### 12. The joint solver was modelled as bipartite matching. It is set packing.

Three attempts, in order, all wrong:

1. Greedy with a blocker set and manual rollback — settlements ended up
   **sharing** GL lines.
2. Kuhn-style augmenting paths keyed on one candidate per node — never tried a
   node's second option, so it abandoned a settlement that had a valid
   alternative run.
3. Augmenting paths over every `(deposit, run)` pair — "evicting" a node meant
   re-placing it on the very run it already held, which succeeded vacuously and
   silently dropped an already-matched settlement from the result.

The mistake underneath all three: a settlement's line run is **indivisible**. You
cannot move half of it out of the way, so "give way" can only ever mean "take a
different candidate run". That is set packing, not matching.

*Fix:* backtracking over deposits, most-constrained-first, picking the next
deposit dynamically and trying **every** unplaced one rather than only the most
constrained — otherwise the first deposit placed can never be the one that moves
out of the way. The answer is re-validated for disjointness and is the largest
packing found within the node budget, not a provable maximum; deposits that
don't fit go to the exception desk.

### 13. …and two of the tests written for it had numbers that don't work

The regression tests for #10 and #12 asserted a joint assignment for amounts
where **no joint assignment exists** (100 = 40+30+20+10 and 60 = 10+25+25 both
need the 10.00 line). The solver was right and the test was wrong — twice. Every
candidate run set in those tests is now brute-force checked for a disjoint
solution before anything is asserted, and a third test pins the no-solution case:
one match, one leftover, never a shared line.

---

## Known limits

* Multi-line matching is exponential in the worst case, bounded by
  `subset_sum_node_budget` (400k nodes) and `max_split_candidates` (80). 644 GL
  rows reconcile in 2.2 s. A million-row ledger needs a real solver.
* The joint settlement solve is the largest packing found within the node budget,
  not a provable maximum. On a pathological date it can leave a settlement
  unmatched that a slower search would have placed — it fails toward the
  exception desk, never toward a shared line.
* When two settlements land on the same GL date and their lines tie out more than
  one way, the matcher refuses and escalates (`ambiguous_refused`). That used to
  cost 9 matches on the 518-row month; after the joint solve in #10/#12 it is 0,
  and match recall is 1.000.
* PDF parsing is a line-regex, not OCR. Unparseable lines are reported as a
  counted exception rather than dropped.
* The offline model is a transparent heuristic that scores the same evidence a
  real model receives, with seeded errors so the metrics are honest. It is not a
  claim about any particular frontier model.
