import sqlite3
import threading

import pytest

from autosub import db as dbmod
from autosub.accounts import AccountError, Accounts
from autosub.db import Database, DatabaseError, split_sql
from autosub.repository import Repository
from autosub.subtitles import Cue, Word


@pytest.fixture
def repo(cfg):
    database = Database(cfg)
    database.init_schema()
    return Repository(database)


def make_job(repo, owner, job_id="j1", **kw):
    repo.create_job(job_id=job_id, owner=owner, original_name="clip.mp4", video_path="/x/clip.mp4",
                    duration_sec=12.5, width=640, height=360, language=None, **kw)


def test_schema_files_define_the_same_tables():
    mysql = dbmod.table_names(split_sql(dbmod.MYSQL_SCHEMA_FILE.read_text(encoding="utf-8")))
    assert mysql == dbmod.table_names(dbmod.SQLITE_DDL) == ["users", "jobs", "cues", "renders"]


def test_mysql_schema_uses_utf8mb4_innodb_and_foreign_keys():
    sql = dbmod.MYSQL_SCHEMA_FILE.read_text(encoding="utf-8")
    assert sql.count("ENGINE=InnoDB DEFAULT CHARSET=utf8mb4") == 4
    assert sql.count("FOREIGN KEY") == 3
    assert "?" not in sql  # placeholders never appear in DDL


def test_init_schema_is_idempotent(cfg):
    database = Database(cfg)
    database.init_schema()
    database.init_schema()
    assert database.ping()


def test_adapt_translates_placeholders_for_mysql(cfg):
    from dataclasses import replace

    mysql = Database(replace(cfg, db_host="db.example.com"))
    assert mysql.adapt("SELECT * FROM t WHERE a = ? AND b = ?") == "SELECT * FROM t WHERE a = %s AND b = %s"
    assert mysql.dialect == "mysql" and Database(cfg).dialect == "sqlite"


def test_mysql_connection_failure_is_explained(cfg, monkeypatch):
    import sys
    import types
    from dataclasses import replace

    fake = types.ModuleType("pymysql")
    fake.cursors = types.SimpleNamespace(DictCursor=object)

    class MySQLError(Exception):
        pass

    def refuse(**_):
        raise MySQLError("connection refused")

    fake.MySQLError, fake.connect = MySQLError, refuse
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", fake.cursors)
    database = Database(replace(cfg, db_host="127.0.0.1"))
    with pytest.raises(DatabaseError, match="Could not connect to MySQL"):
        database.init_schema()


def test_mysql_connect_passes_tls_and_charset(cfg, monkeypatch):
    import sys
    import types
    from dataclasses import replace

    captured = {}
    fake = types.ModuleType("pymysql")
    fake.cursors = types.SimpleNamespace(DictCursor=object)
    fake.MySQLError = Exception
    fake.connect = lambda **kw: captured.update(kw) or types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", fake.cursors)
    database = Database(replace(cfg, db_host="gateway.example", db_port=4000, db_ssl=True, db_ssl_ca="/tmp/ca.pem"))
    database._connect_mysql()
    assert captured["charset"] == "utf8mb4" and captured["port"] == 4000
    assert captured["ssl_ca"] == "/tmp/ca.pem" and captured["ssl_verify_cert"] is True


def test_users_jobs_cues_roundtrip(repo):
    repo.create_user(user_id="u1", display_name="Asha")
    make_job(repo, "u1")
    cues = [Cue(0, 1000, "नमस्ते दुनिया 🌍", [Word("नमस्ते", 0, 400), Word("दुनिया", 450, 1000)]), Cue(1500, 2500, "second")]
    repo.replace_cues("j1", cues)
    back = repo.get_cues("j1")
    assert [c.text for c in back] == ["नमस्ते दुनिया 🌍", "second"]  # Unicode survives
    assert back[0].words[1].start_ms == 450
    assert repo.get_job("j1")["cue_count"] == 2
    repo.replace_cues("j1", cues[:1])
    assert repo.get_job("j1")["cue_count"] == 1


