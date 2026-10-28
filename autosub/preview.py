"""Draw one template on one real frame of a video (no database needed).

Used by the website (cached per project), the desktop app and the gallery script.
"""

from __future__ import annotations

from pathlib import Path

from . import media, styles
from . import subtitles as st
from .config import Config
from .media import VideoInfo
from .subtitles import Cue, Word


def draw_preview(
    cfg: Config, video: Path | str, info: VideoInfo, cues: list[Cue], template: str, out_path: Path | str,
    *, cue: Cue | None = None, max_height: int = 720,
) -> Path:
    """Save a JPEG of `template` drawn on the frame where a word of `cue` is being spoken.

    Without `cue`, a good line is picked automatically (or a sample sentence when there are none).
    """
    styles.get_template(template)
    size = media.output_size(info, min(max_height, cfg.render_max_height or max_height))
    video_ms = int(info.duration * 1000)
    cue = cue or st.pick_preview_cue(cues, video_ms)
    words = cue.words or st.redistribute_words(cue.text, cue.start_ms, cue.end_ms)
    mid = words[len(words) // 2]
    at_ms = min(mid.start_ms + max(130, (mid.end_ms - mid.start_ms) // 2), max(video_ms - 100, 0))

    delta = 1000 - cue.start_ms  # move the cue so it starts at 1 s (ASS times cannot be negative)
    shifted = Cue(
        cue.start_ms + delta, cue.end_ms + delta, cue.text,
        [Word(w.text, w.start_ms + delta, w.end_ms + delta) for w in words],
    )
    ass = styles.build_ass([shifted], template, size[0], size[1], cfg.font_name)
    return media.render_preview(cfg, video, info, ass, size, seek_ms=at_ms, pts_ms=at_ms + delta, out_path=out_path)
