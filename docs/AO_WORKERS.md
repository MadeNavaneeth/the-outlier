# AO worker plan — The Outlier

Why this exists: 25 % of the score is AO Usage & Build Process, and it doubles
as an eligibility gate ("no meaningful AO usage → disqualified"). Each worker
below leaves verifiable evidence in the AO daemon (turns, branch, commits) and
in git history once merged. One worker per task, created with New task in the
AO desktop. Paste each prompt as-is.

## ⚠️ Branch hygiene — do NOT push the local `ao/closeloop-*` branches

The local branches `ao/closeloop-1/root`, `ao/closeloop-2/root` and
`ao/closeloop-3/root` are checked out inside AO worktrees and are **stale**:
they still point at the pre-rewrite history (`f6e34bd…`) whose descendant
commit carried a third-party co-author trailer that was deliberately scrubbed
from the public repo. On GitHub these three branches already point at the
clean tip (`5c70e42`). Pushing the local ones again (`git push origin
ao/...`) would silently restore the scrubbed history — don't. If a future AO
worker produces commits, rebase them onto `main` and push with an explicit
new branch name.

## Worker A — landing-page reference fix

```
Fix the broken landing-page reference in docs.

Context: `AO_LOG.md` and `docs/DEMO_SCRIPT.md` reference a
`syndicate-hackathon-landing-page/` click-through as if it lives inside this
repo. It actually lives beside it at `D:\Projects\GPT Astra\
syndicate-hackathon-landing-page\`. A judge clicking through hits a dead end.

Investigate, then do the smaller of: (a) correct every reference to the real
relative location, or (b) remove the click-through claim from the demo flow and
criteria table. Do not invent URLs or claim a deployed site.

Done when:
- `rg -n "syndicate-hackathon-landing-page" --glob '!.git'` shows zero stale
  in-repo references, or every remaining one resolves
- `python -m pytest -q` still passes, `git diff --check` clean
- Report which option you chose and why in one paragraph
```

## Worker B — large-dataset refresh

```
Refresh the large-dataset evidence under the `outlier` entry point.

Context: `README.md` claims large-set numbers (518 bank / 644 GL rows, 90.5 %
auto-match, F1 1.000, 96.0 % accuracy, 101 exceptions, 0 false auto-posts).
Regenerate and confirm they still reproduce exactly.

Run (do not commit the artifacts — `big/`, `data/`, `reports/` are gitignored):
- `python outlier.py generate --out big --size large`
- `python outlier.py run --provider mock --bank big/bank_statement.csv --ledger big/ledger_export.csv --truth big/ground_truth.json --run-id RUN-LARGE`
Compare every number against the README table. If any cell differs, do NOT
edit the README — report the diff and stop.

Done when: all cells match, `python -m pytest -q` passes,
`git diff --check` clean. Report the comparison table.
```

## Worker C — clean-clone reproduction check

```
Prove a judge can reproduce the demo from a clean clone.

Context: the submission checklist requires "GitHub repo runs from a clean
clone (`make demo`)". Simulate it WITHOUT touching this checkout: clone the
pushed public repo to a temp dir, then run `make demo` (or the README
step-by-step with `python` on Windows) and `python -m pytest -q` there.

Done when: cold-run numbers, improvement curve, and test count match the
README, or you file a precise report of the first command that fails
(including missing tracked files — note `data/`, `reports/`, `big/` are
gitignored by design and must be regenerated, never committed).
Report pass/fail with the terminal output.
```

## After all workers finish (human steps)

1. Review each diff in the AO dashboard.
2. Merge every `ao/<session>/root` branch to `main` — merged AO branches are
   the commit history that proves AO-built work.
3. Push public: `git add -A` (required — `outlier/command_center.py`,
   `outlier/learning.py`, and three test files are currently untracked),
   commit, push to the public GitHub repo.
4. Record the 3–5 min video with the AO Kanban + session count on screen,
   following `docs/DEMO_SCRIPT.md`.
5. Fill Devpost from `docs/SUBMISSION.md`, submit 30+ min before
   Sun Sep 6, 6:00 PM EDT.
