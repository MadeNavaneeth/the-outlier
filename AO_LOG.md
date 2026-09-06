# AO build log — The Outlier

Hackathon: **Syndicate by Maximor**, Track 2 — Autonomous Office of the CFO.
Team: **The Outlier**. Devpost submission not yet filled.
The final submission audit was run through AO after registering this
repository as the `The Outlier` project (legacy AO daemon name: `closeloop`;
verifiable session IDs below keep their original form).
The original build-session IDs and recordings were not captured, so this log does
not claim that the earlier implementation work was performed inside AO.

> **Submission note:** only verified AO session IDs are recorded below. Do not
> replace unavailable historical IDs or recording filenames with guesses.

---

## Where each judging criterion is evidenced

| Criterion (weight) | Evidence |
|---|---|
| AO Usage & Build Process (25 %) | Verified finalization sessions `closeloop-1` and `closeloop-2` below; historical build-session evidence is unavailable |
| Technical Execution & Reliability (25 %) | `python3 -m pytest -q` (120 passing) + `reports/RUN-COLD_evaluation.json` (`false_auto_posts: 0`, `missed_exceptions: []`) |
| Track Fit & Real-World Value (25 %) | Track 2 workflow: ingest → match → exceptions → human desk → post → audit; `reports/IMPROVEMENT.md` learning curve; guardrails in `python3 outlier.py policy` |
| Demo & Usability (15 %) | `docs/DEMO_SCRIPT.md` shot list + review desk (`python3 outlier.py serve`) |
| Innovation (10 %) | Deterministic-first matching (LLM never sees easy cases) + recognition/authority split + category-aware rule memory (`docs/ARCHITECTURE.md`) |

---

## Historical Session 1 — kickoff, scaffold, data
**AO session:** not captured  ·  **recording:** not captured

The repository history contains the scaffold and synthetic data generator, but
no AO session ID or recording was captured for this work. Do not present this as
verified AO evidence. The implementation includes the module layout, sign convention
(`amount > 0` = money in), the chart of accounts, and the synthetic dataset
generator with planted anomalies plus an answer key.

Decision made here that shaped everything: **write the ground truth at generation
time and never let the reconciler read it.** Without that there is no way to
report accuracy, and "measurable results" is a quarter of the judging criteria.

Outcome: `python3 outlier.py generate --out sample` → 127 bank rows, 166 GL rows,
planted splits / batches / timing / fees / FX / duplicates / fraud.

## Historical Session 2 — deterministic matcher
**AO session:** not captured  ·  **recording:** not captured

The repository contains the matching passes: exact, reference, split (subset sum in integer
cents), batch, near-amount. Baseline metrics: auto-match rate **74 %**, match
recall 0.70.

Recorded in AO at the end of the session: the split pass was searching the top 6
candidates by *largest* amount, so the real invoice lines were never in the
window.

## Historical Session 3 — agents, tools, critic
**AO session:** not captured  ·  **recording:** not captured

The repository contains the tool registry (all read-only), the exception analyst, the critic
agent with its hard checks, and the orchestrator with the policy gate. First
end-to-end run: 31 exceptions, every one with an explanation, a drafted entry and
a critic verdict. Zero unbalanced entries.

Prompt decisions logged in AO: the analyst may only choose from the enumerated
categories and only use accounts in the chart of accounts — and the code coerces
anything else back into range rather than hoping.

## Historical Session 4 — failure analysis (the session that mattered)
**AO session:** not captured  ·  **recording:** not captured

The evaluator was run and its failures were read instead of only the headline number. Six real
bugs found and fixed, each one now a regression test:

1. split pass searched the wrong candidate window
2. batch pass used a greedy sum that stranded on overshoot and mixed settlements
3. reference pass matched at **any** amount, hiding five FX variances entirely
4. near-amount tolerance of $1.00 manufactured a match
5. duplicate pass consumed an FX line as the match (grouped on reference alone)
6. learned rules keyed on the amount, so nothing ever generalised