def test_job_update_whitelist_and_transcript(repo):
    repo.create_user(user_id="u1", display_name="A")
    make_job(repo, "u1")
    repo.update_job("j1", status="ready", progress=100)
    assert repo.get_job("j1")["status"] == "ready"
    with pytest.raises(ValueError):
        repo.update_job("j1", owner="someone-else")
    assert "transcript_json" not in repo.get_job("j1")  # big column is not loaded with every job
    repo.set_transcript("j1", [Word("hi", 0, 300)])
    assert repo.get_transcript("j1")[0].text == "hi"


def test_list_jobs_is_per_owner_newest_first(repo):
    for uid in ("u1", "u2"):
        repo.create_user(user_id=uid, display_name=uid)
    make_job(repo, "u1", "a")
    make_job(repo, "u2", "b")
    assert [j["id"] for j in repo.list_jobs("u1")] == ["a"]
    assert repo.list_jobs("nobody") == []


def test_delete_job_removes_children(repo):
    repo.create_user(user_id="u1", display_name="A")
    make_job(repo, "u1")
    repo.replace_cues("j1", [Cue(0, 500, "x")])
    repo.create_render(render_id="r1", job_id="j1", template="pop")
    repo.delete_job("j1")
    assert repo.get_job("j1") is None and repo.get_cues("j1") == [] and repo.get_render("r1") is None


def test_foreign_key_rejects_unknown_owner(repo):
    with pytest.raises(sqlite3.IntegrityError):
        make_job(repo, "ghost")


def test_owner_stats_counts_only_finished_jobs(repo):
    repo.create_user(user_id="u1", display_name="A")
    make_job(repo, "u1", "a", status="ready")
    make_job(repo, "u1", "b", status="failed")
    assert repo.owner_stats("u1") == {"projects": 1, "seconds": 12.5}


def test_concurrent_writers_do_not_lock_each_other(repo):
    repo.create_user(user_id="u1", display_name="A")
    make_job(repo, "u1")
    errors = []

    def work(n):
        try:
            for i in range(20):
                repo.update_job("j1", progress=(n * 20 + i) % 100)
                repo.get_job("j1")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


# ---- accounts --------------------------------------------------------------
def test_register_login_and_validation(repo):
    acc = Accounts(repo)
    with pytest.raises(AccountError, match="e-mail"):
        acc.register("A", "not-an-email", "longenough")
    with pytest.raises(AccountError, match="password"):
        acc.register("A", "a@b.co", "short")
    user = acc.register("Asha", "Asha@Example.com", "correct horse")
    assert user["email"] == "asha@example.com" and not user["is_guest"]
    with pytest.raises(AccountError, match="already exists"):
        acc.register("Other", "asha@example.com", "another password")
    assert acc.login("ASHA@example.com", "correct horse")["id"] == user["id"]
    for bad in (("asha@example.com", "wrong"), ("nobody@example.com", "x" * 10)):
        with pytest.raises(AccountError, match="not correct"):
            acc.login(*bad)
    assert "password" not in str(acc.get(user["id"]))
    assert repo.get_user(user["id"])["password_hash"] != "correct horse"


def test_guest_projects_follow_the_visitor_into_an_account(repo):
    acc = Accounts(repo)
    guest = acc.create_guest()
    make_job(repo, guest["id"])
    user = acc.register("Asha", "a@b.co", "correct horse", guest_id=guest["id"])
    assert user["id"] == guest["id"] and repo.get_job("j1")["owner"] == user["id"]  # promoted in place

    other = acc.create_guest()
    make_job(repo, other["id"], "j2")
    again = acc.login("a@b.co", "correct horse", guest_id=other["id"])
    assert repo.get_job("j2")["owner"] == again["id"] and repo.get_user(other["id"]) is None


def test_desktop_local_user_is_stable(repo, tmp_path):
    acc = Accounts(repo)
    first = acc.local_user(tmp_path / "d")
    assert acc.local_user(tmp_path / "d")["id"] == first["id"]
