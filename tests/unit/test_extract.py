from geoecon.stages import extract

def test_extract_one_success():
    body, status, lang = extract.extract_one(
        "http://x", fetcher=lambda u: ("<html><body>" + "word " * 80 + "</body></html>"))
    assert status == "extracted" and body and "word" in body

def test_extract_one_empty_falls_back_to_headline_only():
    body, status, lang = extract.extract_one("http://x", fetcher=lambda u: "")
    assert status == "headline_only" and body is None

def test_extract_one_error_marks_failed():
    def boom(u): raise RuntimeError("timeout")
    body, status, lang = extract.extract_one("http://x", fetcher=boom)
    assert status == "failed" and body is None