Fixing 1, 2 and 5 moved auto-match from 74 % → 87.4 % and match precision to
1.000. Fixing 6 is what made the learning curve exist at all.

Then the follow-on bug: with amount-free signatures, a duplicate rule started
overriding a correct timing classification and **accuracy went down as the system
learned** (85.7 % → 80.0 %). Fix: run the analyst first and make rule lookup
category-aware. Accuracy now never regresses across rounds.

Also found two bugs in the *measurement*: auto-resolved items didn't record which
side they came from, and the evaluator counted a correct duplicate pairing as a
false positive. Some early "regressions" were the ruler, not the system.

## Historical Session 5 — review desk + improvement loop
**AO session:** not captured  ·  **recording:** not captured

The repository contains the review desk (stdlib `http.server`, inline HTML/CSS/JS, no build
step), the approve / re-code / reject flow, rule creation on approval, and the
`improve` command that produces the run-1 → run-N chart.

Added the second metric here: **recognition rate** vs **auto-resolve rate**.
Letting rules auto-post everything would have taken the queue to zero and proved
nothing.

## Historical Session 6 — tests, docs, demo
**AO session:** not captured  ·  **recording:** not captured

The repository contains 120 tests covering the embarrassing-if-wrong behaviours, the README, the
architecture doc with the failure log, the prompt reference, and the demo script.

---

## Verified AO Usage

The repository was registered in the running AO daemon as project `closeloop`.
The following session IDs are verifiable in AO:

| Session | Purpose | Result |
|---|---|---|
| `closeloop-1` | Read-only final submission evidence audit | Created in an isolated AO worktree; agent response was not returned |
| `closeloop-2` | Native-terminal retry of the same read-only audit | Created in an isolated AO worktree; no final audit response was returned |
| `closeloop-3` | Bounded Close Command Center implementation brief | Created in an isolated AO worktree; session stayed idle and produced no code |

These sessions establish AO usage for finalization, but they do not prove that
the earlier build was performed in AO. No recording filenames are available.
The Close Command Center implementation was completed in the primary checkout
after `closeloop-3` remained idle; it is not attributed to AO.

## Historical AO Narrative

* **System design.** The implementation architecture uses an analyst's tool calls,
  the critic's re-check, and the orchestrator's sequencing. The historical build
  session records needed to attribute those iterations to AO were not captured.
* **Evaluation in the loop.** The matcher changes were scored against the planted
  answer key, which is how bugs 3, 4 and 5 were found — none of them produced a
  visible failure. This repository evidence is independent of the later AO audit
  sessions and is not presented as proof of historical AO usage.
* **Evidence.** The verified finalization session IDs are listed above; `python3 outlier.py audit
  --run RUN-COLD --verbose` prints the machine-readable trail for any run.

## Trace to show in the demo

`docs/ARCHITECTURE.md` § *Things we got wrong first* → item 3 (the reference
pass). It is the cleanest example: nothing looked broken, the eval said five
expected exceptions had vanished, and the cause was a pass that treated a shared
invoice number as sufficient evidence.

---

## Team

| Name | Role | Devpost |
|---|---|---|
| Yadamreddy Navaneeth | Builder | @NTHDARKRIDER555 |
| `________` | `________` | `________` |

## Submission checklist

- [ ] Track selected: **Track 2 — Autonomous Office of the CFO**
- [ ] Problem + target users written up (controllers at startups/SMBs)
- [ ] GitHub repo public, README runs from a clean clone (`make demo`)
- [ ] Demo video 3–5 min public, shows the AO dashboard with session count plus the working system
- [ ] Architecture + evaluation method explained (`docs/ARCHITECTURE.md`, `docs/PROMPTS.md`)
- [ ] Measurable results in the submission text, not just the video
- [ ] Every team member registered individually
- [ ] One Devpost project per team
- [ ] Submitted 30+ minutes before Sunday 6:00 PM EDT
