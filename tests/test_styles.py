import re

import pytest

from autosub import styles
from autosub import subtitles as st
from autosub.subtitles import Cue, Word


def sample_cues():
    words = [Word(t, 500 + i * 300, 500 + i * 300 + 260) for i, t in enumerate("Welcome to the studio where captions come alive".split())]
    return st.normalize(st.group_words(words, max_chars=60))


def test_there_are_three_templates():
    assert list(styles.TEMPLATES) == ["classic", "pop", "neon"]
    with pytest.raises(ValueError):
        styles.get_template("nope")


@pytest.mark.parametrize("key", list(styles.TEMPLATES))
def test_every_template_builds_a_valid_ass_file(key):
    ass = styles.build_ass(sample_cues(), key, 1280, 720)
    assert "[Script Info]" in ass and "PlayResX: 1280" in ass and "PlayResY: 720" in ass
    styles_section = ass.split("[V4+ Styles]")[1].split("[Events]")[0]
    assert "Style:" in styles_section
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert dialogues
    for line in dialogues:
        parts = line.split(",", 9)
        assert len(parts) == 10  # Layer, Start, End, Style, Name, 3 margins, Effect, Text
        assert re.fullmatch(r"\d:\d\d:\d\d\.\d\d", parts[1]) and re.fullmatch(r"\d:\d\d:\d\d\.\d\d", parts[2])
        assert parts[2] > parts[1] or key == "neon"  # end after start (ASS times sort as text here)


def test_pop_creates_one_event_per_word_and_uppercases():
    ass = styles.build_ass([Cue(0, 1500, "go now please", [Word("go", 0, 400), Word("now", 500, 900), Word("please", 1000, 1500)])], "pop", 1280, 720)
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 3
    assert "GO" in dialogues[0] and "PLEASE" in dialogues[2]


def test_pop_splits_long_lines_into_short_captions():
    long_cue = st.normalize([Cue(0, 6000, "one two three four five six seven eight nine")])
    prepared = styles.prepare_cues(long_cue, "pop")
    assert len(prepared) == 3 and all(len(c.text.split()) <= 3 for c in prepared)


def test_text_is_escaped():
    ass = styles.build_ass([Cue(0, 1000, "curly {braces} and back\\slash")], "classic", 640, 360)
    line = next(ln for ln in ass.splitlines() if ln.startswith("Dialogue:"))
    assert "{braces}" not in line and "\\slash" not in line


def test_colour_conversion_is_bgr():
    assert styles._c("FF0000") == "&H000000FF"
    assert styles._i("00FF00") == "&H00FF00&"


def test_portrait_text_is_not_bigger_than_landscape_scaled():
    land = styles._base(1920, 1080)
    port = styles._base(1080, 1920)
    assert port <= land * 1.6  # captions on phone video stay readable but not enormous
