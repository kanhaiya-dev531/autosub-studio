"""Editing rules for the desktop app, kept free of any GUI code so they can be tested anywhere.

A "row" is one subtitle line as the editor holds it:
    {"orig": int | None, "start": ms, "end": ms, "text": str}
`orig` is the index of the saved line it came from (the engine uses it to keep word timings).
These rules match the website's editor (web/static/js/workspace.js).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from autosub.subtitles import Cue, parse_timecode


class Row(TypedDict):
    orig: int | None
    start: int
    end: int
    text: str


MIN_PART_MS = 300  # a line shorter than this is hard to read


def format_time(ms: int) -> str:
    """0:00:01.446, the same style the website shows."""
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_time(text: str) -> int:
    """Read what a person typed in a time box. Raises ValueError with a message fit to show."""
    try:
        return parse_timecode(text)
    except ValueError:
        raise ValueError("Enter a time like 1:05.250 or 65.25") from None


def parse_number(text: str, what: str, low: float, high: float) -> float:
    """A decimal number inside [low, high]. Accepts a comma as the decimal mark."""
    try:
        value = float(str(text).strip().replace(",", "."))
    except ValueError:
        raise ValueError(f"Enter {what} as a number.") from None
    if value != value or not low <= value <= high:  # NaN fails both tests
        raise ValueError(f"{what[0].upper()}{what[1:]} must be between {low:g} and {high:g}.")
    return value


def rows_from_cues(cues: Iterable[Cue]) -> list[Row]:
    return [{"orig": i, "start": c.start_ms, "end": c.end_ms, "text": c.text} for i, c in enumerate(cues)]


def split_row(row: Row, at_char: int | None = None) -> tuple[Row, Row]:
    """Cut a line in two at a character position (or at the space nearest the middle).

    The time is divided in proportion to the text. Raises ValueError when no sensible cut exists.
    """
    text = row["text"]
    pos = at_char if at_char is not None and 0 < at_char < len(text) else None
    if pos is None:
        middle = len(text) // 2
        spaces = [m.start() for m in re.finditer(" ", text)]
        pos = min(spaces, key=lambda k: abs(k - middle)) if spaces else 0
    left, right = text[:pos].strip(), text[pos:].strip()
    if not left or not right:
        raise ValueError("Put the cursor inside the text where the line should split.")
    span = row["end"] - row["start"]
    at = row["start"] + round(span * pos / len(text))
    at = min(max(at, row["start"] + 1), row["end"] - 1)
    return (
        {"orig": row["orig"], "start": row["start"], "end": at, "text": left},
        {"orig": None, "start": at, "end": row["end"], "text": right},
    )


def merge_rows(first: Row, second: Row) -> Row:
    return {
        "orig": first["orig"],
        "start": first["start"],
        "end": max(first["end"], second["end"]),
        "text": f"{first['text']} {second['text']}".strip(),
    }


def new_row_after(rows: list[Row], index: int | None) -> Row:
    """An empty two-second line placed after rows[index] (or at the start when there are none)."""
    if index is None or not rows:
        return {"orig": None, "start": 0, "end": 2000, "text": ""}
    start = rows[index]["end"]
    end = start + 2000
    if index + 1 < len(rows) and rows[index + 1]["start"] < end:
        end = max(start + MIN_PART_MS, rows[index + 1]["start"])
    return {"orig": None, "start": start, "end": end, "text": ""}


def validate_row(start_text: str, end_text: str, text: str) -> Row:
    """Check what the edit window holds. Raises ValueError with a message fit to show."""
    start, end = parse_time(start_text), parse_time(end_text)
    if end <= start:
        raise ValueError("The end time must be after the start time.")
    text = " ".join(text.split())
    if not text:
        raise ValueError("Type some text for this line, or delete the line.")
    return {"orig": None, "start": start, "end": end, "text": text}


def to_items(rows: Iterable[Row]) -> list[dict]:
    """Rows in the shape JobService.save_cues expects (empty lines are dropped by the engine)."""
    return [{"orig": r["orig"], "start": r["start"], "end": r["end"], "text": r["text"]} for r in rows]


def status_line(job: dict) -> str:
    """One sentence about where a project is."""
    status = job["status"]
    if status == "ready":
        count = job.get("cue_count") or 0
        return f"{count} subtitle line{'' if count == 1 else 's'}"
    if status == "failed":
        return job.get("message") or "Could not create subtitles."
    message = job.get("message") or "Preparing"
    return f"{message} ({job.get('progress') or 0}%)"


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w.\- ]+", "", Path(name).stem, flags=re.UNICODE).strip(" .")
    return stem[:80] or "subtitles"


def export_name(job: dict, kind: str, template: str | None = None) -> str:
    suffix = f"-{template}" if template and kind in ("ass", "mp4") else ""
    return f"{safe_stem(job['original_name'])}{suffix}.{kind}"

