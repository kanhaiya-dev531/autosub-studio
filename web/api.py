"""JSON endpoints used by the dashboard and the project workspace."""

from __future__ import annotations

import re
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file, url_for

from autosub import styles
from autosub.languages import LANGUAGE_CODES
from autosub.media import MediaError
from autosub.repository import parse_ts
from autosub.service import JobError

from .security import ensure_user, ext, owned_job

bp = Blueprint("api", __name__, url_prefix="/api")

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".3gp", ".wmv", ".flv"}
MAX_PROJECTS_PER_USER = 50
MAX_ACTIVE_JOBS_PER_USER = 3
MAX_CUES = 5000
MAX_TEXT = 500


# ---- serialisation -----------------------------------------------------------
def job_json(job: dict) -> dict:
    service = ext().service
    return {
        "id": job["id"],
        "title": job["original_name"],
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "language": job["language"],
        "duration": job["duration_sec"],
        "width": job["width"],
        "height": job["height"],
        "cue_count": job["cue_count"],
        "updated": int(parse_ts(job["updated_at"]).timestamp()),
        "has_video": service.video_file(job) is not None,
    }


def cues_json(cues) -> list[dict]:
    return [
        {"orig": i, "start": c.start_ms, "end": c.end_ms, "text": c.text,
         "words": [[w.text, w.start_ms, w.end_ms] for w in c.words]}
        for i, c in enumerate(cues)
    ]


def render_json(render: dict) -> dict:
    return {
        "id": render["id"], "template": render["template"], "status": render["status"],
        "progress": render["progress"], "message": render["message"],
        "url": url_for("pages.render_file", render_id=render["id"]) if render["status"] == "done" else None,
    }


def fail(message: str, status: int = 400):
    return jsonify(error=message), status


@bp.errorhandler(JobError)
def _job_error(exc: JobError):
    return fail(str(exc))


@bp.errorhandler(MediaError)
def _media_error(exc: MediaError):
    return fail(str(exc), 422)


@bp.errorhandler(ValueError)
def _value_error(exc: ValueError):
    return fail(str(exc))


def _template_arg(value) -> str:
    if value not in styles.TEMPLATES:
        raise JobError(f"Unknown style. Choose one of: {', '.join(styles.TEMPLATES)}.")
    return value


# ---- upload -----------------------------------------------------------------
@bp.post("/upload")
def upload():
    file = request.files.get("video")
    if file is None or not file.filename:
        return fail("Choose a video file to upload.")
    name = " ".join(Path(file.filename).name.split())[:255] or "video"
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        return fail("That file type is not supported. Use MP4, MOV, MKV, WebM or AVI.")
    language = request.form.get("language", "auto")
    if language not in LANGUAGE_CODES:
        return fail("Choose a language from the list.")

    user = ensure_user()
    service = ext().service
    jobs = service.list_jobs(user["id"], MAX_PROJECTS_PER_USER)
    if len(jobs) >= MAX_PROJECTS_PER_USER:
        return fail(f"You have {MAX_PROJECTS_PER_USER} projects. Delete one before adding another.", 429)
    if sum(1 for j in jobs if j["status"] in ("uploaded", "transcribing")) >= MAX_ACTIVE_JOBS_PER_USER:
        return fail("Three videos are already being processed. Wait for one to finish.", 429)

    job_id, path = service.new_upload_path(name)
    file.save(path)
    job = service.create_job(user["id"], path, name, language, job_id)
    return jsonify(id=job["id"], url=url_for("pages.project", job_id=job["id"])), 201


# ---- project ----------------------------------------------------------------
@bp.get("/p/<job_id>")
def get_job(job_id: str):
    return jsonify(job_json(owned_job(job_id)))


@bp.delete("/p/<job_id>")
def delete_job(job_id: str):
    owned_job(job_id)
    ext().service.delete_job(job_id)
    return jsonify(ok=True)


