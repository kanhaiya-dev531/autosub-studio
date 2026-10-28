"""The website, driven through Flask's test client (mock transcriber, SQLite, real FFmpeg)."""

from __future__ import annotations

import io
import re
import threading

import pytest
from conftest import make_config, needs_ffmpeg, wait_until

from autosub.service import JobService
from autosub.transcribe import MockTranscriber
from web import api as web_api
from web import create_app

META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
SRT = "1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond line\n"


class Browser:
    """A cookie jar plus the CSRF token, the way a real page would send them."""

    def __init__(self, app):
        self.http = app.test_client()

    @property
    def token(self) -> str:
        """The token of the current session. Signing out clears the session, so ask the server again."""
        with self.http.session_transaction() as session:
            token = session.get("csrf")
        if not token:
            token = META.search(self.http.get("/").get_data(as_text=True)).group(1)
        return token

    def start_session(self) -> None:
        """Visit the site once so the browser holds a session cookie."""
        self.http.get("/")

    def json(self, method, url, body=None, **kw):
        return self.http.open(url, method=method, json=body, headers={"X-CSRF-Token": self.token}, **kw)

    def upload(self, video_path, name="clip.mp4", language="auto", extra=None):
        data = {"video": (io.BytesIO(video_path.read_bytes()) if hasattr(video_path, "read_bytes") else video_path, name),
                "language": language}
        data.update(extra or {})
        return self.http.post("/api/upload", data=data, headers={"X-CSRF-Token": self.token},
                              content_type="multipart/form-data")

    def form(self, url, **fields):
        return self.http.post(url, data={"csrf_token": self.token, **fields})


@pytest.fixture
def app(cfg):
    service = JobService(cfg)
    flask_app = create_app(cfg, service)
    yield flask_app
    service.shutdown()


@pytest.fixture
def me(app):
    return Browser(app)


@pytest.fixture
def other(app):
    return Browser(app)


def finished(browser, job_id):
    return wait_until(lambda: (j := browser.http.get(f"/api/p/{job_id}").get_json())["status"] in ("ready", "failed") and j)


@pytest.fixture
def project(me, sample_video):
    """A ready project owned by `me` (a guest)."""
    resp = me.upload(sample_video)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    job_id = resp.get_json()["id"]
    assert finished(me, job_id)["status"] == "ready"
    return job_id


# ---- public pages ---------------------------------------------------------------
def test_home_and_health(me):
    home = me.http.get("/")
    assert home.status_code == 200
    html = home.get_data(as_text=True)
    assert "AutoSub" in html and "style-pop.jpg" in html
    assert me.http.get("/healthz").get_json() == {"ok": True}
    assert me.http.get("/static/img/style-neon.jpg").status_code == 200
    assert me.http.get("/nothing-here").status_code == 404


def test_home_counts_languages_from_the_list(me):
    from autosub.languages import LANGUAGES

    html = me.http.get("/").get_data(as_text=True)
    assert f"and {len(LANGUAGES) - 3} other languages" in html


def test_footer_tells_the_truth_about_where_audio_goes(tmp_path):
    def footer(**overrides):
        cfg = make_config(tmp_path, **overrides)
        service = JobService(cfg)
        try:
            html = Browser(create_app(cfg, service)).http.get("/").get_data(as_text=True)
        finally:
            service.shutdown()
        return html[html.index("site-footer"):]

    local = footer()
    assert "Groq" not in local and "deleted automatically" not in local
    hosted = footer(transcriber="groq", groq_api_key="x", job_ttl_hours=24, data_dir=tmp_path / "other")
    assert "audio is sent to Groq" in hosted and "deleted automatically after 24 hours" in hosted


def test_dashboard_length_wording():
    from web.security import _spoken_length

    assert [_spoken_length(s) for s in (1, 14, 90, 754, 9000)] == [
        "1 second", "14 seconds", "1.5 minutes", "12.6 minutes", "2.5 hours"
    ]


