# Agent prompts and tool schemas

Both agents are called with a system prompt and a **JSON payload** as the user
message. That is deliberate: the payload is the tool output, assembled in code, so
every call is reproducible and auditable. Responses must be JSON; `_extract_json`
pulls the object out of fences or prose, and any failure falls back to a
conservative default that routes the item to a human.

---

## 1. Exception analyst

**When called:** only for residuals the deterministic matcher could not resolve,
plus deterministic matches that carry a variance and therefore still need a
posting.

**System prompt** (`outlier/agents/exception_analyst.py`):

```
You are the exception analyst on an autonomous bank reconciliation.
A deterministic matcher already resolved every clean, unambiguous pairing. You are
looking ONLY at residuals, so assume the easy cases are gone.

Decide, for one unmatched bank or ledger item:
  1. category      -- exactly one of: timing, missing_entry, duplicate, fee, fx,
                      fraud_suspect, unknown
  2. confidence    -- 0.0 to 1.0, calibrated. If the evidence is thin, say so with
                      a LOW number.
  3. explanation   -- one or two sentences a controller can read in five seconds.
  4. account_code  -- from the chart of accounts ONLY. Use the suspense account
                      when unsure.
  5. resolution    -- "journal_entry" (needs posting), "reconciling_item" (report
                      only, no posting) or "investigate" (no action, flag for
                      fraud/audit).
  6. memo          -- short journal memo.

Hard rules:
- Do not invent account codes.
- Do not propose a journal entry for a timing difference; timing differences are
  reconciling items.
- If the item looks like an unexplained third-party debit, category is
  fraud_suspect and resolution is investigate.
- Reply with JSON only, matching the schema in the user message.
```

**User payload** (assembled by `build_context`, i.e. by tool calls in code):

```json
{
  "purpose": "classify_exception",
  "item": {
    "side": "bank",
    "txn_id": "BTX-00112",
    "date": "2026-08-11",
    "amount": -18.4,
    "description": "ACH RETURN FEE",
    "reference": "",
    "counterparty": "BANK",
    "vendor": "BANK",
    "bank_code": "FEE",
    "signature": "ACHRETURNFEE|FEE|BANK"
  },
  "signature": "ACHRETURNFEE|FEE|BANK",
  "duplicate_candidates": [],
  "fx_candidate": null,
  "vendor_history": {
    "vendor": "BANK", "n_entries": 0,
    "account_code_distribution": {}, "usual_account_code": null,
    "usual_account_name": null, "recent": []
  },
  "chart_of_accounts": [ {"code": "1000", "name": "Operating Checking", "type": "asset", "normal_side": "debit"}, "…" ],
  "prior_decisions": {
    "signature": "ACHRETURNFEE|FEE|BANK", "vendor": "BANK",
    "exact_rule": null, "vendor_rules": [], "n_vendor_rules": 0
  },
  "fuzzy_candidates": [],
  "similar_ledger": [],
  "account_prior": ["6100", "description matched keyword set 'FEE'", 0.72]
}
```

**Required response:**

```json
{
  "category": "fee",
  "confidence": 0.86,
  "explanation": "Bank-initiated ACH return charge that never hits the GL until month-end. Book it to the fee account.",
  "account_code": "6100",
  "resolution": "journal_entry",
  "memo": "ACH RETURN FEE"
}
```

**Output coercion (`_coerce`)** — the model is not trusted:

| Model says | Code does |
|---|---|
| category outside the enum | → `unknown` |
| account code not in the chart of accounts | → the tool-derived prior (`account_prior`) |
| resolution outside the enum | → `investigate` |
| `timing` + `journal_entry` | → `reconciling_item` (hard rule, enforced twice) |
| `fraud_suspect` + anything but `investigate` | → `investigate` |
| confidence not a number | → 0.3 (which is below the review threshold, so a human sees it) |

---

## 2. Critic agent

**When called:** on every analyst output that survives the hard checks. It never
sees the analyst's reasoning — only the claim.

**System prompt** (`outlier/agents/critic.py`):

```
You are the critic reviewer on an autonomous bank reconciliation.
Another agent classified an unmatched item and drafted a journal entry. Your job is
to try to break it. Be skeptical; a wrong posting is far worse than an unnecessary
review.

Check:
  1. mechanics  -- do debits equal credits? Are all account codes real? Is the
                   debit/credit side correct for each account type?
  2. plausibility -- does the account match how this vendor/description is
                   historically coded? Is the amount direction sensible?
  3. consistency -- does the stated explanation actually support the category
                   and the entry? Flag hand-waving.

Return JSON only:
{
  "verdict": "PASS | FAIL | ESCALATE",
  "confidence": 0.0-1.0,
  "issues": ["short, specific issues"],
  "suggested_account_code": "only if you disagree with the account, else null",
  "note": "one sentence for the reviewer"
}
```

**Hard checks run before the model is even called** (`hard_checks`):

* unbalanced entry → `FAIL`
* account code not in the chart of accounts → `FAIL`
* a line with both a debit and a credit → `FAIL`
* money left the bank but `1000` is debited (or vice versa) → `FAIL`
* entry amount ≠ bank amount → `FAIL`
* `timing` with a journal entry → `ESCALATE`
* `fraud_suspect` with a journal entry → `ESCALATE`
* `unknown` with a journal entry → `ESCALATE`
* explanation missing or under 25 characters → `ESCALATE`

Verdict merge rule: a hard `FAIL` always wins; a hard `ESCALATE` downgrades a soft
`PASS`.

---

## 3. Tool schemas

`python3 outlier.py tools --bank sample/bank_statement.csv --ledger sample/ledger_export.csv`
prints the OpenAI/AO-compatible spec. Summary:

| Tool | Purpose | Read-only |
|---|---|---|
| `search_ledger` | GL rows by vendor/reference text and/or amount | yes |
| `search_bank` | bank rows by free text | yes |
| `fuzzy_match` | best ledger candidates for an unmatched bank row, with a score | yes |
| `find_duplicates` | GL rows that look like a double-booking, **including already-matched lines** | yes |
| `get_vendor_history` | how this vendor is usually coded + recent entries | yes |
| `get_chart_of_accounts` | the only accounts that may be used | yes |
| `lookup_prior_decisions` | human-approved rule for this signature **and category** | yes |
| `get_ledger_entry` / `get_bank_txn` | fetch one row by id | yes |

**Every tool is read-only.** Agents have no path to the ledger. The only writer is
`Orchestrator.run`, and only through `Policy`.

---

## 4. The learn-from-review contract

A rule is created **only** from an explicit human approval (desk UI or simulated
reviewer), and it stores:

```json
{
  "category": "fee",
  "account_code": "6100",
  "lines": [ {"account_code": "6100", "debit": 18.4, "credit": 0.0, "…": "…"},
             {"account_code": "1000", "debit": 0.0, "credit": 18.4, "…": "…"} ],
  "resolution": "journal_entry",
  "approved_status": "human_approved",
  "amount_cap": 18.4
}
```

At lookup time a rule fires only if **all** of these hold:

1. exact signature match (normalised description + bank code + counterparty)
2. the rule's category equals the category predicted for *this* item
3. `approved_status == "human_approved"`
4. the category is not on the never-auto list
5. `|amount| <= min(policy.auto_resolve_cap, rule.amount_cap)`

If 1 holds but 2 does not, the run logs `rule_category_disagreement` and sends the
item to a human. That is the guard against a stale rule silently overwriting a
correct new classification.
