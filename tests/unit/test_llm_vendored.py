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
