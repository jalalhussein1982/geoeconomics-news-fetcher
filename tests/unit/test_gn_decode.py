import base64

from geoecon.search import gn_decode


def _legacy_gn_url(target: str) -> str:
    # Legacy payload: 0x08 0x13 0x22 <len> <url bytes> (the pre-2024 frame).
    inner = b"\x08\x13\x22" + bytes([len(target)]) + target.encode()
    code = base64.urlsafe_b64encode(inner).decode().rstrip("=")
    return f"https://news.google.com/rss/articles/{code}?oc=5"


def test_offline_decodes_legacy_payload():
    url = _legacy_gn_url("https://example.com/story?id=1")
    assert gn_decode.decode_offline(url) == "https://example.com/story?id=1"


def test_offline_rejects_new_style_payload():
    # New-style payloads decode to an opaque token, not a URL.
    code = base64.urlsafe_b64encode(b"AU_yqLNoTaURLHere1234").decode().rstrip("=")
    assert gn_decode.decode_offline(f"https://news.google.com/rss/articles/{code}") is None


def test_decode_handles_garbage():
    assert gn_decode.decode("https://news.google.com/rss/articles/%%%", http=None) is None
    assert gn_decode.decode("https://example.com/not-google", http=None) is None


def test_online_decode_via_googlenewsdecoder(monkeypatch):
    # CONTINGENCY (2026-07-04): decode_online delegates to the googlenewsdecoder
    # package rather than the vendored GET+batchexecute recipe (see gn_decode
    # module docstring) -- the live test caught the vendored recipe stuck in a
    # consent redirect loop this environment's egress triggers. This test
    # verifies decode() falls through to that online path, and interprets its
    # {"status": ..., "decoded_url": ...} contract correctly, without hitting
    # the network.
    code = base64.urlsafe_b64encode(b"AU_yqLOpaque").decode().rstrip("=")
    url = f"https://news.google.com/rss/articles/{code}"
    monkeypatch.setattr(
        gn_decode, "gnewsdecoder",
        lambda gn_url: {"status": True, "decoded_url": "https://publisher.example/article"})
    got = gn_decode.decode(url)
    assert got == "https://publisher.example/article"


def test_online_decode_reports_none_on_failure_status(monkeypatch):
    code = base64.urlsafe_b64encode(b"AU_yqLOpaque").decode().rstrip("=")
    url = f"https://news.google.com/rss/articles/{code}"
    monkeypatch.setattr(
        gn_decode, "gnewsdecoder",
        lambda gn_url: {"status": False, "message": "boom"})
    assert gn_decode.decode(url) is None


import pytest
import requests as _requests


@pytest.mark.requires_net
def test_live_decode_one_real_link():
    import feedparser
    r = _requests.get(
        "https://news.google.com/rss/search?q=economy%20when:1d&hl=en&gl=US&ceid=US:en",
        timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    entries = feedparser.parse(r.content).entries
    assert entries, "GN feed empty -- endpoint changed?"
    decoded = gn_decode.decode(entries[0].link)
    assert decoded and "news.google.com" not in decoded
