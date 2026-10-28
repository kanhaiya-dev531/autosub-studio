"""Everything that talks to FFmpeg/FFprobe: probing, audio extraction, previews, burn-in."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # hide console flashes on Windows
INSTALL_HINT = "Install FFmpeg and make sure `ffmpeg` works in a terminal (see the README)."


class MediaError(RuntimeError):
    """A problem with the video or with FFmpeg, phrased for the end user."""


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, **kwargs,
        )
    except FileNotFoundError:
        raise MediaError(f"Could not run '{cmd[0]}'. {INSTALL_HINT}") from None


def _rotation(stream: dict) -> int:
    raw = (stream.get("tags") or {}).get("rotate")
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            raw = side["rotation"]
    try:
        return abs(int(float(raw))) % 360 if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def probe(cfg: Config, path: Path | str) -> VideoInfo:
    result = _run(
        [cfg.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    )
    if result.returncode != 0:
        raise MediaError("This file could not be read as a video. Try an MP4, MOV, MKV or WebM file.")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise MediaError("No video track was found in this file.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if _rotation(video) in (90, 270):
        width, height = height, width
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if width <= 0 or height <= 0:
        raise MediaError("The video size could not be determined.")
    return VideoInfo(duration=duration, width=width, height=height, has_audio=has_audio)


def output_size(info: VideoInfo, max_height: int = 0) -> tuple[int, int]:
    """Output size with even numbers (H.264 needs them), optionally capped in height."""
    w, h = info.width, info.height
    if max_height and h > max_height:
        w, h = round(w * max_height / h), max_height
    return w - w % 2, h - h % 2


def extract_audio(cfg: Config, video: Path | str, out_path: Path | str, codec: str = "wav") -> Path:
    """Write the audio track as 16 kHz mono (speech recognition's preferred input)."""
    codec_args = {
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "32k"],
    }[codec]
    result = _run(
        [cfg.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", *codec_args, str(out_path)]
    )
    if result.returncode != 0 or not Path(out_path).exists():
        raise MediaError("The audio could not be extracted from this video.")
    return Path(out_path)


def have_subtitle_filter(cfg: Config) -> bool:
    """True if this FFmpeg build can draw ASS subtitles (needs libass)."""
    result = _run([cfg.ffmpeg_path, "-hide_banner", "-filters"])
    return result.returncode == 0 and any(
        line.split()[1:2] == ["ass"] for line in result.stdout.splitlines() if len(line.split()) > 1
    )


def ffmpeg_version(cfg: Config) -> str:
    result = _run([cfg.ffmpeg_path, "-version"])
    first = (result.stdout or "").splitlines()[:1]
    return first[0] if first else "unknown"


@contextmanager
def _workspace(cfg: Config, ass_text: str):
    """A scratch folder holding subs.ass and a copy of the fonts.

    FFmpeg's filter syntax makes absolute paths painful (especially on Windows),
    so we run FFmpeg *inside* this folder and refer to the files by short names.
    """
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cfg.work_dir, prefix="ff_") as tmp:
        work = Path(tmp)
        (work / "subs.ass").write_text(ass_text, encoding="utf-8")
        has_fonts = False
        if cfg.fonts_dir.is_dir():
            target = work / "fonts"
            target.mkdir()
            for font in cfg.fonts_dir.iterdir():
                if font.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                    shutil.copy2(font, target / font.name)
                    has_fonts = True
        yield work, has_fonts


def _filter_chain(info: VideoInfo, size: tuple[int, int], has_fonts: bool, pts_offset_ms: int = 0) -> str:
    parts = []
    if size != (info.width, info.height):
        parts.append(f"scale={size[0]}:{size[1]}")
    if pts_offset_ms:
        parts.append(f"setpts=PTS+{pts_offset_ms / 1000:.3f}/TB")
    parts.append("ass=subs.ass:fontsdir=fonts" if has_fonts else "ass=subs.ass")
    return ",".join(parts)


def render_preview(
    cfg: Config, video: Path | str, info: VideoInfo, ass_text: str, size: tuple[int, int],
    seek_ms: int, pts_ms: int, out_path: Path | str,
) -> Path:
    """Draw the subtitles on a single frame of the video and save it as a JPEG.

    The frame at `seek_ms` is shifted to timestamp `pts_ms`, which is where the
    ASS cue was placed, so the preview shows exactly what a full render would.
    """
    out_path = Path(out_path).resolve()
    video = Path(video).resolve()  # FFmpeg runs in a scratch folder, so paths must be absolute
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _workspace(cfg, ass_text) as (work, has_fonts):
        cmd = [
            cfg.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(seek_ms, 0) / 1000:.3f}", "-i", str(video),
            "-an", "-frames:v", "1", "-vf", _filter_chain(info, size, has_fonts, pts_ms),
            "-q:v", "3", "-update", "1", str(out_path),
        ]
        result = _run(cmd, cwd=work)
    if result.returncode != 0 or not out_path.exists():
        raise MediaError(f"Could not draw the preview. {_tail(result.stderr)}")
    return out_path


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:] if text else ""


def render_video(
    cfg: Config, video: Path | str, info: VideoInfo, ass_text: str, size: tuple[int, int],
    out_path: Path | str, progress: Callable[[float], None] | None = None,
) -> Path:
    """Burn the subtitles into the video and write an MP4. `progress` gets 0.0 to 1.0."""
    out_path = Path(out_path).resolve()
    video = Path(video).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(".partial.mp4")
    with _workspace(cfg, ass_text) as (work, has_fonts):
        cmd = [
            cfg.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", str(video), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", _filter_chain(info, size, has_fonts),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(partial),
        ]
        log_path = work / "ffmpeg.log"
        try:
            with open(log_path, "w+", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    cmd, cwd=work, stdout=subprocess.PIPE, stderr=log, text=True,
                    encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
                )
                total_us = max(info.duration, 0.1) * 1_000_000
                for line in proc.stdout:  # type: ignore[union-attr]
                    key, _, value = line.strip().partition("=")
                    if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit() and progress:
                        progress(min(0.99, max(0.0, int(value) / total_us)))
                proc.wait()
            error_text = log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise MediaError(f"Could not run '{cfg.ffmpeg_path}'. {INSTALL_HINT}") from None
    if proc.returncode != 0 or not partial.exists():
        partial.unlink(missing_ok=True)
        raise MediaError(f"Rendering failed. {_tail(error_text)}")
    os.replace(partial, out_path)
    if progress:
        progress(1.0)
    return out_path
