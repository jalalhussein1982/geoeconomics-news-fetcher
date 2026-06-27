# Source registry — Task 0 resolution report

> **Superseded in part (2026-06-27):** the "no headless browser" limit below no longer holds at
> runtime — JS-rendered/403 sources are now recovered via the **Firecrawl render path**
> (`render: firecrawl`; see `docs/how-it-works/03-registry-redesign.md`). 6 sources moved
> `needs_review → live` since this report (`eg_cb_cbe`, `actor_imf`, `actor_worldbank`,
> `wire_efe`, `reg_alarabiya_ar`, `eg_mof`); Core-15 is now 7/15. `sources.yaml` is the source
> of truth for current status — the counts in this report are the 2026-06-23 baseline.

Resolved 2026-06-23 by a live, robots-respecting prober (RSS/Atom discovery via alternate-links + probe paths + common defaults; SharePoint/OData list-API probe; no headless browser available, so JS-rendered article lists are left `needs_review`).

**Totals (48 sources):** `rss`=11, `scrape`=34, `sitemap`=3  |  status: `live`=14, `needs_review`=34

> Conservative: only a confirmed DNS failure is `dead`. Stale-path 404s, TLS/connection timeouts, and robots blocks from the resolver environment are `needs_review` (verify from production), never `dead`.


## Core-15 primary coverage

**6/15** Core countries have a live Tier-0 primary; the rest rely on the wire + external-actor + GDELT-firehose tiers until a primary feed/selector/API resolves:


| Country | Why no live primary |
|---|---|
| Chile (CI) | 1 Tier-0 source(s), all `needs_review` |
| Egypt (EG) | 2 Tier-0 source(s), all `needs_review` |
| Guyana (GY) | 1 Tier-0 source(s), all `needs_review` |
| Iran (IR) | 1 Tier-0 source(s), all `needs_review` |
| Iraq (IZ) | 1 Tier-0 source(s), all `needs_review` |
| Kuwait (KU) | 2 Tier-0 source(s), all `needs_review` |
| Mexico (MX) | 2 Tier-0 source(s), all `needs_review` |
| Qatar (QA) | 2 Tier-0 source(s), all `needs_review` |
| Turkey (TU) | 2 Tier-0 source(s), all `needs_review` |


## Every source