def test_security_headers(me):
    resp = me.http.get("/")
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert me.http.get("/api/p/" + "0" * 32).headers["Cache-Control"] == "no-store"


def test_pages_use_no_inline_scripts(me):
    for url in ("/", "/login", "/register", "/dashboard"):
        html = me.http.get(url).get_data(as_text=True)
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), url
        assert " onclick=" not in html and " onload=" not in html


def test_fonts_endpoint_serves_only_ttf(me):
    assert me.http.get("/fonts/Poppins-Bold.ttf").status_code == 200
    assert me.http.get("/fonts/Poppins-Bold.txt").status_code == 404
    assert me.http.get("/fonts/../config.py").status_code == 404


# ---- CSRF -----------------------------------------------------------------------
def test_post_without_csrf_token_is_rejected(me):
    assert me.http.post("/login", data={"email": "a@b.co", "password": "x"}).status_code == 400
    resp = me.http.post("/api/upload", data={})
    assert resp.status_code == 400 and "expired" in resp.get_json()["error"]
    assert me.http.post("/logout").status_code == 400


def test_wrong_csrf_token_is_rejected(me):
    me.start_session()
    resp = me.http.post("/api/upload", data={}, headers={"X-CSRF-Token": "forged"})
    assert resp.status_code == 400


# ---- accounts ---------------------------------------------------------------------
def test_register_login_logout(me, other):
    resp = me.form("/register", name="Asha Rao", email="Asha@Example.com", password="correct horse")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")
    assert "Sign out Asha" in me.http.get("/dashboard").get_data(as_text=True)

    assert me.form("/logout").status_code == 302
    assert "Sign out" not in me.http.get("/dashboard").get_data(as_text=True)

    bad = other.form("/login", email="asha@example.com", password="wrong password")
    assert bad.status_code == 401 and "not correct" in bad.get_data(as_text=True)
    ok = other.form("/login", email="asha@example.com", password="correct horse")
    assert ok.status_code == 302


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "", "email": "a@b.co", "password": "longenough"}, "name"),
        ({"name": "A", "email": "not-an-email", "password": "longenough"}, "valid e-mail"),
        ({"name": "A", "email": "a@b.co", "password": "short"}, "at least 8"),
    ],
)
def test_register_validation(me, fields, message):
    resp = me.form("/register", **fields)
    assert resp.status_code == 400 and message in resp.get_data(as_text=True)


def test_duplicate_email_is_refused(me, other):
    me.form("/register", name="A", email="dup@example.com", password="longenough")
    resp = other.form("/register", name="B", email="DUP@example.com", password="longenough")
    assert resp.status_code == 400 and "already exists" in resp.get_data(as_text=True)


@pytest.mark.parametrize("target", ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)"])
def test_login_redirect_stays_on_site(me, target):
    me.form("/register", name="A", email="r@example.com", password="longenough")
    me.form("/logout")
    resp = me.form("/login", email="r@example.com", password="longenough", next=target)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")


def test_login_redirect_follows_local_path(me):
    me.form("/register", name="A", email="n@example.com", password="longenough")
    me.form("/logout")
    resp = me.form("/login", email="n@example.com", password="longenough", next="/p/abc")
    assert resp.headers["Location"].endswith("/p/abc")


def test_repeated_failed_logins_are_throttled(me):
    me.form("/register", name="A", email="t@example.com", password="longenough")
    me.form("/logout")
    codes = [me.form("/login", email="t@example.com", password=f"wrong-{i}").status_code for i in range(10)]
    assert codes[:8] == [401] * 8 and codes[8:] == [429, 429]
    # even the right password waits out the window
    assert me.form("/login", email="t@example.com", password="longenough").status_code == 429


