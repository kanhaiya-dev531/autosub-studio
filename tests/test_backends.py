"""The same database checks on SQLite (always) and on a real MySQL server (when one is provided).

SQLite is forgiving where MySQL is strict: it ignores column sizes, accepts any text, and does not need utf8mb4.
These tests exist so that a MySQL-only problem shows up before deployment. To run them against MySQL:

    TEST_DB_HOST=127.0.0.1 TEST_DB_USER=root TEST_DB_PASSWORD=secret TEST_DB_NAME=autosub_test pytest tests/test_backends.py

(create the database first:  CREATE DATABASE autosub_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;)
The tables in that database are dropped and recreated, so never point it at data you want to keep.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from conftest import make_config, needs_ffmpeg, wait_until

from autosub.accounts import AccountError, Accounts
from autosub.db import Database
from autosub.repository import Repository
from autosub.service import JobService
from autosub.subtitles import Cue, Word

BACKENDS = ["sqlite", *(["mysql"] if os.environ.get("TEST_DB_HOST") else [])]


@pytest.fixture(params=BACKENDS)
def backend_cfg(request, tmp_path):
    if request.param == "sqlite":
        return make_config(tmp_path)
    cfg = make_config(
        tmp_path,
        db_host=os.environ["TEST_DB_HOST"],
        db_port=int(os.environ.get("TEST_DB_PORT", "3306")),
        db_user=os.environ.get("TEST_DB_USER", "root"),
        db_password=os.environ.get("TEST_DB_PASSWORD", ""),
        db_name=os.environ.get("TEST_DB_NAME", "autosub_test"),
        db_ssl=os.environ.get("TEST_DB_SSL", "").lower() in ("1", "true", "yes"),
    )
    cleaner = Database(cfg)
    for table in ("renders", "cues", "jobs", "users"):  # children first
        cleaner.execute(f"DROP TABLE IF EXISTS {table}")
    return cfg


@pytest.fixture
def repo(backend_cfg):
    database = Database(backend_cfg)
    database.init_schema()
    return Repository(database)


def add_job(repo, owner, job_id="j1", **kw):
    repo.create_job(job_id=job_id, owner=owner, original_name="clip.mp4", video_path="/x/clip.mp4",
                    duration_sec=12.5, width=640, height=360, language=None, **kw)


def test_database_reports_which_engine_it_uses(backend_cfg):
    database = Database(backend_cfg)
    database.init_schema()
    assert database.ping()
    assert ("MySQL" in database.description) == backend_cfg.uses_mysql


def test_schema_can_be_applied_again_and_again(repo):
    repo.db.init_schema()
    repo.db.init_schema()
    assert repo.db.ping()


def test_text_in_any_script_and_emoji_survives(repo):
    owner = Accounts(repo).create_guest()["id"]
    add_job(repo, owner)
    lines = ["Café crème", "नमस्ते दुनिया", "你好，世界", "emoji 😀🎬 need four-byte utf8", "العربية"]
    repo.replace_cues("j1", [Cue(i * 1000, i * 1000 + 900, t, [Word(t, i * 1000, i * 1000 + 900)])
                             for i, t in enumerate(lines)])
    stored = repo.get_cues("j1")
    assert [c.text for c in stored] == lines
    assert stored[3].words[0].text == lines[3]


def test_a_long_transcript_is_not_cut_off(repo):
    """TEXT holds 64 KB in MySQL; transcripts of long videos are much bigger, hence MEDIUMTEXT."""
    owner = Accounts(repo).create_guest()["id"]
    add_job(repo, owner)
    words = [Word(f"word{i}", i * 300, i * 300 + 250) for i in range(20_000)]
    assert len(json.dumps([[w.text, w.start_ms, w.end_ms] for w in words])) > 300_000
    repo.set_transcript("j1", words)
    back = repo.get_transcript("j1")
    assert len(back) == 20_000 and back[-1].text == "word19999"
    repo.replace_cues("j1", [Cue(0, 5000, "long", words[:5000])])
    assert len(repo.get_cues("j1")[0].words) == 5000


def test_deleting_removes_everything_underneath(repo):
    owner = Accounts(repo).create_guest()["id"]
    add_job(repo, owner)
    repo.replace_cues("j1", [Cue(0, 900, "a")])
    repo.create_render(render_id="r" * 32, job_id="j1", template="pop")
    repo.delete_job("j1")
    assert repo.get_job("j1") is None and repo.get_cues("j1") == [] and repo.get_render("r" * 32) is None

    add_job(repo, owner, job_id="j2")
    repo.replace_cues("j2", [Cue(0, 900, "b")])
    repo.delete_user(owner)
    assert repo.get_user(owner) is None and repo.get_job("j2") is None


def test_unknown_owner_is_rejected_by_the_database(repo):
    with pytest.raises(Exception, match=r"(?i)foreign key|constraint|cannot add"):
        add_job(repo, "ghost")


def test_the_same_email_cannot_register_twice(repo):
    accounts = Accounts(repo)
    accounts.register("A", "same@example.com", "longenough")
    with pytest.raises(AccountError, match="already exists"):
        accounts.register("B", "SAME@example.com", "longenough")


def test_values_at_the_column_limits_are_accepted(repo):
    owner = Accounts(repo).create_guest()["id"]
    repo.create_job(job_id="j1", owner=owner, original_name="n" * 255, video_path="/v" * 300,
                    duration_sec=1.0, width=1, height=1, language="l" * 32, message="m" * 500)
    job = repo.get_job("j1")
    assert len(job["original_name"]) == 255 and len(job["message"]) == 500 and len(job["language"]) == 32


def test_parallel_updates_do_not_collide(repo):
    owner = Accounts(repo).create_guest()["id"]
    add_job(repo, owner)
    errors: list[Exception] = []

    def work(n):
        try:
            for i in range(25):
                repo.update_job("j1", progress=(n * 25 + i) % 100)
                repo.get_job("j1")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


@needs_ffmpeg
def test_whole_flow_on_this_backend(backend_cfg, sample_video):
    """Upload, transcribe, edit, export and render, all through the service, against the chosen database."""
    import shutil

    service = JobService(backend_cfg)
    try:
        owner = Accounts(service.repo).create_guest()["id"]
        job_id, path = service.new_upload_path("clip.mp4")
        shutil.copy2(sample_video, path)
        service.create_job(owner, path, "clip.mp4", None, job_id)
        job = wait_until(lambda: (j := service.get_job(job_id))["status"] in ("ready", "failed") and j)
        assert job["status"] == "ready" and job["cue_count"] >= 3

        cues = service.save_cues(job_id, [{"orig": 0, "start": 500, "end": 2500, "text": "Edited ✨ line"}])
        assert [c.text for c in cues] == ["Edited ✨ line"]
        assert "Edited ✨ line" in service.export_srt(job_id)

        render = service.start_render(job_id, "pop")
        done = wait_until(lambda: (r := service.get_render(render["id"]))["status"] in ("done", "failed") and r, 120)
        assert done["status"] == "done"
        service.delete_job(job_id)
        assert service.get_job(job_id) is None
    finally:
        service.shutdown()
