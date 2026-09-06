"""LLM layer.

Two providers, one contract:

* ``MockProvider``  -- offline, deterministic, zero API keys. It is NOT a
  canned-response stub: it scores the evidence the orchestrator hands it and
  returns the same JSON schema a real model must return. This is what lets a
  judge clone the repo and run the whole demo with no setup.
* ``OpenAIProvider`` -- any OpenAI-compatible chat-completions endpoint
  (OpenAI, Azure, Together, OpenRouter, vLLM, TensorMux gateway...).
* ``OpenRouterProvider`` and ``CustomProvider`` -- named configurations of the
  same OpenAI-compatible protocol.
* ``GeminiProvider`` -- Google's ``generateContent`` API.
* ``AnthropicProvider`` -- Claude's ``messages`` API.
* ``ExperientialLabsProvider`` -- the Experiential Labs gateway
  (``https://api.experientiallabs.ai/v1``) serving ``gpt-6-astra``.
  Same JSON contract as above, plus ``reasoning_effort`` support.
  Configure with ``EXPLABS_API_KEY`` (``OUTLIER_MODEL`` /
  ``EXPLABS_MODEL`` override the model name).

Usage (prompt/completion tokens) is recorded per call so the eval harness can
report cost per reconciliation run.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote


class LLMError(RuntimeError):
    pass


@dataclass
class Call:
    provider: str
    model: str
    purpose: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    ok: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "latency_ms": self.latency_ms,
            "ok": self.ok,
            "error": self.error,
        }


def est_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


class BaseProvider:
    name = "base"
    model = "none"

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._last_usage: dict[str, Any] | None = None

    def complete_json(self, system: str, user: str, purpose: str, fallback: dict[str, Any]) -> dict[str, Any]:
        """Return parsed JSON. On any failure, return ``fallback`` and record it."""
        t0 = time.time()
        prompt = system + "\n\n" + user
        self._last_usage = None
        try:
            raw = self._raw(system, user)
            data = _extract_json(raw)
            ok, err = True, ""
        except Exception as exc:  # network, bad JSON, rate limit...
            data, ok, err = fallback, False, f"{type(exc).__name__}: {exc}"
        usage = self._last_usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        call = Call(
            provider=self.name,
            model=self.model,
            purpose=purpose,
            prompt_tokens=prompt_tokens or est_tokens(prompt),
            completion_tokens=completion_tokens or est_tokens(json.dumps(data)),
            latency_ms=int((time.time() - t0) * 1000),
            ok=ok,
            error=err,
        )
        self.calls.append(call)
        self._last_usage = None
        return data

    def _raw(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def usage(self) -> dict[str, Any]:
        total = sum(c.prompt_tokens + c.completion_tokens for c in self.calls)
        return {
            "provider": self.name,
            "model": self.model,
            "calls": len(self.calls),
            "failed_calls": sum(1 for c in self.calls if not c.ok),
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "completion_tokens": sum(c.completion_tokens for c in self.calls),
            "total_tokens": total,
            "latency_ms": sum(c.latency_ms for c in self.calls),
        }


def _extract_json(raw: str) -> dict[str, Any]:
    """Models wrap JSON in prose / fences. Pull the first JSON object out."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        raise LLMError("expected a JSON object")
    except json.JSONDecodeError:
        pass
    start, depth = -1, 0
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = json.loads(raw[start : i + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    start, depth = -1, 0
    raise LLMError("no JSON object found in model output")


class OpenAIProvider(BaseProvider):
    name = "openai_compatible"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout: int = 60,
        headers: dict[str, str] | None = None,
    ):
        super().__init__()
        self.model = model or os.environ.get("OUTLIER_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.headers = headers or {}

    def _raw(self, system: str, user: str) -> str:
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY not set")
        body = json.dumps(
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        req_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.headers,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=req_headers,
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            self._last_usage = usage
        return payload["choices"][0]["message"]["content"]


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter's OpenAI-compatible chat-completions endpoint."""

    name = "openrouter"

    def __init__(self, **kwargs: Any):
        super().__init__(
            model=kwargs.pop("model", None) or os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            api_key=kwargs.pop("api_key", None) or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=kwargs.pop("base_url", None) or os.environ.get(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ),
            headers={
                **(
                    {"HTTP-Referer": os.environ["OPENROUTER_SITE_URL"]}
                    if os.environ.get("OPENROUTER_SITE_URL")
                    else {}
                ),
                "X-Title": os.environ.get("OPENROUTER_APP_NAME", "The Outlier"),
                **kwargs.pop("headers", {}),
            },
            **kwargs,
        )


class CustomProvider(OpenAIProvider):
    """A user-supplied OpenAI-compatible endpoint.

    ``OUTLIER_CUSTOM_BASE_URL`` and ``OUTLIER_CUSTOM_API_KEY`` are the
    canonical settings. The shorter ``CUSTOM_*`` names are accepted too.
    """

    name = "custom"

    def __init__(self, **kwargs: Any):
        base_url = (
            kwargs.pop("base_url", None)
            or os.environ.get("OUTLIER_CUSTOM_BASE_URL")
            or os.environ.get("CUSTOM_BASE_URL", "")
        )
        super().__init__(
            model=kwargs.pop("model", None)
            or os.environ.get("OUTLIER_CUSTOM_MODEL")
            or os.environ.get("CUSTOM_MODEL")
            or os.environ.get("OUTLIER_MODEL", "custom-model"),
            api_key=kwargs.pop("api_key", None)
            or os.environ.get("OUTLIER_CUSTOM_API_KEY")
            or os.environ.get("CUSTOM_API_KEY", ""),
            base_url=base_url,
            **kwargs,
        )
        if not base_url:
            self.base_url = ""

    def _raw(self, system: str, user: str) -> str:
        if not self.base_url:
            raise LLMError("OUTLIER_CUSTOM_BASE_URL not set")
        return super()._raw(system, user)


class GeminiProvider(BaseProvider):
    """Google Gemini ``generateContent`` provider."""

    name = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 60,
    ):
        super().__init__()
        self.model = model or os.environ.get("GEMINI_MODEL") or os.environ.get("OUTLIER_MODEL", "gemini-2.5-flash")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("GEMINI_BASE_URL", self.default_base_url)).rstrip("/")
        self.timeout = timeout

    def _raw(self, system: str, user: str) -> str:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY not set")
        body = json.dumps(
            {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
            }
        ).encode()
        url = f"{self.base_url}/models/{quote(self.model, safe='')}:generateContent?key={quote(self.api_key, safe='')}"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        usage = payload.get("usageMetadata") or {}
        if isinstance(usage, dict):
            self._last_usage = {
                "prompt_tokens": usage.get("promptTokenCount"),
                "completion_tokens": usage.get("candidatesTokenCount"),
            }
        try:
            return "".join(part.get("text", "") for part in payload["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected Gemini response shape: {str(payload)[:500]}")


class AnthropicProvider(BaseProvider):
    """Anthropic Claude ``messages`` provider."""

    name = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        timeout: int = 60,
    ):
        super().__init__()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", self.default_base_url)).rstrip("/")
        self.max_tokens = max_tokens or int(os.environ.get("OUTLIER_MAX_OUTPUT_TOKENS", "4096"))
        self.timeout = timeout

    def _raw(self, system: str, user: str) -> str:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY not set")
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            self._last_usage = usage
        try:
            return "".join(block.get("text", "") for block in payload["content"] if block.get("type") == "text")
        except (KeyError, TypeError):
            raise LLMError(f"unexpected Anthropic response shape: {str(payload)[:500]}")


class ExperientialLabsProvider(BaseProvider):
    """gpt-6-astra via the Experiential Labs gateway.

    Same ``complete_json(system, user, purpose, fallback)`` contract as every
    other provider, so ``exception_analyst`` / ``critic`` never branch on it.

    Env:
        EXPLABS_API_KEY            Bearer virtual key (``xpl_...``). Required.
        EXPLABS_BASE_URL           Default ``https://api.experientiallabs.ai/v1``.
        EXPLABS_MODEL / OUTLIER_MODEL  Default ``gpt-6-astra``.
        EXPLABS_REASONING_EFFORT   Default ``max`` (your curl used max).
        EXPLABS_TIMEOUT            Seconds, default 120 (reasoning takes longer).

    Transient gateway errors (HTTP 429 / 5xx, connection drops) are retried
    with exponential backoff; anything else fails fast into the caller's
    fallback, which routes the item to a human.
    """

    name = "experientiallabs"
    default_model = "gpt-6-astra"
    default_base_url = "https://api.experientiallabs.ai/v1"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        reasoning_effort: str | None = None,
        timeout: int | None = None,
        max_attempts: int = 3,
        retry_backoff: float = 1.0,
    ):
        super().__init__()
        self.model = (
            model
            or os.environ.get("EXPLABS_MODEL")
            or os.environ.get("OUTLIER_MODEL")
            or self.default_model
        )
        self.api_key = api_key or os.environ.get("EXPLABS_API_KEY", "")
        self.base_url = (base_url or os.environ.get("EXPLABS_BASE_URL", self.default_base_url)).rstrip("/")
        self.reasoning_effort = reasoning_effort or os.environ.get("EXPLABS_REASONING_EFFORT", "max")
        self.timeout = timeout or int(os.environ.get("EXPLABS_TIMEOUT", "120"))
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff = max(0.0, retry_backoff)
        self._last_usage: dict[str, Any] | None = None

    def complete_json(self, system: str, user: str, purpose: str, fallback: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        prompt = system + "\n\n" + user
        try:
            raw = self._raw(system, user)
            data = _extract_json(raw)
            ok, err = True, ""
        except Exception as exc:
            data, ok, err = fallback, False, f"{type(exc).__name__}: {exc}"
        if self._last_usage:
            pt = int(self._last_usage.get("prompt_tokens") or 0)
            ct = int(self._last_usage.get("completion_tokens") or 0)
            if pt <= 0:
                pt = est_tokens(prompt)
            if ct <= 0:
                ct = est_tokens(json.dumps(data))
        else:
            pt, ct = est_tokens(prompt), est_tokens(json.dumps(data))
        call = Call(
            provider=self.name,
            model=self.model,
            purpose=purpose,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=int((time.time() - t0) * 1000),
            ok=ok,
            error=err,
        )
        self.calls.append(call)
        self._last_usage = None
        return data

    def _raw(self, system: str, user: str) -> str:
        if not self.api_key:
            raise LLMError("EXPLABS_API_KEY not set")
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        encoded = json.dumps(body).encode()
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=encoded,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:2000]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code != 429 and exc.code < 500:
                    raise LLMError(last_error)
            except urllib.error.URLError as exc:
                last_error = f"connection failed: {exc.reason}"
            if attempt < self.max_attempts and self.retry_backoff > 0:
                time.sleep(self.retry_backoff * (2 ** (attempt - 1)))
        else:
            raise LLMError(f"gateway kept failing after {self.max_attempts} attempts: {last_error}")
        usage = payload.get("usage") or {}
        if isinstance(usage, dict) and usage:
            self._last_usage = usage
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected chat-completions shape: {str(payload)[:500]}")