# ---- upload validation ------------------------------------------------------------------
def test_upload_validation(me, sample_video):
    assert me.http.post("/api/upload", data={}, headers={"X-CSRF-Token": me.token}).status_code == 400
    bad_type = me.upload(sample_video, name="notes.txt")
    assert bad_type.status_code == 400 and "not supported" in bad_type.get_json()["error"]
    bad_lang = me.upload(sample_video, language="klingon")
    assert bad_lang.status_code == 400 and "language" in bad_lang.get_json()["error"]


@needs_ffmpeg
def test_upload_of_a_non_video_is_refused_and_leaves_no_project(me):
    resp = me.upload(io.BytesIO(b"this is not a video"), name="fake.mp4")
    assert resp.status_code == 400 and "could not be read" in resp.get_json()["error"]
    assert "plist" not in me.http.get("/dashboard").get_data(as_text=True)


def test_oversized_upload_gets_413(tmp_path, sample_video):
    cfg = make_config(tmp_path, max_upload_mb=1)
    service = JobService(cfg)
    try:
        browser = Browser(create_app(cfg, service))
        resp = browser.upload(io.BytesIO(b"\0" * (4 * 1024 * 1024)), name="big.mp4")
        assert resp.status_code == 413 and "1 MB" in resp.get_json()["error"]
    finally:
        service.shutdown()


def test_project_limit(me, sample_video, monkeypatch):
    monkeypatch.setattr(web_api, "MAX_PROJECTS_PER_USER", 1)
    first = me.upload(sample_video)
    assert first.status_code == 201
    finished(me, first.get_json()["id"])
    second = me.upload(sample_video)
    assert second.status_code == 429 and "Delete one" in second.get_json()["error"]


def test_only_three_videos_are_processed_at_once(tmp_path, sample_video):
    gate = threading.Event()

    class Slow(MockTranscriber):
        def transcribe(self, audio_path, language, progress=None):
            gate.wait(30)
            return super().transcribe(audio_path, language, progress)

    cfg = make_config(tmp_path)
    service = JobService(cfg, transcriber=Slow(cfg))
    try:
        browser = Browser(create_app(cfg, service))
        codes = [browser.upload(sample_video).status_code for _ in range(4)]
        assert codes == [201, 201, 201, 429]
    finally:
        gate.set()
        service.shutdown()


# ---- the project workspace ----------------------------------------------------------------
@needs_ffmpeg
def test_upload_to_ready_project(me, project):
    job = me.http.get(f"/api/p/{project}").get_json()
    assert job["status"] == "ready" and job["cue_count"] >= 3 and job["has_video"] is True
    assert (job["width"], job["height"]) == (640, 360)

    page = me.http.get(f"/p/{project}").get_data(as_text=True)
    assert "clip.mp4" in page and "workspace.js" in page
    assert "clip.mp4" in me.http.get("/dashboard").get_data(as_text=True)

    cues = me.http.get(f"/api/p/{project}/cues").get_json()["cues"]
    assert len(cues) == job["cue_count"] and cues[0]["words"]


@needs_ffmpeg
def test_video_streaming_supports_ranges(me, project):
    full = me.http.get(f"/p/{project}/video")
    assert full.status_code == 200 and full.headers["Accept-Ranges"] == "bytes"
    part = me.http.get(f"/p/{project}/video", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.data) == 100


@needs_ffmpeg
def test_editing_cues_round_trip(me, project):
    cues = me.http.get(f"/api/p/{project}/cues").get_json()["cues"]
    edited = [{"orig": c["orig"], "start": c["start"], "end": c["end"], "text": c["text"]} for c in cues]
    edited[0]["text"] = "Edited first line"
    saved = me.json("PUT", f"/api/p/{project}/cues", {"cues": edited}).get_json()
    assert saved["cues"][0]["text"] == "Edited first line"
    assert saved["job"]["cue_count"] == len(edited)

    bad = me.json("PUT", f"/api/p/{project}/cues", {"cues": [{"start": 2000, "end": 1000, "text": "x"}]})
    assert bad.status_code == 400 and "Line 1" in bad.get_json()["error"]
    assert me.json("PUT", f"/api/p/{project}/cues", {"cues": "nope"}).status_code == 400
    too_long = [{"start": 0, "end": 1000, "text": "x" * 501}]
    assert me.json("PUT", f"/api/p/{project}/cues", {"cues": too_long}).status_code == 400


