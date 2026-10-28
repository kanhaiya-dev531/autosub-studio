"""Central configuration.

Every setting comes from an environment variable (or a `.env` file in the project
root), so the same code runs on a laptop, in Docker and on a free hosting plan.
See `.env.example` for the full list.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _i(name: str, default: int) -> int:
    raw = _s(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None


def _b(name: str, default: bool = False) -> bool:
    raw = _s(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _persistent_secret(data_dir: Path) -> str:
    """Return a SECRET_KEY that survives restarts when none is configured."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".secret_key"
    try:
        with open(path, "x", encoding="utf-8") as fh:
            key = secrets.token_hex(32)
            fh.write(key)
            return key
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Config:
    # storage
    data_dir: Path
    secret_key: str
    # database (leave db_host empty to use a local SQLite file)
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    db_ssl: bool
    db_ssl_ca: str
    # transcription
    transcriber: str  # "local" (faster-whisper), "groq" (hosted) or "mock" (tests only)
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_beam_size: int
    groq_api_key: str
    groq_model: str
    # limits
    max_upload_mb: int
    max_video_seconds: int  # 0 = unlimited
    render_max_height: int  # 0 = keep the original size
    workers: int
    job_ttl_hours: int  # 0 = keep forever
    secure_cookies: bool  # set True when the site is served over HTTPS
    # rendering
    font_name: str
    fonts_dir: Path
    ffmpeg_path: str
    ffprobe_path: str

    # ---- derived -----------------------------------------------------
    @property
    def uses_mysql(self) -> bool:
        return bool(self.db_host)

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "autosub.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    def ensure_dirs(self) -> None:
        for folder in (
            self.data_dir,
            self.uploads_dir,
            self.renders_dir,
            self.previews_dir,
            self.work_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)

    # ---- construction ------------------------------------------------
    @classmethod
    def from_env(cls, **overrides) -> Config:
        """Build a Config from the environment. Keyword arguments win over env vars."""
        _load_dotenv()

        data_dir = Path(overrides.pop("data_dir", None) or _s("DATA_DIR") or PROJECT_ROOT / "data")
        data_dir = data_dir.expanduser().resolve()

        ffmpeg = _s("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
        ffprobe = _s("FFPROBE_PATH")
        if not ffprobe:
            sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            ffprobe = str(sibling) if sibling.exists() else (shutil.which("ffprobe") or "ffprobe")

        values: dict[str, Any] = dict(
            data_dir=data_dir,
            secret_key=_s("SECRET_KEY"),
            db_host=_s("DB_HOST"),
            db_port=_i("DB_PORT", 3306),
            db_user=_s("DB_USER", "root"),
            db_password=os.environ.get("DB_PASSWORD", ""),
            db_name=_s("DB_NAME", "autosub"),
            db_ssl=_b("DB_SSL", False),
            db_ssl_ca=_s("DB_SSL_CA"),
            transcriber=_s("TRANSCRIBER", "local").lower(),
            whisper_model=_s("WHISPER_MODEL", "small"),
            whisper_device=_s("WHISPER_DEVICE", "cpu"),
            whisper_compute_type=_s("WHISPER_COMPUTE_TYPE", "int8"),
            whisper_beam_size=_i("WHISPER_BEAM_SIZE", 3),
            groq_api_key=_s("GROQ_API_KEY"),
            groq_model=_s("GROQ_MODEL", "whisper-large-v3-turbo"),
            max_upload_mb=_i("MAX_UPLOAD_MB", 200),
            max_video_seconds=_i("MAX_VIDEO_SECONDS", 0),
            render_max_height=_i("RENDER_MAX_HEIGHT", 0),
            workers=max(1, _i("WORKERS", 1)),
            job_ttl_hours=_i("JOB_TTL_HOURS", 0),
            secure_cookies=_b("SESSION_COOKIE_SECURE", False),
            font_name=_s("SUBTITLE_FONT", "Poppins"),
            fonts_dir=Path(_s("FONTS_DIR") or PROJECT_ROOT / "assets" / "fonts"),
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
        )
        values.update(overrides)
        if not values["secret_key"]:
            values["secret_key"] = _persistent_secret(values["data_dir"])
        return cls(**values)
