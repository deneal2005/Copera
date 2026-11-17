from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(database: str | Path | None = None) -> sqlite3.Connection:
    path = str(database or config.DATABASE)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(database: str | Path | None = None) -> None:
    conn = connect(database)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        directory = Path(__file__).resolve().parents[1] / "migrations"
        for migration in sorted(directory.glob("*.sql")):
            if migration.name in applied:
                continue
            statement = (
                "BEGIN IMMEDIATE;\n"
                + migration.read_text(encoding="utf-8")
                + "\nINSERT INTO schema_migrations(name, applied_at) VALUES ('"
                + migration.name.replace("'", "''")
                + "', '"
                + now_iso()
                + "');\nCOMMIT;"
            )
            try:
                conn.executescript(statement)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
    finally:
        conn.close()


@contextmanager
def transaction(database: str | Path | None = None, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn = connect(database)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def atomic(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
