"""Thin database layer: MySQL in production, SQLite for zero-setup development.

The SQL in `repository.py` is written once with `?` placeholders. This module
turns it into `%s` for MySQL (PyMySQL) and runs it unchanged on SQLite.
"""

from __future__ import annotations

import re
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import PROJECT_ROOT, Config

MYSQL_SCHEMA_FILE = PROJECT_ROOT / "schema" / "mysql_schema.sql"

SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE,
        display_name TEXT NOT NULL,
        password_hash TEXT,
        is_guest INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        original_name TEXT NOT NULL,
        video_path TEXT NOT NULL,
        duration_sec REAL,
        width INTEGER,
        height INTEGER,
        language TEXT,
        status TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        message TEXT,
        transcript_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_owner_created ON jobs (owner, created_at)",
    """CREATE TABLE IF NOT EXISTS cues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        text TEXT NOT NULL,
        words_json TEXT,
        UNIQUE (job_id, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS renders (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        template TEXT NOT NULL,
        status TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        output_path TEXT,
        message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_renders_job ON renders (job_id, created_at)",
]


class DatabaseError(RuntimeError):
    """Raised for configuration or connection problems, with a readable message."""


def split_sql(script: str) -> list[str]:
    """Split a .sql file into single statements, dropping `--` comment lines."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


class _Session:
    """One open transaction. Use it through `Database.session()`."""

    def __init__(self, db: Database, cursor):
        self._db = db
        self._cur = cursor

    def execute(self, sql: str, params: tuple = ()) -> int:
        self._cur.execute(self._db.adapt(sql), tuple(params))
        return self._cur.rowcount

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if rows:
            self._cur.executemany(self._db.adapt(sql), [tuple(r) for r in rows])

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        self._cur.execute(self._db.adapt(sql), tuple(params))
        return [dict(row) for row in self._cur.fetchall()]

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        self._cur.execute(self._db.adapt(sql), tuple(params))
        row = self._cur.fetchone()
        return dict(row) if row is not None else None


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dialect = "mysql" if cfg.uses_mysql else "sqlite"
        self._local = threading.local()
        if self.dialect == "sqlite":
            cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- helpers -----------------------------------------------------
    def adapt(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "mysql" else sql

    @property
    def description(self) -> str:
        if self.dialect == "mysql":
            return f"MySQL {self.cfg.db_user}@{self.cfg.db_host}:{self.cfg.db_port}/{self.cfg.db_name}"
        return f"SQLite {self.cfg.sqlite_path}"

    # ---- connections -------------------------------------------------
    def _connect_sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.cfg.sqlite_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _connect_mysql(self):
        try:
            import pymysql
            import pymysql.cursors
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise DatabaseError("PyMySQL is not installed. Run: pip install -r requirements.txt") from exc

        cfg = self.cfg
        kwargs = dict(
            host=cfg.db_host,
            port=cfg.db_port,
            user=cfg.db_user,
            password=cfg.db_password,
            database=cfg.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=15,
            read_timeout=60,
            write_timeout=60,
        )
        if cfg.db_ssl:
            ca = cfg.db_ssl_ca
            if not ca:
                try:
                    import certifi

                    ca = certifi.where()
                except ImportError:
                    ca = ssl.get_default_verify_paths().cafile or ""
            if ca:
                kwargs.update(ssl_ca=ca, ssl_verify_cert=True, ssl_verify_identity=True)
            else:
                kwargs["ssl"] = ssl.create_default_context()
        try:
            return pymysql.connect(**kwargs)
        except pymysql.MySQLError as exc:
            raise DatabaseError(
                f"Could not connect to MySQL at {cfg.db_host}:{cfg.db_port} "
                f"(database '{cfg.db_name}', user '{cfg.db_user}'): {exc}"
            ) from exc

    def _acquire(self):
        if self.dialect == "sqlite":
            return self._connect_sqlite()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                self._local.conn = None
        conn = self._connect_mysql()
        self._local.conn = conn
        return conn

    @contextmanager
    def session(self):
        """Open a transaction: commits on success, rolls back on any error."""
        conn = self._acquire()
        cur = conn.cursor()
        try:
            yield _Session(self, cur)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            if self.dialect == "sqlite":
                conn.close()

    # ---- convenience wrappers (one statement = one transaction) -------
    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.session() as s:
            return s.execute(sql, params)

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.session() as s:
            return s.fetchall(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        with self.session() as s:
            return s.fetchone(sql, params)

    # ---- schema ------------------------------------------------------
    def init_schema(self, attempts: int = 1, delay: float = 3.0) -> None:
        """Create any missing tables. `attempts` > 1 waits for a database that is still booting."""
        if self.dialect == "sqlite":
            statements = SQLITE_DDL
        else:
            statements = split_sql(Path(MYSQL_SCHEMA_FILE).read_text(encoding="utf-8"))
        for attempt in range(1, max(1, attempts) + 1):
            try:
                with self.session() as s:
                    for stmt in statements:
                        s.execute(stmt)
                return
            except DatabaseError:
                if attempt >= attempts:
                    raise
                time.sleep(delay)

    def ping(self) -> bool:
        self.fetchone("SELECT 1 AS ok")
        return True


_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)


def table_names(statements: list[str]) -> list[str]:
    """Names of tables created by a list of DDL statements (used by the tests)."""
    names: list[str] = []
    for stmt in statements:
        match = _TABLE_RE.search(stmt)
        if match:
            names.append(match.group(1))
    return names