| Tier | Bucket | FIPS | id | institution | fetch_type | status | outcome |
|---|---|---|---|---|---|---|---|
| 0 | middle_east | AE | `ae_adia` | Abu Dhabi Investment Authority (ADIA) / Mubadala / ADQ | `scrape` | `needs_review` | origin 403 to bots; no feed. Needs authenticated/headless review. |
| 0 | middle_east | AE | `ae_adx_dfm` | Abu Dhabi Securities Exchange (ADX) / Dubai Financial Market (DFM) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~1); selector needs headless capture. |
| 0 | middle_east | AE | `ae_cb_cbuae` | Central Bank of the UAE (CBUAE) | `scrape` | `needs_review` | origin 403 to bots; no feed. Needs authenticated/headless review. |
| 0 | middle_east | AE | `ae_mof` | Ministry of Finance (UAE) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). |
| 0 | latam | AR | `ar_cb_bcra` | Banco Central de la República Argentina (BCRA) | `scrape` | `needs_review` | origin 404 — registry URL likely stale; needs updated news-section path. |
| 0 | latam | AR | `ar_economia` | Ministerio de Economía (Argentina) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). Portal-root feed (broad) — refine to a section feed later; gate does precision. |
| 0 | latam | BR | `br_b3` | B3 (Brazil Stock Exchange) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~2); selector needs headless capture. |
| 0 | latam | BR | `br_cb_bcb` | Banco Central do Brasil (BCB) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 0 | latam | BR | `br_mof_fazenda` | Ministério da Fazenda (Brazil) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). Portal-root feed (broad) — refine to a section feed later; gate does precision. |
| 0 | latam | CI | `cl_cb_bcch` | Banco Central de Chile (BCCh) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 0 | latam | CO | `co_cb_banrep` | Banco de la República (Colombia) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). |
| 0 | middle_east | EG | `eg_cb_cbe` | Central Bank of Egypt (CBE) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 0 | middle_east | EG | `eg_mof` | Ministry of Finance (Egypt) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 0 | latam | GY | `gy_bog` | Bank of Guyana | `scrape` | `needs_review` | origin 404 — registry URL likely stale; needs updated news-section path. |
| 0 | middle_east | IR | `ir_cb_cbi` | Central Bank of the Islamic Republic of Iran (CBI) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 0 | middle_east | IS | `il_cb_boi` | Bank of Israel (BOI) | `sitemap` | `live` | XML sitemap with <lastmod> found and windowable; article URLs discovered (titles via extraction). |
| 0 | middle_east | IS | `il_mof` | Ministry of Finance (Israel) | `scrape` | `needs_review` | origin 403 to bots; no feed. Needs authenticated/headless review. |
| 0 | middle_east | IS | `il_tase` | Tel Aviv Stock Exchange (TASE) | `sitemap` | `live` | XML sitemap with <lastmod> found and windowable; article URLs discovered (titles via extraction). |
| 0 | middle_east | IZ | `iq_cb_cbi` | Central Bank of Iraq (CBI) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~109); selector needs headless capture. |
| 0 | middle_east | KU | `kw_cb_cbk` | Central Bank of Kuwait (CBK) | `scrape` | `needs_review` | origin 404 — registry URL likely stale; needs updated news-section path. |
| 0 | middle_east | KU | `kw_mof` | Ministry of Finance (Kuwait) | `scrape` | `needs_review` | unreachable from resolver env (ConnectTimeout: HTTPSConnectionPool(host='www.mof.gov.kw', port=443): Max retries exceeded with url: /MOFNews/index.aspx (Caused by ConnectTimeoutError(<HTTPSConnection(host='www.mof.gov.kw', port=443) at 0x106881fd0>, 'Connection to www.mof.gov.kw timed out. (connect timeout=10)'))); verify from production network. |
| 0 | latam | MX | `mx_cb_banxico` | Banco de México (Banxico) | `scrape` | `needs_review` | origin 404 — registry URL likely stale; needs updated news-section path. |
| 0 | latam | MX | `mx_shcp` | Secretaría de Hacienda y Crédito Público (SHCP) | `scrape` | `needs_review` | origin 404 — registry URL likely stale; needs updated news-section path. |
| 0 | middle_east | QA | `qa_cb_qcb` | Qatar Central Bank (QCB) | `scrape` | `needs_review` | unreachable from resolver env (SSLError: HTTPSConnectionPool(host='www.qcb.gov.qa', port=443): Max retries exceeded with url: /en/news/ (Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1010)')))); verify from production network. |
| 0 | middle_east | QA | `qa_qia` | Qatar Investment Authority (QIA) | `scrape` | `needs_review` | origin 404 — registry URL likely stale; needs updated news-section path. |
| 0 | middle_east | SA | `sa_cb_sama` | Saudi Central Bank (SAMA) | `scrape` | `needs_review` | unreachable from resolver env (ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))); verify from production network. |
| 0 | middle_east | SA | `sa_mof` | Ministry of Finance (Saudi Arabia) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~39); selector needs headless capture. |
| 0 | middle_east | SA | `sa_pif` | Public Investment Fund (PIF) | `sitemap` | `live` | XML sitemap with <lastmod> found and windowable; article URLs discovered (titles via extraction). |
| 0 | middle_east | SA | `sa_tadawul` | Saudi Exchange (Tadawul) | `scrape` | `needs_review` | origin 403 to bots; no feed. Needs authenticated/headless review. |
| 0 | middle_east | TU | `tr_cb_tcmb` | Central Bank of the Republic of Türkiye (TCMB) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~3); selector needs headless capture. |
| 0 | middle_east | TU | `tr_mof_hmb` | Ministry of Treasury and Finance (Türkiye) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 0 | — | — | `actor_eu_sanctions` | EU Council (restrictive measures / sanctions) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~8); selector needs headless capture. |
| 0 | — | — | `actor_imf` | IMF (press releases / country programs) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~26); selector needs headless capture. |
| 0 | — | — | `actor_opec` | OPEC (press releases) | `scrape` | `needs_review` | origin 403 to bots; no feed. Needs authenticated/headless review. |
| 0 | — | — | `actor_us_ofac` | US Treasury OFAC (sanctions actions) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). Portal-root feed (broad) — refine to a section feed later; gate does precision. |
| 0 | — | — | `actor_us_treasury_press` | US Treasury (press releases) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). Portal-root feed (broad) — refine to a section feed later; gate does precision. |
| 0 | — | — | `actor_worldbank` | World Bank (news / country) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~9); selector needs headless capture. |
| 1 | — | — | `actor_china_mofcom` | China MOFCOM / Exim (BRI, trade, lending) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~20); selector needs headless capture. |
| 1 | middle_east | — | `wire_aa_anadolu` | Anadolu Agency (AA) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). |
| 1 | — | — | `wire_ap` | Associated Press | `scrape` | `needs_review` | manual hold: only sitemap is /hub/* section indexes (sports, regions), not articles — would discover navigation, not economy stories. Wire better via RSS/scrape; left for headless pass. |
| 1 | latam | — | `wire_bloomberg_linea` | Bloomberg Línea (LatAm) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). |
| 1 | latam | — | `wire_efe` | Agencia EFE (LatAm desks) | `scrape` | `needs_review` | no feed; served HTML JS-rendered/thin (article-links~0); selector needs headless capture. |
| 1 | — | — | `wire_reuters_world` | Reuters (World / Markets / regional desks) | `scrape` | `needs_review` | robots.txt disallows our UA — scraping not permitted; feed-of-links/manual only (ToS). |
| 2 | latam | BR | `reg_valor_br` | Valor Econômico (Brazil) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). |
| 2 | middle_east | — | `reg_alarabiya_ar` | Al Arabiya (Arabic — Aswaq/Economy) | `scrape` | `needs_review` | origin 403 to bots; no feed. Needs authenticated/headless review. |
| 2 | middle_east | — | `reg_aljazeera_ar` | Al Jazeera (Arabic — Economy) | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). |
| 2 | middle_east | — | `reg_almonitor` | Al-Monitor | `rss` | `live` | RSS/Atom feed found and validated (>=1 entry). Portal-root feed (broad) — refine to a section feed later; gate does precision. |
| 2 | latam | — | `reg_americaeconomia` | AméricaEconomía | `scrape` | `needs_review` | unreachable from resolver env (ReadTimeout: HTTPSConnectionPool(host='www.americaeconomia.com', port=443): Read timed out. (read timeout=10)); verify from production network. |
