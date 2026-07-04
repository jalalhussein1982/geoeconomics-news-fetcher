from datetime import datetime, timezone

from geoecon.search import matrix
from geoecon.search.googlenews import GoogleNewsProvider

MX = next(s for s in matrix.load_countries() if s.fips == "MX")
IR = next(s for s in matrix.load_countries() if s.fips == "IR")

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Peso story</title>
 <link>https://news.google.com/rss/articles/TOK1?oc=5</link>
 <pubDate>Fri, 03 Jul 2026 22:00:00 GMT</pubDate>
 <source url="https://elpais.com">El Pais</source></item>
<item><title>Older story</title>
 <link>https://news.google.com/rss/articles/TOK2?oc=5</link>
 <pubDate>Thu, 02 Jul 2026 01:00:00 GMT</pubDate>
 <source url="https://x.mx">X</source></item>
</channel></rss>"""


class FakeHttp:
    def __init__(self): self.urls = []
    def get(self, url, **kw):
        self.urls.append(url)
        class R: content = RSS
        return R()


def _decode_ok(url, **kw):
    return {"https://news.google.com/rss/articles/TOK1?oc=5": "https://elpais.com/a1",
            "https://news.google.com/rss/articles/TOK2?oc=5": None}.get(url)


def test_search_builds_edition_url_and_decodes():
    http = FakeHttp()
    p = GoogleNewsProvider(http=http, decode=_decode_ok, pace_s=0, sleep=lambda s: None)
    items = p.search("(economía OR deuda)", MX, "es", 24, limit=10)
    assert "hl=es-419" in http.urls[0] and "gl=MX" in http.urls[0] and "ceid=MX%3Aes-419" in http.urls[0]
    assert "when%3A1d" in http.urls[0]
    assert len(items) == 2
    ok = next(i for i in items if i.decoded)
    assert ok.url == "https://elpais.com/a1" and ok.source_name == "El Pais"
    assert ok.ts == datetime(2026, 7, 3, 22, 0, tzinfo=timezone.utc)
    failed = next(i for i in items if not i.decoded)
    assert failed.url == "https://news.google.com/rss/articles/TOK2?oc=5"


def test_limit_keeps_newest_and_bounds_decodes():
    calls = []
    def counting_decode(url, **kw):
        calls.append(url); return "https://pub.example/" + str(len(calls))
    p = GoogleNewsProvider(http=FakeHttp(), decode=counting_decode, pace_s=0, sleep=lambda s: None)
    items = p.search("(x)", MX, "es", 24, limit=1)
    assert len(items) == 1 and len(calls) == 1          # decode only the kept item
    assert items[0].title == "Peso story"               # newest kept


def test_global_en_country_prefixes_name_and_uses_global_edition():
    http = FakeHttp()
    p = GoogleNewsProvider(http=http, decode=_decode_ok, pace_s=0, sleep=lambda s: None)
    p.search("(sanctions OR tariffs)", IR, "en", 48, limit=5)
    u = http.urls[0]
    assert "%22Iran%22" in u                            # quoted name ANDed into query
    assert "gl=US" in u and "ceid=US%3Aen" in u         # global edition, not gl=IR
    assert "when%3A2d" in u                             # 48h -> when:2d
