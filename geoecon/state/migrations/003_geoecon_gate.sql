-- Geoeconomic gate: per-article relevance verdict + per-cluster event score.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_geoeconomic BOOLEAN;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_score       REAL;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_country     TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_reason      TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS gate_score       REAL;

ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
    CHECK (stage_name IN
        ('discovery','extraction','rss','gate','cluster','synthesis','summary','render','send'));
