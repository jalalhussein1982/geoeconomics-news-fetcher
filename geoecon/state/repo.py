from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
import psycopg
from psycopg.types.json import Jsonb
from .models import Article, Cluster, SynthesisRecord

class Repo:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # ---- runs / stages ----
    def create_run(self, window_start, window_end, scheduled_for,
                   synthesis_model, synthesis_prompt_id, embedding_model=None) -> int:
        return self.conn.execute(
            """INSERT INTO runs(window_start, window_end, scheduled_for,
                 synthesis_model, synthesis_prompt_id, embedding_model)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING run_id""",
            (window_start, window_end, scheduled_for, synthesis_model,
             synthesis_prompt_id, embedding_model)).fetchone()[0]

    def find_resumable_run(self, window_start, window_end) -> int | None:
        row = self.conn.execute(
            """SELECT run_id FROM runs
               WHERE window_start=%s AND window_end=%s AND status IN ('running','failed','partial')
               ORDER BY run_id DESC LIMIT 1""", (window_start, window_end)).fetchone()
        return row[0] if row else None

    def completed_stages(self, run_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT stage_name FROM stage_runs WHERE run_id=%s AND status='completed'",
            (run_id,)).fetchall()
        return {r[0] for r in rows}

    def _upsert_stage(self, run_id, stage, status, checkpoint=None, error=None):
        self.conn.execute(
            """INSERT INTO stage_runs(run_id, stage_name, status, started_at, finished_at, checkpoint, error)
               VALUES (%s,%s,%s, now(), %s, %s, %s)
               ON CONFLICT (run_id, stage_name) DO UPDATE SET
                 status=EXCLUDED.status,
                 finished_at=COALESCE(EXCLUDED.finished_at, stage_runs.finished_at),
                 checkpoint=COALESCE(EXCLUDED.checkpoint, stage_runs.checkpoint),
                 error=EXCLUDED.error""",
            (run_id, stage, status,
             datetime.now(timezone.utc) if status in ("completed", "failed", "skipped") else None,
             Jsonb(checkpoint) if checkpoint is not None else Jsonb({}), error))

    def start_stage(self, run_id, stage): self._upsert_stage(run_id, stage, "running")
    def complete_stage(self, run_id, stage, checkpoint=None): self._upsert_stage(run_id, stage, "completed", checkpoint)
    def fail_stage(self, run_id, stage, error): self._upsert_stage(run_id, stage, "failed", error=str(error)[:4000])
    def skip_stage(self, run_id, stage): self._upsert_stage(run_id, stage, "skipped")

    def set_run_status(self, run_id, status):
        self.conn.execute(
            "UPDATE runs SET status=%s, finished_at=CASE WHEN %s IN ('completed','failed','partial') THEN now() ELSE finished_at END WHERE run_id=%s",
            (status, status, run_id))

    def merge_run_metrics(self, run_id, metrics: dict):
        self.conn.execute("UPDATE runs SET metrics = metrics || %s WHERE run_id=%s",
                          (Jsonb(metrics), run_id))

    # ---- articles ----
    def insert_article(self, run_id, *, source_url, url_hash, domain=None, region=None,
                       gkg_themes=None, gkg_tone=None, num_mentions=None, source_lang=None,
                       title=None, gdelt_gkg_id=None, canonical_url=None, published_at=None,
                       discovered_via="gdelt", country_fips=None) -> int:
        existing = self.conn.execute(
            "SELECT article_id FROM articles WHERE run_id=%s AND url_hash=%s",
            (run_id, url_hash)).fetchone()
        if existing:
            return existing[0]
        return self.conn.execute(
            """INSERT INTO articles(run_id, gdelt_gkg_id, source_url, canonical_url, url_hash,
                 domain, discovered_via, region, country_fips, gkg_themes, gkg_tone, num_mentions,
                 source_lang, title, published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING article_id""",
            (run_id, gdelt_gkg_id, source_url, canonical_url, url_hash, domain, discovered_via,
             region, country_fips, gkg_themes or [], gkg_tone, num_mentions, source_lang, title,
             published_at)
        ).fetchone()[0]

    def update_extraction(self, article_id, body, fetch_status, source_lang=None,
                          fetch_error=None, title=None):
        self.conn.execute(
            "UPDATE articles SET body=%s, fetch_status=%s, "
            "source_lang=COALESCE(%s, source_lang), fetch_error=%s, "
            "title=COALESCE(%s, title) WHERE article_id=%s",
            (body, fetch_status, source_lang, fetch_error, title, article_id))

    def upsert_gate(self, article_id, *, geoeconomic, score, country, reason, country_fips):
        self.conn.execute(
            "UPDATE articles SET gate_geoeconomic=%s, gate_score=%s, gate_country=%s, "
            "gate_reason=%s, country_fips=%s WHERE article_id=%s",
            (geoeconomic, score, country, reason, country_fips, article_id))

    def articles_for_run(self, run_id, fetch_status=None) -> list[Article]:
        q = ("SELECT article_id, source_url, url_hash, domain, region, gkg_themes, gkg_tone, "
             "num_mentions, source_lang, title, body, fetch_status, country_fips, "
             "gate_geoeconomic, gate_score, gate_country, gate_reason "
             "FROM articles WHERE run_id=%s")
        params: tuple = (run_id,)
        if fetch_status:
            q += " AND fetch_status=%s"; params += (fetch_status,)
        return [Article(*r) for r in self.conn.execute(q + " ORDER BY article_id", params).fetchall()]

    # ---- clusters ----
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False, country_fips=None, gate_score=None) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, country_fips,
                 languages, article_count, framing_divergent, gate_score)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, country_fips, languages, article_count,
             framing_divergent, gate_score)).fetchone()[0]

    def add_membership(self, cluster_id, article_id, frame_label=None):
        self.conn.execute(
            "INSERT INTO cluster_articles(cluster_id, article_id, frame_label) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (cluster_id, article_id, frame_label))

    def clusters_for_run(self, run_id) -> list[Cluster]:
        rows = self.conn.execute(
            "SELECT cluster_id, event_fingerprint, region, languages, article_count, framing_divergent FROM clusters WHERE run_id=%s ORDER BY cluster_id",
            (run_id,)).fetchall()
        return [Cluster(*r) for r in rows]

    def articles_in_cluster(self, cluster_id) -> list[Article]:
        rows = self.conn.execute(
            """SELECT a.article_id, a.source_url, a.url_hash, a.domain, a.region, a.gkg_themes,
                 a.gkg_tone, a.num_mentions, a.source_lang, a.title, a.body, a.fetch_status
               FROM articles a JOIN cluster_articles ca ON ca.article_id=a.article_id
               WHERE ca.cluster_id=%s ORDER BY a.article_id""", (cluster_id,)).fetchall()
        return [Article(*r) for r in rows]

    # ---- synthesis ----
    def upsert_synthesis_record(self, run_id, cluster_id, fields: dict, quality_flag,
                                quality_detail=None, is_headline_only=False) -> int:
        f = fields
        return self.conn.execute(
            """INSERT INTO synthesis_records(run_id, cluster_id, headline_en, headline_original,
                 headline_original_lang, what_happened_en, geoeconomic_significance_en,
                 geoeconomic_vector, entities, key_figures, framing_divergence, sources,
                 synthesis_quality_flag, quality_detail, is_headline_only)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, cluster_id) DO UPDATE SET
                 headline_en=EXCLUDED.headline_en, headline_original=EXCLUDED.headline_original,
                 headline_original_lang=EXCLUDED.headline_original_lang,
                 what_happened_en=EXCLUDED.what_happened_en,
                 geoeconomic_significance_en=EXCLUDED.geoeconomic_significance_en,
                 geoeconomic_vector=EXCLUDED.geoeconomic_vector, entities=EXCLUDED.entities,
                 key_figures=EXCLUDED.key_figures, framing_divergence=EXCLUDED.framing_divergence,
                 sources=EXCLUDED.sources, synthesis_quality_flag=EXCLUDED.synthesis_quality_flag,
                 quality_detail=EXCLUDED.quality_detail, is_headline_only=EXCLUDED.is_headline_only
               RETURNING record_id""",
            (run_id, cluster_id, f.get("headline_en"), f.get("headline_original"),
             f.get("headline_original_lang"), f.get("what_happened_en"),
             f.get("geoeconomic_significance_en"), f.get("geoeconomic_vector") or [],
             Jsonb(f.get("entities") or []), Jsonb(f.get("key_figures") or []),
             Jsonb(f.get("framing_divergence")) if f.get("framing_divergence") is not None else None,
             Jsonb(f.get("sources") or []), quality_flag,
             Jsonb(quality_detail) if quality_detail is not None else None, is_headline_only)
        ).fetchone()[0]

    def ok_records(self, run_id) -> list[SynthesisRecord]:
        rows = self.conn.execute(
            """SELECT record_id, cluster_id, headline_en, headline_original, headline_original_lang,
                 what_happened_en, geoeconomic_significance_en, geoeconomic_vector, entities,
                 key_figures, framing_divergence, sources, is_headline_only
               FROM synthesis_records WHERE run_id=%s AND synthesis_quality_flag='ok'
               ORDER BY record_id""", (run_id,)).fetchall()
        out = []
        for r in rows:
            out.append(SynthesisRecord(
                record_id=r[0], cluster_id=r[1],
                fields={"headline_en": r[2], "headline_original": r[3],
                        "headline_original_lang": r[4], "what_happened_en": r[5],
                        "geoeconomic_significance_en": r[6], "geoeconomic_vector": r[7],
                        "entities": r[8], "key_figures": r[9], "framing_divergence": r[10],
                        "sources": r[11]},
                quality_flag="ok", is_headline_only=r[12]))
        return out

    def records_for_buckets(self, run_id) -> list[tuple[SynthesisRecord, str | None, list[dict], float | None]]:
        """OK records with their cluster country_fips, verified source link(s), and gate score.

        Returns [(SynthesisRecord, country_fips, [{"url","domain"}, ...], gate_score), ...],
        one tuple per record. Source links come from articles.source_url (verified
        GDELT URLs), NOT the LLM 'sources' field. gate_score is the cluster-level
        geoeconomic relevance score (float or None).
        """
        rows = self.conn.execute(
            """SELECT s.record_id, s.cluster_id, s.headline_en, s.headline_original,
                 s.headline_original_lang, s.what_happened_en, s.geoeconomic_significance_en,
                 s.geoeconomic_vector, s.entities, s.key_figures, s.framing_divergence,
                 s.sources, s.is_headline_only, c.country_fips, c.gate_score, a.source_url, a.domain
               FROM synthesis_records s
               JOIN clusters c ON c.cluster_id = s.cluster_id
               LEFT JOIN cluster_articles ca ON ca.cluster_id = c.cluster_id
               LEFT JOIN articles a ON a.article_id = ca.article_id
               WHERE s.run_id=%s AND s.synthesis_quality_flag='ok'
               ORDER BY s.record_id, a.article_id""", (run_id,)).fetchall()
        by_id: dict[int, list] = {}
        order: list[int] = []
        for r in rows:
            rec_id = r[0]
            if rec_id not in by_id:
                rec = SynthesisRecord(
                    record_id=r[0], cluster_id=r[1],
                    fields={"headline_en": r[2], "headline_original": r[3],
                            "headline_original_lang": r[4], "what_happened_en": r[5],
                            "geoeconomic_significance_en": r[6], "geoeconomic_vector": r[7],
                            "entities": r[8], "key_figures": r[9], "framing_divergence": r[10],
                            "sources": r[11]},
                    quality_flag="ok", is_headline_only=r[12])
                by_id[rec_id] = [rec, r[13], [], r[14]]
                order.append(rec_id)
            if r[15]:  # source_url present
                src = {"url": r[15], "domain": r[16]}
                if src not in by_id[rec_id][2]:
                    by_id[rec_id][2].append(src)
        return [tuple(by_id[i]) for i in order]

    # ---- seen-store ----
    def get_seen(self, fingerprint) -> dict | None:
        row = self.conn.execute(
            "SELECT event_fingerprint, times_seen, last_emitted_at, expires_at FROM seen_events WHERE event_fingerprint=%s",
            (fingerprint,)).fetchone()
        if not row:
            return None
        return {"event_fingerprint": row[0], "times_seen": row[1],
                "last_emitted_at": row[2], "expires_at": row[3]}

    def upsert_seen(self, fingerprint, run_id, ttl_days=14):
        expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        self.conn.execute(
            """INSERT INTO seen_events(event_fingerprint, first_seen_run, last_seen_run,
                 last_emitted_at, times_seen, expires_at)
               VALUES (%s,%s,%s, now(), 1, %s)
               ON CONFLICT (event_fingerprint) DO UPDATE SET
                 last_seen_run=EXCLUDED.last_seen_run, last_emitted_at=now(),
                 times_seen=seen_events.times_seen+1, expires_at=EXCLUDED.expires_at""",
            (fingerprint, run_id, run_id, expires))

    # ---- newsletters ----
    def insert_newsletter(self, run_id, render_mode, archive_html_path, email_html,
                          record_count, bucket=None) -> int:
        return self.conn.execute(
            """INSERT INTO newsletters(run_id, render_mode, archive_html_path, email_html,
                 record_count, bucket)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, bucket) DO UPDATE SET
                 render_mode=EXCLUDED.render_mode, archive_html_path=EXCLUDED.archive_html_path,
                 email_html=EXCLUDED.email_html, record_count=EXCLUDED.record_count,
                 send_status='pending', send_error=NULL
               RETURNING newsletter_id""",
            (run_id, render_mode, archive_html_path, email_html, record_count, bucket)).fetchone()[0]

    def newsletters_for_run(self, run_id) -> list[dict]:
        rows = self.conn.execute(
            "SELECT newsletter_id, bucket, record_count, email_html, send_status "
            "FROM newsletters WHERE run_id=%s ORDER BY bucket", (run_id,)).fetchall()
        return [{"newsletter_id": r[0], "bucket": r[1], "record_count": r[2],
                 "email_html": r[3], "send_status": r[4]} for r in rows]

    def update_newsletter_send(self, newsletter_id, send_status, send_error=None):
        self.conn.execute(
            "UPDATE newsletters SET send_status=%s, send_error=%s, sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END WHERE newsletter_id=%s",
            (send_status, send_error, send_status, newsletter_id))

    def get_newsletter(self, run_id) -> dict | None:
        row = self.conn.execute(
            "SELECT newsletter_id, record_count, email_html FROM newsletters WHERE run_id=%s",
            (run_id,)).fetchone()
        if not row:
            return None
        return {"newsletter_id": row[0], "record_count": row[1], "email_html": row[2]}

    def mark_emitted(self, run_id):
        ok_cluster_ids = {rec.cluster_id for rec in self.ok_records(run_id)}
        for cl in self.clusters_for_run(run_id):
            if cl.cluster_id in ok_cluster_ids:
                self.upsert_seen(cl.event_fingerprint, run_id)

    # ---- region summaries ----
    def upsert_region_summary(self, run_id, bucket, lede_en, takeaways, model, record_count):
        self.conn.execute(
            """INSERT INTO region_summaries(run_id, bucket, lede_en, takeaways, model, record_count)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, bucket) DO UPDATE SET
                 lede_en=EXCLUDED.lede_en, takeaways=EXCLUDED.takeaways,
                 model=EXCLUDED.model, record_count=EXCLUDED.record_count, created_at=now()""",
            (run_id, bucket, lede_en, Jsonb(takeaways or []), model, record_count))

    def get_region_summary(self, run_id, bucket) -> dict | None:
        row = self.conn.execute(
            "SELECT bucket, lede_en, takeaways, model, record_count "
            "FROM region_summaries WHERE run_id=%s AND bucket=%s", (run_id, bucket)).fetchone()
        if not row:
            return None
        return {"bucket": row[0], "lede_en": row[1], "takeaways": row[2],
                "model": row[3], "record_count": row[4]}
