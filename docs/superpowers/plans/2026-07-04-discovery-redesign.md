# Discovery Redesign Implementation Plan (Phases 1–3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace supply-driven discovery with a four-layer query-driven ingestion: Google News RSS spine (new), GDELT demoted to the DOC 2.0 API, registry trimmed to tier-0 primaries — all behind flags, converging into the existing `articles` table.

**Architecture:** A new `geoecon/search/` package (provider interface + Google News adapter + query matrix loaded from `config/countries.yaml` / `config/keywords.yaml`) feeds a new `discovery_search` stage that mirrors the `discovery_registry` contract (window-filter, per-source isolation, idempotent `insert_article`). `stages/discovery.py` gains a `GDELT_MODE=docapi` path using a new `geoecon/gdelt_doc.py` client. `cluster.py`'s curated nudge learns to match by domain so the registry trim doesn't kill the editorial preference.

**Tech Stack:** Python 3.12, requests, feedparser, PyYAML, pytest. **No new runtime dependencies** (one contingency noted in Task 3).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-04-discovery-redesign-design.md`.
- New behavior ships OFF: `SEARCH_DISCOVERY` default `false`, `GDELT_MODE` default `gkg`. Deploy flips env vars (Phase 4, not in this plan).
- Downstream stages (extract/gate/cluster/rank/synth/send) unchanged except the curated-domain match in `cluster.py` (Task 9).
- Recall bias everywhere: items with `ts=None` are KEPT (the gate judges); only provably-out-of-window items are dropped.
- One bad source/feed/query must never kill a stage — catch, WARN, count, continue (pattern: `discovery_registry.py:76-80`).
- Pacing: Google News ≥1.0s between feed fetches; GDELT DOC ≥6.0s between queries (probe showed exactly-5s trips the limiter), backoff 30s on 429.
- Reuters stays excluded (ToS). No scraping of news.google.com beyond RSS + the documented decode endpoint.
- Tests: run with `.venv/bin/python -m pytest tests/unit/... -v` from repo root. Network tests carry `@pytest.mark.requires_net` (marker exists in pyproject).
- Tests use a fixed `NOW = datetime(2026, 7, 4, 7, 0, tzinfo=timezone.utc)`; never `datetime.now()` in assertions.
- Commit after every task; messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Config knobs

**Files:**
- Modify: `geoecon/config.py` (dataclass fields after `country_cap`, ~line 48; `from_env` after `country_cap=`, ~line 90)
- Test: `tests/unit/test_config.py` (append)

**Interfaces:**
- Produces: `cfg.search_discovery: bool`, `cfg.search_country_cap: int`, `cfg.countries_path: str | None`, `cfg.keywords_path: str | None`, `cfg.gdelt_mode: str` — read by Tasks 5, 6, 8.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_config.py` (reuse that file's existing `_env()`/base-env helper if present; otherwise this local one):

```python
def _base_env(**over):
    env = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
           "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    env.update(over)
    return env


def test_search_discovery_defaults_off():
    cfg = Config.from_env(_base_env())
    assert cfg.search_discovery is False
    assert cfg.search_country_cap == 25
    assert cfg.countries_path is None
    assert cfg.keywords_path is None
    assert cfg.gdelt_mode == "gkg"


def test_search_discovery_env_overrides():
    cfg = Config.from_env(_base_env(SEARCH_DISCOVERY="true", SEARCH_COUNTRY_CAP="10",
                                    COUNTRIES_PATH="/tmp/c.yaml", KEYWORDS_PATH="/tmp/k.yaml",
                                    GDELT_MODE="docapi"))
    assert cfg.search_discovery is True
    assert cfg.search_country_cap == 10
    assert cfg.countries_path == "/tmp/c.yaml"
    assert cfg.keywords_path == "/tmp/k.yaml"
    assert cfg.gdelt_mode == "docapi"
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_config.py -v` — expect the two new tests FAIL (`AttributeError: search_discovery`).

- [ ] **Step 3: Implement** — in `geoecon/config.py` add fields after `country_cap` (line 48):

```python
    # Discovery redesign (spec 2026-07-04). All default-off / inert until Phase 4 flips env.
    search_discovery: bool = False        # SEARCH_DISCOVERY; enables discovery_search stage
    search_country_cap: int = 25          # SEARCH_COUNTRY_CAP; max inserts per provider+country+family feed
    countries_path: str | None = None     # COUNTRIES_PATH; None => config/countries.yaml
    keywords_path: str | None = None      # KEYWORDS_PATH; None => config/keywords.yaml
    gdelt_mode: str = "gkg"               # GDELT_MODE: gkg (legacy files) | docapi (DOC 2.0)
```

and in `from_env(...)` after the `country_cap=` line:

```python
            search_discovery=env.get("SEARCH_DISCOVERY", "false").lower() == "true",
            search_country_cap=int(env.get("SEARCH_COUNTRY_CAP", "25")),
            countries_path=env.get("COUNTRIES_PATH") or None,
            keywords_path=env.get("KEYWORDS_PATH") or None,
            gdelt_mode=env.get("GDELT_MODE", "gkg"),
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_config.py -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "feat(config): discovery-redesign knobs (SEARCH_DISCOVERY, GDELT_MODE, caps, matrix paths)"
```

---

### Task 2: Coverage matrix — `countries.yaml`, `keywords.yaml`, loader

**Files:**
- Create: `config/countries.yaml`
- Create: `config/keywords.yaml`
- Create: `geoecon/search/__init__.py` (empty)
- Create: `geoecon/search/matrix.py`
- Modify: `geoecon/regions.py:29-35` (north_africa bucket) and `:78-90` (`_NAME_TO_FIPS`)
- Test: `tests/unit/test_search_matrix.py`, `tests/unit/test_regions.py` (append)

**Interfaces:**
- Produces:
  - `CountrySpec` frozen dataclass: `fips: str`, `name: str`, `gl: str`, `langs: tuple[str, ...]`, `gdelt_sc: str`, `global_en: bool = False`, `ceid: dict[str, str]` (per-lang overrides, default `{}`).
  - `load_countries(path: str | None = None) -> list[CountrySpec]` (None → `config/countries.yaml` resolved like `registry.default_path()` does for sources).
  - `load_keywords(path: str | None = None) -> dict[str, dict[str, list[str]]]` — `{"family1": {lang: [terms]}, "family2": {...}}`.
  - `or_expr(terms: list[str]) -> str` — `(t1 OR t2 OR ...)`, terms already quoted in YAML where needed.
  - `hl_for(lang: str) -> str` and `ceid_for(spec, lang) -> str`.
- Consumed by Tasks 4, 5, 8.

- [ ] **Step 0: Extend `regions.py` for Sudan + Mauritania** (spec's North-Africa list includes them; the buckets currently don't). Append to `tests/unit/test_regions.py`:

```python
def test_sudan_and_mauritania_are_north_africa():
    assert regions.buckets_for_country("SU") == ["north_africa"]
    assert regions.buckets_for_country("MR") == ["north_africa"]
    assert regions.country_to_fips("sudan") == "SU"
    assert regions.country_to_fips("mauritania") == "MR"
```

Run it (FAIL), then in `geoecon/regions.py` add to the `"north_africa"` set (line ~34): `"SU",  # Sudan` and `"MR",  # Mauritania`; and to `_NAME_TO_FIPS` (line ~84): `"sudan": "SU", "mauritania": "MR",`. Re-run (PASS).

- [ ] **Step 1: Write `config/countries.yaml`** — the full coverage definition. FIPS is the pipeline's country key (`regions.py` buckets); `gl` is Google's ISO code (they differ: Chile CI→CL, Turkey TU→TR, Tunisia TS→TN, Algeria AG→DZ, Morocco MO→MA, Bolivia BL→BO, Paraguay PA→PY, Panama PM→PA…). `gdelt_sc` is GDELT DOC's spaceless-name form:

```yaml
# Coverage definition for query-driven discovery (spec 2026-07-04).
# fips: pipeline country key (matches geoecon/regions.py buckets)
# gl: Google News ISO country; langs: query languages (keys into keywords.yaml)
# gdelt_sc: GDELT DOC 2.0 sourcecountry (spaceless name form)
# global_en: no viable local-language vocabulary -> query the global English
#   edition with the country name prefixed (e.g. Iran/Farsi).
# ceid: optional per-lang ceid override when Google's edition id != "GL:hl".
countries:
  # ---- Middle East (bucket: middle_east) ----
  - {fips: BA, name: Bahrain,   gl: BH, langs: [ar], gdelt_sc: bahrain}
  - {fips: IR, name: Iran,      gl: IR, langs: [],   gdelt_sc: iran, global_en: true}
  - {fips: IZ, name: Iraq,      gl: IQ, langs: [ar], gdelt_sc: iraq}
  - {fips: IS, name: Israel,    gl: IL, langs: [he, en], gdelt_sc: israel}
  - {fips: JO, name: Jordan,    gl: JO, langs: [ar], gdelt_sc: jordan}
  - {fips: KU, name: Kuwait,    gl: KW, langs: [ar], gdelt_sc: kuwait}
  - {fips: LE, name: Lebanon,   gl: LB, langs: [ar, fr], gdelt_sc: lebanon}
  - {fips: MU, name: Oman,      gl: OM, langs: [ar], gdelt_sc: oman}
  - {fips: QA, name: Qatar,     gl: QA, langs: [ar], gdelt_sc: qatar}
  - {fips: SA, name: Saudi Arabia, gl: SA, langs: [ar, en], gdelt_sc: saudiarabia}
  - {fips: SY, name: Syria,     gl: SY, langs: [ar], gdelt_sc: syria}
  - {fips: AE, name: United Arab Emirates, gl: AE, langs: [ar, en], gdelt_sc: unitedarabemirates}
  - {fips: YM, name: Yemen,     gl: YE, langs: [ar], gdelt_sc: yemen}
  - {fips: TU, name: Turkey,    gl: TR, langs: [tr], gdelt_sc: turkey}
  - {fips: CY, name: Cyprus,    gl: CY, langs: [en], gdelt_sc: cyprus}
  # WE covers the Palestinian territories incl. Gaza (FIPS GZ intentionally has
  # no separate entry -- one query space; gate/bucketing still handle GZ rows).
  - {fips: WE, name: Palestine, gl: PS, langs: [ar], gdelt_sc: palestine}
  # ---- Egypt: dual-bucketed (middle_east + north_africa) via regions.py ----
  - {fips: EG, name: Egypt,     gl: EG, langs: [ar, en], gdelt_sc: egypt}
  # ---- North Africa (bucket: north_africa) ----
  - {fips: LY, name: Libya,     gl: LY, langs: [ar], gdelt_sc: libya}
  - {fips: TS, name: Tunisia,   gl: TN, langs: [ar, fr], gdelt_sc: tunisia}
  - {fips: AG, name: Algeria,   gl: DZ, langs: [ar, fr], gdelt_sc: algeria}
  - {fips: MO, name: Morocco,   gl: MA, langs: [ar, fr], gdelt_sc: morocco}
  - {fips: SU, name: Sudan,     gl: SD, langs: [ar], gdelt_sc: sudan}
  - {fips: MR, name: Mauritania, gl: MR, langs: [ar, fr], gdelt_sc: mauritania}
  # ---- Latin America (bucket: latam) ----
  - {fips: BR, name: Brazil,    gl: BR, langs: [pt], gdelt_sc: brazil, ceid: {pt: "BR:pt-419"}}
  - {fips: AR, name: Argentina, gl: AR, langs: [es], gdelt_sc: argentina}
  - {fips: MX, name: Mexico,    gl: MX, langs: [es], gdelt_sc: mexico}
  - {fips: CI, name: Chile,     gl: CL, langs: [es], gdelt_sc: chile}
  - {fips: CO, name: Colombia,  gl: CO, langs: [es], gdelt_sc: colombia}
  - {fips: PE, name: Peru,      gl: PE, langs: [es], gdelt_sc: peru}
  - {fips: VE, name: Venezuela, gl: VE, langs: [es], gdelt_sc: venezuela}
  - {fips: EC, name: Ecuador,   gl: EC, langs: [es], gdelt_sc: ecuador}
  - {fips: BL, name: Bolivia,   gl: BO, langs: [es], gdelt_sc: bolivia}
  - {fips: PA, name: Paraguay,  gl: PY, langs: [es], gdelt_sc: paraguay}
  - {fips: UY, name: Uruguay,   gl: UY, langs: [es], gdelt_sc: uruguay}
  - {fips: GT, name: Guatemala, gl: GT, langs: [es], gdelt_sc: guatemala}
  - {fips: CU, name: Cuba,      gl: CU, langs: [es], gdelt_sc: cuba}
  - {fips: DR, name: Dominican Republic, gl: DO, langs: [es], gdelt_sc: dominicanrepublic}
  - {fips: HO, name: Honduras,  gl: HN, langs: [es], gdelt_sc: honduras}
  - {fips: NU, name: Nicaragua, gl: NI, langs: [es], gdelt_sc: nicaragua}
  - {fips: CS, name: Costa Rica, gl: CR, langs: [es], gdelt_sc: costarica}
  - {fips: PM, name: Panama,    gl: PA, langs: [es], gdelt_sc: panama}
  - {fips: ES, name: El Salvador, gl: SV, langs: [es], gdelt_sc: elsalvador}
```

- [ ] **Step 2: Write `config/keywords.yaml`** — two families × 7 languages. Multi-word terms pre-quoted so `or_expr` can just join:

```yaml
# Query vocabulary (spec 2026-07-04). family1 = economy core; family2 =
# geopolitics x economy intersection ("economic news framed politically").
# Recall-biased by design: the LLM gate owns precision. Multi-word terms are
# pre-quoted. Edit freely -- no code change needed.
family1:
  en: [economy, '"central bank"', inflation, GDP, '"interest rate"', currency, '"exchange rate"', exports, trade, debt, investment, budget]
  ar: [اقتصاد, '"البنك المركزي"', تضخم, '"الناتج المحلي"', '"سعر الفائدة"', عملة, '"سعر الصرف"', صادرات, تجارة, ديون, استثمار, موازنة]
  es: [economía, '"banco central"', inflación, PIB, '"tasa de interés"', divisa, '"tipo de cambio"', exportaciones, comercio, deuda, inversión, presupuesto]
  pt: [economia, '"banco central"', inflação, PIB, '"taxa de juros"', câmbio, exportações, comércio, dívida, investimento, orçamento]
  tr: [ekonomi, '"merkez bankası"', enflasyon, '"faiz oranı"', döviz, kur, ihracat, ticaret, borç, yatırım, bütçe]
  he: [כלכלה, '"בנק ישראל"', אינפלציה, ריבית, מטבע, '"שער חליפין"', יצוא, סחר, חוב, השקעה, תקציב]
  fr: [économie, '"banque centrale"', inflation, PIB, '"taux d''intérêt"', devise, '"taux de change"', exportations, commerce, dette, investissement, budget]
family2:
  en: [sanctions, tariffs, '"trade war"', embargo, boycott, OPEC, '"export controls"', IMF, '"World Bank"', default, '"debt restructuring"', '"currency swap"', de-dollarization, BRICS, nationalization, '"sovereign wealth fund"', '"trade agreement"']
  ar: [عقوبات, '"رسوم جمركية"', '"حرب تجارية"', حظر, مقاطعة, أوبك, '"صندوق النقد الدولي"', '"البنك الدولي"', '"تخلف عن السداد"', '"إعادة هيكلة الديون"', تأميم, '"صندوق سيادي"', بريكس, '"اتفاقية تجارية"']
  es: [sanciones, aranceles, '"guerra comercial"', embargo, boicot, OPEP, FMI, '"Banco Mundial"', '"cesación de pagos"', '"reestructuración de deuda"', nacionalización, '"fondo soberano"', BRICS, '"acuerdo comercial"']
  pt: [sanções, tarifas, '"guerra comercial"', embargo, boicote, OPEP, FMI, '"Banco Mundial"', calote, '"reestruturação da dívida"', nacionalização, '"fundo soberano"', BRICS, '"acordo comercial"']
  tr: [yaptırım, '"gümrük vergisi"', '"ticaret savaşı"', ambargo, boykot, OPEC, IMF, '"Dünya Bankası"', temerrüt, '"borç yapılandırması"', kamulaştırma, '"varlık fonu"', BRICS, '"ticaret anlaşması"']
  he: [סנקציות, מכסים, '"מלחמת סחר"', אמברגו, חרם, '"קרן המטבע"', '"הבנק העולמי"', '"חדלות פירעון"', הלאמה, '"קרן עושר"', BRICS]
  fr: [sanctions, '"droits de douane"', '"guerre commerciale"', embargo, boycott, OPEP, FMI, '"Banque mondiale"', '"défaut de paiement"', '"restructuration de la dette"', nationalisation, '"fonds souverain"', BRICS, '"accord commercial"']
```

- [ ] **Step 3: Write the failing test** — `tests/unit/test_search_matrix.py`:

```python
from geoecon.search import matrix
from geoecon import regions


def test_load_countries_covers_all_bucket_fips_once():
    specs = matrix.load_countries()
    fips = [s.fips for s in specs]
    assert len(fips) == len(set(fips))                      # no dup entries
    bucket_fips = set().union(*regions.BUCKETS.values())
    # GZ is deliberately folded into WE (Palestine) -- the only allowed gap.
    assert bucket_fips - set(fips) == {"GZ"}
    assert set(fips) <= bucket_fips                          # no out-of-scope countries


def test_country_fields():
    by_fips = {s.fips: s for s in matrix.load_countries()}
    assert by_fips["CI"].gl == "CL"                          # FIPS Chile -> ISO CL
    assert by_fips["IR"].global_en and by_fips["IR"].langs == ()
    assert by_fips["EG"].langs == ("ar", "en")
    assert by_fips["BR"].ceid == {"pt": "BR:pt-419"}
    assert by_fips["SA"].gdelt_sc == "saudiarabia"


def test_load_keywords_families_and_langs():
    kw = matrix.load_keywords()
    assert set(kw) == {"family1", "family2"}
    for fam in kw.values():
        assert set(fam) == {"en", "ar", "es", "pt", "tr", "he", "fr"}
        for terms in fam.values():
            assert len(terms) >= 8


def test_or_expr_and_edition_helpers():
    assert matrix.or_expr(["a", '"b c"']) == '(a OR "b c")'
    specs = {s.fips: s for s in matrix.load_countries()}
    assert matrix.hl_for("es") == "es-419"
    assert matrix.ceid_for(specs["MX"], "es") == "MX:es-419"
    assert matrix.ceid_for(specs["BR"], "pt") == "BR:pt-419"   # explicit override
    assert matrix.ceid_for(specs["EG"], "ar") == "EG:ar"
```

- [ ] **Step 4: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_search_matrix.py -v` — FAIL (`ModuleNotFoundError: geoecon.search`).

- [ ] **Step 5: Implement `geoecon/search/__init__.py`** (empty file) **and `geoecon/search/matrix.py`:**

```python
"""Coverage matrix: countries x languages x keyword families (spec 2026-07-04).

`config/countries.yaml` is THE coverage definition; `config/keywords.yaml` is
the two-family query vocabulary. Both are data, editable without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# Google News interface language per keyword language. LATAM editions use the
# regional es-419 / pt-419 forms (validated in the 2026-07-04 probe).
_HL = {"ar": "ar", "es": "es-419", "pt": "pt-BR", "tr": "tr", "he": "he",
       "fr": "fr", "en": "en"}


@dataclass(frozen=True)
class CountrySpec:
    fips: str
    name: str
    gl: str
    langs: tuple[str, ...]
    gdelt_sc: str
    global_en: bool = False
    ceid: dict[str, str] = field(default_factory=dict)


def load_countries(path: str | None = None) -> list[CountrySpec]:
    p = Path(path) if path else _CONFIG_DIR / "countries.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    out = []
    for row in data["countries"]:
        out.append(CountrySpec(
            fips=row["fips"], name=row["name"], gl=row["gl"],
            langs=tuple(row.get("langs") or ()),
            gdelt_sc=row["gdelt_sc"],
            global_en=bool(row.get("global_en", False)),
            ceid=dict(row.get("ceid") or {}),
        ))
    return out


def load_keywords(path: str | None = None) -> dict[str, dict[str, list[str]]]:
    p = Path(path) if path else _CONFIG_DIR / "keywords.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def or_expr(terms: list[str]) -> str:
    return "(" + " OR ".join(terms) + ")"


def hl_for(lang: str) -> str:
    return _HL[lang]


def ceid_for(spec: CountrySpec, lang: str) -> str:
    return spec.ceid.get(lang) or f"{spec.gl}:{hl_for(lang)}"
```

- [ ] **Step 6: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_search_matrix.py -v` — all PASS.

- [ ] **Step 7: Commit**

```bash
git add config/countries.yaml config/keywords.yaml geoecon/search/ tests/unit/test_search_matrix.py
git commit -m "feat(search): coverage matrix -- countries.yaml (42 countries incl. Sudan/Mauritania), keywords.yaml (2 families x 7 langs), loader"
```

---

### Task 3: `SearchItem` + Google News URL decoder

**Files:**
- Create: `geoecon/search/base.py`
- Create: `geoecon/search/gn_decode.py`
- Test: `tests/unit/test_gn_decode.py`

**Interfaces:**
- Produces:
  - `SearchItem` dataclass: `url: str`, `title: str | None = None`, `ts: datetime | None = None`, `source_name: str | None = None`, `lang: str | None = None`, `decoded: bool = True`.
  - `SearchProvider` Protocol: `name: str`; `search(self, expr: str, spec, lang: str, window_hours: int, limit: int) -> list[SearchItem]`.
  - `gn_decode.decode(gn_url: str, *, http=requests, timeout: int = 20) -> str | None` — publisher URL or None. Tries offline base64 first (zero network), then the batchexecute endpoint (2 HTTP calls).
- Consumed by Tasks 4, 5.

- [ ] **Step 1: Write `geoecon/search/base.py`:**

```python
"""Search-provider contract (spec 2026-07-04). Mirrors fetchers/base.py:
a provider takes one query and returns links it can see right now; bodies are
the extraction stage's job. Adding a vendor = one adapter file, no stage change."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class SearchItem:
    url: str
    title: str | None = None
    ts: datetime | None = None          # publish time if known; None => keep (gate judges)
    source_name: str | None = None
    lang: str | None = None
    decoded: bool = True                # False => url is still an aggregator redirect


class SearchProvider(Protocol):
    name: str

    def search(self, expr: str, spec, lang: str, window_hours: int,
               limit: int) -> list[SearchItem]:
        ...
```

- [ ] **Step 2: Write the failing decoder test** — `tests/unit/test_gn_decode.py`. The offline fixture is a real legacy-format Google News URL (base64 payload wraps `https://example.com/story` in the old protobuf frame — built programmatically so the test is self-verifying):

```python
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


class _FakeResp:
    def __init__(self, text): self.text = text


class _FakeHttp:
    """Serves the article page (with data attrs) then the batchexecute reply."""
    def __init__(self):
        self.posts = []
    def get(self, url, **kw):
        return _FakeResp('<c-wiz><div data-n-a-sg="SIG123" data-n-a-ts="99"></div></c-wiz>')
    def post(self, url, **kw):
        self.posts.append(kw)
        inner = '[["garturlres","https://publisher.example/article",1]]'
        import json
        outer = json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "generic"]])
        return _FakeResp(")]}'\n\n" + outer)


def test_online_decode_via_batchexecute():
    code = base64.urlsafe_b64encode(b"AU_yqLOpaque").decode().rstrip("=")
    url = f"https://news.google.com/rss/articles/{code}"
    got = gn_decode.decode(url, http=_FakeHttp())
    assert got == "https://publisher.example/article"
```

- [ ] **Step 3: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_gn_decode.py -v` — FAIL (module missing).

- [ ] **Step 4: Implement `geoecon/search/gn_decode.py`:**

```python
"""Decode news.google.com/rss/articles/<token> links to publisher URLs.

Google News RSS links don't resolve with a plain GET (they land on
consent.google.com -- verified 2026-07-04). Two strategies, cheapest first:

1. offline: base64-decode the path token. Pre-2024 tokens embed the URL in a
   tiny protobuf frame; still true for a share of links. Zero network.
2. online: the token needs Google's own decoder -- GET the article page for
   its data-n-a-sg / data-n-a-ts params, then POST the DotsSplashUi
   batchexecute endpoint (the approach the googlenewsdecoder package uses).

Both return None on any failure; callers keep the encoded URL and count it
(telemetry), never raise.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

import requests

from ..fetchers.http import BROWSER_UA

_TOKEN_RE = re.compile(r"news\.google\.com/(?:rss/articles|articles|read)/([^?/&#]+)")
_URL_RE = re.compile(rb"https?://[\x21-\x7e]+")
_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def _token(gn_url: str) -> str | None:
    m = _TOKEN_RE.search(gn_url or "")
    return m.group(1) if m else None


def decode_offline(gn_url: str) -> str | None:
    tok = _token(gn_url)
    if not tok:
        return None
    try:
        raw = base64.urlsafe_b64decode(tok + "===")
    except Exception:
        return None
    m = _URL_RE.search(raw)
    if not m:
        return None
    url = m.group(0).decode(errors="replace")
    # The frame length-prefixes the URL; the regex can overrun into trailing
    # protobuf bytes for the AMP variant -- cut at the first non-URL byte the
    # length byte declared. Simplest robust trim: stop at first byte < 0x20
    # already excluded; drop a trailing partial field marker if present.
    url = url.split("\xd2")[0]
    return url if url.startswith("http") else None


def _decoding_params(tok: str, http, timeout: int) -> tuple[str, str] | None:
    r = http.get(f"https://news.google.com/rss/articles/{tok}",
                 headers={"User-Agent": BROWSER_UA}, timeout=timeout)
    sig = re.search(r'data-n-a-sg="([^"]+)"', r.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
    return (sig.group(1), ts.group(1)) if sig and ts else None


def decode_online(gn_url: str, *, http=requests, timeout: int = 20) -> str | None:
    tok = _token(gn_url)
    if not tok:
        return None
    try:
        params = _decoding_params(tok, http, timeout)
        if not params:
            return None
        sig, ts = params
        req = ["Fbv4je",
               f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
               f'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,'
               f'null,0],"{tok}",{ts},"{sig}"]']
        payload = "f.req=" + quote(json.dumps([[req]]))
        r = http.post(_BATCH_URL, data=payload, timeout=timeout,
                      headers={"User-Agent": BROWSER_UA,
                               "Content-Type":
                               "application/x-www-form-urlencoded;charset=UTF-8"})
        body = r.text.split("\n\n", 1)[-1]
        data = json.loads(body)
        inner = json.loads(data[0][2])
        url = inner[1]
        return url if isinstance(url, str) and url.startswith("http") else None
    except Exception:
        return None


def decode(gn_url: str, *, http=requests, timeout: int = 20) -> str | None:
    got = decode_offline(gn_url)
    if got:
        return got
    if http is None:
        return None
    return decode_online(gn_url, http=http, timeout=timeout)
```

- [ ] **Step 5: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_gn_decode.py -v` — all PASS.

- [ ] **Step 6: Live smoke test (network)** — append to `tests/unit/test_gn_decode.py`:

```python
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
```

Run: `.venv/bin/python -m pytest tests/unit/test_gn_decode.py -m requires_net -v`
Expected: PASS. **Contingency:** if this live test fails, Google's batchexecute frame drifted — add the `googlenewsdecoder>=0.1.7` PyPI package as a dependency and reimplement `decode_online`'s body as a call into it (same signature, same None-on-failure semantics). Do NOT ship with the live test failing silently.

- [ ] **Step 7: Commit**

```bash
git add geoecon/search/base.py geoecon/search/gn_decode.py tests/unit/test_gn_decode.py
git commit -m "feat(search): SearchItem/SearchProvider contract + Google News URL decoder (offline b64 + batchexecute)"
```

---

### Task 4: Google News RSS provider

**Files:**
- Create: `geoecon/search/googlenews.py`
- Test: `tests/unit/test_googlenews_provider.py`

**Interfaces:**
- Consumes: `matrix.hl_for/ceid_for` (Task 2), `base.SearchItem` (Task 3), `gn_decode.decode` (Task 3).
- Produces: `GoogleNewsProvider(http=requests, decode=gn_decode.decode, pace_s=1.0, sleep=time.sleep)` with `name = "googlenews"` and `search(expr, spec, lang, window_hours, limit) -> list[SearchItem]`.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_googlenews_provider.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_googlenews_provider.py -v` — FAIL (module missing).

- [ ] **Step 3: Implement `geoecon/search/googlenews.py`:**

```python
"""Google News RSS search provider -- the Layer-1 spine (spec 2026-07-04).

One feed per (country, lang, family): /rss/search?q=<expr> when:Nd with the
country's edition params. Feeds cap at ~100 items server-side. Links are
aggregator-encoded; we keep the newest `limit` items and decode only those
(each online decode costs up to 2 HTTP calls -- see gn_decode)."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import requests

from ..fetchers.http import BROWSER_UA
from . import gn_decode, matrix
from .base import SearchItem

_SEARCH_URL = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def _entry_ts(entry) -> datetime | None:
    st = entry.get("published_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class GoogleNewsProvider:
    name = "googlenews"

    def __init__(self, *, http=requests, decode=gn_decode.decode,
                 pace_s: float = 1.0, sleep=time.sleep):
        self._http = http
        self._decode = decode
        self._pace_s = pace_s
        self._sleep = sleep

    def search(self, expr: str, spec, lang: str, window_hours: int,
               limit: int) -> list[SearchItem]:
        days = max(1, math.ceil(window_hours / 24))
        q = f"{expr} when:{days}d"
        if spec.global_en:
            # No viable local edition/vocabulary (e.g. Iran/Farsi): global
            # English edition, country name ANDed into the query.
            q = f'"{spec.name}" {q}'
            hl, gl, ceid = "en", "US", "US:en"
        else:
            hl, gl, ceid = matrix.hl_for(lang), spec.gl, matrix.ceid_for(spec, lang)
        url = _SEARCH_URL.format(q=quote(q), hl=hl, gl=gl, ceid=quote(ceid))
        r = self._http.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
        entries = feedparser.parse(r.content).entries or []
        # newest first; undated entries sort last but are kept (recall bias)
        entries = sorted(entries, key=lambda e: _entry_ts(e) or datetime.min.replace(
            tzinfo=timezone.utc), reverse=True)[:limit]
        items: list[SearchItem] = []
        for e in entries:
            link = e.get("link")
            if not link:
                continue
            src = getattr(e, "source", None)
            decoded = self._decode(link, http=self._http)
            items.append(SearchItem(
                url=decoded or link,
                title=e.get("title"),
                ts=_entry_ts(e),
                source_name=(src.get("title") if src else None),
                lang=lang if not spec.global_en else "en",
                decoded=decoded is not None,
            ))
            if self._pace_s:
                self._sleep(self._pace_s)
        return items
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_googlenews_provider.py -v` — all PASS. (Note: test asserts `ceid=MX%3Aes-419` — `quote(ceid)` encodes the colon; keep both sides consistent.)

- [ ] **Step 5: Commit**

```bash
git add geoecon/search/googlenews.py tests/unit/test_googlenews_provider.py
git commit -m "feat(search): Google News RSS provider (edition URLs, when:Nd, cap-then-decode, global_en fallback)"
```

---

### Task 5: `discovery_search` stage

**Files:**
- Create: `geoecon/stages/discovery_search.py`
- Test: `tests/unit/test_discovery_search.py`

**Interfaces:**
- Consumes: `matrix.load_countries/load_keywords/or_expr` (Task 2), `SearchItem` (Task 3), providers (Task 4), `repo.insert_article` (existing, `geoecon/state/repo.py`), `gdelt.region_for_country`, `urlnorm.normalized_url_hash` (existing).
- Produces: `run(cfg, repo, run_id, *, providers=None, countries=None, keywords=None, now=None) -> dict` metrics: `search_discovered`, `search_per_provider: dict`, `search_decode_failures`, `search_feeds_failed`, `search_feeds_empty`.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_discovery_search.py` (FakeRepo/`_cfg` pattern copied from `test_discovery_registry.py`):

```python
from datetime import datetime, timedelta, timezone

from geoecon.config import Config
from geoecon.search.base import SearchItem
from geoecon.search.matrix import CountrySpec
from geoecon.stages import discovery_search

NOW = datetime(2026, 7, 4, 7, 0, tzinfo=timezone.utc)
EG = CountrySpec(fips="EG", name="Egypt", gl="EG", langs=("ar",), gdelt_sc="egypt")
MX = CountrySpec(fips="MX", name="Mexico", gl="MX", langs=("es",), gdelt_sc="mexico")
KW = {"family1": {"ar": ["اقتصاد"], "es": ["economía"]},
      "family2": {"ar": ["عقوبات"], "es": ["sanciones"]}}


def _cfg(**over):
    env = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
           "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
           "SEARCH_DISCOVERY": "true"}
    env.update(over)
    return Config.from_env(env)


class FakeRepo:
    def __init__(self): self.captured = []
    def insert_article(self, run_id, **kw):
        self.captured.append(kw); return len(self.captured)


class FakeProvider:
    name = "fake"
    def __init__(self, items_by_key): self.items = items_by_key; self.calls = []
    def search(self, expr, spec, lang, window_hours, limit):
        self.calls.append((spec.fips, lang, expr, limit))
        return self.items.get((spec.fips, expr), [])


def test_inserts_with_provenance_window_and_dedup():
    old = NOW - timedelta(hours=30)
    items = {
        ("EG", "(اقتصاد)"): [
            SearchItem(url="https://ahram.org/a1", title="T1", ts=NOW - timedelta(hours=2),
                       source_name="Ahram", lang="ar"),
            SearchItem(url="https://ahram.org/a1?utm_source=x", title="dup", ts=None),  # same url_hash
            SearchItem(url="https://old.example/x", title="old", ts=old),               # out of window
            SearchItem(url="https://undated.example/y", title="undated", ts=None),      # kept
        ],
        ("EG", "(عقوبات)"): [],
        ("MX", "(economía)"): [SearchItem(url="https://elpais.com/m1", title="M1",
                                          ts=NOW - timedelta(hours=1), decoded=False)],
        ("MX", "(sanciones)"): [],
    }
    repo, prov = FakeRepo(), FakeProvider(items)
    m = discovery_search.run(_cfg(), repo, 1, providers=[prov],
                             countries=[EG, MX], keywords=KW, now=NOW)
    urls = [c["source_url"] for c in repo.captured]
    assert urls == ["https://ahram.org/a1", "https://undated.example/y", "https://elpais.com/m1"]
    eg = repo.captured[0]
    assert eg["discovery_source"] == "search:fake"
    assert eg["country_fips"] == "EG" and eg["region"] == "MENA"
    assert eg["source_lang"] == "ar" and eg["title"] == "T1"
    assert eg["published_at"] == NOW - timedelta(hours=2)
    assert eg["domain"] == "ahram.org"
    assert m["search_discovered"] == 3
    assert m["search_per_provider"] == {"fake": 3}
    assert m["search_decode_failures"] == 1              # the decoded=False one
    # limit passed through = search_country_cap default
    assert prov.calls[0][3] == 25


def test_provider_failure_is_isolated_and_counted():
    class Boom:
        name = "boom"
        def search(self, *a, **k): raise RuntimeError("nope")
    repo = FakeRepo()
    m = discovery_search.run(_cfg(), repo, 1, providers=[Boom()],
                             countries=[EG], keywords=KW, now=NOW)
    assert repo.captured == []
    assert m["search_feeds_failed"] == 2                 # both family queries failed
    assert m["search_discovered"] == 0


def test_limit_caps_feed_budget():
    repo = FakeRepo()
    prov = FakeProvider({})
    discovery_search.run(_cfg(LIMIT="5"), repo, 1, providers=[prov],
                         countries=[MX], keywords=KW, now=NOW)
    assert prov.calls[0][3] == 5                          # min(cap, limit)
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_discovery_search.py -v` — FAIL.

- [ ] **Step 3: Implement `geoecon/stages/discovery_search.py`:**

```python
"""Query-driven discovery -- the search spine (spec 2026-07-04).

Fans out (country x lang x family) queries per provider; converges into the
same `articles` table as GDELT/registry discovery via url_hash dedup. Contract
mirrors discovery_registry: window-filter, one-bad-query-never-aborts,
insert_article(fetch_status='pending'). Precision is the LLM gate's job."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .. import gdelt, urlnorm
from ..search import matrix

log = logging.getLogger("geoecon.discovery_search")


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _default_providers():
    from ..search.googlenews import GoogleNewsProvider
    return [GoogleNewsProvider()]


def run(cfg, repo, run_id: int, *, providers=None, countries=None,
        keywords=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=cfg.window_hours)
    providers = providers if providers is not None else _default_providers()
    countries = countries if countries is not None else matrix.load_countries(cfg.countries_path)
    keywords = keywords if keywords is not None else matrix.load_keywords(cfg.keywords_path)

    feed_limit = cfg.search_country_cap
    if cfg.limit:
        feed_limit = min(feed_limit, cfg.limit)

    discovered = decode_failures = feeds_failed = feeds_empty = 0
    per_provider: dict[str, int] = {}
    seen_hashes: set[str] = set()

    for prov in providers:
        per_provider.setdefault(prov.name, 0)
        for spec in countries:
            langs = spec.langs or (("en",) if spec.global_en else ())
            for lang in langs:
                for family in ("family1", "family2"):
                    terms = keywords[family].get(lang)
                    if not terms:
                        continue
                    expr = matrix.or_expr(terms)
                    try:
                        items = prov.search(expr, spec, lang, cfg.window_hours, feed_limit)
                    except Exception as e:  # one bad feed must not kill the stage
                        feeds_failed += 1
                        log.warning("search %s %s/%s/%s failed: %s",
                                    prov.name, spec.fips, lang, family, e)
                        continue
                    if not items:
                        feeds_empty += 1
                        continue
                    region = gdelt.region_for_country(spec.fips)
                    for it in items:
                        if it.ts is not None and it.ts < window_start:
                            continue  # out of window; null ts kept (recall)
                        h = urlnorm.normalized_url_hash(it.url)
                        if h in seen_hashes:
                            continue
                        seen_hashes.add(h)
                        if not it.decoded:
                            decode_failures += 1
                        repo.insert_article(
                            run_id,
                            source_url=it.url,
                            url_hash=h,
                            domain=_host(it.url),
                            region=region,
                            country_fips=spec.fips,
                            source_lang=it.lang,
                            title=it.title,
                            published_at=it.ts,
                            discovery_source=f"search:{prov.name}",
                        )
                        discovered += 1
                        per_provider[prov.name] += 1

    if discovered == 0 and providers:
        # The spine returning nothing is a recall-floor hole -- make it LOUD
        # (mirrors the tier-1 wire guard in discovery_registry).
        log.warning("recall-floor: discovery_search found 0 items across all "
                    "providers/countries -- endpoint change or network issue?")

    metrics = {
        "search_discovered": discovered,
        "search_per_provider": per_provider,
        "search_decode_failures": decode_failures,
        "search_feeds_failed": feeds_failed,
        "search_feeds_empty": feeds_empty,
    }
    log.info("discovery_search: %s", metrics)
    return metrics
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_discovery_search.py -v` — all PASS. (Check `gdelt.region_for_country("EG") == "MENA"` — it's the same helper `discovery_registry.py:82` uses.)

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/discovery_search.py tests/unit/test_discovery_search.py
git commit -m "feat(stages): discovery_search -- query-driven spine stage (fan-out, window, cap, dedup, telemetry)"
```

---

### Task 6: Orchestrator wiring behind the flag

**Files:**
- Modify: `geoecon/orchestrator.py:8-40` (`_default_stages`)
- Test: `tests/unit/test_discovery_search.py` (append)

**Interfaces:**
- Consumes: `cfg.search_discovery` (Task 1), `discovery_search.run` (Task 5).
- Produces: stage tuple `("discovery_search", ...)` appended after `discovery_registry` **only when** `cfg.search_discovery` is true. Insert order matters: GDELT and registry insert first, so their richer provenance wins the `(run_id, url_hash)` first-insert race.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_discovery_search.py`:

```python
def test_orchestrator_includes_stage_only_when_flag_on():
    from geoecon.orchestrator import _default_stages
    names_off = [n for n, _ in _default_stages(_cfg(SEARCH_DISCOVERY="false"), None)]
    names_on = [n for n, _ in _default_stages(_cfg(), None)]   # _cfg sets it true
    assert "discovery_search" not in names_off
    assert names_on.index("discovery_search") == names_on.index("discovery_registry") + 1
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_discovery_search.py::test_orchestrator_includes_stage_only_when_flag_on -v` — FAIL.

- [ ] **Step 3: Implement** — in `orchestrator.py` `_default_stages`: add `discovery_search` to the stage import line, add the wrapper next to its siblings, and build the list conditionally:

```python
    from .stages import (discovery, discovery_registry, discovery_search, extract,
                         gate, cluster, synth, summary, render, send)
```

```python
    def _discovery_search(c, r, rid): return discovery_search.run(c, r, rid)
```

replace the final `return [...]` with:

```python
    stages = [("discovery", _discovery), ("discovery_registry", _discovery_registry)]
    if cfg.search_discovery:
        stages.append(("discovery_search", _discovery_search))
    stages += [("extraction", _extract), ("gate", _gate), ("cluster", _cluster),
               ("synthesis", _synth), ("summary", _summary), ("render", _render),
               ("send", _send)]
    return stages
```

- [ ] **Step 4: Run the full unit suite** — `.venv/bin/python -m pytest tests/unit -v` — all PASS (existing orchestrator tests must not break; they use explicit `stages=` or flag-off configs).

- [ ] **Step 5: Commit**

```bash
git add geoecon/orchestrator.py tests/unit/test_discovery_search.py
git commit -m "feat(orchestrator): wire discovery_search stage behind SEARCH_DISCOVERY flag"
```

---

### Task 7: GDELT DOC 2.0 client

**Files:**
- Create: `geoecon/gdelt_doc.py`
- Test: `tests/unit/test_gdelt_doc.py`

**Interfaces:**
- Consumes: `keywords.yaml` `family1/family2` `en` lists (Task 2) — GDELT full-text search runs over machine-translated English, so English vocabulary covers all source languages.
- Produces:
  - `build_query(gdelt_sc: str, en_terms: list[str]) -> str` → `'sourcecountry:egypt (economy OR "central bank" OR ...)'`.
  - `fetch_articles(query: str, timespan_h: int, *, max_records=250, http=requests, sleep=time.sleep, pace_s=6.0) -> list[DocArticle]` where `DocArticle` is a dataclass `url, title, domain, language, seendate: datetime | None`.
  - Raises nothing on 429/non-JSON: retries once after 30s, then returns `[]` (caller counts).

- [ ] **Step 1: Write the failing test** — `tests/unit/test_gdelt_doc.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_gdelt_doc.py -v` — FAIL.

- [ ] **Step 3: Implement `geoecon/gdelt_doc.py`:**

```python
"""GDELT DOC 2.0 API client (spec 2026-07-04) -- replaces raw GKG file pulls.

Full-text search runs over GDELT's machine-translated English, so English
keyword expressions cover Arabic/Spanish/etc. sources; `sourcecountry:` gives
source-based country attribution (cleaner than most-mentioned-FIPS).

Operational lessons baked in (2026-07-04 probe): the public limiter wants
MORE than 5s between requests -- we pace 6s BEFORE every call and back off
30s once on 429; errors degrade to [] (the caller counts, never crashes)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_UA = "geoecon-pipeline/0.1 (research newsletter; low volume)"


@dataclass
class DocArticle:
    url: str
    title: str | None
    domain: str | None
    language: str | None
    seendate: datetime | None


def build_query(gdelt_sc: str, en_terms: list[str]) -> str:
    return f"sourcecountry:{gdelt_sc} (" + " OR ".join(en_terms) + ")"


def _parse_seendate(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_articles(query: str, timespan_h: int, *, max_records: int = 250,
                   http=requests, sleep=time.sleep, pace_s: float = 6.0) -> list[DocArticle]:
    params = {"query": query, "mode": "artlist", "format": "json",
              "timespan": f"{timespan_h}h", "maxrecords": max_records,
              "sort": "datedesc"}
    for attempt in (1, 2):
        sleep(pace_s)
        r = http.get(BASE_URL, params=params, timeout=60,
                     headers={"User-Agent": _UA})
        if r.status_code == 429:
            if attempt == 1:
                sleep(30.0)
                continue
            return []
        try:
            data = json.loads(r.text)
        except (ValueError, TypeError):
            return []
        return [DocArticle(url=a.get("url"), title=a.get("title"),
                           domain=a.get("domain"), language=a.get("language"),
                           seendate=_parse_seendate(a.get("seendate")))
                for a in data.get("articles", []) if a.get("url")]
    return []
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_gdelt_doc.py -v` — all PASS.

- [ ] **Step 5: Live validation (network)** — append to `tests/unit/test_gdelt_doc.py`:

```python
import pytest


@pytest.mark.requires_net
def test_live_egypt_query_returns_articles():
    from geoecon.search import matrix
    kw = matrix.load_keywords()
    q = gdelt_doc.build_query("egypt", kw["family1"]["en"][:6])
    arts = gdelt_doc.fetch_articles(q, 48)
    assert arts, ("0 articles for Egypt/48h -- likely sourcecountry or query "
                  "syntax drift; verify manually before proceeding")
```

Run: `.venv/bin/python -m pytest tests/unit/test_gdelt_doc.py -m requires_net -v`
Expected: PASS. **If it 429s persistently:** the dev IP may still be in GDELT's penalty box from the 2026-07-04 probe — wait 30+ min or run from another network. **If it returns 0 articles:** test `sourcecountry:EG` (FIPS form) vs `sourcecountry:egypt` interactively and fix `build_query`/`countries.yaml` `gdelt_sc` values accordingly before continuing.

- [ ] **Step 6: Commit**

```bash
git add geoecon/gdelt_doc.py tests/unit/test_gdelt_doc.py
git commit -m "feat(gdelt): DOC 2.0 API client (paced 6s, 429 backoff, english-translingual queries)"
```

---

### Task 8: `discovery.py` docapi mode

**Files:**
- Modify: `geoecon/stages/discovery.py` (add mode branch in `run()`; GKG path untouched)
- Test: `tests/unit/test_discovery.py` (append)

**Interfaces:**
- Consumes: `cfg.gdelt_mode` (Task 1), `gdelt_doc.build_query/fetch_articles` (Task 7), `matrix.load_countries` (Task 2), `keywords.yaml` en lists.
- Produces: when `cfg.gdelt_mode == "docapi"`, `discovery.run(cfg, repo, run_id, *, doc_fetch=None, now=None)` queries one `sourcecountry` per country in the matrix and inserts with `discovery_source='gdelt'`, `country_fips=<queried fips>`. Existing metric keys keep working; adds `gdelt_doc_queries`, `gdelt_doc_failed`. When mode is `gkg` behavior is byte-identical to today.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_discovery.py`. That file has no shared FakeRepo/`_cfg` (each test defines its own), so define these locally at the bottom:

```python
from datetime import datetime, timedelta, timezone as _tz

from geoecon import gdelt_doc
from geoecon.config import Config
from geoecon.search.matrix import CountrySpec


def _cfg(**over):
    env = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
           "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    env.update(over)
    return Config.from_env(env)


class FakeRepo:
    def __init__(self): self.captured = []
    def insert_article(self, run_id, **kw):
        self.captured.append(kw); return len(self.captured)

_NOW = datetime(2026, 7, 4, 7, 0, tzinfo=_tz.utc)
_EG = CountrySpec(fips="EG", name="Egypt", gl="EG", langs=("ar",), gdelt_sc="egypt")
_MX = CountrySpec(fips="MX", name="Mexico", gl="MX", langs=("es",), gdelt_sc="mexico")


def test_docapi_mode_inserts_with_queried_country():
    calls = []
    def doc_fetch(query, timespan_h, **kw):
        calls.append(query)
        if "egypt" in query:
            return [gdelt_doc.DocArticle(url="https://ahram.org.eg/a", title="T",
                                         domain="ahram.org.eg", language="Arabic",
                                         seendate=_NOW - timedelta(hours=3))]
        return []
    repo = FakeRepo()
    m = discovery.run(_cfg(GDELT_MODE="docapi"), repo, 1,
                      doc_fetch=doc_fetch, countries=[_EG, _MX], now=_NOW)
    assert len(calls) == 2 and calls[0].startswith("sourcecountry:egypt ")
    kw = repo.captured[0]
    assert kw["discovery_source"] == "gdelt"
    assert kw["country_fips"] == "EG"           # attribution from the query, not text
    assert kw["region"] == "MENA"
    assert kw["published_at"] == _NOW - timedelta(hours=3)
    assert m["gdelt_doc_queries"] == 2
    assert m["discovered"] == 1                  # same headline metric as gkg mode


def test_docapi_mode_out_of_window_and_dedup():
    old = _NOW - timedelta(hours=30)
    def doc_fetch(query, timespan_h, **kw):
        return [gdelt_doc.DocArticle(url="https://x.eg/1", title="a", domain="x.eg",
                                     language="Arabic", seendate=old),
                gdelt_doc.DocArticle(url="https://x.eg/2", title="b", domain="x.eg",
                                     language="Arabic", seendate=None),
                gdelt_doc.DocArticle(url="https://x.eg/2?ref=t", title="dup",
                                     domain="x.eg", language="Arabic", seendate=None)]
    repo = FakeRepo()
    m = discovery.run(_cfg(GDELT_MODE="docapi"), repo, 1,
                      doc_fetch=doc_fetch, countries=[_EG], now=_NOW)
    assert [c["source_url"] for c in repo.captured] == ["https://x.eg/2"]
    assert m["discovered"] == 1
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_discovery.py -v` — new tests FAIL (`run() got unexpected keyword 'doc_fetch'`).

- [ ] **Step 3: Implement** — in `geoecon/stages/discovery.py` add a `doc_fetch=None, countries=None` kwarg to `run(...)` and branch at the top of the function body:

```python
def run(cfg, repo, run_id, *, fetch=None, doc_fetch=None, countries=None, now=None) -> dict:
    if getattr(cfg, "gdelt_mode", "gkg") == "docapi":
        return _run_docapi(cfg, repo, run_id, doc_fetch=doc_fetch,
                           countries=countries, now=now)
    # ... existing GKG body unchanged below ...
```

and add at module bottom:

```python
def _run_docapi(cfg, repo, run_id, *, doc_fetch=None, countries=None, now=None) -> dict:
    """DOC 2.0 path: one sourcecountry query per matrix country. English
    vocabulary rides GDELT's machine translation across all source languages;
    attribution comes from the query (clean), not most-mentioned-FIPS (noisy)."""
    from datetime import datetime, timedelta, timezone
    from .. import gdelt, gdelt_doc, urlnorm
    from ..search import matrix

    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=cfg.window_hours)
    doc_fetch = doc_fetch or gdelt_doc.fetch_articles
    countries = countries if countries is not None else matrix.load_countries(cfg.countries_path)
    kw = matrix.load_keywords(cfg.keywords_path)
    en_terms = kw["family1"]["en"] + kw["family2"]["en"]

    ceiling = cfg.limit or cfg.discovery_ceiling
    discovered = queries = failed = 0
    per_bucket: dict[str, int] = {}
    seen_hashes: set[str] = set()

    for spec in countries:
        buckets = regions.buckets_for_country(spec.fips, cfg.buckets)
        if not buckets:
            continue
        query = gdelt_doc.build_query(spec.gdelt_sc, en_terms)
        queries += 1
        try:
            arts = doc_fetch(query, cfg.window_hours, max_records=ceiling)
        except Exception as e:  # one bad country must not kill the stage
            failed += 1
            log.warning("gdelt-doc %s failed: %s", spec.fips, e)
            continue
        region = gdelt.region_for_country(spec.fips)
        for a in arts:
            if a.seendate is not None and a.seendate < window_start:
                continue
            if not any(per_bucket.get(b, 0) < ceiling for b in buckets):
                break
            h = urlnorm.normalized_url_hash(a.url)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            repo.insert_article(
                run_id, source_url=a.url, url_hash=h, domain=a.domain,
                region=region, country_fips=spec.fips, source_lang=a.language,
                title=a.title, published_at=a.seendate, discovery_source="gdelt",
            )
            discovered += 1
            for b in buckets:
                per_bucket[b] = per_bucket.get(b, 0) + 1

    metrics = {"discovered": discovered, "per_bucket": per_bucket,
               "gdelt_doc_queries": queries, "gdelt_doc_failed": failed}
    log.info("discovery(docapi): %s", metrics)
    return metrics
```

(Match the existing module's actual metric key names — if the GKG path's headline count key is not literally `"discovered"`, use that path's key so downstream funnel dashboards keep working. Check the top of `discovery.py` for `regions`/`log` imports already present; add only what's missing.)

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_discovery.py -v` — all PASS, including all pre-existing GKG-path tests untouched.

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/discovery.py tests/unit/test_discovery.py
git commit -m "feat(discovery): GDELT_MODE=docapi path -- per-country DOC queries, clean attribution; gkg path untouched"
```

---

### Task 9: `retired` registry status + curated-domain nudge

**Files:**
- Modify: `geoecon/registry.py` (status handling, ~line 47-56)
- Modify: `geoecon/stages/discovery_registry.py` (skip+count retired; exclude retired from tier-1 guard)
- Modify: `geoecon/stages/cluster.py:123,152` (curated match by domain too)
- Test: `tests/unit/test_registry.py`, `tests/unit/test_discovery_registry.py`, `tests/unit/test_cluster.py` (append to each)

**Interfaces:**
- Produces: `status: retired` = intentionally not fetched (search covers it), skipped silently with its own counter `registry_skipped_retired`; retired tier-1 wires do NOT trip the recall-floor WARNING. `cluster.py` marks `has_curated` when a member article's `registry_id` is curated **or** its `domain` matches a curated source's host (www-insensitive).

- [ ] **Step 1: Write the failing tests.** Append to `tests/unit/test_discovery_registry.py`:

```python
def test_retired_sources_skipped_quietly_and_counted(caplog):
    sources = [
        _src(id="w", tier=1, fetch_type="rss", status="retired"),   # retired wire: no warning
        _src(id="d", fetch_type="rss", status="live", country_fips="SA", bucket="middle_east"),
    ]
    items = {"d": [DiscoveredItem(url="https://example.org/d1", title="t")]}
    repo = FakeRepo()
    import logging
    with caplog.at_level(logging.WARNING, logger="geoecon.discovery_registry"):
        m = discovery_registry.run(_cfg(), repo, 1, sources=sources,
                                   get_fetcher=_fetcher_for(items), now=NOW)
    assert m["registry_skipped_retired"] == 1
    assert m["registry_discovered"] == 1
    assert not any("recall-floor" in r.message for r in caplog.records)
    assert m["tier1_held"] == 0                     # retired != held
```

Append to `tests/unit/test_cluster.py` (adapt helper names to that file's existing fixtures — the behavior under test: an article with `registry_id=None` but a curated domain sets `has_curated`):

```python
def test_curated_matches_by_domain_for_search_articles():
    from geoecon.stages.cluster import curated_match_sets
    class S:  # minimal source stand-in
        def __init__(self, id, url, curated): self.id, self.url, self.curated = id, url, curated
    ids, domains = curated_match_sets([S("a", "https://www.ahram.org.eg/news", True),
                                       S("b", "https://x.example", False)])
    assert ids == {"a"}
    assert domains == {"ahram.org.eg"}
    class A:  # minimal article stand-in
        registry_id = None
        domain = "ahram.org.eg"
    assert A.registry_id in ids or A.domain in domains
```

Append to `tests/unit/test_registry.py`:

```python
def test_retired_is_valid_status_and_not_fetchable():
    s = Source(id="x", institution="X", tier=2, fetch_type="rss",
               url="https://x.example", lang="es", status="retired")
    assert s.is_fetchable is False    # is_fetchable is a @property returning bool
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_registry.py tests/unit/test_discovery_registry.py tests/unit/test_cluster.py -v` — new tests FAIL.

- [ ] **Step 3: Implement.**
  - `registry.py`: extend the status docstring/comment to `live | needs_review | dead | retired`; make `is_fetchable` also exclude `"retired"` (`... and self.status not in ("needs_review", "retired")`). If there's a status validation set, add `retired` to it.
  - `discovery_registry.py`: in the source loop add before the `needs_review` check:

```python
        if src.status == "retired":
            skipped_retired += 1
            continue
```

  initialize `skipped_retired = 0` beside the other counters, add `"registry_skipped_retired": skipped_retired` to metrics, and change the tier-1 guard filter to `tier1_held = [s for s in tier1 if s.status not in ("live", "retired")]`.
  - `cluster.py`: replace line 123's set-build with a helper (module-level, so it's unit-testable):

```python
def curated_match_sets(sources):
    """(curated registry ids, curated hosts) -- hosts let search-layer articles
    from a curated outlet keep the ranking nudge after the registry trim."""
    from urllib.parse import urlsplit
    ids = {s.id for s in sources if s.curated}
    domains = set()
    for s in sources:
        if s.curated:
            host = (urlsplit(s.url).hostname or "").removeprefix("www.")
            if host:
                domains.add(host)
    return ids, domains
```

  at the old line 123: `curated_ids, curated_domains = curated_match_sets(sources)`; at line 152:

```python
        has_curated = any(
            a.registry_id in curated_ids
            or ((a.domain or "").removeprefix("www.") in curated_domains)
            for a in member_arts)
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/unit/test_registry.py tests/unit/test_discovery_registry.py tests/unit/test_cluster.py -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add geoecon/registry.py geoecon/stages/discovery_registry.py geoecon/stages/cluster.py tests/unit/
git commit -m "feat(registry): retired status (quiet skip) + curated nudge matches by domain for search-layer articles"
```

---

### Task 10: Trim `sources.yaml` + full-suite gate

**Files:**
- Modify: `config/sources.yaml` (scripted edit — do NOT hand-edit 1538 lines)
- Create: `tools/retire_generic_outlets.py` (throwaway, committed for audit)

**Interfaces:**
- Consumes: `retired` status support (Task 9).
- Produces: tier-1/2 sources whose id does not start with `actor_` get `status: retired`; tier-0 and `actor_*` untouched. `curated:` flags stay in place (Task 9 reads their domains).

- [ ] **Step 1: Write `tools/retire_generic_outlets.py`** (ruamel preserves comments; it's already a dev dependency for `tools/registry_apply.py`):

```python
"""One-shot: retire tier-1/2 generic outlets (spec 2026-07-04 registry trim).
Search discovery covers these outlets now; tier-0 primaries and actor_* stay.
Run: .venv/bin/python tools/retire_generic_outlets.py"""
from pathlib import Path

from ruamel.yaml import YAML

path = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"
yaml = YAML()
yaml.preserve_quotes = True
data = yaml.load(path.read_text(encoding="utf-8"))

changed = []
for src in data["sources"]:
    if src.get("tier") in (1, 2) and not str(src.get("id", "")).startswith("actor_"):
        if src.get("status") != "retired":
            src["status"] = "retired"
            changed.append(src["id"])

yaml.dump(data, path.open("w", encoding="utf-8"))
print(f"retired {len(changed)}: {', '.join(changed)}")
```

- [ ] **Step 2: Run it** — `.venv/bin/python tools/retire_generic_outlets.py`
Expected output: `retired ~25-29: wire_efe, reg_alarabiya_ar, ...` (tier-1/2 minus any `actor_*`). Then sanity-check:

```bash
grep -c "status: retired" config/sources.yaml     # expect the same count printed
.venv/bin/python -c "
from geoecon.registry import load_sources
ss = load_sources()
live0 = [s for s in ss if s.tier == 0 and s.status == 'live']
ret = [s for s in ss if s.status == 'retired']
print('tier0 live:', len(live0), '| retired:', len(ret), '| total:', len(ss))"
```

Expected: tier-0 live count unchanged from before the trim; retired ≈ 25–29; total still 66 (nothing deleted).

- [ ] **Step 3: Full unit suite** — `.venv/bin/python -m pytest tests/unit -v` — ALL tests in the repo PASS.

- [ ] **Step 4: Smoke the flagged pipeline paths locally (no network, no DB):**

```bash
.venv/bin/python -c "
from geoecon.orchestrator import _default_stages
from geoecon.config import Config
env = {'DB_DSN':'x','MAETCENTRUM_KEY':'k','SMTP_HOST':'h','SMTP_USER':'u',
       'SMTP_PASSWORD':'p','FROM_ADDR':'f','RECIPIENTS':'r@x',
       'SEARCH_DISCOVERY':'true','GDELT_MODE':'docapi'}
names = [n for n,_ in _default_stages(Config.from_env(env), None)]
print(names)"
```

Expected: `['discovery', 'discovery_registry', 'discovery_search', 'extraction', 'gate', 'cluster', 'synthesis', 'summary', 'render', 'send']`.

- [ ] **Step 5: Commit**

```bash
git add config/sources.yaml tools/retire_generic_outlets.py
git commit -m "feat(registry): retire tier-1/2 generic outlets -- search layer covers them (spec 2026-07-04)"
```

---

## Post-plan (Phase 4 — deploy, NOT in this plan)

Live end-to-end validation needs the cluster env (DB + LLM + SMTP + non-penalized IP): set `SEARCH_DISCOVERY=true`, `GDELT_MODE=docapi` on a manual k8s Job, compare funnel telemetry (`search_discovered`, `search_decode_failures`, `gdelt_doc_queries`, per-region brief quality) against the previous run, then bake the flags into the CronJob env and build the release image. Follow the ControlMaster deploy mechanics in memory/`firecrawl-render-path.md`.