@bp.get("/p/<job_id>/cues")
def get_cues(job_id: str):
    owned_job(job_id)
    return jsonify(cues=cues_json(ext().service.get_cues(job_id)))


def _parse_items(raw) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > MAX_CUES:
        raise JobError(f"Send a list of at most {MAX_CUES} subtitle lines.")
    items = []
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise JobError(f"Line {n} is not valid.")
        text = item.get("text", "")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise JobError(f"Line {n}: the text can have at most {MAX_TEXT} characters.")
        orig = item.get("orig")
        items.append({
            "orig": orig if isinstance(orig, int) and not isinstance(orig, bool) else None,
            "start": item.get("start"), "end": item.get("end"), "text": text,
        })
    return items


@bp.put("/p/<job_id>/cues")
def put_cues(job_id: str):
    job = owned_job(job_id)
    if job["status"] != "ready":
        raise JobError("The subtitles are not ready yet.")
    body = request.get_json(silent=True) or {}
    service = ext().service
    cues = service.save_cues(job_id, _parse_items(body.get("cues")))
    return jsonify(cues=cues_json(cues), job=job_json(service.get_job(job_id)))


@bp.post("/p/<job_id>/sync")
def sync(job_id: str):
    job = owned_job(job_id)
    if job["status"] != "ready":
        raise JobError("The subtitles are not ready yet.")
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    params: dict = {}
    try:
        if mode == "offset":
            params["delta_ms"] = int(round(float(body["seconds"]) * 1000))
            if abs(params["delta_ms"]) > 6 * 3600 * 1000:
                raise JobError("That shift is too large.")
        elif mode == "stretch":
            params["factor"] = float(body["factor"])
        elif mode == "anchors":
            params["first"], params["last"] = str(body["first"]), str(body["last"])
        elif mode != "auto":
            raise JobError("Choose how to sync the subtitles.")
    except (KeyError, TypeError, ValueError):
        raise JobError("Enter a valid number.") from None
    service = ext().service
    cues, message = service.apply_sync(job_id, mode, **params)
    return jsonify(cues=cues_json(cues), message=message, job=job_json(service.get_job(job_id)))


@bp.post("/p/<job_id>/import-srt")
def import_srt(job_id: str):
    job = owned_job(job_id)
    if job["status"] != "ready":
        raise JobError("The subtitles are not ready yet.")
    file = request.files.get("srt")
    if file is None or not file.filename:
        raise JobError("Choose an .srt file.")
    raw = file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise JobError("That subtitle file is too large.")
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise JobError("The file could not be read as text.")
    auto = request.form.get("auto") in ("1", "true", "on")
    service = ext().service
    cues, message = service.import_srt(job_id, text, auto_sync=auto)
    return jsonify(cues=cues_json(cues), message=message, job=job_json(service.get_job(job_id)))


# ---- template previews + rendering ------------------------------------------------
@bp.get("/p/<job_id>/preview/<template>.jpg")
def preview(job_id: str, template: str):
    owned_job(job_id)
    _template_arg(template)
    path = ext().service.ensure_preview(job_id, template)
    response = send_file(path, mimetype="image/jpeg", max_age=0)
    response.headers["Cache-Control"] = "private, max-age=600"
    return response


@bp.post("/p/<job_id>/render")
def start_render(job_id: str):
    owned_job(job_id)
    body = request.get_json(silent=True) or {}
    template = _template_arg(body.get("template"))
    render = ext().service.start_render(job_id, template)
    return jsonify(render_json(render)), 202


@bp.get("/p/<job_id>/renders")
def list_renders(job_id: str):
    owned_job(job_id)
    return jsonify(renders=[render_json(r) for r in ext().service.list_renders(job_id)])


@bp.get("/renders/<render_id>")
def get_render(render_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", render_id):
        abort(404)
    render = ext().service.get_render(render_id)
    if not render:
        abort(404)
    owned_job(render["job_id"])
    return jsonify(render_json(render))
