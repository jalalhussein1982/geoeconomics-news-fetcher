from __future__ import annotations
from pathlib import Path
import psycopg

_MIGRATIONS = Path(__file__).parent / "migrations"

def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)

def apply_migrations(conn: psycopg.Connection, migrations_dir: str | None = None) -> list[str]:
    d = Path(migrations_dir) if migrations_dir else _MIGRATIONS
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
    done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    applied = []
    for sql_file in sorted(d.glob("*.sql")):
        if sql_file.name in done:
            continue
        conn.execute(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(filename) VALUES (%s)", (sql_file.name,))
        applied.append(sql_file.name)
    return applied
