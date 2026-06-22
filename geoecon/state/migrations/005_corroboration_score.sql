-- Change 4: corroboration score on clusters.
-- Additive + nullable. Populated by the cluster stage from the Change-2 aggregates.
-- Consumed for ranking ONLY when Config.rank_by_corroboration is on (ships OFF), so
-- a run before/after this migration ships byte-identical brief content.

ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS corroboration_score REAL;
