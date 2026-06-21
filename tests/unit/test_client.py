from geoecon.llm.client import LLMClient


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status; self._payload = payload; self.text = text
    def json(self): return self._payload


class _Session:
    def __init__(self, responses): self._responses = list(responses); self.calls = []
    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json); return self._responses.pop(0)


def _client(session, **kw):
    return LLMClient("http://x/v1", "key", session=session, **kw)


def test_embed_returns_vectors_in_input_order():
    payload = {"data": [{"index": 1, "embedding": [3.0, 4.0]},
                        {"index": 0, "embedding": [1.0, 2.0]}]}
    s = _Session([_Resp(200, payload)])
    out = _client(s).embed("m", ["a", "b"])
    assert out == [[1.0, 2.0], [3.0, 4.0]]          # sorted by index, input order
    assert s.calls[0] == {"model": "m", "input": ["a", "b"]}


def test_embed_batches_and_concatenates():
    s = _Session([_Resp(200, {"data": [{"index": 0, "embedding": [1.0]}]}),
                  _Resp(200, {"data": [{"index": 0, "embedding": [2.0]}]})])
    out = _client(s).embed("m", ["a", "b"], batch_size=1)
    assert out == [[1.0], [2.0]] and len(s.calls) == 2


def test_embed_returns_none_on_http_error():
    s = _Session([_Resp(500, text="boom")])
    out = _client(s, max_retries=0).embed("m", ["a"])
    assert out is None


def test_embed_returns_none_on_network_error():
    import requests
    class _BoomSession:
        def post(self, *a, **k): raise requests.RequestException("down")
    out = _client(_BoomSession(), max_retries=0).embed("m", ["a"])
    assert out is None
