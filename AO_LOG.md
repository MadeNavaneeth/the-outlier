# AO build log — CloseLoop

Hackathon: **Syndicate by Maximor**, Track 2 — Autonomous Office of the CFO.
Every team member registered individually on Devpost. The project was started at
kickoff and built in AO from the first commit to the submission.

> **Fill in before submitting:** session IDs, screen-recording filenames, and the
> team roster at the bottom. The session narrative below is the actual build
> order — match it against your AO session list and paste the real IDs in.

---

## Session 1 — kickoff, scaffold, data
**AO session:** `________`  ·  **recording:** `________`

Started a fresh AO session at kickoff (this is the "not started before the
hackathon" proof) and had AO scaffold the repo: module layout, sign convention
(`amount > 0` = money in), the chart of accounts, and the synthetic dataset
generator with planted anomalies plus an answer key.

Decision made here that shaped everything: **write the ground truth at generation
time and never let the reconciler read it.** Without that there is no way to
report accuracy, and "measurable results" is a quarter of the judging criteria.

Outcome: `python3 closeloop.py generate --out sample` → 127 bank rows, 166 GL rows,
planted splits / batches / timing / fees / FX / duplicates / fraud.

## Session 2 — deterministic matcher
**AO session:** `________`  ·  **recording:** `________`

AO built the matching passes: exact, reference, split (subset sum in integer
cents), batch, near-amount. Baseline metrics: auto-match rate **74 %**, match
recall 0.70.

Recorded in AO at the end of the session: the split pass was searching the top 6
candidates by *largest* amount, so the real invoice lines were never in the
window.

## Session 3 — agents, tools, critic
**AO session:** `________`  ·  **recording:** `________`

AO wired the tool registry (all read-only), the exception analyst, the critic
agent with its hard checks, and the orchestrator with the policy gate. First
end-to-end run: 31 exceptions, every one with an explanation, a drafted entry and
a critic verdict. Zero unbalanced entries.

Prompt decisions logged in AO: the analyst may only choose from the enumerated
categories and only use accounts in the chart of accounts — and the code coerces
anything else back into range rather than hoping.

## Session 4 — failure analysis (the session that mattered)
**AO session:** `________`  ·  **recording:** `________`

Ran the evaluator and read the failures instead of the headline number. Six real
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

## Session 5 — review desk + improvement loop
**AO session:** `________`  ·  **recording:** `________`

AO built the review desk (stdlib `http.server`, inline HTML/CSS/JS, no build
step), the approve / re-code / reject flow, rule creation on approval, and the
`improve` command that produces the run-1 → run-N chart.

Added the second metric here: **recognition rate** vs **auto-resolve rate**.
Letting rules auto-post everything would have taken the queue to zero and proved
nothing.

## Session 6 — tests, docs, demo
**AO session:** `________`  ·  **recording:** `________`

63 tests covering the embarrassing-if-wrong behaviours, the README, the
architecture doc with the failure log, the prompt reference, and the demo script.

---

## How AO was used (for the 25 % criterion)

* **Orchestration, not codegen.** AO drove the sub-agent work: the analyst's tool
  calls, the critic's re-check, and the orchestrator's sequencing were iterated in
  AO sessions against live run output, not written once and shipped.
* **Evaluation in the loop.** Every matcher change was scored against the planted
  answer key inside the session, which is how bugs 3, 4 and 5 were found — none of
  them produced a visible failure.
* **Evidence.** Session IDs and recordings above; `python3 closeloop.py audit
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
| `________` | `________` | `________` |
| `________` | `________` | `________` |

## Submission checklist

- [ ] Track selected: **Track 2 — Autonomous Office of the CFO**
- [ ] Problem + target users written up (controllers at startups/SMBs)
- [ ] GitHub repo public, README runs from a clean clone (`make demo`)
- [ ] Demo video ≤ 3 min, shows AO sessions and the working system
- [ ] Architecture + evaluation method explained (`docs/ARCHITECTURE.md`, `docs/PROMPTS.md`)
- [ ] Measurable results in the submission text, not just the video
- [ ] Every team member registered individually
- [ ] One Devpost project per team
- [ ] Submitted 30+ minutes before Sunday 6:00 PM EDT
