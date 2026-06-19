-- ============================================================================
-- Geoeconomic Newsletter Pipeline — PostgreSQL schema (v1)
--
-- Spine for the daily 07:00 batch. Encodes every locked decision:
--   * full run-state (resumable, audited)            [Stage 0 / state = C]
--   * article-level provenance, B-compatible          [provenance = A]
--   * multilingual event clusters + framing flag       [Stage 2]
--   * paired-language synthesis records                [output lang = store-both]
--   * synthesis_quality_flag (from validator.py)        [production guard]
--   * seen-store for non-overlap across the 07:00 edge  [Stage 0 overlap rule]
--
-- Conventions: snake_case, timestamptz everywhere (store UTC), JSONB for
-- variable-shape payloads, explicit FKs with ON DELETE rules, enums via CHECK.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 0. Enumerated states (CHECK-constrained text; cheap to evolve)
-- ---------------------------------------------------------------------------
-- run.status      : lifecycle of one daily batch
-- stage_name      : the pipeline stages, in order
-- stage.status    : per-stage checkpoint state (enables resume)
-- article.fetch_status : extraction outcome (drives headline-only fallback)
-- quality_flag    : validator verdict on a synthesis record

-- ---------------------------------------------------------------------------
-- 1. RUNS — one row per daily batch execution
-- ---------------------------------------------------------------------------
CREATE TABLE runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- the fixed [T-24h, T] window this run covers
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    scheduled_for   TIMESTAMPTZ NOT NULL,              -- the 07:00 trigger instant
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','completed','failed','partial')),
    -- model + prompt actually used, for audit / reproducibility
    synthesis_model TEXT,                              -- e.g. 'deepseek-v3.2'
    synthesis_prompt_id TEXT,                          -- e.g. 'task_a_cov'
    embedding_model TEXT,                              -- e.g. 'bge-m3'
    -- free-form run metrics (counts, token totals, timings)
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes           TEXT,
    CONSTRAINT runs_window_order CHECK (window_end > window_start)
);

COMMENT ON TABLE runs IS 'One row per daily 07:00 batch; the audit + resume anchor.';

