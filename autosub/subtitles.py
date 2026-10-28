"""Subtitle data model and every operation on it: SRT in/out, timing, syncing.

Times are always integer milliseconds. This module has no dependencies outside
the standard library, which keeps it easy to test.
"""

from __future__ import annotations

import copy
import difflib
import itertools
import re
from dataclasses import dataclass, field

SENTENCE_END = (".", "?", "!", "。", "？", "！", "।")
CLAUSE_END = (",", ";", ":", "，", "、")


@dataclass
class Word:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


# --------------------------------------------------------------------------
# Time formatting and parsing
# --------------------------------------------------------------------------
def ms_to_srt(ms: int) -> str:
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def ms_to_ass(ms: int) -> str:
    cs = max(0, int(round(ms / 10)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, centi = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{centi:02d}"


def parse_timecode(value) -> int:
    """Accept `HH:MM:SS,mmm`, `MM:SS.mmm`, `SS.mmm`, plain seconds, or an int of ms.

    Integers are treated as milliseconds, strings without a colon as seconds.
    """
    if isinstance(value, bool):
        raise ValueError("Invalid time")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ValueError("Time is empty")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time: {value!r}")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2]) if len(parts) >= 2 else 0
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except ValueError:
        raise ValueError(f"Invalid time: {value!r}") from None
    if seconds < 0 or minutes < 0 or hours < 0:
        raise ValueError(f"Time cannot be negative: {value!r}")
    return int(round(((hours * 60 + minutes) * 60 + seconds) * 1000))


# --------------------------------------------------------------------------
# Text and word timing helpers
# --------------------------------------------------------------------------
def clean_text(text: str) -> str:
    """Collapse whitespace and drop markup that SRT files sometimes carry."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{\\[^}]*\}", "", text)
    return " ".join(text.split())


def redistribute_words(text: str, start_ms: int, end_ms: int) -> list[Word]:
    """Spread the words of `text` over a time range, weighted by word length."""
    tokens = text.split()
    if not tokens:
        return []
    total = max(1, end_ms - start_ms)
    weights = [len(t) + 1 for t in tokens]
    weight_sum = sum(weights)
    words: list[Word] = []
    cursor = 0.0
    for token, weight in zip(tokens, weights):
        begin = start_ms + int(round(cursor / weight_sum * total))
        cursor += weight
        finish = start_ms + int(round(cursor / weight_sum * total))
        words.append(Word(token, begin, max(finish, begin)))
    words[-1].end_ms = max(end_ms, words[-1].start_ms)
    return words


def rescale_words(words: list[Word], old_start: int, old_end: int, new_start: int, new_end: int) -> list[Word]:
    """Map word times from one cue range to another (used when only the timing was edited)."""
    if not words:
        return []
    old_span = old_end - old_start
    if old_span <= 0:
        return redistribute_words(" ".join(w.text for w in words), new_start, new_end)
    ratio = (new_end - new_start) / old_span

    def mapped(t: int) -> int:
        return int(round(new_start + (t - old_start) * ratio))

    return [Word(w.text, mapped(w.start_ms), mapped(w.end_ms)) for w in words]


def _clamp_words(cue: Cue) -> None:
    for w in cue.words:
        w.start_ms = min(max(w.start_ms, cue.start_ms), cue.end_ms)
        w.end_ms = min(max(w.end_ms, w.start_ms), cue.end_ms)


# --------------------------------------------------------------------------
# Building cues from a transcript
# --------------------------------------------------------------------------
def group_words(
    words: list[Word],
    max_chars: int = 38,
    max_words: int | None = None,
    max_duration_ms: int = 4800,
    pause_ms: int = 700,
) -> list[Cue]:
    """Group a stream of timed words into readable subtitle lines."""
    cues: list[Cue] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            cues.append(
                Cue(
                    start_ms=current[0].start_ms,
                    end_ms=max(current[-1].end_ms, current[0].start_ms + 1),
                    text=" ".join(w.text for w in current),
                    words=list(current),
                )
            )
            current.clear()

    for word in words:
        if current:
            projected = len(" ".join(w.text for w in current)) + 1 + len(word.text)
            duration = word.end_ms - current[0].start_ms
            gap = word.start_ms - current[-1].end_ms
            last = current[-1].text
            too_long = projected > max_chars
            too_many = max_words is not None and len(current) >= max_words
            sentence_break = last.endswith(SENTENCE_END) and len(current) >= 3
            clause_break = last.endswith(CLAUSE_END) and projected > max_chars * 0.6
            if too_long or too_many or duration > max_duration_ms or gap > pause_ms or sentence_break or clause_break:
                flush()
        current.append(word)
    flush()
    return cues


def cues_from_segments(segments: list[tuple[int, int, str]], **group_kwargs) -> list[Cue]:
    """Fallback when the engine gives no word times: spread each segment's words evenly."""
    words: list[Word] = []
    for start, end, text in segments:
        words.extend(redistribute_words(clean_text(text), start, end))
    return group_words(words, **group_kwargs)


def rechunk(cue: Cue, max_words: int | None, max_chars: int) -> list[Cue]:
    """Split one cue into smaller ones (never merges). Used for short, punchy styles."""
    words = cue.words or redistribute_words(cue.text, cue.start_ms, cue.end_ms)
    if not words:
        return []
    pieces = group_words(words, max_chars=max_chars, max_words=max_words, max_duration_ms=10**9, pause_ms=10**9)
    # keep the original outer timing so the line does not vanish early
    pieces[0].start_ms = cue.start_ms
    pieces[-1].end_ms = max(pieces[-1].end_ms, cue.end_ms)
    return pieces


# --------------------------------------------------------------------------
# Cleaning up a cue list
# --------------------------------------------------------------------------
def normalize(cues: list[Cue], min_duration_ms: int = 300) -> list[Cue]:
    """Sort, drop empty lines, enforce a minimum length and remove overlaps."""
    result = [copy.deepcopy(c) for c in cues if clean_text(c.text)]
    for c in result:
        c.text = clean_text(c.text)
        c.start_ms = max(0, int(c.start_ms))
        c.end_ms = max(int(c.end_ms), c.start_ms + min_duration_ms)
    result.sort(key=lambda c: (c.start_ms, c.end_ms))
    for current, nxt in itertools.pairwise(result):
        if nxt.start_ms < current.end_ms:
            if nxt.start_ms > current.start_ms:
                current.end_ms = nxt.start_ms
            else:  # identical start: push the later one back
                nxt.start_ms = current.end_ms
                nxt.end_ms = max(nxt.end_ms, nxt.start_ms + min_duration_ms)
    for c in result:
        if not c.words:
            c.words = redistribute_words(c.text, c.start_ms, c.end_ms)
        _clamp_words(c)
    return result


# --------------------------------------------------------------------------
# Syncing
# --------------------------------------------------------------------------
def _map_time(t: int, mapper) -> int:
    return max(0, int(round(mapper(t))))


def _remap(cues: list[Cue], mapper) -> list[Cue]:
    out: list[Cue] = []
    for c in cues:
        new = copy.deepcopy(c)
        new.start_ms = _map_time(c.start_ms, mapper)
        new.end_ms = _map_time(c.end_ms, mapper)
        for w in new.words:
            w.start_ms = _map_time(w.start_ms, mapper)
            w.end_ms = _map_time(w.end_ms, mapper)
        if new.end_ms > 0 and new.end_ms > new.start_ms:
            out.append(new)
    return out


def shift(cues: list[Cue], delta_ms: int) -> list[Cue]:
    """Move every cue by `delta_ms` (negative = earlier). Cues pushed before 0 are dropped."""
    out: list[Cue] = []
    for c in cues:
        if c.end_ms + delta_ms <= 0:
            continue
        new = copy.deepcopy(c)
        new.start_ms = max(0, c.start_ms + delta_ms)
        new.end_ms = c.end_ms + delta_ms
        for w in new.words:
            w.start_ms = max(0, w.start_ms + delta_ms)
            w.end_ms = max(0, w.end_ms + delta_ms)
        out.append(new)
    return out


def sync_linear(cues: list[Cue], src1: int, dst1: int, src2: int, dst2: int) -> list[Cue]:
    """Stretch/shift so that time `src1` becomes `dst1` and `src2` becomes `dst2`."""
    if src1 == src2:
        raise ValueError("The two reference times must be different")
    scale = (dst2 - dst1) / (src2 - src1)
    return _remap(cues, lambda t: dst1 + (t - src1) * scale)


def sync_anchors(cues: list[Cue], first_start_ms: int, last_start_ms: int) -> list[Cue]:
    """The first line should start at `first_start_ms` and the last line at `last_start_ms`."""
    if not cues:
        return []
    src_first, src_last = cues[0].start_ms, cues[-1].start_ms
    if len(cues) == 1 or src_first == src_last:
        return shift(cues, first_start_ms - src_first)
    if last_start_ms <= first_start_ms:
        raise ValueError("The last line must start after the first line")
    return sync_linear(cues, src_first, first_start_ms, src_last, last_start_ms)


def stretch(cues: list[Cue], factor: float) -> list[Cue]:
    """Multiply every time by `factor`. Fixes subtitles made for another frame rate
    (for example 25 / 23.976 = 1.0427 when lines drift later and later)."""
    if not 0.5 <= factor <= 2.0:
        raise ValueError("The stretch factor must be between 0.5 and 2")
    return _remap(cues, lambda t: t * factor)


@dataclass
class SyncReport:
    ok: bool
    matched: int  # words of the subtitle file that were found in the speech
    total: int  # words in the subtitle file
    scale: float = 1.0
    offset_ms: int = 0
    message: str = ""


def _token(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _robust_line(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Fit y = scale * x + offset, ignoring outliers (wrong matches)."""

    def fit(pts):
        n = len(pts)
        mx = sum(x for x, _ in pts) / n
        my = sum(y for _, y in pts) / n
        var = sum((x - mx) ** 2 for x, _ in pts)
        scale = sum((x - mx) * (y - my) for x, y in pts) / var if var else 1.0
        return scale, my - scale * mx

    pts = list(points)
    scale, offset = fit(pts)
    for _ in range(4):
        residuals = [abs(y - (scale * x + offset)) for x, y in pts]
        ordered = sorted(residuals)
        limit = max(400.0, 3 * ordered[len(ordered) // 2])
        kept = [p for p, r in zip(pts, residuals) if r <= limit]
        if len(kept) < 5 or len(kept) == len(pts):
            break
        pts = kept
        scale, offset = fit(pts)
    return scale, offset


def auto_sync(cues: list[Cue], heard: list[Word], min_matches: int = 8) -> tuple[list[Cue], SyncReport]:
    """Line up an existing subtitle file with the real speech.

    `heard` are the words the speech engine detected (with true times). Words that
    appear in both are paired up; a straight line through those pairs gives the
    shift and stretch to apply. Returns the new cues and a report; if too few
    words match, the cues come back unchanged with `report.ok == False`.
    """
    sub_tokens: list[tuple[str, float]] = []
    for cue in cues:
        for w in cue.words or redistribute_words(cue.text, cue.start_ms, cue.end_ms):
            tok = _token(w.text)
            if tok:
                sub_tokens.append((tok, (w.start_ms + w.end_ms) / 2))
    heard_tokens = [(_token(w.text), (w.start_ms + w.end_ms) / 2) for w in heard if _token(w.text)]
    total = len(sub_tokens)
    if total == 0 or not heard_tokens:
        return cues, SyncReport(False, 0, total, message="There is nothing to compare. No speech was detected in the video.")

    matcher = difflib.SequenceMatcher(None, [t for t, _ in sub_tokens], [t for t, _ in heard_tokens], autojunk=False)
    pairs: list[tuple[float, float]] = []
    for block in matcher.get_matching_blocks():
        if block.size >= 3:  # single-word matches are mostly coincidences
            for k in range(block.size):
                pairs.append((sub_tokens[block.a + k][1], heard_tokens[block.b + k][1]))
    if len(pairs) < min_matches:
        return cues, SyncReport(
            False, len(pairs), total,
            message="Too few words match the speech. Is the subtitle file in the same language as the video?",
        )

    scale, offset = _robust_line(pairs)
    if not 0.75 <= scale <= 1.33:
        return cues, SyncReport(False, len(pairs), total, message="The timing differs too much to sync automatically.")
    result = _remap(cues, lambda t: scale * t + offset)
    sign = "+" if offset >= 0 else "-"
    message = f"Matched {len(pairs)} of {total} words. Moved by {sign}{abs(offset) / 1000:.2f} s"
    if abs(scale - 1) > 0.0005:
        message += f" and stretched by x{scale:.4f}"
    return result, SyncReport(True, len(pairs), total, scale, int(round(offset)), message + ".")


# --------------------------------------------------------------------------
# Editing single lines (used by the desktop editor)
# --------------------------------------------------------------------------
def split_cue(cue: Cue, at_word: int | None = None) -> tuple[Cue, Cue]:
    """Split one line in two, before word number `at_word` (default: the middle)."""
    words = cue.words or redistribute_words(cue.text, cue.start_ms, cue.end_ms)
    if len(words) < 2:
        raise ValueError("A line needs at least two words to be split")
    at = len(words) // 2 if at_word is None else at_word
    if not 1 <= at <= len(words) - 1:
        raise ValueError("Choose a split point between two words")
    first, second = words[:at], words[at:]
    gap_mid = (first[-1].end_ms + second[0].start_ms) // 2
    a_end = max(cue.start_ms + 1, min(first[-1].end_ms, gap_mid))
    b_start = min(cue.end_ms - 1, max(second[0].start_ms, a_end))
    return (
        Cue(cue.start_ms, a_end, " ".join(w.text for w in first), first),
        Cue(b_start, cue.end_ms, " ".join(w.text for w in second), second),
    )


def merge_cues(first: Cue, second: Cue) -> Cue:
    words = (first.words or redistribute_words(first.text, first.start_ms, first.end_ms)) + (
        second.words or redistribute_words(second.text, second.start_ms, second.end_ms)
    )
    return Cue(first.start_ms, max(first.end_ms, second.end_ms), f"{first.text} {second.text}".strip(), words)


# --------------------------------------------------------------------------
# SRT
# --------------------------------------------------------------------------
_TIMING = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _hms(h: str, m: str, s: str, frac: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def parse_srt(text: str) -> list[Cue]:
    """Parse the text of an .srt file. Raises ValueError if no cues are found."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            match = _TIMING.search(line)
            if match:
                g = match.groups()
                body = clean_text(" ".join(lines[i + 1 :]))
                if body:
                    start, end = _hms(*g[:4]), _hms(*g[4:])
                    if end > start:
                        cues.append(Cue(start, end, body))
                break
    if not cues:
        raise ValueError("No subtitle lines were found. Is this a valid .srt file?")
    for c in cues:
        c.words = redistribute_words(c.text, c.start_ms, c.end_ms)
    return cues


def wrap_two_lines(text: str, width: int = 42) -> str:
    """Break long text into at most two balanced lines for SRT output."""
    text = " ".join(text.split())
    if len(text) <= width or " " not in text:
        return text
    mid = len(text) / 2
    cut = min((m.start() for m in re.finditer(" ", text)), key=lambda k: abs(k - mid))
    return text[:cut] + "\n" + text[cut + 1 :]


def format_srt(cues: list[Cue], wrap: int = 42) -> str:
    blocks = []
    for i, c in enumerate(cues, start=1):
        blocks.append(f"{i}\n{ms_to_srt(c.start_ms)} --> {ms_to_srt(c.end_ms)}\n{wrap_two_lines(c.text, wrap)}\n")
    return "\n".join(blocks)


def ms_to_vtt(ms: int) -> str:
    return ms_to_srt(ms).replace(",", ".")


def format_vtt(cues: list[Cue], wrap: int = 42) -> str:
    """WebVTT, the subtitle format web browsers understand."""
    blocks = ["WEBVTT\n"]
    for c in cues:
        text = wrap_two_lines(c.text, wrap).replace("&", "&amp;").replace("<", "&lt;")
        blocks.append(f"{ms_to_vtt(c.start_ms)} --> {ms_to_vtt(c.end_ms)}\n{text}\n")
    return "\n".join(blocks)


def format_txt(cues: list[Cue]) -> str:
    """The plain transcript, one subtitle line per row, without any timing."""
    return "\n".join(c.text for c in cues) + "\n"


# --------------------------------------------------------------------------
# Picking a representative line for template previews
# --------------------------------------------------------------------------
SAMPLE_TEXT = "This is how your subtitles will look"


def pick_preview_cue(cues: list[Cue], video_ms: int) -> Cue:
    """Choose a cue with enough words to show off a style (or invent a sample)."""
    candidates = [c for c in cues[:12] if len(c.text.split()) >= 3 and c.end_ms <= max(video_ms, 1)]
    if candidates:
        return max(candidates, key=lambda c: min(len(c.text), 40))
    start = min(500, max(video_ms - 2500, 0))
    end = start + 2500
    return Cue(start, end, SAMPLE_TEXT, redistribute_words(SAMPLE_TEXT, start, end))
