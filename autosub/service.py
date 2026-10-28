"""The application layer. The website and the desktop app both call this class.

It owns the background worker pool, so uploads return immediately while
transcription and rendering run in the background. Progress is written to the
database, and the user interface simply reads it back.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from . import media, preview, styles
from . import subtitles as st
from .config import Config
from .db import Database
from .media import MediaError, VideoInfo
from .repository import TS_FORMAT, Repository, parse_ts
from .subtitles import Cue
from .transcribe import Transcriber, TranscriptionError, build_transcriber, transcript_to_cues

log = logging.getLogger("autosub")

ACTIVE_JOB = ("uploaded", "transcribing")
ACTIVE_RENDER = ("queued", "rendering")
STALE_AFTER_SECONDS = 25  # an "active" row that nothing here is working on is dead


class JobError(RuntimeError):
    """A problem the user can fix. The message is safe to show on screen."""


def _throttled(update: Callable[[int], None]) -> Callable[[float], None]:
    """Turn a 0.0-1.0 progress stream into a few database writes."""
    last = {"pct": -10}

    def report(fraction: float) -> None:
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        if pct - last["pct"] >= 2 or pct >= 100:
            last["pct"] = pct
            update(pct)

    return report


class JobService:
    def __init__(self, cfg: Config, transcriber: Transcriber | None = None):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.db = Database(cfg)
        self.db.init_schema(attempts=10 if cfg.uses_mysql else 1)
        self.repo = Repository(self.db)
        self._transcriber = transcriber
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._preview_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="autosub")

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    @property
    def transcriber(self) -> Transcriber:
        if self._transcriber is None:
            try:
                self._transcriber = build_transcriber(self.cfg)
            except TranscriptionError as exc:
                raise JobError(str(exc)) from exc
        return self._transcriber

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(self, key: str, fn: Callable, *args) -> None:
        with self._lock:
            self._active.add(key)

        def run() -> None:
            try:
                fn(*args)
            finally:
                with self._lock:
                    self._active.discard(key)

        self._executor.submit(run)

    def _is_stale(self, row: dict, key: str) -> bool:
        with self._lock:
            if key in self._active:
                return False
        age = datetime.now(timezone.utc) - parse_ts(row["updated_at"])
        return age > timedelta(seconds=STALE_AFTER_SECONDS)

    def _managed(self, path: Path) -> bool:
        """True if the file lives in our uploads folder (so we may delete it)."""
        try:
            return path.resolve().is_relative_to(self.cfg.uploads_dir.resolve())
        except (OSError, ValueError):
            return False

    def video_file(self, job: dict) -> Path | None:
        """The job's video if it is still on disk (uploads can expire), else None."""
        path = Path(job["video_path"])
        return path if path.is_file() else None

    def _discard(self, path: Path) -> None:
        if self._managed(path):
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    def new_upload_path(self, filename: str) -> tuple[str, Path]:
        """Reserve an id and a path in the uploads folder for a file about to be saved."""
        job_id = uuid.uuid4().hex
        suffix = Path(filename).suffix.lower()[:8] or ".mp4"
        return job_id, self.cfg.uploads_dir / f"{job_id}{suffix}"

    def create_job(
        self, owner: str, video_path: Path | str, original_name: str,
        language: str | None = None, job_id: str | None = None,
    ) -> dict:
        self.purge_expired()
        video_path = Path(video_path)
        try:
            info = media.probe(self.cfg, video_path)
        except MediaError as exc:
            self._discard(video_path)
            raise JobError(str(exc)) from exc

        limit = self.cfg.max_video_seconds
        if limit and info.duration > limit:
            self._discard(video_path)
            raise JobError(
                f"This site accepts videos up to {limit} seconds. Yours is {int(info.duration)} seconds long."
            )

        job_id = job_id or uuid.uuid4().hex
        language = None if language in (None, "", "auto") else language
        silent = not info.has_audio
        self.repo.create_job(
            job_id=job_id, owner=owner, original_name=original_name, video_path=str(video_path),
            duration_sec=info.duration, width=info.width, height=info.height, language=language,
            status="ready" if silent else "uploaded",
            message="This video has no audio. Import an .srt file or add lines by hand." if silent else None,
        )
        if silent:
            self.repo.update_job(job_id, progress=100)
        else:
            self._submit(job_id, self._transcribe_job, job_id, language)
        job = self.get_job(job_id)
        if job is None:  # deleted by someone else in the meantime
            raise JobError("That project no longer exists.")
        return job

    def get_job(self, job_id: str) -> dict | None:
        job = self.repo.get_job(job_id)
        if job and job["status"] in ACTIVE_JOB and self._is_stale(job, job_id):
            self.repo.update_job(
                job_id, status="failed",
                message="This job stopped unexpectedly. Please upload the video again.",
            )
            job = self.repo.get_job(job_id)
        return job

    def list_jobs(self, owner: str, limit: int = 20) -> list[dict]:
        jobs = (self.get_job(j["id"]) for j in self.repo.list_jobs(owner, limit))
        return [job for job in jobs if job is not None]  # a project deleted mid-way is simply left out

    def delete_job(self, job_id: str) -> None:
        job = self.repo.get_job(job_id)
        if not job:
            return
        for render in self.repo.list_renders(job_id):
            if render.get("output_path"):
                Path(render["output_path"]).unlink(missing_ok=True)
        self._discard(Path(job["video_path"]))
        shutil.rmtree(self.cfg.previews_dir / job_id, ignore_errors=True)
        self.repo.delete_job(job_id)

    def purge_expired(self) -> None:
        if self.cfg.job_ttl_hours <= 0:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=self.cfg.job_ttl_hours)).strftime(TS_FORMAT)
        for job_id in self.repo.expired_job_ids(cutoff):
            with self._lock:
                if job_id in self._active:
                    continue
            self.delete_job(job_id)

    # ------------------------------------------------------------------
    # transcription (runs in the background)
    # ------------------------------------------------------------------
    def _transcribe_job(self, job_id: str, language: str | None) -> None:
        try:
            job = self.repo.get_job(job_id)
            if job is None:
                return
            self.repo.update_job(job_id, status="transcribing", progress=2, message="Preparing the audio")
            engine = self.transcriber
            with TemporaryDirectory(dir=self.cfg.work_dir, prefix="audio_") as tmp:
                audio = Path(tmp) / f"audio.{engine.audio_codec}"
                media.extract_audio(self.cfg, job["video_path"], audio, engine.audio_codec)
                if engine.max_audio_bytes and audio.stat().st_size > engine.max_audio_bytes:
                    audio = media.extract_audio(self.cfg, job["video_path"], Path(tmp) / "audio.mp3", "mp3")
                    if audio.stat().st_size > engine.max_audio_bytes:
                        raise JobError("The audio is too long for the online transcription service. Try a shorter clip.")
                self.repo.update_job(job_id, progress=8, message="Listening to the video")
                report = _throttled(lambda pct: self.repo.update_job(job_id, progress=8 + int(pct * 0.9)))
                transcript = engine.transcribe(audio, language, report)

            cues = st.normalize(transcript_to_cues(transcript))
            self.repo.replace_cues(job_id, cues)
            heard = transcript.words or [
                w for start, end, text in transcript.segments for w in st.redistribute_words(st.clean_text(text), start, end)
            ]
            self.repo.set_transcript(job_id, heard)
            self.repo.update_job(
                job_id, status="ready", progress=100, language=(transcript.language or language or "")[:32] or None,
                message=None if cues else "No speech was found. Import an .srt file or add lines by hand.",
            )
        except (JobError, MediaError, TranscriptionError) as exc:
            log.warning("Transcription failed for %s: %s", job_id, exc)
            self.repo.update_job(job_id, status="failed", message=str(exc)[:480])
        except Exception as exc:  # never leave a job stuck in "transcribing"
            log.exception("Unexpected error while transcribing %s", job_id)
            self.repo.update_job(job_id, status="failed", message=f"Unexpected error: {exc}"[:480])

    # ------------------------------------------------------------------
    # editing subtitles
    # ------------------------------------------------------------------
    def get_cues(self, job_id: str) -> list[Cue]:
        return self.repo.get_cues(job_id)

    def _store(self, job_id: str, cues: list[Cue]) -> list[Cue]:
        self.repo.replace_cues(job_id, cues)
        shutil.rmtree(self.cfg.previews_dir / job_id, ignore_errors=True)
        return cues

    def save_cues(self, job_id: str, items: list[dict]) -> list[Cue]:
        """Replace the subtitles with an edited list.

        Each item: {"orig": index of the line it came from or None, "start", "end", "text"}.
        Word timings are kept when only a line's times changed.
        """
        existing = dict(enumerate(self.repo.get_cues(job_id)))
        cues: list[Cue] = []
        for n, item in enumerate(items, start=1):
            text = st.clean_text(str(item.get("text", "")))
            if not text:
                continue
            try:
                start = st.parse_timecode(item["start"])
                end = st.parse_timecode(item["end"])
            except KeyError:
                raise JobError(f"Line {n}: a start and an end time are required.") from None
            except ValueError as exc:
                raise JobError(f"Line {n}: {exc}") from None
            if end <= start:
                raise JobError(f"Line {n}: the end time must be after the start time.")
            orig = item.get("orig")
            old = existing.get(orig) if isinstance(orig, int) else None
            if old and old.words and old.text == text:
                words = st.rescale_words(old.words, old.start_ms, old.end_ms, start, end)
            else:
                words = st.redistribute_words(text, start, end)
            cues.append(Cue(start, end, text, words))
        return self._store(job_id, st.normalize(cues))

    def apply_sync(self, job_id: str, mode: str, **params) -> tuple[list[Cue], str]:
        """Re-time the subtitles. Returns the new cues and a sentence describing what happened.

        mode "offset":  params delta_ms           (move everything earlier or later)
        mode "stretch": params factor             (fix a frame-rate mismatch)
        mode "anchors": params first, last        (first line starts here, last line starts there)
        mode "auto":    no params                 (compare with what was heard in the video)
        """
        cues = self.repo.get_cues(job_id)
        if not cues:
            raise JobError("There are no subtitle lines to sync.")
        message = "Subtitles synced."
        try:
            if mode == "offset":
                delta = int(params["delta_ms"])
                result = st.shift(cues, delta)
                message = f"Moved every line by {delta / 1000:+.2f} s."
            elif mode == "stretch":
                factor = float(params["factor"])
                result = st.stretch(cues, factor)
                message = f"Stretched all timings by x{factor:.4f}."
            elif mode == "anchors":
                result = st.sync_anchors(
                    cues, st.parse_timecode(params["first"]), st.parse_timecode(params["last"])
                )
                message = "Aligned the first and last lines."
            elif mode == "auto":
                heard = self.repo.get_transcript(job_id)
                if not heard:
                    raise JobError("This project has no recorded speech to compare with.")
                result, report = st.auto_sync(cues, heard)
                if not report.ok:
                    raise JobError(report.message)
                message = report.message
            else:
                raise JobError(f"Unknown sync mode '{mode}'.")
        except KeyError as exc:
            raise JobError(f"Missing value: {exc.args[0]}") from None
        except (ValueError, TypeError) as exc:
            raise JobError(str(exc)) from None
        if not result:
            raise JobError("That would move every line before the start of the video.")
        return self._store(job_id, st.normalize(result)), message

    def import_srt(self, job_id: str, text: str, auto_sync: bool = False) -> tuple[list[Cue], str]:
        """Replace the subtitles with the lines of an .srt file, optionally re-timed to the speech."""
        try:
            cues = st.parse_srt(text)
        except ValueError as exc:
            raise JobError(str(exc)) from None
        message = f"Imported {len(cues)} lines."
        if auto_sync:
            heard = self.repo.get_transcript(job_id)
            if not heard:
                raise JobError("This project has no recorded speech to sync with. Import without auto-sync.")
            cues, report = st.auto_sync(cues, heard)
            if not report.ok:
                raise JobError(report.message)
            message = f"Imported {len(cues)} lines. {report.message}"
        return self._store(job_id, st.normalize(cues)), message

    # ---- exports -----------------------------------------------------
    def export_srt(self, job_id: str) -> str:
        return st.format_srt(self.repo.get_cues(job_id))

    def export_vtt(self, job_id: str) -> str:
        return st.format_vtt(self.repo.get_cues(job_id))

    def export_txt(self, job_id: str) -> str:
        return st.format_txt(self.repo.get_cues(job_id))

    def export_ass(self, job_id: str, template: str) -> str:
        """A styled .ass file for players such as VLC (fonts must be installed on that computer)."""
        styles.get_template(template)
        job = self.repo.get_job(job_id)
        if job is None:
            raise JobError("That project no longer exists.")
        return styles.build_ass(
            self.repo.get_cues(job_id), template, int(job["width"]), int(job["height"]), self.cfg.font_name
        )

    # ------------------------------------------------------------------
    # template previews
    # ------------------------------------------------------------------
    def preview_path(self, job_id: str, template: str) -> Path:
        return self.cfg.previews_dir / job_id / f"{template}.jpg"

    def _video_info(self, job: dict) -> VideoInfo:
        return VideoInfo(float(job["duration_sec"] or 0), int(job["width"]), int(job["height"]), True)

    def ensure_preview(self, job_id: str, template: str) -> Path:
        """Return a JPEG showing the template on a real frame of the video (cached)."""
        styles.get_template(template)
        path = self.preview_path(job_id, template)
        with self._preview_lock:
            if path.exists():
                return path
            job = self.repo.get_job(job_id)
            if job is None:
                raise JobError("That project no longer exists.")
            if self.video_file(job) is None:
                raise JobError("The video file is no longer stored. Upload it again to preview styles.")
            preview.draw_preview(
                self.cfg, job["video_path"], self._video_info(job), self.repo.get_cues(job_id), template, path
            )
        return path

    # ------------------------------------------------------------------
    # burning subtitles into the video
    # ------------------------------------------------------------------
    def start_render(self, job_id: str, template: str) -> dict:
        styles.get_template(template)
        job = self.get_job(job_id)
        if job is None:
            raise JobError("That project no longer exists.")
        if job["status"] != "ready":
            raise JobError("The subtitles are not ready yet.")
        if self.video_file(job) is None:
            raise JobError("The video file is no longer stored. Upload it again to render a video.")
        if not self.repo.get_cues(job_id):
            raise JobError("There are no subtitle lines to burn in. Add some or import an .srt file first.")
        render_id = uuid.uuid4().hex
        self.repo.create_render(render_id=render_id, job_id=job_id, template=template)
        self._submit(render_id, self._render_job, render_id)
        render = self.repo.get_render(render_id)
        if render is None:
            raise JobError("That project no longer exists.")
        return render

    def get_render(self, render_id: str) -> dict | None:
        render = self.repo.get_render(render_id)
        if render and render["status"] in ACTIVE_RENDER and self._is_stale(render, render_id):
            self.repo.update_render(
                render_id, status="failed", message="This render stopped unexpectedly. Please start it again."
            )
            render = self.repo.get_render(render_id)
        return render

    def list_renders(self, job_id: str) -> list[dict]:
        renders = (self.get_render(r["id"]) for r in self.repo.list_renders(job_id))
        return [render for render in renders if render is not None]

    def _render_job(self, render_id: str) -> None:
        try:
            render = self.repo.get_render(render_id)
            job = self.repo.get_job(render["job_id"]) if render else None
            if render is None or job is None:
                return  # the project was deleted while this render was waiting in line
            cues = self.repo.get_cues(job["id"])
            self.repo.update_render(render_id, status="rendering", progress=1)
            info = self._video_info(job)
            size = media.output_size(info, self.cfg.render_max_height)
            ass = styles.build_ass(cues, render["template"], size[0], size[1], self.cfg.font_name)
            out = self.cfg.renders_dir / f"{render_id}.mp4"
            report = _throttled(lambda pct: self.repo.update_render(render_id, progress=pct))
            media.render_video(self.cfg, job["video_path"], info, ass, size, out, report)
            self.repo.update_render(render_id, status="done", progress=100, output_path=str(out))
        except (MediaError, JobError) as exc:
            log.warning("Render %s failed: %s", render_id, exc)
            self.repo.update_render(render_id, status="failed", message=str(exc)[:480])
        except Exception as exc:
            log.exception("Unexpected error while rendering %s", render_id)
            self.repo.update_render(render_id, status="failed", message=f"Unexpected error: {exc}"[:480])
