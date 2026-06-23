-- Fix: the Change-1 discovery_registry stage was missing from the
-- stage_runs.stage_name CHECK, so start_stage('discovery_registry') raised a
-- CheckViolation. Because start_stage runs OUTSIDE the orchestrator's per-stage
-- try/except, the error escaped uncaught and main() swallowed it (silent exit 1,
-- no stage row). Add the stage name to the constraint.

ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
    CHECK (stage_name IN
        ('discovery','discovery_registry','extraction','rss','gate','cluster',
         'synthesis','summary','render','send'));