@needs_ffmpeg
def test_sync_endpoint(me, project):
    before = me.http.get(f"/api/p/{project}/cues").get_json()["cues"]
    shifted = me.json("POST", f"/api/p/{project}/sync", {"mode": "offset", "seconds": 0.5}).get_json()
    assert shifted["cues"][0]["start"] == before[0]["start"] + 500 and "0.5" in shifted["message"]
    assert me.json("POST", f"/api/p/{project}/sync", {"mode": "stretch", "factor": 1.01}).status_code == 200
    assert me.json("POST", f"/api/p/{project}/sync", {"mode": "offset", "seconds": "abc"}).status_code == 400
    assert me.json("POST", f"/api/p/{project}/sync", {"mode": "offset", "seconds": 999999}).status_code == 400
    assert me.json("POST", f"/api/p/{project}/sync", {"mode": "warp"}).status_code == 400
    assert me.json("POST", f"/api/p/{project}/sync", {"mode": "auto"}).status_code in (200, 400)


@needs_ffmpeg
def test_import_srt(me, project):
    resp = me.http.post(
        f"/api/p/{project}/import-srt", headers={"X-CSRF-Token": me.token}, content_type="multipart/form-data",
        data={"srt": (io.BytesIO(SRT.encode("utf-8")), "subs.srt")},
    )
    body = resp.get_json()
    assert resp.status_code == 200 and [c["text"] for c in body["cues"]] == ["Hello there", "Second line"]

    empty = me.http.post(f"/api/p/{project}/import-srt", headers={"X-CSRF-Token": me.token},
                         data={"srt": (io.BytesIO(b"no cues here"), "x.srt")}, content_type="multipart/form-data")
    assert empty.status_code == 400
    missing = me.http.post(f"/api/p/{project}/import-srt", headers={"X-CSRF-Token": me.token}, data={},
                           content_type="multipart/form-data")
    assert missing.status_code == 400


@needs_ffmpeg
def test_downloads(me, project):
    srt = me.http.get(f"/p/{project}/download.srt")
    assert srt.status_code == 200 and "attachment" in srt.headers["Content-Disposition"]
    assert "clip.srt" in srt.headers["Content-Disposition"] and "-->" in srt.get_data(as_text=True)
    assert me.http.get(f"/p/{project}/download.vtt").get_data(as_text=True).startswith("WEBVTT")
    assert me.http.get(f"/p/{project}/download.txt").get_data(as_text=True).strip()
    ass = me.http.get(f"/p/{project}/download.ass?template=neon")
    assert "[Script Info]" in ass.get_data(as_text=True) and "clip-neon.ass" in ass.headers["Content-Disposition"]
    assert me.http.get(f"/p/{project}/download.ass?template=nope").status_code == 400
    assert me.http.get(f"/p/{project}/download.exe").status_code == 404


@needs_ffmpeg
def test_template_previews(me, project):
    for key in ("classic", "pop", "neon"):
        resp = me.http.get(f"/api/p/{project}/preview/{key}.jpg")
        assert resp.status_code == 200 and resp.mimetype == "image/jpeg" and resp.data[:2] == b"\xff\xd8"
    assert me.http.get(f"/api/p/{project}/preview/sparkle.jpg").status_code == 400


