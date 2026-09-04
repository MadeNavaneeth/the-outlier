"""Human review desk.

A zero-dependency HTTP server (``http.server``) serving one HTML page with
inline CSS/JS. No build step, no CDN, no node_modules -- a judge can run it
with ``python3 closeloop.py serve`` and click through the queue.

Everything the page does is a POST to ``/api/...``; the JSON APIs are also
usable on their own, which is how the CLI's simulated reviewer and the UI stay
in sync.
"""

from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..ledger import ACCOUNT_BY_CODE, Ledger, chart_of_accounts
from ..models import ApprovedRule, JournalLine, ProposedEntry, next_id
from ..store import Store

mimetypes.add_type("application/javascript", ".js")


class Api:
    """All the state mutation lives here so the HTTP layer stays thin."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.store = Store(self.db_path)

    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        run = self.store.latest_run()
        if not run:
            return {"has_run": False}
        m = run["metrics"]
        decisions = self.store.decisions(run["run_id"])
        pending = [
            e for e in run["exceptions"]
            if e["needs_review"] and e["exception_id"] not in decisions
        ]
        return {
            "has_run": True,
            "run_id": run["run_id"],
            "round_no": run["round_no"],
            "created_at": run["created_at"],
            "bank_file": run["bank_file"],
            "ledger_file": run["ledger_file"],
            "metrics": m,
            "policy": run["config"]["policy"],
            "llm": run["config"].get("llm", {}),
            "counts": {
                "exceptions": len(run["exceptions"]),
                "queue": len([e for e in run["exceptions"] if e["needs_review"]]),
                "pending": len(pending),
                "decided": len(decisions),
                "auto_resolved": m.get("auto_resolved", 0),
                "rules": self.store.rule_count(),
                "matches": len(run["matches"]),
            },
            "runs": [
                {
                    "run_id": r["run_id"],
                    "round_no": r["round_no"],
                    "auto_resolve_rate": r["metrics"].get("auto_resolve_rate"),
                    "rule_hit_rate": r["metrics"].get("rule_hit_rate"),
                    "needs_review": r["metrics"].get("needs_review"),
                    "false_auto_posts": 0,
                }
                for r in self.store.all_runs()
            ],
        }

    def exceptions(self, only_queue: bool = True) -> list[dict[str, Any]]:
        run = self.store.latest_run()
        if not run:
            return []
        proposals = {p["proposal_id"]: p for p in run["proposals"]}
        decisions = self.store.decisions(run["run_id"])
        out = []
        for e in run["exceptions"]:
            if only_queue and not e["needs_review"]:
                continue
            d = decisions.get(e["exception_id"])
            item = dict(e)
            item["proposal"] = proposals.get(e["proposal_id"]) if e.get("proposal_id") else None
            item["decision"] = d
            out.append(item)
        out.sort(key=lambda x: (-abs(x["amount"]), x["exception_id"]))
        return out

    def matches(self, require_review: bool = False) -> list[dict[str, Any]]:
        run = self.store.latest_run()
        if not run:
            return []
        ms = [m for m in run["matches"] if (not require_review or m.get("requires_review"))]
        return sorted(ms, key=lambda m: -len(m["ledger_entry_ids"]))

    def rules(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.store.all_rules()]

    def audit(self, limit: int = 80) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.store.audit_events(limit=limit)]

    # ------------------------------------------------------------------
    def decide(self, exception_id: str, action: str, account_code: str = "", memo: str = "",
               reviewer: str = "human:desk") -> dict[str, Any]:
        run = self.store.latest_run()
        if not run:
            return {"ok": False, "error": "no run loaded"}
        if action not in {"approve", "edit", "reject"}:
            return {"ok": False, "error": f"unknown action {action!r}; expected approve, edit or reject"}
        exc = next((e for e in run["exceptions"] if e["exception_id"] == exception_id), None)
        if exc is None:
            return {"ok": False, "error": f"unknown exception {exception_id}"}
        proposals = {p["proposal_id"]: p for p in run["proposals"]}
        prop = proposals.get(exc["proposal_id"]) if exc.get("proposal_id") else None

        if action == "reject":
            decision = {"status": "REJECTED", "proposal": prop, "by": reviewer}
            self.store.save_decision(run["run_id"], exception_id, decision)
            self.store.audit(run["run_id"], reviewer, "review_rejected", "exception", exception_id,
                             category=exc["category"], amount=exc["amount"])
            return {"ok": True, "decision": decision, "rule_created": False}

        if prop is None:
            return {"ok": False, "error": "this exception has no journal entry to approve (it is a reconciling item or was vetoed)"}

        code = (account_code or prop.get("account_code") or "").strip()
        if code not in ACCOUNT_BY_CODE:
            return {"ok": False, "error": f"{code} is not in the chart of accounts"}

        amt = abs(float(prop.get("amount", exc["amount"])))
        original_amt = float(exc["amount"])
        acct = ACCOUNT_BY_CODE[code]
        if original_amt < 0:
            lines = [
                {"account_code": code, "account_name": acct.name, "debit": amt, "credit": 0.0,
                 "memo": memo or prop["lines"][0]["memo"]},
                {"account_code": "1000", "account_name": "Operating Checking", "debit": 0.0, "credit": amt,
                 "memo": memo or prop["lines"][1]["memo"]},
            ]
        else:
            credit_code = code if acct.type == "revenue" else "2300"
            lines = [
                {"account_code": "1000", "account_name": "Operating Checking", "debit": amt, "credit": 0.0,
                 "memo": memo or prop["lines"][0]["memo"]},
                {"account_code": credit_code, "account_name": ACCOUNT_BY_CODE[credit_code].name,
                 "debit": 0.0, "credit": amt, "memo": memo or prop["lines"][1]["memo"]},
            ]
        edited = dict(prop)
        edited["lines"] = lines
        edited["account_code"] = code
        edited["status"] = "EDITED" if action == "edit" else "APPROVED"
        edited["rationale"] = (prop.get("rationale", "") + f" [{reviewer} {'re-coded to ' + code if action == 'edit' else 'approved'}]").strip()

        decision = {"status": edited["status"], "proposal": edited, "by": reviewer}
        self.store.save_decision(run["run_id"], exception_id, decision)
        self.store.audit(run["run_id"], reviewer, f"review_{action}d", "exception", exception_id,
                         category=exc["category"], amount=original_amt, account_code=code)

        # ---- the learning step: a human decision becomes next month's rule ----
        sig = exc.get("evidence", {}).get("signature")
        rule_created = False
        if sig and exc["category"] not in ("fraud_suspect", "unknown"):
            if self.store.get_rule(sig, category=exc["category"]) is None:
                rule = ApprovedRule(
                    rule_id=next_id("RULE"),
                    kind="classification",
                    signature=sig,
                    payload={
                        "category": exc["category"],
                        "confidence": 0.95,
                        "explanation": exc.get("explanation", "")[:300],
                        "resolution": "journal_entry",
                        "account_code": code,
                        "lines": lines,
                        "vendor": exc.get("description", "")[:40],
                        "approved_status": "human_approved",
                        "amount_cap": abs(original_amt),
                    },
                    created_from=reviewer,
                    exception_id=exception_id,
                )
                self.store.add_rule(rule)
                rule_created = True
                self.store.audit(run["run_id"], reviewer, "rule_learned", "rule", rule.rule_id,
                                 signature=sig, category=exc["category"], amount_cap=abs(original_amt))
        return {"ok": True, "decision": decision, "rule_created": rule_created}

    # ------------------------------------------------------------------
    def post(self, reviewer: str = "human:desk") -> dict[str, Any]:
        run = self.store.latest_run()
        if not run:
            return {"ok": False, "error": "no run loaded"}
        ledger = Ledger.load(self.db_path.parent / "ledger.json")
        decisions = self.store.decisions(run["run_id"])
        posted = 0
        for exc in run["exceptions"]:
            d = decisions.get(exc["exception_id"])
            if not d or d["status"] == "REJECTED" or not d.get("proposal"):
                continue
            p = d["proposal"]
            pe = ProposedEntry(
                proposal_id=p["proposal_id"],
                exception_id=exc["exception_id"],
                lines=[JournalLine(**l) for l in p["lines"]],
                rationale=p.get("rationale", ""),
                amount=p.get("amount", 0.0),
                account_code=p.get("account_code", ""),
                source=reviewer,
            )
            if not pe.balanced:
                continue
            je = ledger.post(pe, run["run_id"], actor=reviewer)
            posted += 1
            self.store.audit(run["run_id"], reviewer, "posted_je", "journal_entry", je["je_id"],
                             proposal=pe.proposal_id, amount=pe.amount)
        return {"ok": True, "posted": posted, "gl_entries": ledger.count(), "bank_balance": ledger.balance("1000")}

    def coa(self) -> list[dict[str, Any]]:
        return chart_of_accounts()


# ----------------------------------------------------------------------
def _handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CloseLoop/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            qs = parse_qs(urlparse(self.path).query)
            if path in ("/", "/index.html"):
                self._send(200, page().encode(), "text/html; charset=utf-8")
                return
            try:
                if path == "/api/summary":
                    return self._json(api.summary())
                if path == "/api/exceptions":
                    return self._json(api.exceptions(only_queue=qs.get("queue", ["1"])[0] != "0"))
                if path == "/api/matches":
                    return self._json(api.matches(require_review=qs.get("review", ["0"])[0] == "1"))
                if path == "/api/rules":
                    return self._json(api.rules())
                if path == "/api/audit":
                    return self._json(api.audit(limit=int(qs.get("limit", ["80"])[0])))
                if path == "/api/coa":
                    return self._json(api.coa())
            except Exception as exc:  # pragma: no cover
                return self._json({"ok": False, "error": str(exc)}, 500)
            self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            try:
                if path == "/api/decide":
                    return self._json(api.decide(
                        body.get("exception_id", ""), body.get("action", ""),
                        account_code=body.get("account_code", ""), memo=body.get("memo", ""),
                        reviewer=body.get("reviewer", "human:desk")))
                if path == "/api/post":
                    return self._json(api.post(reviewer=body.get("reviewer", "human:desk")))
            except Exception as exc:  # pragma: no cover
                return self._json({"ok": False, "error": str(exc)}, 500)
            self._json({"ok": False, "error": "not found"}, 404)

    return Handler


def page() -> str:
    p = Path(__file__).with_name("static.html")
    return p.read_text(encoding="utf-8") if p.exists() else "<h1>missing static.html</h1>"


def serve(host: str = "0.0.0.0", port: int = 8000, db: str = "data/closeloop.db", reports: str = "reports") -> int:
    api = Api(db)
    httpd = ThreadingHTTPServer((host, port), _handler(api))
    print(f"CloseLoop review desk on http://{host}:{port}  (db={db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()
    return 0
