-- Curated-source / economy-section flags on clusters (Unit B).
-- Additive + defaulted FALSE. Populated by the cluster stage from member
-- registry_ids resolved against the registry (curated / section=economy).
-- Consumed for ranking ONLY when Config.curated_preference is on.

ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS has_curated          BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS has_economy_section  BOOLEAN NOT NULL DEFAULT FALSE;
