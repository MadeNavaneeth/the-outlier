"""Experiential Labs (gpt-6-astra) provider + `ask` command."""

import io
import json
import urllib.error

import pytest

from outlier import cli as cli_mod
from outlier.cli import build_parser, main
from outlier.llm import (
    AnthropicProvider,
    CustomProvider,
    ExperientialLabsProvider,
    GeminiProvider,
    LLMError,
    MockProvider,
    OpenAIProvider,
    OpenRouterProvider,
    get_provider,
)


def _resp(payload):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return FakeResp()


def _http_error(code, body=b'{"error": {"message": "x"}}'):
    return urllib.error.HTTPError("http://gateway.test/v1/chat/completions", code, "err", {}, io.BytesIO(body))


def _provider(**kw):
    kw.setdefault("api_key", "xpl_test_key")
    kw.setdefault("retry_backoff", 0.0)
    return ExperientialLabsProvider(**kw)


# ----------------------------------------------------------------------
# provider selection
# ----------------------------------------------------------------------

def test_explabs_aliases_all_resolve(monkeypatch):
    monkeypatch.delenv("EXPLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for kind in ("explabs", "astra", "experiential", "experientiallabs", "gpt-6-astra"):
        assert isinstance(get_provider(kind), ExperientialLabsProvider), kind


def test_auto_prefers_explabs_when_its_key_is_present(monkeypatch):
    monkeypatch.setenv("EXPLABS_API_KEY", "xpl_abc")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc")
    assert isinstance(get_provider("auto"), ExperientialLabsProvider)


def test_auto_falls_back_to_mock_with_no_keys(monkeypatch):
    monkeypatch.delenv("EXPLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(get_provider("auto"), MockProvider)
    assert isinstance(get_provider("openai"), OpenAIProvider)


def test_named_provider_selection(monkeypatch):
    monkeypatch.delenv("EXPLABS_API_KEY", raising=False)
    assert isinstance(get_provider("openrouter"), OpenRouterProvider)
    assert isinstance(get_provider("gemini"), GeminiProvider)
    assert isinstance(get_provider("claude"), AnthropicProvider)
    assert isinstance(get_provider("custom"), CustomProvider)


def test_auto_provider_priority(monkeypatch):
    for key in ("EXPLABS_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OUTLIER_CUSTOM_API_KEY", "OUTLIER_CUSTOM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert isinstance(get_provider("auto"), GeminiProvider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert isinstance(get_provider("auto"), AnthropicProvider)


# ----------------------------------------------------------------------
# request shape
# ----------------------------------------------------------------------

def test_body_carries_reasoning_effort_and_no_temperature(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.get_header("Authorization")
        return _resp({"choices": [{"message": {"content": '{"a": 1}'}}], "usage": {}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = _provider(model="gpt-6-astra", reasoning_effort="max").complete_json("sys", "hi", "t", {})
    assert out == {"a": 1}
    assert seen["body"]["model"] == "gpt-6-astra"
    assert seen["body"]["reasoning_effort"] == "max"
    assert seen["body"]["stream"] is False
    assert "temperature" not in seen["body"]
    assert seen["auth"] == "Bearer xpl_test_key"


def test_openrouter_uses_its_endpoint_and_headers(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _resp({"choices": [{"message": {"content": '{"a": 1}'}}], "usage": {}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    p = OpenRouterProvider(api_key="router-key", model="provider/model")
    assert p.complete_json("sys", "hi", "t", {}) == {"a": 1}
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer router-key"
    assert seen["headers"]["X-title"] == "The Outlier"


def test_gemini_request_shape_and_usage(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _resp({
            "candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    p = GeminiProvider(api_key="gemini-key", model="gemini-test")
    assert p.complete_json("sys", "hi", "t", {}) == {"a": 1}
    assert ":generateContent?key=gemini-key" in seen["url"]
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "hi"
    assert (p.calls[0].prompt_tokens, p.calls[0].completion_tokens) == (7, 3)


def test_anthropic_request_shape_and_usage(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.data.decode())
        return _resp({
            "content": [{"type": "text", "text": '{"a": 1}'}],
            "usage": {"input_tokens": 9, "output_tokens": 4},
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    p = AnthropicProvider(api_key="anthropic-key", model="claude-test")
    assert p.complete_json("sys", "hi", "t", {}) == {"a": 1}
    assert seen["headers"]["X-api-key"] == "anthropic-key"
    assert seen["body"]["system"] == "sys"
    assert seen["body"]["model"] == "claude-test"
    assert (p.calls[0].prompt_tokens, p.calls[0].completion_tokens) == (9, 4)


def test_custom_provider_requires_a_base_url(monkeypatch):
    monkeypatch.delenv("OUTLIER_CUSTOM_BASE_URL", raising=False)
    p = CustomProvider(api_key="custom-key")
    out = p.complete_json("sys", "hi", "t", {"category": "unknown"})
    assert out == {"category": "unknown"}
    assert "OUTLIER_CUSTOM_BASE_URL" in p.calls[0].error


def test_missing_key_falls_back_and_records_failure(monkeypatch):
    monkeypatch.delenv("EXPLABS_API_KEY", raising=False)
    p = ExperientialLabsProvider(api_key="", retry_backoff=0.0)
    out = p.complete_json("sys", "hi", "t", fallback={"category": "unknown"})
    assert out == {"category": "unknown"}
    assert p.calls[0].ok is False
    assert "EXPLABS_API_KEY" in p.calls[0].error


def test_real_usage_tokens_are_recorded_when_present(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _resp({
            "choices": [{"message": {"content": '{"a": 1}'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    p = _provider()
    p.complete_json("sys", "hi", "t", {})
    assert (p.calls[0].prompt_tokens, p.calls[0].completion_tokens) == (11, 22)


# ----------------------------------------------------------------------
# retry behaviour
# ----------------------------------------------------------------------

def test_retries_a_429_then_succeeds(monkeypatch):
    calls = []
    sleeps = []
    responses = [_http_error(429), _resp({"choices": [{"message": {"content": '{"a": 1}'}}]})]

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    out = _provider().complete_json("sys", "hi", "t", {})
    assert out == {"a": 1}
    assert len(calls) == 2
    assert sleeps == []  # backoff was 0 in this provider, so no sleeping


def test_backoff_sleeps_between_attempts(monkeypatch):
    responses = [_http_error(503), _resp({"choices": [{"message": {"content": "{}"}}]})]
    sleeps = []

    def fake_urlopen(req, timeout=None):
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    p = ExperientialLabsProvider(api_key="xpl_test_key", retry_backoff=1.0)
    assert p.complete_json("sys", "hi", "t", {}) == {}
    assert sleeps == [1.0]


def test_no_retry_on_401_auth_errors(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise _http_error(401, b'{"error": {"message": "bad key"}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    p = _provider()
    out = p.complete_json("sys", "hi", "t", fallback={"category": "unknown"})
    assert out == {"category": "unknown"}
    assert len(calls) == 1
    assert "401" in p.calls[0].error


def test_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(_http_error(500)),
    )
    monkeypatch.setattr("time.sleep", lambda s: None)
    p = _provider()
    with pytest.raises(LLMError, match="after 3 attempts"):
        p._raw("sys", "hi")


# ----------------------------------------------------------------------
# ask command
# ----------------------------------------------------------------------

def test_ask_parser_defaults():
    args = build_parser().parse_args(["ask", "--question", "hi"])
    assert args.format == "text" and args.provider == "auto"


def test_ask_refuses_the_mock_provider(capsys):
    assert main(["ask", "--provider", "mock", "--question", "hi"]) == 1
    assert "mock provider" in capsys.readouterr().err


def test_ask_json_uses_the_json_contract(monkeypatch, capsys):
    class Stub:
        name = "stub"
        model = "stub-1"

        def complete_json(self, system, user, purpose, fallback):
            assert purpose == "ask"
            assert "JSON" in system
            return {"category": "fee", "confidence": 0.9}

    monkeypatch.setattr(cli_mod, "get_provider", lambda kind="auto": Stub())
    assert main(["ask", "--question", "fee?", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["category"] == "fee"


def test_ask_json_fails_cleanly_on_empty_output(monkeypatch, capsys):
    class Stub:
        name = "stub"
        model = "stub-1"

        def complete_json(self, system, user, purpose, fallback):
            return {}

    monkeypatch.setattr(cli_mod, "get_provider", lambda kind="auto": Stub())
    assert main(["ask", "--question", "fee?", "--format", "json"]) == 1
    assert "no usable JSON" in capsys.readouterr().err
