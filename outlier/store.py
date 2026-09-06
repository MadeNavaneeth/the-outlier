"""Persistent state: rules ("memory") + append-only audit trail + run store.

Single SQLite file, three tables. No ORM, no migrations, stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ApprovedRule, AuditEvent, RunResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    signature TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_from TEXT,
    exception_id TEXT,
    hits INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS rules_sig ON rules(signature);

CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    round_no INTEGER,
    created_at TEXT,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #: check_same_thread=False is required because the review desk serves
        #: each request on its own thread. Safety comes from the lock below:
        #: every public method takes it, so the connection is only ever touched
        #: by one thread at a time. Without this every API call raises
        #: "SQLite objects created in a thread can only be used in that same
        #: thread" -- which is exactly what the first live run of the desk did.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------
    # rules / memory
    # ------------------------------------------------------------------
    def add_rule(self, rule: ApprovedRule) -> None:
        self._exec(
            "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?,?)",
            (
                rule.rule_id,
                rule.kind,
                rule.signature,
                json.dumps(rule.payload),
                rule.created_from,
                rule.exception_id,
                rule.hits,
                rule.created_at,
            ),
        )

    def get_rule(self, signature: str, kind: str | None = None, category: str | None = None) -> ApprovedRule | None:
        """Fetch the approved rule for a signature.

        ``category`` matters: digit-stripped signatures collide (a cheque line
        and a duplicate-entry line can normalise to the same string), so a rule
        is only returned when the category a human approved matches the
        category the system predicts for this item. Otherwise a duplicate rule
        can silently overwrite a correct timing classification -- which is a
        bug we hit and fixed.
        """
        q = "SELECT * FROM rules WHERE signature = ?"
        args: list[Any] = [signature]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY created_at DESC"
        for row in self._rows(q, tuple(args)):
            rule = self._row_to_rule(row)
            if category is None or rule.payload.get("category") == category:
                return rule
        return None

    def all_rules(self) -> list[ApprovedRule]:
        return [self._row_to_rule(r) for r in self._rows("SELECT * FROM rules ORDER BY created_at")]

    def bump_rule(self, rule_id: str) -> None:
        self._exec("UPDATE rules SET hits = hits + 1 WHERE rule_id = ?", (rule_id,))

    def rule_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM rules")
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> ApprovedRule:
        return ApprovedRule(
            rule_id=row["rule_id"],
            kind=row["kind"],
            signature=row["signature"],
            payload=json.loads(row["payload"]),
            created_from=row["created_from"] or "",
            exception_id=row["exception_id"] or "",
            hits=row["hits"] or 0,
            created_at=row["created_at"] or "",
        )

    def lookup(self, signature: str, vendor: str = "", category: str | None = None) -> dict[str, Any]:
        """Tool-facing view: exact signature rule + any vendor-level rules."""
        exact = self.get_rule(signature, category=category)
        vendor_rules = [
            r.to_dict()
            for r in self.all_rules()
            if vendor and (r.payload.get("vendor", "").lower() == vendor.lower())
        ]
        return {
            "signature": signature,
            "vendor": vendor,
            "exact_rule": exact.to_dict() if exact else None,
            "vendor_rules": vendor_rules[:5],
            "n_vendor_rules": len(vendor_rules),
        }

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    def audit(
        self,
        run_id: str,
        actor: str,
        action: str,
        entity_type: str = "",
        entity_id: str = "",
        **detail: Any,
    ) -> int:
        cur = self._exec(
            "INSERT INTO audit (ts, run_id, actor, action, entity_type, entity_id, detail) VALUES (?,?,?,?,?,?,?)",
            (datetime.now(UTC).isoformat(timespec="seconds"), run_id, actor, action, entity_type, entity_id, json.dumps(detail, default=str)),
        )
        return int(cur.lastrowid)

    def audit_events(self, run_id: str | None = None, limit: int = 500) -> list[AuditEvent]:
        if run_id:
            rows = self._rows("SELECT * FROM audit WHERE run_id = ? ORDER BY seq LIMIT ?", (run_id, limit))
        else:
            rows = self._rows("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [
            AuditEvent(
                seq=r["seq"],
                ts=r["ts"],
                run_id=r["run_id"] or "",
                actor=r["actor"],
                action=r["action"],
                entity_type=r["entity_type"] or "",
                entity_id=r["entity_id"] or "",
                detail=json.loads(r["detail"] or "{}"),
            )
            for r in rows
        ]

    def audit_trail_json(self, run_id: str) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.audit_events(run_id, limit=100000)]

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------
    def save_run(self, result: RunResult) -> None:
        self._exec(
            "INSERT OR REPLACE INTO runs (run_id, round_no, created_at, payload) VALUES (?,?,?,?)",
            (result.run_id, result.round_no, result.created_at, json.dumps(result.to_dict(), default=str)),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT payload FROM runs WHERE run_id = ?", (run_id,))
        return json.loads(row["payload"]) if row else None

    def latest_run(self) -> dict[str, Any] | None:
        row = self._one("SELECT payload FROM runs ORDER BY round_no DESC, created_at DESC LIMIT 1")
        return json.loads(row["payload"]) if row else None

    def all_runs(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT payload FROM runs ORDER BY round_no ASC, created_at ASC")
        return [json.loads(r["payload"]) for r in rows]

    def update_run_metrics(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Persist evaluator fields that are computed after reconciliation.

        The reconciler can run without ground truth, so evaluation is attached
        later by the CLI. Keeping the selected fields on the run makes the
        learning summary identical in the CLI, browser, and report artifacts.
        """
        run = self.get_run(run_id)
        if run is None:
            return None
        run.setdefault("metrics", {}).update(updates)
        self._exec("UPDATE runs SET payload = ? WHERE run_id = ?",
                   (json.dumps(run, default=str), run_id))
        return run

    # ------------------------------------------------------------------
    # review decisions (so the UI survives a restart)
    # ------------------------------------------------------------------
    def save_decision(self, run_id: str, exception_id: str, decision: dict[str, Any]) -> None:
        self._exec(
            "CREATE TABLE IF NOT EXISTS decisions (run_id TEXT, exception_id TEXT, payload TEXT, PRIMARY KEY (run_id, exception_id))"
        )
        self._exec("INSERT OR REPLACE INTO decisions VALUES (?,?,?)",
                   (run_id, exception_id, json.dumps(decision, default=str)))

    def decisions(self, run_id: str) -> dict[str, dict[str, Any]]:
        try:
            rows = self._rows("SELECT exception_id, payload FROM decisions WHERE run_id = ?", (run_id,))
        except sqlite3.OperationalError:
            return {}
        return {r["exception_id"]: json.loads(r["payload"]) for r in rows}
