"""All SQL lives here. Written with `?` placeholders; see db.py for how MySQL is handled."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import Database
from .subtitles import Cue, Word

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

JOB_FIELDS = {
    "original_name", "video_path", "duration_sec", "width", "height",
    "language", "status", "progress", "message", "transcript_json",
}
RENDER_FIELDS = {"status", "progress", "output_path", "message"}

# Everything except transcript_json, which can be large and is only needed for auto-sync.
_JOB_SELECT = (
    "SELECT j.id, j.owner, j.original_name, j.video_path, j.duration_sec, j.width, j.height, "
    "j.language, j.status, j.progress, j.message, j.created_at, j.updated_at, "
    "(SELECT COUNT(*) FROM cues c WHERE c.job_id = j.id) AS cue_count "
    "FROM jobs j"
)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def _ts(value):
    if isinstance(value, datetime):
        return value.strftime(TS_FORMAT)
    return None if value is None else str(value)


def _clean_all(rows: list[dict]) -> list[dict]:
    return [clean for row in rows if (clean := _clean(row)) is not None]


def _clean(row: dict | None) -> dict | None:
    if row is None:
        return None
    for key in ("created_at", "updated_at"):
        if key in row:
            row[key] = _ts(row[key])
    return row


def _encode_words(words: list[Word]) -> str:
    return json.dumps([[w.text, w.start_ms, w.end_ms] for w in words], ensure_ascii=False, separators=(",", ":"))


def _decode_words(raw: str | None) -> list[Word]:
    if not raw:
        return []
    return [Word(t, int(s), int(e)) for t, s, e in json.loads(raw)]


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ---- users -------------------------------------------------------
    def create_user(
        self, *, user_id: str, display_name: str, email: str | None = None,
        password_hash: str | None = None, is_guest: bool = True,
    ) -> None:
        self.db.execute(
            "INSERT INTO users (id, email, display_name, password_hash, is_guest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, email, display_name[:80], password_hash, 1 if is_guest else 0, now_str()),
        )

    def get_user(self, user_id: str) -> dict | None:
        return _clean(self.db.fetchone(
            "SELECT id, email, display_name, password_hash, is_guest, created_at FROM users WHERE id = ?",
            (user_id,)))

    def get_user_by_email(self, email: str) -> dict | None:
        return _clean(self.db.fetchone(
            "SELECT id, email, display_name, password_hash, is_guest, created_at FROM users WHERE email = ?",
            (email,)))

    def promote_guest(self, user_id: str, *, email: str, display_name: str, password_hash: str) -> None:
        """Turn a guest into a registered user, keeping their projects."""
        self.db.execute(
            "UPDATE users SET email = ?, display_name = ?, password_hash = ?, is_guest = 0 WHERE id = ?",
            (email, display_name[:80], password_hash, user_id),
        )

    def adopt_jobs(self, from_owner: str, to_owner: str) -> int:
        """Move every project of one user to another (used when a guest signs in)."""
        if from_owner == to_owner:
            return 0
        return self.db.execute("UPDATE jobs SET owner = ? WHERE owner = ?", (to_owner, from_owner))

    def delete_user(self, user_id: str) -> None:
        with self.db.session() as s:
            s.execute("DELETE FROM renders WHERE job_id IN (SELECT id FROM jobs WHERE owner = ?)", (user_id,))
            s.execute("DELETE FROM cues WHERE job_id IN (SELECT id FROM jobs WHERE owner = ?)", (user_id,))
            s.execute("DELETE FROM jobs WHERE owner = ?", (user_id,))
            s.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def owner_stats(self, owner: str) -> dict:
        row = self.db.fetchone(
            "SELECT COUNT(*) AS projects, COALESCE(SUM(duration_sec), 0) AS seconds "
            "FROM jobs WHERE owner = ? AND status = 'ready'", (owner,))
        return {"projects": int(row["projects"]), "seconds": float(row["seconds"])} if row else {"projects": 0, "seconds": 0.0}

    # ---- jobs --------------------------------------------------------
    def create_job(
        self, *, job_id: str, owner: str, original_name: str, video_path: str,
        duration_sec: float | None, width: int | None, height: int | None,
        language: str | None, status: str = "uploaded", message: str | None = None,
    ) -> None:
        now = now_str()
        self.db.execute(
            "INSERT INTO jobs (id, owner, original_name, video_path, duration_sec, width, height, "
            "language, status, progress, message, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (job_id, owner, original_name[:255], video_path, duration_sec, width, height,
             language, status, message, now, now),
        )

    def get_job(self, job_id: str) -> dict | None:
        return _clean(self.db.fetchone(_JOB_SELECT + " WHERE j.id = ?", (job_id,)))

    def list_jobs(self, owner: str, limit: int = 20) -> list[dict]:
        rows = self.db.fetchall(
            _JOB_SELECT + " WHERE j.owner = ? ORDER BY j.created_at DESC, j.id LIMIT ?", (owner, int(limit)))
        return _clean_all(rows)

    def update_job(self, job_id: str, **fields) -> None:
        self._update("jobs", job_id, fields, JOB_FIELDS)

    def get_transcript(self, job_id: str) -> list[Word]:
        """The words exactly as the speech engine heard them (before any editing)."""
        row = self.db.fetchone("SELECT transcript_json FROM jobs WHERE id = ?", (job_id,))
        return _decode_words(row["transcript_json"]) if row else []

    def set_transcript(self, job_id: str, words: list[Word]) -> None:
        self.update_job(job_id, transcript_json=_encode_words(words))

    def expired_job_ids(self, cutoff: str) -> list[str]:
        rows = self.db.fetchall("SELECT id FROM jobs WHERE created_at < ?", (cutoff,))
        return [r["id"] for r in rows]

    def delete_job(self, job_id: str) -> None:
        # Children are deleted explicitly so this also works on databases
        # that do not enforce foreign keys.
        with self.db.session() as s:
            s.execute("DELETE FROM renders WHERE job_id = ?", (job_id,))
            s.execute("DELETE FROM cues WHERE job_id = ?", (job_id,))
            s.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # ---- cues --------------------------------------------------------
    def replace_cues(self, job_id: str, cues: list[Cue]) -> None:
        rows = [
            (job_id, i, c.start_ms, c.end_ms, c.text, _encode_words(c.words))
            for i, c in enumerate(cues)
        ]
        with self.db.session() as s:
            s.execute("DELETE FROM cues WHERE job_id = ?", (job_id,))
            s.executemany(
                "INSERT INTO cues (job_id, idx, start_ms, end_ms, text, words_json) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            s.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now_str(), job_id))

    def get_cues(self, job_id: str) -> list[Cue]:
        rows = self.db.fetchall(
            "SELECT start_ms, end_ms, text, words_json FROM cues WHERE job_id = ? ORDER BY idx", (job_id,))
        return [
            Cue(int(r["start_ms"]), int(r["end_ms"]), r["text"], _decode_words(r["words_json"]))
            for r in rows
        ]

    # ---- renders -----------------------------------------------------
    def create_render(self, *, render_id: str, job_id: str, template: str) -> None:
        now = now_str()
        self.db.execute(
            "INSERT INTO renders (id, job_id, template, status, progress, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', 0, ?, ?)",
            (render_id, job_id, template, now, now),
        )

    def get_render(self, render_id: str) -> dict | None:
        return _clean(self.db.fetchone("SELECT * FROM renders WHERE id = ?", (render_id,)))

    def list_renders(self, job_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM renders WHERE job_id = ? ORDER BY created_at DESC, id", (job_id,))
        return _clean_all(rows)

    def update_render(self, render_id: str, **fields) -> None:
        self._update("renders", render_id, fields, RENDER_FIELDS)

    # ---- shared ------------------------------------------------------
    def _update(self, table: str, row_id: str, fields: dict, allowed: set[str]) -> None:
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update unknown columns: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [now_str(), row_id]
        self.db.execute(f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ?", tuple(values))
