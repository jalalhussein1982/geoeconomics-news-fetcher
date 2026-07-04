from types import SimpleNamespace

from geoecon.stages import extract

_HTML = ("<html><head><title>Saudi non-oil exports rise</title></head><body>"
         + "<article><h1>Saudi non-oil exports rise</h1><p>"
         + ("Riyadh reported a sharp rise in non-oil exports this quarter. " * 12)
         + "</p></article></body></html>")


def _cfg(**overrides):
    defaults = dict(EXTRACT_WORKERS="12", EXTRACT_TIMEOUT="20")
    defaults.update(overrides)
    return SimpleNamespace(
        extract_workers=int(defaults["EXTRACT_WORKERS"]),
        extract_timeout=int(defaults["EXTRACT_TIMEOUT"]),
    )


class FakeRepo:
    def __init__(self, arts):
        self._arts = arts
        self.updates = {}

    def articles_for_run(self, rid, fetch_status=None):
        return list(self._arts)

    def update_extraction(self, aid, body, status, source_lang=None,
                          fetch_error=None, title=None):
        self.updates[aid] = dict(body=body, status=status, source_lang=source_lang,
                                 fetch_error=fetch_error, title=title)


def test_extract_one_returns_body_title_status():
    body, title, status, lang = extract.extract_one("http://x", fetcher=lambda u: _HTML)
    assert status == "extracted"
    assert body and len(body) >= 200
    assert title == "Saudi non-oil exports rise"


def test_extract_one_failed_when_fetch_raises():
    def boom(u): raise RuntimeError("net")
    body, title, status, lang = extract.extract_one("http://x", fetcher=boom)
    assert status == "failed" and body is None


def test_run_passes_title_to_repo():
    captured = {}
    class FakeRepo:
        def articles_for_run(self, rid, fetch_status=None):
            from geoecon.state.models import Article
            return [Article(1, "http://x", "h", "d", "MENA", [], None, None, None,
                            None, None, "pending")]
        def update_extraction(self, aid, body, status, source_lang=None,
                              fetch_error=None, title=None):
            captured.update(aid=aid, status=status, title=title)
    out = extract.run(object(), FakeRepo(), 1, fetcher=lambda u: _HTML)
    assert out["extracted"] == 1
    assert captured["title"] == "Saudi non-oil exports rise"


def test_extract_run_parallel_processes_all_and_counts():
    # 30 articles, fetcher records which thread it ran on; workers=4
    import threading
    seen_threads = set()

    def fetcher(url):
        seen_threads.add(threading.current_thread().name)
        return "<html><body>" + ("x" * 300) + "</body></html>"

    arts = [SimpleNamespace(article_id=i, source_url=f"https://x/{i}") for i in range(30)]
    repo = FakeRepo(arts)
    counts = extract.run(_cfg(EXTRACT_WORKERS="4"), repo, 1, fetcher=fetcher)
    assert sum(counts.values()) == 30
    assert len(repo.updates) == 30                      # every article written exactly once
    assert len(seen_threads) > 1                         # actually ran on multiple threads


def test_extract_one_bad_fetch_isolated():
    def fetcher(url):
        if url.endswith("/13"):
            raise RuntimeError("boom")
        return None

    arts = [SimpleNamespace(article_id=i, source_url=f"https://x/{i}") for i in (12, 13, 14)]
    repo = FakeRepo(arts)
    counts = extract.run(_cfg(EXTRACT_WORKERS="3"), repo, 1, fetcher=fetcher)
    assert counts["failed"] == 1 and counts["headline_only"] == 2


def test_extract_run_serial_still_works():
    arts = [SimpleNamespace(article_id=i, source_url=f"https://x/{i}") for i in range(5)]
    repo = FakeRepo(arts)
    counts = extract.run(_cfg(EXTRACT_WORKERS="1"), repo, 1, fetcher=lambda u: _HTML)
    assert counts["extracted"] == 5
    assert len(repo.updates) == 5
