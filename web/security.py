"""Cross-cutting web security: CSRF tokens, response headers, login throttling, who is signed in."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import defaultdict, deque

from flask import Flask, abort, current_app, g, jsonify, render_template, request, session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
JOB_ID = re.compile(r"^[0-9a-f]{32}$")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)


def ext():
    """The shared objects created in create_app (config, job service, accounts, limiter)."""
    return current_app.extensions["autosub"]


# ---- CSRF ----------------------------------------------------------------
def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def wants_json() -> bool:
    return request.path.startswith("/api/")


def error_response(status: int, message: str):
    if wants_json():
        return jsonify(error=message), status
    return render_template("error.html", status=status, message=message), status


# ---- login throttling ------------------------------------------------------
class RateLimiter:
    """Allows `max_hits` failures per key inside a sliding window (in memory, per process)."""

    def __init__(self, max_hits: int, window_seconds: int):
        self.max_hits, self.window = max_hits, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, time.monotonic())) >= self.max_hits

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# ---- who is signed in --------------------------------------------------------
def current_user() -> dict | None:
    if "user" in g:
        return g.user
    uid = session.get("uid")
    user = ext().accounts.get(uid) if uid else None
    if uid and user is None:  # the account was deleted (or the database was reset)
        session.pop("uid", None)
    g.user = user
    return user


def ensure_user() -> dict:
    """The signed-in user, or a brand-new guest. Called when somebody first uploads."""
    user = current_user()
    if user:
        return user
    user = ext().accounts.create_guest()
    session["uid"] = user["id"]
    session.permanent = True
    g.user = user
    return user


def owned_job(job_id: str) -> dict:
    """The job if it belongs to the current user. Anything else is a 404 (never reveal other people's ids)."""
    user = current_user()
    job = ext().service.get_job(job_id) if JOB_ID.match(job_id or "") else None
    if not user or not job or job["owner"] != user["id"]:
        abort(404)
    return job


# ---- wiring ----------------------------------------------------------------------
def init_app(app: Flask) -> None:
    from autosub import styles
    from autosub.languages import LANGUAGES, language_label

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _check_csrf():
        if request.method in SAFE_METHODS:
            return None
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not expected or not sent or not _same(sent, expected):
            return error_response(400, "Your session expired. Reload the page and try again.")
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _inject():
        cfg = ext().cfg
        return {
            "current_user": current_user(),
            "templates": list(styles.TEMPLATES.values()),
            "languages": LANGUAGES,
            "max_upload_mb": cfg.max_upload_mb,
            "max_video_seconds": cfg.max_video_seconds,
            "transcriber": cfg.transcriber,
            "job_ttl_hours": cfg.job_ttl_hours,
        }

    app.jinja_env.filters["language_label"] = language_label
    app.jinja_env.filters["duration"] = _duration
    app.jinja_env.filters["spoken_length"] = _spoken_length
    app.jinja_env.filters["nice_date"] = _nice_date

    @app.errorhandler(404)
    def _404(_e):
        return error_response(404, "We could not find that page.")

    @app.errorhandler(405)
    def _405(_e):
        return error_response(405, "That action is not allowed here.")

    @app.errorhandler(413)
    def _413(_e):
        return error_response(413, f"That file is too large. The limit is {ext().cfg.max_upload_mb} MB.")

    @app.errorhandler(500)
    def _500(_e):
        return error_response(500, "Something went wrong on our side. Please try again.")


def _duration(seconds) -> str:
    seconds = int(round(float(seconds or 0)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _spoken_length(seconds) -> str:
    """'45 seconds', '1 minute', '12.5 minutes', '2.3 hours': a length a person would say out loud."""
    seconds = float(seconds or 0)
    if seconds < 90:
        n = round(seconds)
        return f"{n} second{'' if n == 1 else 's'}"
    if seconds < 5400:
        minutes = round(seconds / 60, 1)
        return f"{minutes:g} minute{'' if minutes == 1 else 's'}"
    return f"{round(seconds / 3600, 1):g} hours"


def _nice_date(value) -> str:
    from datetime import datetime

    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return str(value or "")
