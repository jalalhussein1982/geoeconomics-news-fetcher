import os, uuid, pytest, psycopg
from datetime import datetime, timezone, timedelta
from geoecon.state import db
from geoecon.state.repo import Repo

requires_db = pytest.mark.requires_db
DSN = os.environ.get("TEST_DATABASE_URL")

@pytest.fixture
def repo():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE {name}')
    test_dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = db.connect(test_dsn); db.apply_migrations(conn)
    yield Repo(conn)
    conn.close(); admin.execute(f'DROP DATABASE {name} WITH (FORCE)'); admin.close()

def _win():
    end = datetime(2026, 6, 19, 5, 0, tzinfo=timezone.utc)
    return end - timedelta(hours=24), end, end

@requires_db
def test_run_and_stage_lifecycle(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    assert repo.find_resumable_run(ws, we) == rid
    repo.start_stage(rid, "discovery"); repo.complete_stage(rid, "discovery", {"n": 3})
    assert "discovery" in repo.completed_stages(rid)

@requires_db
def test_article_dedup_by_url_hash(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    a1 = repo.insert_article(rid, source_url="http://x/1", url_hash="h1", domain="x",
                             region="MENA", gkg_themes=["ECON_X"], title="t")
    a2 = repo.insert_article(rid, source_url="http://x/1", url_hash="h1", domain="x",
                             region="MENA", gkg_themes=["ECON_X"], title="t")
    assert a1 == a2  # dedup

@requires_db
def test_only_ok_records_returned(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", domain="d",
                              region="MENA", gkg_themes=[], title="t")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 1)
    repo.add_membership(cid, aid)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    bad = repo.insert_cluster(rid, "fp2", "MENA", ["en"], 1)
    repo.upsert_synthesis_record(rid, bad, {"headline_en": "B"}, "zero_entities")
    recs = repo.ok_records(rid)
    assert len(recs) == 1 and recs[0].fields["headline_en"] == "A"

@requires_db
def test_seen_store_roundtrip(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    assert repo.get_seen("fpX") is None
    repo.upsert_seen("fpX", rid)
    assert repo.get_seen("fpX")["times_seen"] == 1
    repo.upsert_seen("fpX", rid)
    assert repo.get_seen("fpX")["times_seen"] == 2

@requires_db
def test_get_newsletter_roundtrip(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    # no newsletter yet
    assert repo.get_newsletter(rid) is None
    nid = repo.insert_newsletter(rid, "en_only", "/archive/x.html", "<p>hello</p>", 3)
    result = repo.get_newsletter(rid)
    assert result is not None
    assert result["newsletter_id"] == nid
    assert result["record_count"] == 3
    assert result["email_html"] == "<p>hello</p>"

@requires_db
def test_mark_emitted_marks_only_ok_clusters(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    # cluster A — ok synthesis record
    aid_a = repo.insert_article(rid, source_url="http://a/1", url_hash="ha1", domain="a",
                                region="MENA", gkg_themes=[], title="A")
    cid_a = repo.insert_cluster(rid, "fpA", "MENA", ["en"], 1)
    repo.add_membership(cid_a, aid_a)
    repo.upsert_synthesis_record(rid, cid_a, {"headline_en": "A"}, "ok")
    # cluster B — non-ok synthesis record
    aid_b = repo.insert_article(rid, source_url="http://b/1", url_hash="hb1", domain="b",
                                region="other", gkg_themes=[], title="B")
    cid_b = repo.insert_cluster(rid, "fpB", "other", ["de"], 1)
    repo.add_membership(cid_b, aid_b)
    repo.upsert_synthesis_record(rid, cid_b, {"headline_en": "B"}, "zero_entities")
    # neither seen yet
    assert repo.get_seen("fpA") is None
    assert repo.get_seen("fpB") is None
    repo.mark_emitted(rid)
    seen_a = repo.get_seen("fpA")
    assert seen_a is not None
    assert seen_a["times_seen"] == 1
    assert seen_a["last_emitted_at"] is not None
    assert repo.get_seen("fpB") is None

@requires_db
def test_records_for_buckets_carries_country_and_source(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="https://reuters.com/x", url_hash="h",
                              domain="reuters.com", region="MENA", gkg_themes=[],
                              title="t", country_fips="EG")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 1, country_fips="EG")
    repo.add_membership(cid, aid)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    out = repo.records_for_buckets(rid)
    assert len(out) == 1
    rec, country, sources = out[0]
    assert rec.fields["headline_en"] == "A"
    assert country == "EG"
    assert sources == [{"url": "https://reuters.com/x", "domain": "reuters.com"}]

@requires_db
def test_migration_adds_country_and_per_region_newsletters(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    # articles.country_fips column exists (raw insert; repo keyword arrives in a later task)
    repo.conn.execute(
        "INSERT INTO articles(run_id, source_url, url_hash, region, country_fips) "
        "VALUES (%s,'u','h','MENA','EG')", (rid,))
    assert repo.conn.execute(
        "SELECT country_fips FROM articles WHERE run_id=%s", (rid,)).fetchone()[0] == "EG"
    # clusters.country_fips column exists
    repo.conn.execute(
        "INSERT INTO clusters(run_id, event_fingerprint, region, country_fips, article_count) "
        "VALUES (%s,'fp','MENA','EG',1)", (rid,))
    assert repo.conn.execute(
        "SELECT country_fips FROM clusters WHERE run_id=%s", (rid,)).fetchone()[0] == "EG"
    # newsletters: two rows for same run, different bucket -> allowed by UNIQUE(run_id,bucket)
    repo.conn.execute(
        "INSERT INTO newsletters(run_id, render_mode, record_count, bucket) "
        "VALUES (%s,'en_only',0,'middle_east'),(%s,'en_only',0,'north_africa')", (rid, rid))
    assert repo.conn.execute(
        "SELECT count(*) FROM newsletters WHERE run_id=%s", (rid,)).fetchone()[0] == 2
    # region_summaries table exists and round-trips
    repo.conn.execute(
        "INSERT INTO region_summaries(run_id, bucket, lede_en) VALUES (%s,'latam','hi')", (rid,))
    assert repo.conn.execute(
        "SELECT lede_en FROM region_summaries WHERE run_id=%s AND bucket='latam'",
        (rid,)).fetchone()[0] == "hi"
    # 'summary' is now an allowed stage_name (CHECK updated)
    repo.start_stage(rid, "summary")  # raises if CHECK rejects 'summary'


@requires_db
def test_records_for_buckets_aggregates_multiple_articles(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    a1 = repo.insert_article(rid, source_url="https://a.com/x", url_hash="h1",
                             domain="a.com", region="MENA", title="t", country_fips="EG")
    a2 = repo.insert_article(rid, source_url="https://b.com/y", url_hash="h2",
                             domain="b.com", region="MENA", title="t", country_fips="EG")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 2, country_fips="EG")
    repo.add_membership(cid, a1)
    repo.add_membership(cid, a2)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    out = repo.records_for_buckets(rid)
    assert len(out) == 1                       # one tuple per record
    _rec, country, sources = out[0]
    assert country == "EG"
    assert {s["url"] for s in sources} == {"https://a.com/x", "https://b.com/y"}


@requires_db
def test_records_for_buckets_no_member_article_has_empty_sources(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 0, country_fips="EG")
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    out = repo.records_for_buckets(rid)
    assert len(out) == 1
    _rec, country, sources = out[0]
    assert country == "EG" and sources == []


@requires_db
def test_records_for_buckets_excludes_non_ok(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 0, country_fips="EG")
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "held"}, "zero_entities")
    assert repo.records_for_buckets(rid) == []


@requires_db
def test_newsletter_per_bucket_upsert_and_list(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    n1 = repo.insert_newsletter(rid, "en_only", "/a/me.html", "<b>me</b>", 3, bucket="middle_east")
    n2 = repo.insert_newsletter(rid, "en_only", "/a/na.html", "<b>na</b>", 1, bucket="north_africa")
    assert n1 != n2
    # re-render same bucket -> same row, content updated, status reset
    repo.update_newsletter_send(n1, "sent")
    n1b = repo.insert_newsletter(rid, "en_only", "/a/me2.html", "<b>me2</b>", 4, bucket="middle_east")
    assert n1b == n1
    rows = {r["bucket"]: r for r in repo.newsletters_for_run(rid)}
    assert rows["middle_east"]["record_count"] == 4
    assert rows["middle_east"]["email_html"] == "<b>me2</b>"
    assert rows["middle_east"]["send_status"] == "pending"  # reset on re-render
    assert set(rows) == {"middle_east", "north_africa"}


@requires_db
def test_region_summary_upsert_and_get(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    repo.upsert_region_summary(rid, "middle_east", "Lede one.", ["a", "b"], "deepseek-v3.2", 2)
    got = repo.get_region_summary(rid, "middle_east")
    assert got["lede_en"] == "Lede one."
    assert got["takeaways"] == ["a", "b"]
    assert got["record_count"] == 2
    # upsert overwrites
    repo.upsert_region_summary(rid, "middle_east", "Lede two.", ["c"], "deepseek-v3.2", 1)
    got2 = repo.get_region_summary(rid, "middle_east")
    assert got2["lede_en"] == "Lede two." and got2["takeaways"] == ["c"]
    assert repo.get_region_summary(rid, "latam") is None


@requires_db
def test_articles_for_run_includes_country_fips(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    repo.insert_article(rid, source_url="u", url_hash="h", region="MENA",
                        country_fips="EG", title="t")
    arts = repo.articles_for_run(rid)
    assert arts[0].country_fips == "EG"


@requires_db
def test_cluster_run_persists_country_fips_via_repo(repo):
    from geoecon.stages import cluster as cluster_stage
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", region="MENA",
                              country_fips="EG", title="t")
    repo.upsert_gate(aid, geoeconomic=True, score=0.8, country="Egypt",
                     reason="trade", country_fips="EG")
    cfg = type("C", (), {"buckets": ("middle_east", "north_africa"), "dedup_jaccard": 0.6})()
    cluster_stage.run(cfg, repo, rid)
    crow = repo.conn.execute(
        "SELECT country_fips FROM clusters WHERE run_id=%s", (rid,)).fetchone()
    assert crow[0] == "EG"


@requires_db
def test_synthesis_remains_allowed_stage_name(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    repo.start_stage(rid, "synthesis")   # must not raise (CHECK must include 'synthesis')
    repo.complete_stage(rid, "synthesis")
    assert "synthesis" in repo.completed_stages(rid)


@requires_db
def test_update_extraction_sets_title_and_upsert_gate(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", region="MENA",
                              country_fips="EG")
    repo.update_extraction(aid, "body text here", "extracted", title="A Real Headline")
    arts = {a.article_id: a for a in repo.articles_for_run(rid)}
    assert arts[aid].title == "A Real Headline"
    assert arts[aid].body == "body text here"
    # gate overwrites country_fips + records verdict
    repo.upsert_gate(aid, geoeconomic=True, score=0.77, country="Egypt",
                     reason="econ x geopolitics", country_fips="EG")
    a = {x.article_id: x for x in repo.articles_for_run(rid)}[aid]
    assert a.gate_geoeconomic is True and abs(a.gate_score - 0.77) < 1e-6
    assert a.gate_country == "Egypt" and a.country_fips == "EG"


@requires_db
def test_migration_003_gate_columns_and_stage(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    repo.conn.execute(
        "INSERT INTO articles(run_id, source_url, url_hash, region, country_fips, "
        "gate_geoeconomic, gate_score, gate_country, gate_reason) "
        "VALUES (%s,'u','h','MENA','EG', true, 0.8, 'Egypt', 'why')", (rid,))
    row = repo.conn.execute(
        "SELECT gate_geoeconomic, gate_score, gate_country FROM articles WHERE run_id=%s",
        (rid,)).fetchone()
    assert row[0] is True and abs(row[1] - 0.8) < 1e-6 and row[2] == "Egypt"
    repo.conn.execute(
        "INSERT INTO clusters(run_id, event_fingerprint, region, article_count, gate_score) "
        "VALUES (%s,'fp','MENA',1,0.9)", (rid,))
    assert repo.conn.execute(
        "SELECT gate_score FROM clusters WHERE run_id=%s", (rid,)).fetchone()[0] is not None
    repo.start_stage(rid, "gate")  # must not raise (CHECK includes 'gate')
    assert "gate" not in repo.completed_stages(rid)
