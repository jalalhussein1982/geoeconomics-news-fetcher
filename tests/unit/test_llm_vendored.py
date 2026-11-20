import pytest

from geoecon.llm.client import (split_reasoning, LLMClient, LLMResponse,
                                SystemicLLMFailure)

# The real 429 body from run 73's logs (2026-07-30). Verified against the live
# secret: the value LiteLLM echoes is sha256(api_key), NOT the key itself (the key
# is 35 chars, `sk-...`). So this is a caller identifier rather than a usable
# credential — but it is still account-identifying material that has no business
# in pod logs, and note that literal key-matching would never catch it. That is
# what `_KEY_ECHO_RE` is for.
_KEY = "sk-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk"          # shape of the real key
_HASHED = "6ecc91ed1eb2b399e55c518c5f2eb8f0957d5943fe0254289d5cd1238bb9493f"
_RATE_LIMIT_BODY = (
    '{"error":{"message":"Rate limit exceeded for api_key: ' + _HASHED +
    '. Limit type: max_parallel_requests. Current limit: 4, Remaining: 0.",'
    '"type":"None","param":"None","code":"429"}}')


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status; self.text = text
    def json(self): return {}


def _client_returning(monkeypatch, resp, **kw):
    """A client whose every POST yields `resp`, with sleeps disabled."""
    client = LLMClient("https://x/v1", _KEY, **kw)
    calls = []
    def post(*a, **k):
        calls.append(1); return resp
    monkeypatch.setattr(client.session, "post", post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return client, calls


def test_hashed_key_is_redacted_from_retryable_failure_logs(monkeypatch, caplog):
    """The production case, reproduced verbatim from run 73's logs: a 429 whose
    body carries sha256(api_key). The 2026-07-29 observability fix logged that
    body as-is, writing account-identifying material into the pod logs on every
    rate-limited attempt — 24 times on run 73 alone."""
    client, _ = _client_returning(monkeypatch, _Resp(429, _RATE_LIMIT_BODY), max_retries=1)

    with caplog.at_level("WARNING"):
        resp = client.complete("gemma4", [{"role": "user", "content": "hi"}])

    assert _HASHED not in caplog.text
    assert _HASHED not in (resp.error or "")
    # redaction must not destroy the diagnostic value of the message
    assert "429" in caplog.text and "max_parallel_requests" in caplog.text


def test_a_literally_echoed_key_is_also_redacted(monkeypatch, caplog):
    """Defence in depth: LiteLLM hashes today, but a config or version change that
    started echoing raw keys must not silently become a credential leak."""
    client, _ = _client_returning(
        monkeypatch, _Resp(429, "Rate limit exceeded for token " + _KEY), max_retries=0)

    with caplog.at_level("WARNING"):
        client.complete("gemma4", [{"role": "user", "content": "hi"}])

    assert _KEY not in caplog.text


def test_key_is_redacted_from_the_persisted_raw_body(monkeypatch):
    """Non-retryable errors keep body[:2000] in `raw`, which is persisted with the
    run — so redaction has to cover that path too, not just the log line."""
    client, _ = _client_returning(monkeypatch, _Resp(401, "denied for api_key: " + _HASHED))

    resp = client.complete("gemma4", [{"role": "user", "content": "hi"}])

    assert _HASHED not in resp.raw.get("body", "")
    assert _HASHED not in (resp.error or "")


def test_exhausted_calls_below_the_threshold_still_just_return_an_error(monkeypatch):
    client, _ = _client_returning(monkeypatch, _Resp(500, "boom"),
                                  max_retries=0, systemic_threshold=3)
    for _ in range(2):
        resp = client.complete("gemma4", [{"role": "user", "content": "hi"}])
        assert not resp.ok()


def test_raises_systemic_failure_after_n_consecutive_exhausted_calls(monkeypatch):
    """2026-07-28: every deepseek call burned all 5 attempts for 6h50m and the
    stage kept going, so the run neither failed nor produced a brief. N straight
    fully-exhausted calls is not flakiness — it is an outage, and must fail the
    run loudly instead of grinding until the deadline."""
    client, _ = _client_returning(monkeypatch, _Resp(500, "boom"),
                                  max_retries=0, systemic_threshold=3)
    for _ in range(2):
        client.complete("gemma4", [{"role": "user", "content": "hi"}])

    with pytest.raises(SystemicLLMFailure) as ei:
        client.complete("gemma4", [{"role": "user", "content": "hi"}])
    assert "gemma4" in str(ei.value)


def test_an_open_breaker_rejects_without_making_a_request(monkeypatch):
    """Once systemic, remaining queued batches must fail instantly. If each one
    still burned its full retry ladder, the ThreadPoolExecutor shutdown would
    drag the 'fast failure' out to hours across ~100 batches."""
    client, calls = _client_returning(monkeypatch, _Resp(500, "boom"),
                                      max_retries=0, systemic_threshold=1)
    with pytest.raises(SystemicLLMFailure):
        client.complete("gemma4", [{"role": "user", "content": "hi"}])
    before = len(calls)

    with pytest.raises(SystemicLLMFailure):
        client.complete("gemma4", [{"role": "user", "content": "hi"}])

    assert len(calls) == before, "open breaker must not hit the network"


def test_a_success_resets_the_consecutive_failure_counter(monkeypatch):
    """Transient 500s interleaved with successes must never trip the breaker."""
    client = LLMClient("https://x/v1", _KEY, max_retries=0, systemic_threshold=3)
    monkeypatch.setattr("time.sleep", lambda s: None)
    seq = [_Resp(500, "boom"), _Resp(500, "boom"), _Resp(200), _Resp(500, "boom"),
           _Resp(500, "boom"), _Resp(500, "boom")]
    monkeypatch.setattr(client.session, "post", lambda *a, **k: seq.pop(0))

    client.complete("gemma4", [{}]); client.complete("gemma4", [{}])
    assert client.complete("gemma4", [{}]).ok()          # success -> reset
    client.complete("gemma4", [{}]); client.complete("gemma4", [{}])

    with pytest.raises(SystemicLLMFailure):               # 3rd since the reset
        client.complete("gemma4", [{}])


def test_non_retryable_errors_do_not_trip_the_breaker(monkeypatch):
    """A 400 already fails fast, so it cannot cause the grind the breaker exists
    to stop. Counting it would turn one bad prompt into a failed run."""
    client, _ = _client_returning(monkeypatch, _Resp(400, "bad request"),
                                  systemic_threshold=2)
    for _ in range(5):
        assert not client.complete("gemma4", [{"role": "user", "content": "hi"}]).ok()


def test_a_zero_threshold_disables_the_breaker(monkeypatch):
    """Operational kill switch. If the breaker ever misfires at 01:00, it must be
    disableable by editing the CronJob env — not by rebuilding and pushing an
    image, which is what blocked every code fix during the 07-28/29 incidents."""
    client, _ = _client_returning(monkeypatch, _Resp(500, "boom"),
                                 max_retries=0, systemic_threshold=0)
    for _ in range(20):
        assert not client.complete("gemma4", [{"role": "user", "content": "hi"}]).ok()


def test_systemic_threshold_is_env_tunable():
    from geoecon.config import Config
    base = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    assert Config.from_env(base).llm_systemic_threshold == 5
    assert Config.from_env({**base, "LLM_SYSTEMIC_THRESHOLD": "0"}).llm_systemic_threshold == 0


def test_failure_counter_does_not_lose_increments_across_workers(monkeypatch):
    """The client is shared by GATE_WORKERS/SYNTH_WORKERS threads, so the counter
    must be lock-guarded — an unguarded `+= 1` can drop concurrent increments and
    silently raise the effective threshold. Race detector, not a proof."""
    from concurrent.futures import ThreadPoolExecutor
    client, _ = _client_returning(monkeypatch, _Resp(500, "boom"),
                                  max_retries=0, systemic_threshold=10_000)

    def burn():
        for _ in range(25):
            client.complete("gemma4", [{"role": "user", "content": "hi"}])

    with ThreadPoolExecutor(max_workers=8) as ex:
        [f.result() for f in [ex.submit(burn) for _ in range(8)]]

    assert client.consecutive_exhausted == 8 * 25


def test_split_reasoning_strips_think_block():
    final, reasoning = split_reasoning("<think>secret</think>ANSWER")
    assert final == "ANSWER" and "secret" in reasoning


def test_complete_parses_mocked_200(monkeypatch):
    client = LLMClient("https://x/v1", "key")

    class R:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5},
            }

    monkeypatch.setattr(client.session, "post", lambda *a, **k: R())
    resp = client.complete("deepseek-v3.2", [{"role": "user", "content": "hi"}])
    assert resp.ok() and resp.final_answer == '{"ok": true}'


