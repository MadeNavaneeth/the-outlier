"""Synthetic dataset generator with *planted* anomalies and known ground truth.

Judges explicitly ask for measurable results, and you cannot measure
reconciliation accuracy against real-world data (nobody has the labels).
So we generate a month of activity where we know, for every bank row, exactly
which ledger entries it corresponds to and why it should not match.

Output (into ``data/roundN/``):
    bank_statement.csv     -- what the bank sent
    ledger_export.csv      -- what the GL / ERP exported
    ground_truth.json      -- the answer key (never read by the reconciler)

Anomaly mix is configurable; defaults approximate a real SMB month:
~88% clean 1:1, plus splits, batches, timing, fees, FX, duplicates, fraud.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .models import money

VENDORS = [
    ("Amazon Web Services", "6050", "Cloud & Hosting", (-4500, -180)),
    ("Google Cloud Platform", "6050", "Cloud & Hosting", (-2600, -90)),
    ("Snowflake", "6050", "Cloud & Hosting", (-1900, -300)),
    ("Figma", "6000", "Software & Subscriptions", (-540, -45)),
    ("Notion Labs", "6000", "Software & Subscriptions", (-320, -40)),
    ("Slack Technologies", "6000", "Software & Subscriptions", (-780, -60)),
    ("Datadog", "6000", "Software & Subscriptions", (-1450, -220)),
    ("Stripe Payouts", "4000", "Product Revenue", (18000, 3500)),
    ("PayPal Settlement", "4000", "Product Revenue", (9000, 1200)),
    ("Acme Corp", "4000", "Product Revenue", (48000, 6000)),
    ("Globex Industries", "4000", "Product Revenue", (27500, 4200)),
    ("Initech LLC", "4100", "Services Revenue", (16500, 2500)),
    ("WeWork", "6400", "Rent & Facilities", (-12400, -12400)),
    ("Delta Air Lines", "6500", "Travel & Meals", (-2400, -180)),
    ("Marriott", "6500", "Travel & Meals", (-1800, -150)),
    ("Baker McKenzie", "6300", "Professional Fees", (-9500, -1500)),
    ("LinkedIn Ads", "6200", "Advertising & Marketing", (-3200, -400)),
    ("Google Ads", "6200", "Advertising & Marketing", (-5400, -700)),
    ("Gusto Payroll", "6600", "Payroll & Contractors", (-86000, -86000)),
    ("CDW", "5000", "Cost of Goods Sold", (-7300, -900)),
]

FEE_PATTERNS = [
    ("MONTHLY SERVICE CHARGE", (-45, -12), "6100"),
    ("WIRE TRANSFER FEE", (-35, -25), "6100"),
    ("ACH RETURN FEE", (-18, -12), "6100"),
    ("CARD MERCHANT FEE", (-240, -35), "6150"),
    ("PROCESSING FEE STRIPE", (-1800, -220), "6150"),
    ("FX CONVERSION MARKUP", (-95, -18), "6100"),
]

FRAUD_DESCRIPTIONS = [
    "POS PURCHASE UNKNOWNMERCHANT 4471",
    "CARD NOT PRESENT TXN 8842XYZ",
    "ATM WITHDRAWAL OUT OF NETWORK",
    "CRYPTO EXCHANGE TRANSFER",
]

BANK_CHATTER = [
    ("INTEREST PAID", (2, 40)),
    ("REVERSAL OF ERRONEOUS DEBIT", (18, 240)),
]


@dataclass
class GroundTruthItem:
    bank_txn_id: str
    expected_ledger_ids: list[str] = field(default_factory=list)
    expected_category: str = ""  # "" when the row is supposed to match
    match_type: str = ""  # exact | split | batch | fx | none
    note: str = ""
    side: str = "bank"  # bank | ledger  (ledger-only items have no bank row)
    #: for ledger-only truth, the exact GL row the exception should be about.
    #: Without this the evaluator has to guess by position, which breaks the
    #: moment any other GL row is also unmatched.
    entry_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GeneratorConfig:
    n_clean: int = 90
    n_split: int = 8  # 1 bank txn -> 2-4 ledger entries
    n_batch: int = 4  # 1 bank txn -> many small ledger entries
    n_timing_out: int = 6  # ledger only (outstanding cheques)
    n_timing_in: int = 4  # bank only (deposit in transit)
    n_fee: int = 7  # bank only, bank charge
    n_fx: int = 5  # amount differs by FX markup
    n_duplicate: int = 4  # same ledger row booked twice
    n_fraud: int = 2  # bank only, unexplained
    n_chatter: int = 3  # interest / reversals
    seed: int = 7
    month_start: str = "2026-08-01"

    @property
    def total_bank(self) -> int:
        return (
            self.n_clean
            + self.n_split
            + self.n_batch
            + self.n_timing_in
            + self.n_fee
            + self.n_fx
            + self.n_duplicate
            + self.n_fraud
            + self.n_chatter
        )


class SyntheticGenerator:
    def __init__(self, cfg: GeneratorConfig | None = None):
        self.cfg = cfg or GeneratorConfig()
        self.rng = random.Random(self.cfg.seed)
        self.month_start = date.fromisoformat(self.cfg.month_start)
        self.bank: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []
        self.truth: list[GroundTruthItem] = []
        self._b = 0
        self._l = 0

    # -- id factories ----------------------------------------------------
    def _bid(self) -> str:
        self._b += 1
        return f"BTX-{self._b:05d}"

    def _lid(self) -> str:
        self._l += 1
        return f"GL-{self._l:05d}"

    def _day(self) -> date:
        return self.month_start + timedelta(days=self.rng.randint(0, 27))

    def _inv(self) -> str:
        return f"INV-{self.rng.randint(10000, 99999)}"

    # -- writers ---------------------------------------------------------
    def _add_bank(self, d, amount, desc, ref, cp, code="") -> str:
        tid = self._bid()
        self.bank.append(
            {
                "txn_id": tid,
                "date": d.isoformat(),
                "amount": f"{money(amount):.2f}",
                "description": desc,
                "reference": ref,
                "counterparty": cp,
                "bank_code": code,
            }
        )
        return tid

    def _add_ledger(self, d, amount, code, name, desc, ref, vendor) -> str:
        lid = self._lid()
        self.ledger.append(
            {
                "entry_id": lid,
                "date": d.isoformat(),
                "amount": f"{money(amount):.2f}",
                "account_code": code,
                "account_name": name,
                "description": desc,
                "reference": ref,
                "vendor": vendor,
            }
        )
        return lid

    # -- generators ------------------------------------------------------
    def gen_clean(self) -> None:
        for _ in range(self.cfg.n_clean):
            vendor, code, name, (lo, hi) = self.rng.choice(VENDORS)
            amount = round(self.rng.uniform(min(lo, hi), max(lo, hi)), 2)
            d = self._day()
            inv = self._inv()
            lid = self._add_ledger(d, amount, code, name, f"{vendor} - {inv}", inv, vendor)
            # bank posts 0-4 days after the GL entry
            bd = d + timedelta(days=self.rng.randint(0, 4))
            tid = self._add_bank(bd, amount, f"{vendor} PAYMENT {inv}"[:60], inv, vendor)
            self.truth.append(
                GroundTruthItem(tid, [lid], "", "exact", "clean 1:1 match")
            )

    def gen_split(self) -> None:
        """Customer pays several invoices in one bank credit."""
        for _ in range(self.cfg.n_split):
            vendor, code, name, (lo, hi) = self.rng.choice(
                [v for v in VENDORS if v[3][0] > 0]
            )
            d = self._day()
            lids: list[str] = []
            total = 0.0
            for _ in range(self.rng.randint(2, 4)):
                amt = round(self.rng.uniform(min(lo, hi) / 3, max(lo, hi) / 2), 2)
                inv = self._inv()
                lids.append(self._add_ledger(d, amt, code, name, f"{vendor} - {inv}", inv, vendor))
                total += amt
            tid = self._add_bank(
                d + timedelta(days=self.rng.randint(1, 5)),
                round(total, 2),
                f"{vendor} BATCH REMITTANCE",
                "",
                vendor,
            )
            self.truth.append(GroundTruthItem(tid, lids, "", "split", "split payment"))

    def gen_batch(self) -> None:
        """Processor settles many small sales in one deposit (net of fees)."""
        for _ in range(self.cfg.n_batch):
            d = self._day()
            lids: list[str] = []
            total = 0.0
            for _ in range(self.rng.randint(6, 12)):
                amt = round(self.rng.uniform(45, 900), 2)
                inv = self._inv()
                lids.append(
                    self._add_ledger(d, amt, "4000", "Product Revenue", f"Order {inv}", inv, "Stripe Payouts")
                )
                total += amt
            tid = self._add_bank(
                d + timedelta(days=1),
                round(total, 2),
                "STRIPE PAYOUT - DAILY SETTLEMENT",
                "",
                "Stripe Payouts",
            )
            self.truth.append(GroundTruthItem(tid, lids, "", "batch", "processor batch settlement"))

    def gen_timing_out(self) -> None:
        """Ledger entry recorded, bank has not cleared it -> outstanding cheque."""
        for _ in range(self.cfg.n_timing_out):
            vendor, code, name, (lo, hi) = self.rng.choice([v for v in VENDORS if v[3][0] < 0])
            d = self.month_start + timedelta(days=self.rng.randint(25, 28))
            amt = round(self.rng.uniform(min(lo, hi), max(lo, hi)), 2)
            inv = self._inv()
            lid = self._add_ledger(d, amt, code, name, f"{vendor} CHEQUE {inv}", inv, vendor)
            self.truth.append(
                GroundTruthItem("", [], "timing", "none", f"outstanding cheque, ledger only: {inv}",
                                side="ledger", entry_id=lid)
            )

    def gen_timing_in(self) -> None:
        """Cash received at the bank, not yet booked -> deposit in transit."""
        for _ in range(self.cfg.n_timing_in):
            vendor, code, name, (lo, hi) = self.rng.choice([v for v in VENDORS if v[3][0] > 0])
            d = self.month_start + timedelta(days=self.rng.randint(24, 28))
            amt = round(self.rng.uniform(min(lo, hi), max(lo, hi)), 2)
            tid = self._add_bank(d, amt, f"CASH DEPOSIT - {vendor}", "", vendor, "DEP")
            self.truth.append(
                GroundTruthItem(tid, [], "timing", "none", "deposit in transit")
            )

    def gen_fee(self) -> None:
        for _ in range(self.cfg.n_fee):
            desc, (lo, hi), _code = self.rng.choice(FEE_PATTERNS)
            amt = round(self.rng.uniform(lo, hi), 2)
            tid = self._add_bank(self._day(), amt, desc, "", "BANK", "FEE")
            self.truth.append(
                GroundTruthItem(tid, [], "fee", "none", f"bank charge not booked: {desc}")
            )

    def gen_fx(self) -> None:
        """Booked at invoice rate, bank settles at a slightly different rate."""
        for _ in range(self.cfg.n_fx):
            vendor, code, name, (lo, hi) = self.rng.choice(VENDORS)
            booked = round(self.rng.uniform(min(lo, hi), max(lo, hi)), 2)
            drift = round(booked * self.rng.uniform(0.004, 0.028), 2)
            settled = round(booked + drift, 2)
            d = self._day()
            inv = self._inv()
            lid = self._add_ledger(d, booked, code, name, f"{vendor} FX INVOICE {inv}", inv, vendor)
            tid = self._add_bank(d + timedelta(days=self.rng.randint(1, 4)), settled, f"{vendor} FX SETTLEMENT", inv, vendor)
            self.truth.append(
                GroundTruthItem(tid, [lid], "fx", "fx", f"FX variance {settled - booked:.2f}")
            )

    def gen_duplicate(self) -> None:
        """Same invoice booked twice in the GL; the bank only paid once."""
        for _ in range(self.cfg.n_duplicate):
            vendor, code, name, (lo, hi) = self.rng.choice([v for v in VENDORS if v[3][0] < 0])
            amt = round(self.rng.uniform(min(lo, hi), max(lo, hi)), 2)
            d = self._day()
            inv = self._inv()
            lid = self._add_ledger(d, amt, code, name, f"{vendor} - {inv}", inv, vendor)
            # the copy is keyed in without a vendor name -- which is exactly how
            # a re-keyed duplicate looks in a real GL export
            dup = self._add_ledger(
                d + timedelta(days=self.rng.randint(0, 2)), amt, code, name,
                f"{inv} (DUPLICATE ENTRY)", inv, ""
            )
            tid = self._add_bank(d + timedelta(days=self.rng.randint(1, 3)), amt, f"{vendor} PAYMENT {inv}", inv, vendor)
            self.truth.append(
                GroundTruthItem(tid, [lid], "duplicate", "exact", f"duplicate GL entry {dup}")
            )
            self.truth.append(
                GroundTruthItem("", [], "duplicate", "none", f"duplicate ledger entry {dup}",
                                side="ledger", entry_id=dup)
            )

    def gen_fraud(self) -> None:
        for _ in range(self.cfg.n_fraud):
            amt = round(-self.rng.uniform(300, 4200), 2)
            tid = self._add_bank(self._day(), amt, self.rng.choice(FRAUD_DESCRIPTIONS), "", "UNKNOWN", "POS")
            self.truth.append(
                GroundTruthItem(tid, [], "fraud_suspect", "none", "unexplained debit")
            )

    def gen_chatter(self) -> None:
        for _ in range(self.cfg.n_chatter):
            desc, (lo, hi) = self.rng.choice(BANK_CHATTER)
            amt = round(self.rng.uniform(lo, hi), 2)
            tid = self._add_bank(self._day(), amt, desc, "", "BANK", "INT")
            self.truth.append(
                GroundTruthItem(tid, [], "missing_entry", "none", f"unbooked bank item: {desc}")
            )

    # -- run -------------------------------------------------------------
    def generate(self) -> dict[str, Any]:
        self.gen_clean()
        self.gen_split()
        self.gen_batch()
        self.gen_timing_out()
        self.gen_timing_in()
        self.gen_fee()
        self.gen_fx()
        self.gen_duplicate()
        self.gen_fraud()
        self.gen_chatter()
        self.rng.shuffle(self.bank)
        self.rng.shuffle(self.ledger)
        return {
            "bank": self.bank,
            "ledger": self.ledger,
            "truth": [t.to_dict() for t in self.truth],
            "config": asdict(self.cfg),
        }


def write_dataset(outdir: str | Path, cfg: GeneratorConfig | None = None) -> dict[str, Any]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    data = SyntheticGenerator(cfg).generate()

    with open(out / "bank_statement.csv", "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["txn_id", "date", "amount", "description", "reference", "counterparty", "bank_code"],
        )
        w.writeheader()
        w.writerows(data["bank"])

    with open(out / "ledger_export.csv", "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["entry_id", "date", "amount", "account_code", "account_name", "description", "reference", "vendor"],
        )
        w.writeheader()
        w.writerows(data["ledger"])

    (out / "ground_truth.json").write_text(json.dumps(data["truth"], indent=2))
    (out / "generation_config.json").write_text(json.dumps(data["config"], indent=2))
    return data


if __name__ == "__main__":  # pragma: no cover
    d = write_dataset("sample")
    print(f"bank rows: {len(d['bank'])}  ledger rows: {len(d['ledger'])}  truth items: {len(d['truth'])}")