@needs_ffmpeg
def test_render_a_styled_video(me, project):
    assert me.json("POST", f"/api/p/{project}/render", {"template": "sparkle"}).status_code == 400
    started = me.json("POST", f"/api/p/{project}/render", {"template": "pop"})
    assert started.status_code == 202
    render_id = started.get_json()["id"]
    done = wait_until(
        lambda: (r := me.http.get(f"/api/renders/{render_id}").get_json())["status"] in ("done", "failed") and r,
        timeout=120,
    )
    assert done["status"] == "done" and done["url"].endswith("/file")
    assert [r["id"] for r in me.http.get(f"/api/p/{project}/renders").get_json()["renders"]] == [render_id]

    download = me.http.get(done["url"])
    assert download.status_code == 200 and "attachment" in download.headers["Content-Disposition"]
    assert "clip-pop.mp4" in download.headers["Content-Disposition"]
    assert download.data[4:8] == b"ftyp"  # an MP4 container
    assert "attachment" not in me.http.get(done["url"] + "?inline=1").headers.get("Content-Disposition", "")


# ---- who may see what ------------------------------------------------------------------------
@needs_ffmpeg
def test_other_visitors_cannot_reach_a_project(me, other, project):
    me_render = me.json("POST", f"/api/p/{project}/render", {"template": "classic"}).get_json()["id"]
    wait_until(lambda: me.http.get(f"/api/renders/{me_render}").get_json()["status"] in ("done", "failed"), timeout=120)
    other.start_session()  # a session of their own, even a guest session

    urls = [
        ("GET", f"/p/{project}"), ("GET", f"/p/{project}/video"), ("GET", f"/p/{project}/download.srt"),
        ("GET", f"/api/p/{project}"), ("GET", f"/api/p/{project}/cues"), ("GET", f"/api/p/{project}/renders"),
        ("GET", f"/api/p/{project}/preview/pop.jpg"), ("GET", f"/api/renders/{me_render}"),
        ("GET", f"/renders/{me_render}/file"),
    ]
    for method, url in urls:
        assert other.http.open(url, method=method).status_code == 404, url
    for url, body in (
        (f"/api/p/{project}/render", {"template": "pop"}), (f"/api/p/{project}/sync", {"mode": "auto"}),
        (f"/api/p/{project}/cues", {"cues": []}),
    ):
        assert other.json("POST", url, body).status_code in (404, 405), url
    assert other.json("PUT", f"/api/p/{project}/cues", {"cues": []}).status_code == 404
    assert other.json("DELETE", f"/api/p/{project}").status_code == 404
    assert me.http.get(f"/api/p/{project}").status_code == 200  # untouched


def test_unknown_and_malformed_ids_are_404(me):
    me.start_session()
    for job_id in ("0" * 32, "../../etc/passwd", "x" * 10):
        assert me.http.get(f"/api/p/{job_id}").status_code == 404
    assert me.http.get("/api/renders/not-hex").status_code == 404


@needs_ffmpeg
def test_deleting_a_project_removes_it_and_its_files(me, project, app):
    service = app.extensions["autosub"].service
    path = service.video_file(service.get_job(project))
    assert path and path.exists()
    assert me.json("DELETE", f"/api/p/{project}").get_json() == {"ok": True}
    assert me.http.get(f"/api/p/{project}").status_code == 404
    assert not path.exists()


# ---- guests keep their work when they sign up or in ----------------------------------------------
@needs_ffmpeg
def test_guest_projects_follow_registration(me, project):
    resp = me.form("/register", name="Guest Turned Member", email="member@example.com", password="longenough")
    assert resp.status_code == 302
    assert me.http.get(f"/api/p/{project}").status_code == 200
    assert "clip.mp4" in me.http.get("/dashboard").get_data(as_text=True)


@needs_ffmpeg
def test_guest_projects_move_into_an_existing_account_on_login(me, other, sample_video):
    other.form("/register", name="Member", email="old@example.com", password="longenough")
    other.form("/logout")

    job_id = me.upload(sample_video).get_json()["id"]  # `me` is a fresh guest
    finished(me, job_id)
    assert me.form("/login", email="old@example.com", password="longenough").status_code == 302
    assert me.http.get(f"/api/p/{job_id}").status_code == 200
    assert "clip.mp4" in me.http.get("/dashboard").get_data(as_text=True)
