-- Fix: the discovery_search stage (search-spine redesign, spec 2026-07-04) was
-- missing from the stage_runs.stage_name CHECK, so start_stage('discovery_search')
-- raised a CheckViolation the moment SEARCH_DISCOVERY=true went live (caught by
-- the Phase-4 smoke job, 2026-07-04). Same failure and same fix as migration 006
-- (discovery_registry). Add the stage name to the constraint.

ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
    CHECK (stage_name IN
        ('discovery','discovery_registry','discovery_search','extraction','rss',
         'gate','cluster','synthesis','summary','render','send'));