def test_validator_importable():
    import geoecon.llm.validator as v
    assert v is not None


def test_retryable_failure_is_logged_not_swallowed(monkeypatch, caplog):
    """2026-07-28: the deepseek backend 500'd on every call for 6h50m and the
    client emitted NOTHING — it did not import logging at all, so ~12,300 doomed
    requests produced zero output and the hang was invisible. Every failure path
    must name the model, attempt, status and body."""
    client = LLMClient("https://x/v1", "key", max_retries=1)

    class R:
        status_code = 500
        text = "litellm.InternalServerError: no healthy deployments for deepseek"

    monkeypatch.setattr(client.session, "post", lambda *a, **k: R())
    monkeypatch.setattr("time.sleep", lambda s: None)

    with caplog.at_level("WARNING"):
        resp = client.complete("deepseek", [{"role": "user", "content": "hi"}])

    assert not resp.ok()
    text = caplog.text
    assert "deepseek" in text
    assert "500" in text
    # both the per-attempt retry warning and the final give-up must be visible
    assert any(r.levelname == "WARNING" for r in caplog.records), text
    assert any(r.levelname == "ERROR" for r in caplog.records), text


def test_non_retryable_failure_is_logged(monkeypatch, caplog):
    """2026-07-29: deepseek degraded to HTTP 400 'no healthy deployments'. 400 is
    not retryable, so it returns immediately — and must still say why."""
    client = LLMClient("https://x/v1", "key")

    class R:
        status_code = 400
        text = "litellm.BadRequestError: There are no healthy deployments for this model"

    monkeypatch.setattr(client.session, "post", lambda *a, **k: R())
    with caplog.at_level("ERROR"):
        resp = client.complete("deepseek", [{"role": "user", "content": "hi"}])

    assert not resp.ok()
    assert "400" in caplog.text and "deepseek" in caplog.text
