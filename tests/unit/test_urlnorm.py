from geoecon.urlnorm import normalize_url, normalized_url_hash


def test_amp_utm_bare_variants_collapse():
    """The required property: AMP, utm-tagged, and bare variants -> one hash."""
    bare = "https://www.example.com/news/article-123"
    amp = "https://www.example.com/news/article-123/amp"
    utm = "https://example.com/news/article-123?utm_source=twitter&utm_medium=social"
    h = normalized_url_hash(bare)
    assert normalized_url_hash(amp) == h
    assert normalized_url_hash(utm) == h
    assert normalize_url(bare) == "https://example.com/news/article-123"


def test_strips_known_trackers_only():
    assert normalize_url("https://x.com/a?gclid=1&fbclid=2&ref=hp&cmpid=z&mc_cid=q&at_x=1") \
        == "https://x.com/a"
    assert normalize_url("https://x.com/a?amp=1") == "https://x.com/a"


def test_keeps_meaningful_params():
    # Distinct article ids must NOT collapse (conservative: keep ?id=, ?p=).
    assert normalize_url("https://x.com/story?id=42") == "https://x.com/story?id=42"
    assert normalized_url_hash("https://x.com/s?id=42") != normalized_url_hash("https://x.com/s?id=43")
    # a meaningful param survives alongside a stripped tracker
    assert normalize_url("https://x.com/s?id=42&utm_source=z") == "https://x.com/s?id=42"


def test_query_order_independent():
    assert normalized_url_hash("https://x.com/s?a=1&b=2") == \
        normalized_url_hash("https://x.com/s?b=2&a=1")


def test_fragment_slash_and_case():
    assert normalize_url("HTTPS://WWW.Example.com//news//a/#section") == \
        "https://example.com/news/a"
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_unwraps_redirect_only_when_target_in_url():
    # destination present in a query param -> unwrap to it
    wrapped = "https://news.google.com/articles/abc?url=https://example.com/news/article-123&utm_source=x"
    assert normalized_url_hash(wrapped) == normalized_url_hash("https://example.com/news/article-123")
    # opaque wrapper (no recoverable target) -> left as-is, just normalized
    opaque = "https://news.google.com/rss/articles/CBMiOpaqueBlob"
    assert normalize_url(opaque) == "https://news.google.com/rss/articles/CBMiOpaqueBlob"
