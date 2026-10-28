"""HTML pages, media streaming and file downloads."""

from __future__ import annotations

import io
import mimetypes
import re
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, send_file, send_from_directory

from autosub import styles

from .security import current_user, ext, owned_job

bp = Blueprint("pages", __name__)

DOWNLOADS = {
    "srt": ("application/x-subrip", "export_srt"),
    "vtt": ("text/vtt", "export_vtt"),
    "txt": ("text/plain", "export_txt"),
}


def present(job: dict) -> dict:
    """A job row plus the few extra values the HTML templates need."""
    job = dict(job)
    job["title"] = job["original_name"]
    job["active"] = job["status"] in ("uploaded", "transcribing")
    return job


def _stem(job: dict) -> str:
    stem = re.sub(r"[^\w.\- ]+", "", Path(job["original_name"]).stem, flags=re.UNICODE).strip(" .")
    return stem[:80] or "subtitles"


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/healthz")
def healthz():
    ext().service.db.ping()
    return jsonify(ok=True)


@bp.get("/dashboard")
def dashboard():
    user = current_user()
    service = ext().service
    jobs = [present(j) for j in service.list_jobs(user["id"], 50)] if user else []
    stats = service.repo.owner_stats(user["id"]) if user else {"projects": 0, "seconds": 0.0}
    return render_template("dashboard.html", jobs=jobs, stats=stats)


@bp.get("/p/<job_id>")
def project(job_id: str):
    job = owned_job(job_id)
    return render_template("project.html", job=present(job), default_template="pop")


@bp.get("/p/<job_id>/video")
def video(job_id: str):
    job = owned_job(job_id)
    path = ext().service.video_file(job)
    if path is None:
        abort(404)
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return send_file(path, mimetype=mime, conditional=True, max_age=3600)


@bp.get("/p/<job_id>/download.<kind>")
def download(job_id: str, kind: str):
    job = owned_job(job_id)
    service = ext().service
    if kind == "ass":
        template = request.args.get("template", "pop")
        if template not in styles.TEMPLATES:
            abort(400)
        text, mime = service.export_ass(job_id, template), "text/x-ssa"
        name = f"{_stem(job)}-{template}.ass"
    elif kind in DOWNLOADS:
        mime, method = DOWNLOADS[kind]
        text = getattr(service, method)(job_id)
        name = f"{_stem(job)}.{kind}"
    else:
        abort(404)
    return send_file(io.BytesIO(text.encode("utf-8")), mimetype=mime, as_attachment=True, download_name=name)


@bp.get("/renders/<render_id>/file")
def render_file(render_id: str):
    service = ext().service
    render = service.get_render(render_id) if re.fullmatch(r"[0-9a-f]{32}", render_id) else None
    if not render:
        abort(404)
    job = owned_job(render["job_id"])
    path = Path(render["output_path"]) if render.get("output_path") else None
    if render["status"] != "done" or path is None or not path.is_file():
        abort(404)
    inline = request.args.get("inline") == "1"
    return send_file(
        path, mimetype="video/mp4", conditional=True, as_attachment=not inline,
        download_name=f"{_stem(job)}-{render['template']}.mp4", max_age=3600,
    )


@bp.get("/fonts/<path:name>")
def fonts(name: str):
    """The Poppins font files, shared with the subtitle renderer."""
    if not name.lower().endswith(".ttf"):
        abort(404)
    response = send_from_directory(ext().cfg.fonts_dir, name, max_age=86400 * 30)
    return response
