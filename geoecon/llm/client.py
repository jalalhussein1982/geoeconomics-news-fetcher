"""Single client abstraction for the e-INFRA CZ / CERIT-SC OpenAI-compatible LLM API.

Design goal (per spec): swapping the model under test is ONE field. Every model in
models.yaml is reached through the same endpoint, auth, retry, timing and token-
accounting path; only the `model` string changes.

Endpoint:  https://llm.ai.e-infra.cz/v1   (LiteLLM proxy, OpenAI /chat/completions schema)
Auth:      Bearer token from .env  (var MAETCENTRUM_KEY, fallback METACENTRUM_KEY)

Thinking models: the chat `content` sometimes embeds the reasoning trace in
<think>...</think> (or a separate `reasoning_content` field). We split that off so
downstream JSON parsing only ever sees the FINAL answer (`final_answer`), never the
trace. The untouched `content` is still kept in `raw`/`text` so nothing is discarded.
`truncated` is True when the API stopped on length (finish_reason == "length").
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("geoecon.llm")

# inline reasoning markers some models emit inside `content`
_THINK_RE = re.compile(r"<(think|thinking|reasoning|thought)>.*?</\1>", re.S | re.I)
_CLOSE_RE = re.compile(r"</(think|thinking|reasoning|thought)>", re.I)


def split_reasoning(content: str, reasoning_field: str | None = None) -> tuple[str, str]:
    """Return (final_answer, reasoning_text) for a possibly-thinking response.

    Strips paired <think>..</think> blocks; if a closing tag exists without a clean
    pair (dangling trace), keeps only what follows the LAST closing tag.
    """
    content = content or ""
    inline = "\n".join(m.group(0) for m in _THINK_RE.finditer(content))
    cleaned = _THINK_RE.sub("", content).strip()
    if _CLOSE_RE.search(content):                     # dangling/unpaired trace
        tail = _CLOSE_RE.split(content)[-1].strip()
        if tail:
            cleaned = tail
    reasoning = "\n".join(x for x in [(reasoning_field or "").strip(), inline.strip()] if x)
    return cleaned, reasoning


def load_env_file(start: Path | None = None) -> Path | None:
    """Populate os.environ from the nearest .env, walking up from `start`.
    Does NOT overwrite already-set vars and never prints values. Returns path used."""
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        f = d / ".env"
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return f
    return None


@dataclass
class LLMResponse:
    model: str
    text: str                            # FULL content as returned (never discarded)
    latency_ms: int
    timestamp: str                       # ISO-8601 UTC at request send
    final_answer: str = ""               # reasoning-stripped content (parse THIS)
    reasoning_text: str = ""             # extracted trace, if any
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    truncated: bool = False              # finish_reason == "length"
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def ok(self) -> bool:
        return self.error is None


class SystemicLLMFailure(RuntimeError):
    """The backend is systemically unavailable, not merely flaky.

    Raised once `SYSTEMIC_FAILURE_THRESHOLD` consecutive `complete()` calls have
    each exhausted their full retry ladder. On 2026-07-28 that condition held for
    6h50m — ~12,300 doomed requests — while the gate stage kept submitting
    batches, so the run neither failed nor produced a brief. Nothing catches this
    on the LLM path (the only handlers there are for JSONDecodeError), so it
    propagates through fut.result() to run_pipeline's `except Exception`, which
    records the stage failure and sends a crash alert naming the real cause.
    """


class LLMClient:
    RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
    SYSTEMIC_FAILURE_THRESHOLD = 5

    def __init__(self, base_url, api_key, *, timeout_s=240, max_retries=4,
                 default_params=None, session=None, systemic_threshold=None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.default_params = default_params or {}
        self.session = session or requests.Session()
        # 0 (or negative) disables the breaker entirely — an operational kill
        # switch settable from the CronJob env, so a misfiring breaker never needs
        # an image rebuild to defuse. `None` means "use the default".
        self.systemic_threshold = (self.SYSTEMIC_FAILURE_THRESHOLD
                                   if systemic_threshold is None else systemic_threshold)
        # Shared across GATE_WORKERS/SYNTH_WORKERS threads, so every read and
        # write of the counter is lock-guarded.
        self._lock = threading.Lock()
        self._consecutive_exhausted = 0

    @property
    def consecutive_exhausted(self) -> int:
        with self._lock:
            return self._consecutive_exhausted

    # `api_key: <token>`, `api-key=<token>` etc., whatever the gateway echoes.
    _KEY_ECHO_RE = re.compile(r"(api[_-]?key\S*\s*[:=]\s*)\S+", re.I)

    def _redact(self, text: str) -> str:
        """Strip credentials out of anything we are about to log or persist.

        LiteLLM's 429 body quotes the caller's key ("Rate limit exceeded for
        api_key: <token>"), and the 2026-07-29 observability fix started logging
        error bodies verbatim — 24 such lines on run 73 alone. Verified against the
        live secret on 2026-07-30: the echoed token is **sha256(api_key)**, not the
        key, so this is account-identifying material rather than a usable
        credential. It is scrubbed regardless, and note the consequence: matching
        `self._api_key` literally would never catch the hashed form, so the
        `api_key: <token>` pattern is what actually does the work here. The literal
        match remains as defence in case the gateway ever echoes raw keys.

        Redact before truncating, so a token straddling the cut cannot survive in part.
        """
        if not text:
            return text
        if self._api_key:
            text = text.replace(self._api_key, "***REDACTED***")
        return self._KEY_ECHO_RE.sub(r"\1***REDACTED***", text)

    def _note_success(self) -> None:
        with self._lock:
            self._consecutive_exhausted = 0

    def _note_exhausted(self) -> int:
        with self._lock:
            self._consecutive_exhausted += 1
            return self._consecutive_exhausted

    def _breaker_is_open(self) -> bool:
        if self.systemic_threshold <= 0:
            return False
        with self._lock:
            return self._consecutive_exhausted >= self.systemic_threshold

    @classmethod
    def from_config(cls, cfg, *, env_from=None):
        ep = cfg["endpoint"]
        load_env_file(env_from)
        key_env = ep.get("api_key_env", "MAETCENTRUM_KEY")
        api_key = os.environ.get(key_env) or os.environ.get("METACENTRUM_KEY")
        if not api_key:
            raise RuntimeError(
                f"No API key. Set {key_env} (or METACENTRUM_KEY) in env or a .env at/above "
                f"{env_from or Path.cwd()}.")
        return cls(ep["base_url"], api_key, timeout_s=ep.get("timeout_s", 240),
                   max_retries=ep.get("max_retries", 4), default_params=cfg.get("defaults", {}))

    def complete(self, model, messages, **params) -> LLMResponse:
        """Single chat completion. `model` is the only field that changes per model.
        default_params (temperature=0, seed, max_tokens) merge first; explicit params win."""
        # Circuit already open: reject immediately rather than burn another full
        # retry ladder. With ~100 gate batches queued behind 4 workers, letting
        # each one retry on the way down would stretch the "fast failure" this
        # breaker exists to produce back out into hours.
        if self._breaker_is_open():
            raise SystemicLLMFailure(
                f"{model}: circuit open after {self.systemic_threshold} consecutive "
                f"exhausted calls — refusing to send further requests")
        body = {**self.default_params, **params, "model": model, "messages": messages}
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        last_err = "unknown"
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            ts = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()
            try:
                r = self.session.post(url, headers=headers, json=body, timeout=self.timeout_s)
            except requests.RequestException as e:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                last_err = f"network: {self._redact(str(e))}"
                log.warning("llm %s attempt %d/%d failed after %dms: %s",
                            model, attempt + 1, attempts, latency_ms, last_err)
            else:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                if r.status_code == 200:
                    self._note_success()
                    return self._parse(model, r.json(), latency_ms, ts)
                last_err = f"http {r.status_code}: {self._redact(r.text)[:300]}"
                if r.status_code not in self.RETRYABLE:
                    # Terminal: no retry will help (e.g. 400 "no healthy
                    # deployments", 401, 404). Say so loudly and return. This does
                    # NOT feed the breaker: it already fails fast, so it cannot
                    # cause the grind the breaker exists to stop, and counting it
                    # would turn one malformed prompt into a failed run.
                    log.error("llm %s non-retryable after %dms: %s",
                              model, latency_ms, last_err)
                    return LLMResponse(model, "", latency_ms, ts, error=last_err,
                                       raw={"status": r.status_code,
                                            "body": self._redact(r.text)[:2000]})
                log.warning("llm %s attempt %d/%d failed after %dms: %s",
                            model, attempt + 1, attempts, latency_ms, last_err)
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt + random.random(), 30))
        # All attempts exhausted. This is the path that stayed silent for 6h50m
        # on 2026-07-28 while every deepseek call burned 5 attempts.
        log.error("llm %s EXHAUSTED all %d attempts, giving up: %s",
                  model, attempts, last_err)
        n = self._note_exhausted()
        if 0 < self.systemic_threshold <= n:
            log.error("llm %s SYSTEMIC FAILURE: %d consecutive exhausted calls, "
                      "failing the run", model, n)
            raise SystemicLLMFailure(
                f"{model}: {n} consecutive complete() calls exhausted all {attempts} "
                f"attempts — the backend appears systemically unavailable, not flaky. "
                f"Last error: {last_err}")
        return LLMResponse(model, "", 0, datetime.now(timezone.utc).isoformat(), error=last_err)

    @staticmethod
    def _parse(model, data, latency_ms, ts) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content") or ""
        reasoning_field = msg.get("reasoning_content") or msg.get("reasoning")
        final, reasoning = split_reasoning(content, reasoning_field)
        usage = data.get("usage", {}) or {}
        details = usage.get("completion_tokens_details", {}) or {}
        fr = choice.get("finish_reason")
        return LLMResponse(
            model=model, text=content, final_answer=final, reasoning_text=reasoning,
            latency_ms=latency_ms, timestamp=ts,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
            finish_reason=fr, truncated=(fr == "length"), raw=data)
