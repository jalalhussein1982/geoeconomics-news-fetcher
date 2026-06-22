-- Change 2: carry source provenance to articles + clusters.
-- Additive + nullable only (no drops, no rewrites). A run before/after this
-- migration ships byte-identical brief content; only the DB gains columns.

ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS discovery_source TEXT,     -- 'gdelt' | 'registry'
    ADD COLUMN IF NOT EXISTS registry_id      TEXT,     -- Source.id when from registry
    ADD COLUMN IF NOT EXISTS source_tier      SMALLINT, -- 0 primary | 1 wire | 2 regional
    ADD COLUMN IF NOT EXISTS origin_class     TEXT[],
    ADD COLUMN IF NOT EXISTS ownership        TEXT,
    ADD COLUMN IF NOT EXISTS state_alignment  TEXT;

ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS tier_min                  SMALLINT,
    ADD COLUMN IF NOT EXISTS has_primary               BOOLEAN,
    ADD COLUMN IF NOT EXISTS distinct_state_alignments INTEGER,
    ADD COLUMN IF NOT EXISTS source_count              INTEGER;
