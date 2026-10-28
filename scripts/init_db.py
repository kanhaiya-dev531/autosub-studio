"""Create the MySQL database (if your account may) and the tables.

    python scripts/init_db.py

Reads the DB_* settings from .env. The app also creates the tables by itself on start-up, so this
script is mainly a way to check the connection and to see a clear error message. It is safe to run again.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autosub.config import Config  # noqa: E402
from autosub.db import Database, DatabaseError  # noqa: E402


def create_database(cfg: Config) -> None:
    """CREATE DATABASE IF NOT EXISTS, using a connection that does not name a database yet."""
    import re
    import ssl

    import pymysql

    if not re.fullmatch(r"[A-Za-z0-9_$]{1,64}", cfg.db_name):
        raise DatabaseError("DB_NAME may only contain letters, digits, _ and $.")
    kwargs: dict = {
        "host": cfg.db_host, "port": cfg.db_port, "user": cfg.db_user, "password": cfg.db_password,
        "charset": "utf8mb4", "connect_timeout": 15,
    }
    if cfg.db_ssl:
        kwargs["ssl"] = ssl.create_default_context(cafile=cfg.db_ssl_ca or None)
    try:
        connection = pymysql.connect(**kwargs)
    except pymysql.MySQLError as exc:
        raise DatabaseError(f"Could not connect to {cfg.db_host}:{cfg.db_port} as '{cfg.db_user}': {exc}") from exc
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        connection.commit()
    except pymysql.MySQLError as exc:
        # Hosted databases usually give you one database already and do not let you create more.
        print(f"Note: could not create database '{cfg.db_name}' ({exc.args[-1] if exc.args else exc}).")
        print("      If it already exists that is fine. Otherwise set DB_NAME to the database your host created for you.")
    finally:
        connection.close()


def main() -> int:
    cfg = Config.from_env()
    if not cfg.uses_mysql:
        print(f"DB_HOST is empty, so the app uses a local SQLite file: {cfg.sqlite_path}")
        print("Set DB_HOST (and DB_USER, DB_PASSWORD, DB_NAME) in .env to use MySQL.")
    else:
        print(f"Connecting to MySQL at {cfg.db_host}:{cfg.db_port} ...")
        try:
            create_database(cfg)
        except (DatabaseError, ImportError) as exc:
            print(f"Failed: {exc}")
            return 1
    try:
        database = Database(cfg)
        database.init_schema(attempts=3, delay=2.0)
    except DatabaseError as exc:
        print(f"Failed: {exc}")
        return 1
    print(f"Ready: {database.description}")
    for row in database.fetchall("SELECT 1 AS ok"):
        assert row["ok"] == 1
    print("Tables: users, jobs, cues, renders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
