-- deploy/diagnostics/funnel_report.sql
-- Per-region capture funnel for the geoeconomic pipeline.
--
-- Targets the LATEST run by default (max(run_id)). To pin a specific run,
-- replace `(SELECT max(run_id) FROM runs)` below with the run_id.
--
-- Run it (multi-statement, on stdin) via the deploy access path:
--   kubectl -n hussein-ns exec -i geoecon-db-1 -c postgres -- \
--     psql -U postgres -d geoeconomics -f - < deploy/diagnostics/funnel_report.sql
--
-- The point of this report is to answer, per region bucket, whether the bucket
-- is SUPPLY-limited (the funnel runs dry before the per-region cap binds) or
-- ALLOCATION-limited (more OK records survive than region_cap, so the cap drops
-- some). Compare `synth_ok` (pre-cap, OK records in the bucket) against
-- `rendered` (post-cap newsletter record_count). region_cap is config
-- (default 12) and is not stored in the DB; if rendered == region_cap and
-- synth_ok > rendered, the cap is binding => allocation-limited.
--
-- Egypt (EG) intentionally belongs to BOTH middle_east and north_africa, so it
-- is counted in both buckets here, matching the pipeline's dual-membership.

\pset pager off

-- FIPS country-code -> bucket membership (mirrors geoecon/regions.py BUCKETS).
-- Plain TEMP table (no ON COMMIT DROP — psql autocommits each statement, which
-- would drop the table before the inserts; it auto-drops at session end anyway).
CREATE TEMP TABLE bucket_map(country_fips text, bucket text);
INSERT INTO bucket_map(country_fips, bucket) VALUES
  ('BA','middle_east'),('IR','middle_east'),('IZ','middle_east'),('IS','middle_east'),
  ('JO','middle_east'),('KU','middle_east'),('LE','middle_east'),('MU','middle_east'),
  ('QA','middle_east'),('SA','middle_east'),('SY','middle_east'),('AE','middle_east'),
  ('YM','middle_east'),('EG','middle_east'),('TU','middle_east'),('CY','middle_east'),
  ('WE','middle_east'),('GZ','middle_east'),
  ('EG','north_africa'),('LY','north_africa'),('TS','north_africa'),('AG','north_africa'),
  ('MO','north_africa'),
  ('BR','latam'),('AR','latam'),('MX','latam'),('CI','latam'),('CO','latam'),('PE','latam'),
  ('VE','latam'),('EC','latam'),('BL','latam'),('PA','latam'),('UY','latam'),('GT','latam'),
  ('CU','latam'),('DR','latam'),('HO','latam'),('NU','latam'),('CS','latam'),('PM','latam'),
  ('ES','latam');

\echo '================ RUN HEADER ================'
SELECT run_id, status, window_start, window_end, scheduled_for,
       synthesis_model, finished_at
FROM runs
WHERE run_id = (SELECT max(run_id) FROM runs);

\echo '================ OVERALL FUNNEL ================'
WITH rid AS (SELECT max(run_id) AS r FROM runs)
SELECT
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r)                                   AS discovered,
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r AND a.fetch_status NOT IN ('failed'))    AS extracted_or_headline,
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r AND a.fetch_status = 'headline_only')    AS headline_only,
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r AND a.fetch_status = 'failed')           AS fetch_failed,
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r AND a.gate_geoeconomic IS TRUE)          AS gate_kept,
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r AND a.gate_geoeconomic IS FALSE)         AS gate_dropped,
  (SELECT count(*) FROM articles a, rid WHERE a.run_id = rid.r AND a.gate_reason = 'gate_unparsed')     AS gate_failopen,
  (SELECT count(*) FROM clusters c, rid WHERE c.run_id = rid.r)                                         AS clusters,
  (SELECT count(*) FROM synthesis_records s, rid WHERE s.run_id = rid.r AND s.synthesis_quality_flag = 'ok') AS synth_ok_total;

