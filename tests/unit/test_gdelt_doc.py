import json
from datetime import datetime, timezone

from geoecon import gdelt_doc

DOC_JSON = json.dumps({"articles": [
    {"url": "https://ahram.org.eg/x", "title": "T", "domain": "ahram.org.eg",
     "language": "Arabic", "seendate": "20260704T063000Z"},
    {"url": "https://bad.example/y", "title": "n", "domain": "bad.example",
     "language": "English", "seendate": "not-a-date"},
]})


class _Resp:
    def __init__(self, text, status=200): self.text, self.status_code = text, status


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def get(self, url, params=None, **kw):
        self.calls.append(params); return self.responses.pop(0)


def test_build_query():
    q = gdelt_doc.build_query("egypt", ["economy", '"central bank"'])
    assert q == 'sourcecountry:egypt (economy OR "central bank")'


def test_fetch_parses_articles_and_paces():
    slept = []
    http = FakeHttp([_Resp(DOC_JSON)])
    arts = gdelt_doc.fetch_articles("q", 24, http=http, sleep=slept.append, pace_s=6.0)
    assert len(arts) == 2
    assert arts[0].url == "https://ahram.org.eg/x"
    assert arts[0].seendate == datetime(2026, 7, 4, 6, 30, tzinfo=timezone.utc)
    assert arts[1].seendate is None                      # bad date -> None, kept
    assert http.calls[0]["timespan"] == "24h"
    assert http.calls[0]["maxrecords"] == 250
    assert slept == [6.0]                                # paces BEFORE the request


def test_429_retries_once_then_gives_up_empty():
    slept = []
    http = FakeHttp([_Resp("rate limited", 429), _Resp("rate limited", 429)])
    arts = gdelt_doc.fetch_articles("q", 24, http=http, sleep=slept.append)
    assert arts == [] and len(http.calls) == 2
    assert 30.0 in slept                                 # backoff between attempts


def test_non_json_body_returns_empty():
    http = FakeHttp([_Resp("<html>error</html>", 200)])
    assert gdelt_doc.fetch_articles("q", 24, http=http, sleep=lambda s: None) == []


import pytest


@pytest.mark.requires_net
def test_live_egypt_query_returns_articles():
    from geoecon.search import matrix
    kw = matrix.load_keywords()
    q = gdelt_doc.build_query("egypt", kw["family1"]["en"][:6])
    arts = gdelt_doc.fetch_articles(q, 48)
    assert arts, ("0 articles for Egypt/48h -- likely sourcecountry or query "
                  "syntax drift; verify manually before proceeding")
