"""Check that this computer is ready to run AutoSub Studio, and say what to fix if it is not.

    python scripts/doctor.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autosub import media  # noqa: E402
from autosub.config import Config  # noqa: E402
from autosub.db import Database, DatabaseError  # noqa: E402

problems = 0


def report(ok: bool, label: str, detail: str = "", fix: str = "", required: bool = True) -> None:
    global problems
    mark = "OK  " if ok else ("FAIL" if required else "WARN")
    print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))
    if not ok:
        if fix:
            print(f"       -> {fix}")
        if required:
            problems += 1


def has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def main() -> int:
    print("AutoSub Studio: checking your setup\n")
    version = ".".join(map(str, sys.version_info[:3]))
    report(sys.version_info >= (3, 10), "Python 3.10 or newer", version, "Install a newer Python from python.org.")

    for module, package in (("flask", "Flask"), ("requests", "requests"), ("dotenv", "python-dotenv")):
        report(has_module(module), f"package {package}", fix="Run: pip install -r requirements.txt")

    try:
        cfg = Config.from_env()
    except ValueError as exc:
        report(False, "settings (.env)", str(exc), "Fix the value in your .env file.")
        return 1

    # FFmpeg does the audio extraction and the subtitle burning
    try:
        report(True, "FFmpeg", media.ffmpeg_version(cfg))
        report(media.have_subtitle_filter(cfg), "FFmpeg can burn in subtitles (libass)",
               fix="Install a full build, for example on Windows: winget install Gyan.FFmpeg")
    except Exception as exc:
        report(False, "FFmpeg", str(exc).splitlines()[0] if str(exc) else "not found",
               "Install it (Windows: winget install Gyan.FFmpeg) and reopen the terminal, "
               "or set FFMPEG_PATH in .env.")

    fonts = sorted(p.name for p in cfg.fonts_dir.glob("*.ttf")) if cfg.fonts_dir.is_dir() else []
    report(bool(fonts), f"subtitle font ({cfg.font_name})", ", ".join(fonts) or f"nothing in {cfg.fonts_dir}",
           "Keep the assets/fonts folder from the project.")

    # database
    kind = "MySQL" if cfg.uses_mysql else "SQLite file"
    try:
        database = Database(cfg)
        database.init_schema()
        report(database.ping(), f"database ({kind})", database.description)
    except DatabaseError as exc:
        report(False, f"database ({kind})", str(exc),
               "Check DB_HOST, DB_USER, DB_PASSWORD and DB_NAME in .env (and DB_SSL=true for cloud MySQL). "
               "Run: python scripts/init_db.py")
    if cfg.uses_mysql:
        report(has_module("pymysql"), "package PyMySQL", fix="Run: pip install -r requirements.txt")

    # speech engine
    if cfg.transcriber == "local":
        report(has_module("faster_whisper"), "speech engine: faster-whisper (runs on your computer)",
               f"model '{cfg.whisper_model}' downloads on first use (about 480 MB for 'small', 150 MB for 'base')",
               "Run: pip install faster-whisper")
    elif cfg.transcriber == "groq":
        report(bool(cfg.groq_api_key), "speech engine: Groq (hosted, free tier)", cfg.groq_model,
               "Create a free key at console.groq.com and set GROQ_API_KEY in .env")
    else:
        report(cfg.transcriber == "mock", f"speech engine: {cfg.transcriber}",
               "the mock engine writes sample text; it does not listen" if cfg.transcriber == "mock" else "",
               "Set TRANSCRIBER to local or groq in .env")

    try:
        cfg.ensure_dirs()
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        report(True, "data folder is writable", str(cfg.data_dir))
    except OSError as exc:
        report(False, "data folder is writable", str(exc), "Set DATA_DIR in .env to a folder you can write to.")

    report(has_module("tkinter") and has_module("PIL"), "desktop app (tkinter and Pillow)",
           fix="Optional. Run: pip install Pillow. Linux also needs: sudo apt install python3-tk", required=False)

    print()
    if problems:
        print(f"{problems} problem(s) to fix before the app will work properly.")
        return 1
    print("All good. Start the website with: python run_web.py   or the desktop app with: python run_desktop.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
