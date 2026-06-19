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