\echo '================ PER-REGION FUNNEL (supply vs allocation) ================'
-- gate_kept: articles kept by the gate, attributed to bucket via gate-resolved country_fips
-- clusters:  clusters in the bucket (cluster.country_fips = primary article FIPS)
-- synth_ok:  OK synthesis records whose cluster is in the bucket (PRE per-region cap)
-- rendered:  newsletter.record_count for the bucket (POST per-region cap)
-- summary_n: region_summaries.record_count (records fed to the exec-summary, also post-cap)
-- send:      newsletter send_status
WITH rid AS (SELECT max(run_id) AS r FROM runs),
buckets(bucket) AS (VALUES ('middle_east'),('north_africa'),('latam')),
gk AS (
  SELECT bm.bucket, count(DISTINCT a.article_id) AS n
  FROM articles a JOIN bucket_map bm ON bm.country_fips = a.country_fips, rid
  WHERE a.run_id = rid.r AND a.gate_geoeconomic IS TRUE
  GROUP BY bm.bucket),
cl AS (
  SELECT bm.bucket, count(DISTINCT c.cluster_id) AS n
  FROM clusters c JOIN bucket_map bm ON bm.country_fips = c.country_fips, rid
  WHERE c.run_id = rid.r
  GROUP BY bm.bucket),
so AS (
  SELECT bm.bucket, count(DISTINCT s.record_id) AS n
  FROM synthesis_records s
       JOIN clusters c ON c.cluster_id = s.cluster_id
       JOIN bucket_map bm ON bm.country_fips = c.country_fips, rid
  WHERE s.run_id = rid.r AND s.synthesis_quality_flag = 'ok'
  GROUP BY bm.bucket),
nl AS (
  SELECT n.bucket, n.record_count AS n, n.send_status
  FROM newsletters n, rid WHERE n.run_id = rid.r),
rs AS (
  SELECT r.bucket, r.record_count AS n, (r.lede_en IS NOT NULL) AS has_lede
  FROM region_summaries r, rid WHERE r.run_id = rid.r)
SELECT
  b.bucket,
  COALESCE(gk.n,0)  AS gate_kept,
  COALESCE(cl.n,0)  AS clusters,
  COALESCE(so.n,0)  AS synth_ok,
  COALESCE(nl.n,0)  AS rendered,
  COALESCE(rs.n,0)  AS summary_n,
  rs.has_lede,
  COALESCE(nl.send_status,'(no newsletter)') AS send_status,
  CASE
    WHEN COALESCE(so.n,0) = 0 THEN 'EMPTY (no OK records)'
    WHEN COALESCE(so.n,0) > COALESCE(nl.n,0) THEN 'ALLOCATION-limited (cap binding: synth_ok > rendered)'
    ELSE 'SUPPLY-limited (cap not binding: all OK records rendered)'
  END AS verdict
FROM buckets b
LEFT JOIN gk ON gk.bucket = b.bucket
LEFT JOIN cl ON cl.bucket = b.bucket
LEFT JOIN so ON so.bucket = b.bucket
LEFT JOIN nl ON nl.bucket = b.bucket
LEFT JOIN rs ON rs.bucket = b.bucket
ORDER BY CASE b.bucket WHEN 'middle_east' THEN 1 WHEN 'north_africa' THEN 2 ELSE 3 END;

\echo '================ LATAM DETAIL (gate-kept countries) ================'
-- Where LatAm supply actually comes from (or does not): gate-kept articles by FIPS.
WITH rid AS (SELECT max(run_id) AS r FROM runs)
SELECT a.country_fips, count(*) AS gate_kept_articles,
       round(avg(a.gate_score)::numeric, 3) AS avg_gate_score
FROM articles a JOIN bucket_map bm ON bm.country_fips = a.country_fips, rid
WHERE a.run_id = rid.r AND a.gate_geoeconomic IS TRUE AND bm.bucket = 'latam'
GROUP BY a.country_fips
ORDER BY gate_kept_articles DESC;
