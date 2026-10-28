"""The AutoSub Studio website (Flask)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

from flask import Flask

from autosub.accounts import Accounts
from autosub.config import Config
from autosub.service import JobService

log = logging.getLogger("autosub")


def create_app(cfg: Config | None = None, service: JobService | None = None) -> Flask:
    """Build the Flask app. Tests pass their own `cfg` / `service`."""
    cfg = cfg or Config.from_env()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        MAX_CONTENT_LENGTH=(cfg.max_upload_mb + 2) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
        PERMANENT_SESSION_LIFETIME=timedelta(days=60),
        JSON_AS_ASCII=False,
    )
    service = service or JobService(cfg)
    from .security import RateLimiter

    app.extensions["autosub"] = SimpleNamespace(
        cfg=cfg, service=service, accounts=Accounts(service.repo), limiter=RateLimiter(max_hits=8, window_seconds=300)
    )

    from . import api, auth, pages, security

    security.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(api.bp)

    if cfg.job_ttl_hours > 0:
        _start_cleanup_thread(service)
    return app


def _start_cleanup_thread(service: JobService) -> None:
    """Delete projects older than JOB_TTL_HOURS once an hour (privacy + disk space on free hosts)."""

    def loop() -> None:
        while True:
            try:
                service.purge_expired()
            except Exception:  # never let the janitor crash the server
                log.exception("Cleanup failed")
            time.sleep(3600)

    threading.Thread(target=loop, name="autosub-cleanup", daemon=True).start()
