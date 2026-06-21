-- geoecon/state/migrations/002_region_split.sql
-- Region split: per-country bucketing, per-region newsletters, region summaries.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS country_fips TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS country_fips TEXT;

-- newsletters: one row per (run, region) instead of one per run
ALTER TABLE newsletters DROP CONSTRAINT IF EXISTS newsletters_run_id_key;
ALTER TABLE newsletters ADD COLUMN IF NOT EXISTS bucket TEXT;
ALTER TABLE newsletters ADD CONSTRAINT newsletters_run_bucket_key UNIQUE (run_id, bucket);

-- per-run, per-region executive summary
CREATE TABLE IF NOT EXISTS region_summaries (
    run_id        BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    bucket        TEXT   NOT NULL,
    lede_en       TEXT,
    takeaways     JSONB  NOT NULL DEFAULT '[]'::jsonb,
    model         TEXT,
    record_count  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, bucket)
);

-- add the new 'summary' stage to the stage_runs CHECK
ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
    CHECK (stage_name IN
        ('discovery','extraction','rss','cluster','summary','render','send'));
