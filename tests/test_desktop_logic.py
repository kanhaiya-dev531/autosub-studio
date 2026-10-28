"""Editing rules used by the desktop app (no GUI needed)."""

import pytest

from autosub.subtitles import Cue
from desktop import logic


def row(text="one two three four", start=1000, end=5000, orig=0):
    return {"orig": orig, "start": start, "end": end, "text": text}


def test_time_formatting_and_parsing_agree():
    assert logic.format_time(0) == "0:00:00.000"
    assert logic.format_time(3_723_456) == "1:02:03.456"
    assert logic.format_time(-5) == "0:00:00.000"
    for ms in (0, 999, 61_250, 3_723_456):
        assert logic.parse_time(logic.format_time(ms)) == ms
    assert logic.parse_time("1:05.250") == 65_250
    assert logic.parse_time("65,25") == 65_250


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "-3"])
def test_bad_time_gets_a_friendly_message(bad):
    with pytest.raises(ValueError, match="Enter a time like"):
        logic.parse_time(bad)


def test_parse_number_bounds_and_decimal_comma():
    assert logic.parse_number("0,5", "the shift", -10, 10) == 0.5
    with pytest.raises(ValueError, match="as a number"):
        logic.parse_number("half", "the shift", -10, 10)
    with pytest.raises(ValueError, match="between -10 and 10"):
        logic.parse_number("11", "the shift", -10, 10)
    with pytest.raises(ValueError, match="between"):
        logic.parse_number("nan", "the factor", 0.5, 2)


def test_rows_from_cues_keep_the_original_index():
    rows = logic.rows_from_cues([Cue(0, 900, "a"), Cue(1000, 2000, "b")])
    assert rows == [
        {"orig": 0, "start": 0, "end": 900, "text": "a"},
        {"orig": 1, "start": 1000, "end": 2000, "text": "b"},
    ]


def test_split_at_the_cursor_divides_time_by_text():
    left, right = logic.split_row(row(), at_char=8)  # "one two" | "three four"
    assert (left["text"], right["text"]) == ("one two", "three four")
    assert left["start"] == 1000 and left["end"] == right["start"] == 1000 + round(4000 * 8 / 18)
    assert right["end"] == 5000
    assert left["orig"] == 0 and right["orig"] is None


def test_split_without_a_cursor_uses_the_space_nearest_the_middle():
    left, right = logic.split_row(row("alpha beta gamma delta"))
    assert (left["text"], right["text"]) == ("alpha beta", "gamma delta")


@pytest.mark.parametrize("text", ["single", "x", ""])
def test_split_refuses_text_it_cannot_cut(text):
    with pytest.raises(ValueError, match="cursor"):
        logic.split_row(row(text))


def test_split_never_produces_empty_time_spans():
    left, right = logic.split_row(row("ab cd", 1000, 1002), at_char=2)
    assert left["start"] < left["end"] <= right["start"] < right["end"]


def test_merge_joins_text_and_keeps_the_later_end():
    merged = logic.merge_rows(row("first", 0, 1000, 3), row("second", 1200, 2500, 4))
    assert merged == {"orig": 3, "start": 0, "end": 2500, "text": "first second"}


def test_new_line_after_fits_in_the_gap():
    rows = [row("a", 0, 1000), row("b", 1500, 3000)]
    assert logic.new_row_after(rows, 0) == {"orig": None, "start": 1000, "end": 1500, "text": ""}
    assert logic.new_row_after(rows, 1) == {"orig": None, "start": 3000, "end": 5000, "text": ""}
    assert logic.new_row_after([], None)["end"] == 2000
    tight = [row("a", 0, 1000), row("b", 1050, 3000)]
    assert logic.new_row_after(tight, 0)["end"] == 1300  # never shorter than 0.3 s


def test_validate_row_reports_what_is_wrong():
    ok = logic.validate_row("0:01.000", "0:02.500", "  Hello   there ")
    assert ok == {"orig": None, "start": 1000, "end": 2500, "text": "Hello there"}
    with pytest.raises(ValueError, match="after the start"):
        logic.validate_row("0:02", "0:01", "x")
    with pytest.raises(ValueError, match="Type some text"):
        logic.validate_row("0:01", "0:02", "   ")
    with pytest.raises(ValueError, match="Enter a time"):
        logic.validate_row("soon", "0:02", "x")


def test_status_lines():
    assert logic.status_line({"status": "ready", "cue_count": 1}) == "1 subtitle line"
    assert logic.status_line({"status": "ready", "cue_count": 7}) == "7 subtitle lines"
    assert logic.status_line({"status": "transcribing", "progress": 40, "message": "Listening"}) == "Listening (40%)"
    assert logic.status_line({"status": "uploaded", "progress": 0, "message": None}) == "Preparing (0%)"
    assert "model" in logic.status_line({"status": "failed", "message": "The speech model failed"})


def test_export_names_are_safe():
    job = {"original_name": "my holiday: day 12?.mp4"}
    assert logic.export_name(job, "srt") == "my holiday day 12.srt"
    assert logic.export_name(job, "ass", "neon") == "my holiday day 12-neon.ass"
    assert logic.export_name(job, "mp4", "pop") == "my holiday day 12-pop.mp4"
    assert logic.export_name({"original_name": "???.mp4"}, "txt") == "subtitles.txt"
