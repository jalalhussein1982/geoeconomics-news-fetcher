from geoecon.llm.client import split_reasoning, LLMClient, LLMResponse


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
