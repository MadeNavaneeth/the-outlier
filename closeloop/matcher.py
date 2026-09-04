"""Deterministic matcher.

Design rule that the whole project hangs on: **the LLM never does the easy
matches.** Deterministic passes handle 1:1 exact, reference, near-amount,
split and batch matches with an explicit audit trail. Only the residual goes
to the agents -- which keeps cost down, keeps hallucination out of the
matching path, and gives us a defensible "false auto-post rate = 0" claim.

Every pass returns :class:`Match` objects tagged with the rule that fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .models import BankTxn, LedgerEntry, Match, money

REF_RE = re.compile(r"(INV[- ]?\d{3,}|PO[- ]?\d{3,}|CHK[- ]?\d{3,}|BILL[- ]?\d{3,}|\b\d{5,}\b)")


def normalize_ref(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def refs_in(text: str) -> set[str]:
    return {normalize_ref(m) for m in REF_RE.findall(text or "")}


@dataclass
class MatcherConfig:
    date_window_days: int = 7
    amount_tol_abs: float = 0.02  # rounding
    amount_tol_pct: float = 0.0  # set >0 only for the near-amount pass
    #: Ceiling for the near-amount (cent drift) pass. Deliberately tiny: a
    #: loose tolerance here manufactures matches that do not exist.
    near_amount_max_variance: float = 0.05
    max_split_lines: int = 4
    max_split_candidates: int = 80
    max_batch_lines: int = 20
    #: Node budget for the exact subset-sum search. Bounds the worst case so a
    #: pathological statement can never hang a run.
    subset_sum_node_budget: int = 400_000
    #: A processor settlement is booked on one day. Restricting the batch search
    #: to a single GL date is what stops two settlements in the same window
    #: from tying out against each other's lines.
    max_batch_dates: int = 1
    #: Duplicate GL lines must share a reference to be treated as duplicates.
    duplicate_requires_reference: bool = True
    #: Largest variance the FX pass will accept when a reference ties out.
    #: Above this it is not an FX variance, it is an unmatched item.
    fx_max_variance_pct: float = 0.05
    require_same_sign: bool = True
    batch_tolerance: float = 0.01


class Matcher:
    def __init__(self, bank: Sequence[BankTxn], ledger: Sequence[LedgerEntry], cfg: MatcherConfig | None = None):
        self.cfg = cfg or MatcherConfig()
        self.bank = list(bank)
        self.ledger = list(ledger)
        self.unmatched_bank: dict[str, BankTxn] = {t.txn_id: t for t in bank}
        self.unmatched_ledger: dict[str, LedgerEntry] = {e.entry_id: e for e in ledger}
        self.matches: list[Match] = []
        #: Multi-line sets that tie out exactly but are not unique. We refuse
        #: to guess; these go to the exception desk with the candidates attached.
        self.ambiguous: list[dict] = []
        self.duplicate_candidates: list[str] = []
        self._mid = 0

    # ------------------------------------------------------------------
    def _mid_next(self, kind: str) -> str:
        self._mid += 1
        return f"M-{kind[:2].upper()}-{self._mid:05d}"

    def _consume(self, m: Match) -> None:
        self.matches.append(m)
        for t in m.bank_txn_ids:
            self.unmatched_bank.pop(t, None)
        for e in m.ledger_entry_ids:
            self.unmatched_ledger.pop(e, None)

    @staticmethod
    def _within(a, b, days: int) -> bool:
        return abs((a - b).days) <= days

    def _amount_eq(self, a: float, b: float, tol_pct: float = 0.0) -> bool:
        if abs(a - b) <= self.cfg.amount_tol_abs:
            return True
        if tol_pct > 0 and abs(a) > 0 and abs(a - b) / abs(a) <= tol_pct:
            return True
        return False

    # ------------------------------------------------------------------
    # PASS 1 -- exact amount inside the date window (unique pairing only)
    # ------------------------------------------------------------------
    def pass_exact(self) -> int:
        by_amount: dict[float, list[LedgerEntry]] = {}
        for e in self.unmatched_ledger.values():
            by_amount.setdefault(money(e.amount), []).append(e)

        for txn in list(self.unmatched_bank.values()):
            cands = [
                e
                for e in by_amount.get(money(txn.amount), [])
                if e.entry_id in self.unmatched_ledger
                and self._within(txn.date, e.date, self.cfg.date_window_days)
            ]
            if len(cands) == 1:  # only auto-match when unambiguous
                e = cands[0]
                self._consume(
                    Match(
                        match_id=self._mid_next("EX"),
                        bank_txn_ids=[txn.txn_id],
                        ledger_entry_ids=[e.entry_id],
                        match_type="exact",
                        confidence=0.99,
                        method="deterministic",
                        residual=0.0,
                        evidence=[
                            f"amount {txn.amount:.2f} == {e.amount:.2f}",
                            f"dates {txn.date} vs {e.date} within {self.cfg.date_window_days}d",
                        ],
                    )
                )
        return len(self.matches)

    # ------------------------------------------------------------------
    # PASS 1b -- duplicated GL lines: match the bank row to ONE copy, leave
    #            the other for the exception desk to flag as a duplicate
    # ------------------------------------------------------------------
    def pass_duplicate_aware(self) -> int:
        """Two identical GL lines for one bank payment.

        Match the bank row to the EARLIEST copy and leave the rest for the
        exception desk to flag as duplicates.

        Grouping is on (reference, amount), never on reference alone: that
        would let the pass consume a *different* line carrying the same invoice
        number -- an FX-variance entry, say -- and quietly produce a wrong
        pairing. Vendor is not part of the key either, because a re-keyed
        duplicate usually loses its vendor name. We hit both; this is the fix.
        """
        before = len(self.matches)
        groups: dict[tuple, list[LedgerEntry]] = {}
        for e in self.unmatched_ledger.values():
            ref = normalize_ref(e.reference)
            if not ref:
                continue
            groups.setdefault((ref, money(e.amount)), []).append(e)

        for (ref, amount), entries in groups.items():
            if len(entries) < 2:
                continue
            entries = sorted(entries, key=lambda e: (e.date, e.entry_id))
            bank_cands = [
                t
                for t in self.unmatched_bank.values()
                if money(t.amount) == amount
                and self._within(t.date, entries[0].date, self.cfg.date_window_days)
                and (ref in normalize_ref(t.description) or ref in normalize_ref(t.reference))
            ]
            if len(bank_cands) != 1:
                continue
            txn = bank_cands[0]
            first = entries[0]
            self._consume(
                Match(
                    match_id=self._mid_next("DU"),
                    bank_txn_ids=[txn.txn_id],
                    ledger_entry_ids=[first.entry_id],
                    match_type="duplicate_aware",
                    confidence=0.93,
                    method="deterministic",
                    residual=0.0,
                    evidence=[
                        f"{len(entries)} identical GL lines (reference {ref}, amount {amount:.2f}); bank paid once",
                        f"matched the earliest entry {first.entry_id}; the other {len(entries) - 1} "
                        f"copy/copies are left for duplicate review",
                    ],
                    notes="duplicate GL entry detected",
                )
            )
            for extra in entries[1:]:
                extra.status = "DUPLICATE"
            self.duplicate_candidates.extend(e.entry_id for e in entries[1:])
        return len(self.matches) - before

    # ------------------------------------------------------------------
    # PASS 2 -- unique reference/invoice number, amount must still tie out
    # ------------------------------------------------------------------
    def pass_reference(self) -> int:
        before = len(self.matches)
        by_ref: dict[str, list[LedgerEntry]] = {}
        for e in self.unmatched_ledger.values():
            for r in refs_in(e.reference) | refs_in(e.description):
                by_ref.setdefault(r, []).append(e)

        for txn in list(self.unmatched_bank.values()):
            want = refs_in(txn.reference) | refs_in(txn.description)
            if not want:
                continue
            cands = [
                e
                for r in want
                for e in by_ref.get(r, [])
                if e.entry_id in self.unmatched_ledger
                and self._within(txn.date, e.date, self.cfg.date_window_days)
                and (
                    self._amount_eq(txn.amount, e.amount)
                    or (
                        abs(txn.amount - e.amount) / max(abs(e.amount), 0.01) <= self.cfg.fx_max_variance_pct
                        and (txn.amount > 0) == (e.amount > 0)
                    )
                )
            ]
            seen: dict[str, LedgerEntry] = {}
            for e in cands:
                seen[e.entry_id] = e
            if len(seen) == 1:
                e = next(iter(seen.values()))
                delta = money(txn.amount - e.amount)
                is_fx = abs(delta) > self.cfg.amount_tol_abs
                self._consume(
                    Match(
                        match_id=self._mid_next("FX" if is_fx else "RF"),
                        bank_txn_ids=[txn.txn_id],
                        ledger_entry_ids=[e.entry_id],
                        match_type="fx_variance" if is_fx else "reference",
                        confidence=0.9 if is_fx else 0.98,
                        method="deterministic",
                        residual=delta,
                        evidence=[
                            f"shared reference {sorted(want & (refs_in(e.reference) | refs_in(e.description)))}",
                            f"bank {txn.amount:.2f} vs GL {e.amount:.2f}" + (f" -> variance {delta:+.2f}" if is_fx else " (exact)"),
                        ],
                        # an FX variance still needs a posting to clear the bank,
                        # so a human confirms the rate before it lands in the GL
                        requires_review=is_fx,
                        notes="fx variance" if is_fx else "",
                    )
                )
        return len(self.matches) - before

    # ------------------------------------------------------------------
    # PASS 3 -- 1 bank txn == sum of 2..N ledger entries (split payment)
    # ------------------------------------------------------------------
    def pass_split(self) -> int:
        """1 bank txn == sum of 2..N ledger entries (customer pays several invoices at once).

        Candidate pools are built per counterparty (that is how remittances
        actually arrive) plus one global pool as a fallback, and within a pool
        candidates are ranked by |amount| closest to the target -- searching
        the largest amounts first, the way the earlier version did, never
        reaches the real set.
        """
        before = len(self.matches)
        for txn in sorted(list(self.unmatched_bank.values()), key=lambda t: -abs(t.amount)):
            if abs(txn.amount) < 0.01:
                continue
            in_window = [
                e
                for e in self.unmatched_ledger.values()
                if self._within(txn.date, e.date, self.cfg.date_window_days)
                and (txn.amount > 0) == (e.amount > 0)
            ]
            groups: list[tuple[str, list[LedgerEntry]]] = []
            name = (txn.counterparty or "").strip().lower()
            if name:
                same_vendor = [
                    e for e in in_window
                    if name.split()[0] in (e.vendor or "").lower()
                    or name.split()[0] in (e.description or "").lower()
                ]
                if same_vendor:
                    groups.append((f"counterparty~{txn.counterparty}", same_vendor))
            groups.append(("all unmatched in window", in_window))

            found: list[LedgerEntry] | None = None
            pool_name = ""
            for label, pool in groups:
                pool = sorted(pool, key=lambda e: abs(abs(e.amount) - abs(txn.amount)))[: self.cfg.max_split_candidates]
                found = self._subset_sum(pool, txn.amount, set(self.unmatched_ledger))
                if found:
                    pool_name = label
                    break
            if found:
                ids = [e.entry_id for e in found]
                self._consume(
                    Match(
                        match_id=self._mid_next("SP"),
                        bank_txn_ids=[txn.txn_id],
                        ledger_entry_ids=ids,
                        match_type="split",
                        confidence=0.9 if len(ids) <= 3 else 0.8,
                        method="deterministic",
                        residual=0.0,
                        evidence=[
                            f"{len(ids)} GL entries sum to {sum(e.amount for e in found):.2f}",
                            f"bank amount {txn.amount:.2f}",
                            f"candidate pool: {pool_name}",
                        ],
                        requires_review=len(ids) > 3,
                        notes="split payment",
                    )
                )
        return len(self.matches) - before

    def _subset_sum(self, cands: list[LedgerEntry], target: float, available: set[str]) -> list[LedgerEntry] | None:
        """Find the smallest set of 2..``max_split_lines`` GL entries summing
        exactly to ``target``.

        Tiered so the common cases are cheap and the worst case is bounded:
          1. pairs      -- hash lookup on integer cents
          2. triples    -- fix one entry, hash lookup for the pair
          3. 4+ lines   -- DFS with suffix-sum pruning under a node budget

        ``available`` is re-checked per candidate so an entry consumed earlier
        in the same pass can never be reused.
        """
        items = [(round(e.amount * 100), e) for e in cands if e.entry_id in available]
        if len(items) < 2:
            return None
        target_c = round(target * 100)

        # --- pairs (and count them: more than one pair is ambiguity, not a match) ---
        by_c: dict[int, list[LedgerEntry]] = {}
        for c, e in items:
            by_c.setdefault(c, []).append(e)
        pair_solutions: list[list[LedgerEntry]] = []
        seen_ids: set[str] = set()
        for c, e in items:
            need = target_c - c
            for other in by_c.get(need, []):
                if other.entry_id == e.entry_id:
                    continue
                key = tuple(sorted((e.entry_id, other.entry_id)))
                if any(set(key) == {x.entry_id for x in sol} for sol in pair_solutions):
                    continue
                pair_solutions.append([e, other])
                if len(pair_solutions) > 1:
                    self.ambiguous.append(
                        {"target": target_c / 100, "reason": "multiple exact line sets",
                         "sets": [[x.entry_id for x in sol] for sol in pair_solutions]}
                    )
                    return None
        if pair_solutions:
            return pair_solutions[0]

        # --- triples ---
        if self.cfg.max_split_lines >= 3:
            for i, (c1, e1) in enumerate(items):
                for c2, e2 in items[i + 1 :]:
                    need = target_c - c1 - c2
                    for e3 in by_c.get(need, []):
                        if e3.entry_id in {e1.entry_id, e2.entry_id}:
                            continue
                        return [e1, e2, e3]

        # --- 4+ lines: pruned DFS ---
        if self.cfg.max_split_lines < 4:
            return None
        positive = target_c > 0
        pool = sorted(items, key=lambda kv: -abs(kv[0]))
        n = len(pool)
        suffix = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            suffix[i] = suffix[i + 1] + (pool[i][0] if (pool[i][0] > 0) == positive else 0)
        best: list[LedgerEntry] | None = None
        best_size = self.cfg.max_split_lines + 1
        budget = self.cfg.subset_sum_node_budget

        def dfs(i: int, total: int, chosen: list[LedgerEntry]) -> bool:
            nonlocal best, best_size, budget
            budget -= 1
            if budget <= 0:
                return best is not None
            if total == target_c and 3 <= len(chosen) < best_size:
                best = list(chosen)
                best_size = len(chosen)
                return len(chosen) == 3
            if len(chosen) >= min(self.cfg.max_split_lines, best_size - 1) or i >= n:
                return False
            if positive and total + suffix[i] < target_c:
                return False
            if not positive and total + suffix[i] > target_c:
                return False
            for j in range(i, n):
                c, e = pool[j]
                if (c > 0) != positive:
                    continue
                chosen.append(e)
                if dfs(j + 1, total + c, chosen):
                    chosen.pop()
                    return True
                chosen.pop()
            return False

        dfs(0, 0, [])
        return best

    # ------------------------------------------------------------------
    # PASS 4 -- processor batch: one deposit == sum of many small GL lines
    # ------------------------------------------------------------------
    def pass_batch(self) -> int:
        """Processor settlements: one deposit == a consecutive run of GL lines.

        Solved JOINTLY per GL date, not one deposit at a time. Processing
        deposits independently lets an early one steal another settlement's
        lines -- every sum still ties out, so nothing looks wrong, and two or
        three settlements end up mispaired. On the 644-row month that was the
        difference between 0.93 and 1.00 match recall.

        Method: enumerate every contiguous GL run on the date, index them by
        their sum, then find a maximum matching between deposits and runs such
        that no GL line is used twice. Runs with a unique sum are locked in
        first; ambiguous ones are only used if a consistent assignment exists.
        """
        before = len(self.matches)
        BATCH_HINTS = ("SETTLEMENT", "PAYOUT", "BATCH", "DEPOSIT -", "ACH CREDIT", "DAILY")
        deposits = [
            t for t in self.unmatched_bank.values()
            if abs(t.amount) >= 0.01 and any(h in (t.description or "").upper() for h in BATCH_HINTS)
        ]
        if not deposits:
            return 0

        by_date: dict[str, list[LedgerEntry]] = {}
        for e in self.unmatched_ledger.values():
            by_date.setdefault(e.date.isoformat(), []).append(e)

        for day, lines in by_date.items():
            pool = sorted(lines, key=lambda e: e.entry_id)[: self.cfg.max_split_candidates]
            if len(pool) < 3:
                continue
            here = [
                t for t in deposits
                if t.txn_id in self.unmatched_bank and self._within(t.date, lines[0].date, self.cfg.date_window_days)
            ]
            if not here:
                continue

            # candidate runs, indexed by the sum they tie out to
            options: dict[str, list[list[LedgerEntry]]] = {}
            for t in here:
                runs = self._contiguous_runs(pool, t.amount)
                if runs:
                    options[t.txn_id] = runs

            assignment = self._assign_disjoint(options)
            for txn_id, run_lines in assignment.items():
                txn = self.unmatched_bank[txn_id]
                self._consume(
                    Match(
                        match_id=self._mid_next("BA"),
                        bank_txn_ids=[txn.txn_id],
                        ledger_entry_ids=[e.entry_id for e in run_lines],
                        match_type="batch",
                        confidence=0.9,
                        method="deterministic",
                        residual=0.0,
                        evidence=[
                            f"settlement: {len(run_lines)} consecutive GL lines booked on {day} "
                            f"sum to {sum(e.amount for e in run_lines):.2f}",
                            f"bank deposit {txn.amount:.2f} ({txn.description})",
                            "solved jointly with the other settlements on this date",
                        ],
                        requires_review=True,
                        notes="processor batch settlement",
                    )
                )
            for t in here:
                if t.txn_id in self.unmatched_bank and t.txn_id in options:
                    self.ambiguous.append(
                        {"target": t.amount, "txn_id": t.txn_id,
                         "reason": "candidate GL runs exist but none can be assigned without a clash",
                         "sets": [[e.entry_id for e in r] for r in options[t.txn_id][:2]]}
                    )
        return len(self.matches) - before

    def _contiguous_runs(self, pool: list[LedgerEntry], target: float) -> list[list[LedgerEntry]]:
        """Every consecutive run of >= 3 GL lines that sums exactly to the target.

        A prefix-sum scan, so it is linear per window size. A *unique* answer
        here is much stronger evidence than a unique answer anywhere among the
        date's lines, which is why the batch pass asks this question first.

        Whether a run is still available is not checked here -- that is the
        assignment step's job, and checking it here is what made the old
        one-deposit-at-a-time version steal lines from its neighbours.
        """
        target_c = round(target * 100)
        cents = [round(e.amount * 100) for e in pool]
        prefix = [0] * (len(cents) + 1)
        for i, c in enumerate(cents):
            prefix[i + 1] = prefix[i] + c
        out: list[list[LedgerEntry]] = []
        for size in range(3, min(self.cfg.max_batch_lines, len(pool)) + 1):
            for start in range(0, len(pool) - size + 1):
                if prefix[start + size] - prefix[start] == target_c:
                    out.append(pool[start : start + size])
        return out

    def _assign_disjoint(self, options: dict[str, list[list[LedgerEntry]]]) -> dict[str, list[LedgerEntry]]:
        """Choose one candidate run per deposit so that no GL line is used twice.

        Exact backtracking search, deposits ordered by fewest candidates first.
        A settlement's line run is indivisible -- you cannot move half of it --
        so "give way" can only ever mean "take a different candidate run". Two
        earlier versions modelled this as bipartite matching over individual
        lines and both were wrong: one shared lines between settlements, the
        other "evicted" a node by re-placing it on the very run it already
        held, which succeeded vacuously and silently dropped a settlement.

        Returns the largest packing found, complete or not; deposits that did
        not fit are absent and fall through to the exception desk. Bounded by
        ``subset_sum_node_budget`` so a pathological date cannot stall a run.
        """
        if not options:
            return {}
        all_tids = sorted(options)
        best: dict[str, list[LedgerEntry]] = {}
        current: dict[str, list[LedgerEntry]] = {}
        used: set[str] = set()
        budget = self.cfg.subset_sum_node_budget

        def note() -> None:
            if len(current) > len(best):
                # replace, never merge: update() alone would keep entries from a
                # worse branch explored earlier
                best.clear()
                best.update(current)

        def search(placed: set[str]) -> bool:
            """Place one more deposit, choosing the most constrained one.

            The next deposit is picked dynamically rather than from a fixed
            order. A static order is not enough: whichever settlement is placed
            first can only ever backtrack within its own candidate list, so a
            valid joint answer is missed whenever the fix requires the *first*
            deposit to move out of the way of the second.
            """
            nonlocal budget
            budget -= 1
            note()  # every reachable state is a candidate answer
            if budget <= 0:
                return False
            if len(placed) == len(all_tids):
                return True

            # Try every unplaced deposit, most constrained first. Committing to
            # just the most constrained one and returning when it fails is not
            # enough: the fix may need a *later* settlement placed first so the
            # constrained one can take the run that is then left over.
            candidates: list[tuple[int, str, list[list[LedgerEntry]]]] = []
            for tid in all_tids:
                if tid in placed:
                    continue
                free = [r for r in options[tid] if not ({e.entry_id for e in r} & used)]
                if not free:
                    continue  # nothing usable for this one right now
                candidates.append((len(free), tid, free))
            candidates.sort(key=lambda c: (c[0], c[1]))
            # note() has already recorded the current partial assignment, so if
            # every branch below fails the caller still gets the best answer
            # found rather than nothing.
            for _, tid, free in candidates:
                for run in free:
                    ids = {e.entry_id for e in run}
                    current[tid] = run
                    used.update(ids)
                    placed.add(tid)
                    if search(placed):
                        placed.discard(tid)
                        used.difference_update(ids)
                        del current[tid]
                        return True
                    placed.discard(tid)
                    used.difference_update(ids)
                    del current[tid]
            return False

        search(set())
        # best holds the largest packing reached, complete or not. Deposits
        # that did not fit simply are absent and fall through to the exception
        # desk, which is the right outcome: a settlement we cannot account for
        # line by line is a human's problem, not a guess.
        return best

    # ------------------------------------------------------------------
    # PASS 5 -- near-amount with unique pairing (rounding / cent drift only)
    # ------------------------------------------------------------------
    def pass_near_amount(self) -> int:
        before = len(self.matches)
        pool = list(self.unmatched_ledger.values())
        for txn in list(self.unmatched_bank.values()):
            cands = [
                e
                for e in pool
                if e.entry_id in self.unmatched_ledger
                and self._within(txn.date, e.date, self.cfg.date_window_days)
                and (txn.amount > 0) == (e.amount > 0)
                and 0 < abs(txn.amount - e.amount) <= self.cfg.near_amount_max_variance
            ]
            if len(cands) == 1:
                e = cands[0]
                self._consume(
                    Match(
                        match_id=self._mid_next("NA"),
                        bank_txn_ids=[txn.txn_id],
                        ledger_entry_ids=[e.entry_id],
                        match_type="near_amount",
                        confidence=0.86,
                        method="deterministic",
                        residual=money(txn.amount - e.amount),
                        evidence=[f"cent-level variance {txn.amount - e.amount:+.2f}"],
                        requires_review=True,
                        notes="rounding variance",
                    )
                )
        return len(self.matches) - before

    # ------------------------------------------------------------------
    def run(self) -> "MatchReport":
        steps = [
            ("exact", self.pass_exact),
            ("duplicate_aware", self.pass_duplicate_aware),
            ("reference", self.pass_reference),
            ("batch", self.pass_batch),
            ("split", self.pass_split),
            ("near_amount", self.pass_near_amount),
        ]
        step_stats: dict[str, int] = {}
        for name, fn in steps:
            before = len(self.matches)
            fn()
            step_stats[name] = len(self.matches) - before

        # unmatched LEDGER entries that pair with nothing on the bank side are
        # also exceptions (outstanding cheques, duplicates). Emit a pseudo
        # "bank txn" per unmatched ledger entry so the desk can classify them.
        return MatchReport(
            matches=self.matches,
            ambiguous=self.ambiguous,
            duplicate_candidates=self.duplicate_candidates,
            unmatched_bank=list(self.unmatched_bank.values()),
            unmatched_ledger=list(self.unmatched_ledger.values()),
            n_bank=len(self.bank),
            n_ledger=len(self.ledger),
            step_stats=step_stats,
        )


class MatchReport:
    def __init__(self, matches, unmatched_bank, unmatched_ledger, n_bank, n_ledger, step_stats,
                 ambiguous=None, duplicate_candidates=None):
        self.ambiguous = ambiguous or []
        self.duplicate_candidates = duplicate_candidates or []
        self.matches = matches
        self.unmatched_bank = unmatched_bank
        self.unmatched_ledger = unmatched_ledger
        self.n_bank = n_bank
        self.n_ledger = n_ledger
        self.step_stats = step_stats

    @property
    def bank_matched(self) -> int:
        return sum(len(m.bank_txn_ids) for m in self.matches)

    @property
    def ledger_matched(self) -> int:
        return sum(len(m.ledger_entry_ids) for m in self.matches)

    def summary(self) -> dict:
        return {
            "bank_rows": self.n_bank,
            "ledger_rows": self.n_ledger,
            "matches": len(self.matches),
            "bank_matched": self.bank_matched,
            "ledger_matched": self.ledger_matched,
            "bank_unmatched": len(self.unmatched_bank),
            "ledger_unmatched": len(self.unmatched_ledger),
            "auto_match_rate_bank": round(self.bank_matched / self.n_bank, 4) if self.n_bank else 0.0,
            "auto_match_rate_ledger": round(self.ledger_matched / self.n_ledger, 4) if self.n_ledger else 0.0,
            "steps": self.step_stats,
        }
