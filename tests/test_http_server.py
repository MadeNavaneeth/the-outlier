"""The review desk over real HTTP.

This exists because the API-object tests all passed while the live server
returned 500 on every endpoint: ``ThreadingHTTPServer`` serves each request on a
new thread and the SQLite connection was bound to the main one. Only an actual
multi-threaded HTTP request reproduces it.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from outlier.agents.orchestrator import Orchestrator
from outlier.config import Policy
from outlier.llm import MockProvider
from outlier.store import Store
from outlier.web.server import Api, _handler


@pytest.fixture
def server(dataset, tmp_path):
    store = Store(tmp_path / "outlier.db")
    Orchestrator(MockProvider(), store, policy=Policy()).run(
        dataset / "bank_statement.csv", dataset / "ledger_export.csv", run_id="RUN-HTTP", round_no=1
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Api(tmp_path / "outlier.db")))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


class _NoRaise(urllib.request.HTTPErrorProcessor):
    """Let us assert on 4xx responses instead of catching HTTPError."""

    def http_response(self, request, response):
        return response


def _open(req):
    opener = urllib.request.build_opener(_NoRaise)
    with opener.open(req, timeout=10) as r:
        return r.status, r.read().decode()


def get(url):
    status, body = _open(url)
    return status, json.loads(body)


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    status, text = _open(req)
    return status, json.loads(text)


def test_page_serves(server):
    with urllib.request.urlopen(server + "/", timeout=10) as r:
        html = r.read().decode()
    assert r.status == 200
    assert "The Outlier" in html and "<script>" in html
    assert "Learning lab" in html and "Close room" in html


def test_every_endpoint_answers_on_a_worker_thread(server):
    for path in ("/api/summary", "/api/exceptions", "/api/rules", "/api/audit?limit=5",
                 "/api/matches?review=1", "/api/command-center", "/api/learning", "/api/coa"):
        status, body = get(server + path)
        assert status == 200, path
        assert "SQLite objects created in a thread" not in json.dumps(body), path


def test_command_center_endpoint_returns_prioritized_actions(server):
    status, body = get(server + "/api/command-center")
    assert status == 200
    assert body["run_id"] == "RUN-HTTP"
    assert body["summary"]["open_actions"] > 0
    assert body["actions"][0]["priority"] == 1


def test_concurrent_requests_do_not_corrupt_state(server):
    status, summary = get(server + "/api/summary")
    assert summary["counts"]["queue"] > 0

    _, items = get(server + "/api/exceptions")
    targets = [e["exception_id"] for e in items if e["proposal"]][:5]

    errors = []

    def decide(eid):
        try:
            s, b = post(server + "/api/decide", {"exception_id": eid, "action": "approve"})
            if s != 200 or not b.get("ok"):
                errors.append((eid, b))
        except Exception as exc:  # pragma: no cover
            errors.append((eid, str(exc)))

    threads = [threading.Thread(target=decide, args=(eid,)) for eid in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    _, after = get(server + "/api/summary")
    assert after["counts"]["decided"] == len(targets)
    assert after["counts"]["rules"] >= 1


def test_decide_and_post_over_http(server):
    _, items = get(server + "/api/exceptions")
    target = next(e for e in items if e["proposal"])
    status, res = post(server + "/api/decide", {"exception_id": target["exception_id"], "action": "approve"})
    assert status == 200 and res["ok"] and res["rule_created"]

    status, res = post(server + "/api/post", {})
    assert status == 200 and res["ok"] and res["posted"] == 1


def test_bad_requests_are_rejected_not_crashed(server):
    status, res = post(server + "/api/decide", {"exception_id": "NOPE", "action": "approve"})
    assert status == 200 and res["ok"] is False
    status, res = post(server + "/api/decide", {"exception_id": "NOPE", "action": "teleport"})
    assert res["ok"] is False and "unknown action" in res["error"]


def test_unknown_paths_return_json_404(server):
    status, body = get(server + "/api/nope")
    assert status == 404
    assert body["ok"] is False


def test_malformed_json_is_a_400_not_a_crash(server):
    req = urllib.request.Request(server + "/api/decide", data=b"{not json",
                                 headers={"Content-Type": "application/json"})
    status, body = _open(req)
    assert status == 400
    assert json.loads(body)["ok"] is False
