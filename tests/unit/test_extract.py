from geoecon.stages import extract

_HTML = ("<html><head><title>Saudi non-oil exports rise</title></head><body>"
         + "<article><h1>Saudi non-oil exports rise</h1><p>"
         + ("Riyadh reported a sharp rise in non-oil exports this quarter. " * 12)
         + "</p></article></body></html>")


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