class MockProvider(BaseProvider):
    """Deterministic offline 'model'.

    Scores evidence with transparent heuristics and injects a small amount of
    seeded imprecision, so the eval numbers are real measurements of the
    pipeline rather than a rigged 100%.
    """

    name = "mock"

    def __init__(self, error_rate: float = 0.06, seed: int = 13):
        super().__init__()
        self.model = "outlier-mock-1"
        self.error_rate = error_rate
        self.seed = seed
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.last_payload: dict[str, Any] = {}

    def register(self, purpose: str, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.handlers[purpose] = handler

    def _raw(self, system: str, user: str) -> str:
        payload = _extract_json(user) if user.strip().startswith("{") else _extract_json(user[user.index("{") :])
        self.last_payload = payload
        purpose = payload.get("purpose", "")
        handler = self.handlers.get(purpose)
        if handler is None:
            raise LLMError(f"mock provider has no handler for purpose={purpose!r}")
        out = dict(handler(payload))
        # Seeded imperfection so the metrics are honest rather than a rigged
        # 100%. The seed is keyed on the ITEM, not on the whole prompt: if it
        # were keyed on the prompt, adding learned-rule context to the prompt
        # would reshuffle which items get mislabelled and the improvement
        # chart would measure noise instead of learning.
        item = payload.get("item") or {}
        stable_key = f"{purpose}|{item.get('txn_id') or item.get('entry_id') or ''}|{item.get('description', '')}"
        rng = random.Random(int(hashlib.md5(stable_key.encode()).hexdigest()[:8], 16) ^ self.seed)
        if rng.random() < self.error_rate and out.get("category"):
            out["category"] = rng.choice(["unknown", "missing_entry", "timing"])
            out["confidence"] = round(min(float(out.get("confidence", 0.5)), 0.45), 2)
            out["degraded"] = True
        return json.dumps(out)


def get_provider(kind: str = "auto") -> BaseProvider:
    """Resolve a provider from a CLI name and environment-backed credentials."""
    kind = (kind or "auto").lower()
    if kind == "mock":
        return MockProvider()
    if kind == "openai":
        return OpenAIProvider()
    if kind in {"openrouter", "router"}:
        return OpenRouterProvider()
    if kind in {"gemini", "google"}:
        return GeminiProvider()
    if kind in {"anthropic", "claude"}:
        return AnthropicProvider()
    if kind in {"custom", "compatible"}:
        return CustomProvider()
    if kind in {"explabs", "astra", "experiential", "experientiallabs", "gpt-6-astra"}:
        return ExperientialLabsProvider()
    if os.environ.get("EXPLABS_API_KEY"):
        return ExperientialLabsProvider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiProvider()
    if os.environ.get("OPENROUTER_API_KEY"):
        return OpenRouterProvider()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider()
    if os.environ.get("OUTLIER_CUSTOM_API_KEY") and os.environ.get("OUTLIER_CUSTOM_BASE_URL"):
        return CustomProvider()
    return MockProvider()
