import os, uuid, pytest, psycopg
from geoecon.state import db

requires_db = pytest.mark.requires_db
DSN = os.environ.get("TEST_DATABASE_URL")

@pytest.fixture
def fresh_db():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE {name}')
    test_dsn = DSN.rsplit("/", 1)[0] + "/" + name
    yield test_dsn
    admin.execute(f'DROP DATABASE {name} WITH (FORCE)')
    admin.close()

@requires_db
def test_apply_migrations_creates_tables(fresh_db):
    conn = db.connect(fresh_db)
    applied = db.apply_migrations(conn)
    assert "001_init.sql" in applied
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"runs", "stage_runs", "articles", "clusters", "cluster_articles",
            "seen_events", "synthesis_records", "throughlines", "newsletters"} <= names

@requires_db
def test_apply_migrations_idempotent(fresh_db):
    conn = db.connect(fresh_db)
    db.apply_migrations(conn)
    db.apply_migrations(conn)  # second run must not raise