-- ---------------------------------------------------------------------------
-- 2. STAGE_RUNS — per-stage checkpoint (makes the pipeline resumable)
-- ---------------------------------------------------------------------------
CREATE TABLE stage_runs (
    stage_run_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_name      TEXT NOT NULL
                    CHECK (stage_name IN
                        ('discovery','extraction','rss','cluster','synthesis','render','send')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','failed','skipped')),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    -- checkpoint payload: enough to resume this stage without redoing prior work
    checkpoint      JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT,
    UNIQUE (run_id, stage_name)
);

COMMENT ON TABLE stage_runs IS 'Per-stage state + checkpoint; a failed run resumes from the last completed stage.';

-- ---------------------------------------------------------------------------
-- 3. ARTICLES — every discovered item + extraction provenance (article-level)
-- ---------------------------------------------------------------------------
CREATE TABLE articles (
    article_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    -- discovery (GDELT) provenance
    gdelt_gkg_id    TEXT,                              -- GDELT record id, if from discovery
    source_url      TEXT NOT NULL,
    canonical_url   TEXT,                              -- post-canonicalization (dedup aid)
    url_hash        TEXT NOT NULL,                     -- hash of canonical_url, indexed for dedup
    domain          TEXT,
    discovered_via  TEXT NOT NULL DEFAULT 'gdelt'
                    CHECK (discovered_via IN ('gdelt','rss')),
    -- geo + theme tags carried from GDELT GKG (the discovery filter surface)
    region          TEXT CHECK (region IN ('MENA','LatAm','other')),
    gkg_themes      TEXT[],                            -- the ECON_*/WB_* codes that matched
    gkg_tone        REAL,
    num_mentions    INTEGER,                           -- salience tiebreaker
    -- language + extraction
    source_lang     TEXT,                              -- ar/es/pt/fr/en/zh/...
    title           TEXT,
    body            TEXT,                              -- full text if extracted; NULL if fallback
    fetch_status    TEXT NOT NULL DEFAULT 'pending'
                    CHECK (fetch_status IN
                        ('pending','extracted','headline_only','failed')),
    fetch_error     TEXT,
    published_at    TIMESTAMPTZ,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX articles_run_idx        ON articles (run_id);
CREATE INDEX articles_url_hash_idx   ON articles (url_hash);
CREATE INDEX articles_region_idx     ON articles (region);
CREATE INDEX articles_fetch_idx      ON articles (fetch_status);

COMMENT ON TABLE articles IS 'Every discovered article + its extraction outcome. headline_only = Stage-1b GDELT-metadata fallback (body NULL).';

-- ---------------------------------------------------------------------------
-- 4. CLUSTERS — multilingual event clusters (Stage 2, BGE-M3)
-- ---------------------------------------------------------------------------
CREATE TABLE clusters (
    cluster_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    -- the seen-store key: stable fingerprint of the EVENT, language-agnostic.
    -- enforces "no overlap unless necessary" across the 07:00 boundary.
    event_fingerprint   TEXT NOT NULL,
    region              TEXT CHECK (region IN ('MENA','LatAm','other')),
    article_count       INTEGER NOT NULL DEFAULT 0,
    languages           TEXT[],                        -- distinct source langs in the cluster
    -- framing-divergence flag set by Stage 2 when intra-cluster framing diverges
    framing_divergent   BOOLEAN NOT NULL DEFAULT false,
    -- centroid kept for debugging / threshold tuning (optional; vector ext if available)
    centroid            JSONB,                         -- or migrate to pgvector later
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX clusters_run_idx ON clusters (run_id);
CREATE INDEX clusters_fp_idx  ON clusters (event_fingerprint);

COMMENT ON TABLE clusters IS 'One event = one cluster across languages. event_fingerprint is the seen-store key for non-overlap.';

-- membership: which articles belong to which cluster (many-to-one)
CREATE TABLE cluster_articles (
    cluster_id      BIGINT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
    article_id      BIGINT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    -- per-membership framing label (the divergent frame this article carries)
    frame_label     TEXT,
    PRIMARY KEY (cluster_id, article_id)
);

-- ---------------------------------------------------------------------------
-- 5. SEEN_STORE — cross-run event memory (the non-overlap enforcer)
-- ---------------------------------------------------------------------------
-- A cluster emitted on day N is suppressed on day N+1 unless materially updated.
-- Keyed on event_fingerprint, with a TTL so old events age out.
CREATE TABLE seen_events (
    event_fingerprint   TEXT PRIMARY KEY,
    first_seen_run      BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE SET NULL,
    last_seen_run       BIGINT REFERENCES runs(run_id) ON DELETE SET NULL,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_emitted_at     TIMESTAMPTZ,                   -- when it last went INTO a newsletter
    times_seen          INTEGER NOT NULL DEFAULT 1,
    expires_at          TIMESTAMPTZ NOT NULL           -- TTL; rows past this are purgeable
);

CREATE INDEX seen_expires_idx ON seen_events (expires_at);

COMMENT ON TABLE seen_events IS 'Cross-run dedup. A fingerprint here = already emitted; suppress unless materially updated. expires_at ages out stale events.';

-- ---------------------------------------------------------------------------
-- 6. SYNTHESIS_RECORDS — the per-cluster output (Stage 3), paired-language
-- ---------------------------------------------------------------------------
-- "Store both, decide at render": surface forms (headline, entities, figures,
-- framing) are stored in BOTH source-language original and English. Synthesis
-- PROSE (what_happened, significance) is English-only (generated, not a
-- preserved surface form). Render templates choose what to display.
CREATE TABLE synthesis_records (
    record_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                  BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    cluster_id              BIGINT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,

    -- paired surface form: headline
    headline_en             TEXT,
    headline_original       TEXT,
    headline_original_lang  TEXT,

    -- English-only synthesis prose
    what_happened_en        TEXT,
    geoeconomic_significance_en TEXT,
    geoeconomic_vector      TEXT[],                    -- ['trade','currency','sanctions',...]

    -- paired structured payloads (JSONB; shapes below)
    -- entities:    [{ "canonical_en":..., "surface_original":..., "lang":..., "type":... }]
    entities                JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- key_figures: [{ "value":..., "unit":..., "source_span_original":..., "gloss_en":... }]
    key_figures             JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- framing_divergence (only when cluster.framing_divergent):
    --   [{ "source_lang":..., "frame_en":..., "frame_original":... }]
    framing_divergence      JSONB,

    -- source links (multilingual), denormalized for render convenience
    sources                 JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- production guard output (from validator.py)
    synthesis_quality_flag  TEXT NOT NULL DEFAULT 'ok'
                            CHECK (synthesis_quality_flag IN
                                ('ok','empty_figures','zero_entities',
                                 'lang_mismatch','pair_incomplete','retry_fallback','invalid_json')),
    quality_detail          JSONB,                     -- which check tripped, specifics
    is_headline_only        BOOLEAN NOT NULL DEFAULT false,  -- record built from fallback record(s)

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, cluster_id)
);

CREATE INDEX synth_run_idx     ON synthesis_records (run_id);
CREATE INDEX synth_quality_idx ON synthesis_records (synthesis_quality_flag);

COMMENT ON TABLE synthesis_records IS 'Per-cluster synthesis. Surface forms paired (en+original); prose English-only. synthesis_quality_flag from validator.py gates what ships.';
COMMENT ON COLUMN synthesis_records.synthesis_quality_flag IS 'ok ships; pair_incomplete = an _original lacks its _en partner or vice versa (same severity as empty_figures).';

-- ---------------------------------------------------------------------------
-- 7. THROUGHLINE — optional cross-cluster editorial pass (Stage 3, null-permitted)
-- ---------------------------------------------------------------------------
CREATE TABLE throughlines (
    run_id          BIGINT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    -- NULL body = "no significant throughline today" (the model is allowed, and
    -- frequently should, return null). Presence of row with NULL text records
    -- that the pass RAN and declined; absence of row = pass not run.
    throughline_en  TEXT,                              -- nullable on purpose
    cluster_ids     BIGINT[],                          -- clusters the thread connects
    model           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE throughlines IS 'Cross-cluster thread. NULL throughline_en = pass ran and correctly found no thread.';

-- ---------------------------------------------------------------------------
-- 8. NEWSLETTERS — the rendered + sent artifact (Stage 4)
-- ---------------------------------------------------------------------------
CREATE TABLE newsletters (
    newsletter_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    -- which render template/language mode shipped (the render-time decision)
    render_mode     TEXT NOT NULL
                    CHECK (render_mode IN ('en_only','en_plus_original','bilingual_rtl')),
    archive_html_path TEXT,                            -- standalone rich HTML archive
    email_html      TEXT,                              -- email-safe inlined version (or path)
    record_count    INTEGER NOT NULL DEFAULT 0,
    sent_at         TIMESTAMPTZ,
    send_status     TEXT NOT NULL DEFAULT 'pending'
                    CHECK (send_status IN ('pending','sent','failed')),
    send_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id)
);

COMMENT ON TABLE newsletters IS 'Rendered artifact + SMTP send outcome. render_mode is the deferred output-language decision, made here.';

-- ============================================================================
-- Notes for the build:
--  * pgvector: if available on MetaCentrum Postgres, migrate clusters.centroid
--    and an articles.embedding column to vector(1024) for BGE-M3; CHECK the ext
--    with `CREATE EXTENSION IF NOT EXISTS vector;` and swap JSONB -> vector.
--  * Partitioning: at scale, partition articles + synthesis_records BY RANGE
--    (run window_start) monthly. Not needed at v1 volume (~150 articles/day).
--  * The seen-store dedup query at discovery time:
--      filter out clusters whose event_fingerprint exists in seen_events
--      AND last_emitted_at within the suppression window AND not materially updated.
--  * Claim-level provenance (the deferred "B" upgrade) attaches later as a
--    claim_sources table FK'd to synthesis_records.record_id — no migration of
--    these tables required.
-- ============================================================================
