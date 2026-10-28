import pytest

from autosub import subtitles as st
from autosub.subtitles import Cue, Word

SPEECH = ("the quick brown fox jumps over the lazy dog and then runs far away into the quiet forest "
          "where nobody can follow because the trees are very tall and the night is dark").split()


def heard_words():
    return [Word(t, 1000 + i * 400, 1000 + i * 400 + 350) for i, t in enumerate(SPEECH)]


def test_time_formats():
    assert st.ms_to_srt(3_723_456) == "01:02:03,456"
    assert st.ms_to_ass(3_723_456) == "1:02:03.46"
    assert st.ms_to_vtt(61_005) == "00:01:01.005"
    assert st.ms_to_srt(-5) == "00:00:00,000"


@pytest.mark.parametrize("value,expected", [
    ("00:00:01,500", 1500), ("1:02.250", 62250), ("3.5", 3500), (1200, 1200), ("01:00:00", 3_600_000),
])
def test_parse_timecode(value, expected):
    assert st.parse_timecode(value) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "-3", True])
def test_parse_timecode_rejects_garbage(bad):
    with pytest.raises(ValueError):
        st.parse_timecode(bad)


def test_srt_roundtrip():
    cues = [Cue(1000, 2500, "Hello world"), Cue(3000, 6000, "A much longer second line that will need to wrap onto two lines")]
    text = st.format_srt(cues)
    assert text.startswith("1\n00:00:01,000 --> 00:00:02,500\nHello world\n")
    back = st.parse_srt(text)
    assert [(c.start_ms, c.end_ms) for c in back] == [(1000, 2500), (3000, 6000)]
    assert back[1].text.startswith("A much longer second line")


def test_parse_srt_handles_bom_crlf_and_markup():
    raw = "\ufeff1\r\n00:00:01,000 --> 00:00:02,000\r\n<i>Hi</i> {\\an8}there\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nBye\r\n"
    cues = st.parse_srt(raw)
    assert [c.text for c in cues] == ["Hi there", "Bye"]


def test_parse_srt_rejects_non_subtitles():
    with pytest.raises(ValueError):
        st.parse_srt("this is not a subtitle file")


def test_vtt_and_txt():
    cues = [Cue(0, 1000, "Tom & <Jerry>")]
    vtt = st.format_vtt(cues)
    assert vtt.startswith("WEBVTT") and "00:00:00.000 --> 00:00:01.000" in vtt and "Tom &amp; &lt;Jerry>" in vtt
    assert st.format_txt(cues) == "Tom & <Jerry>\n"


def test_group_words_respects_limits_and_punctuation():
    words = [Word(t, i * 300, i * 300 + 250) for i, t in enumerate("Hello there my friend. How are you doing today".split())]
    cues = st.group_words(words, max_chars=24)
    assert all(len(c.text) <= 24 for c in cues)
    assert cues[0].text == "Hello there my friend."  # a full stop after 3+ words ends the line
    assert " ".join(c.text for c in cues) == " ".join(w.text for w in words)


def test_group_words_breaks_on_long_pause():
    words = [Word("one", 0, 200), Word("two", 300, 500), Word("three", 3000, 3200)]
    assert [c.text for c in st.group_words(words)] == ["one two", "three"]


def test_normalize_removes_overlap_and_enforces_minimum():
    cues = st.normalize([Cue(0, 2000, "a b"), Cue(1000, 1100, "c d"), Cue(5000, 5001, "e")])
    assert cues[0].end_ms <= cues[1].start_ms
    assert all(c.duration_ms >= 300 for c in cues)
    assert all(c.words for c in cues)


def test_shift_drops_lines_pushed_before_zero():
    cues = [Cue(0, 500, "gone"), Cue(1000, 2000, "stays")]
    out = st.shift(cues, -800)
    assert [c.text for c in out] == ["stays"]
    assert out[0].start_ms == 200


def test_stretch_and_limits():
    out = st.stretch([Cue(1000, 2000, "x")], 1.5)
    assert (out[0].start_ms, out[0].end_ms) == (1500, 3000)
    with pytest.raises(ValueError):
        st.stretch([Cue(0, 1, "x")], 5)


def test_anchor_sync_maps_first_and_last():
    cues = [Cue(1000, 2000, "first"), Cue(5000, 6000, "mid"), Cue(9000, 10000, "last")]
    out = st.sync_anchors(cues, 2000, 12000)
    assert out[0].start_ms == 2000 and out[-1].start_ms == 12000
    with pytest.raises(ValueError):
        st.sync_anchors(cues, 5000, 4000)


def test_auto_sync_recovers_offset_and_drift():
    true_cues = st.group_words(heard_words())
    late = st._remap(true_cues, lambda t: (t + 3200) / 1.04)  # late by 3.2 s and 4 % slow
    for c in late:
        c.words = []
    fixed, report = st.auto_sync(late, heard_words())
    assert report.ok and report.matched == report.total == len(SPEECH)
    assert max(abs(a.start_ms - b.start_ms) for a, b in zip(fixed, true_cues)) < 60  # measured: 37 ms


def test_auto_sync_refuses_other_languages():
    junk = [Cue(0, 2000, "namaste doston aap sab ka swagat hai"), Cue(2500, 5000, "aaj hum kuch naya seekhenge")]
    out, report = st.auto_sync(junk, heard_words())
    assert not report.ok and out == junk


def test_split_and_merge_keep_text_and_order():
    cue = st.normalize([Cue(0, 4000, "one two three four five six")])[0]
    a, b = st.split_cue(cue, 2)
    assert (a.text, b.text) == ("one two", "three four five six")
    assert a.end_ms <= b.start_ms
    assert st.merge_cues(a, b).text == cue.text
    with pytest.raises(ValueError):
        st.split_cue(Cue(0, 1000, "single"))


def test_wrap_two_lines_balances():
    assert st.wrap_two_lines("short") == "short"
    wrapped = st.wrap_two_lines("this line is definitely longer than forty two characters in total")
    first, second = wrapped.split("\n")
    assert abs(len(first) - len(second)) < 12


def test_pick_preview_cue_falls_back_to_sample_text():
    cue = st.pick_preview_cue([], 8000)
    assert cue.text == st.SAMPLE_TEXT and cue.words
