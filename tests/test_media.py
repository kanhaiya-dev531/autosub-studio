import shutil
import subprocess

import pytest
from conftest import needs_ffmpeg

from autosub import media, styles
from autosub import subtitles as st
from autosub.media import MediaError

pytestmark = needs_ffmpeg


def cues_for(duration_ms=6000):
    words = [Word(t, 500 + i * 330, 500 + i * 330 + 290) for i, t in enumerate("Captions that keep up with every single word you say".split())]
    return st.normalize(st.group_words(words))


from autosub.subtitles import Word  # noqa: E402


def test_probe_reads_size_duration_and_audio(cfg, sample_video, silent_video):
    info = media.probe(cfg, sample_video)
    assert (info.width, info.height) == (640, 360) and 5.5 < info.duration < 6.5 and info.has_audio
    assert media.probe(cfg, silent_video).has_audio is False


def test_probe_rejects_non_video(cfg, tmp_path):
    junk = tmp_path / "notes.mp4"
    junk.write_text("definitely not a video")
    with pytest.raises(MediaError):
        media.probe(cfg, junk)


def test_probe_swaps_size_for_rotated_phone_video(cfg, tmp_path, sample_video):
    rotated = tmp_path / "rot.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(sample_video),
                    "-c", "copy", str(rotated)], check=True)
    info = media.probe(cfg, rotated)
    assert (info.width, info.height) == (360, 640)


def test_output_size_is_even_and_capped():
    info = media.VideoInfo(10, 1919, 1081, True)
    assert media.output_size(info) == (1918, 1080)
    assert media.output_size(info, 720) == (1278, 720)


def test_extract_audio_makes_a_16k_mono_wav(cfg, sample_video, tmp_path):
    out = media.extract_audio(cfg, sample_video, tmp_path / "a.wav")
    assert out.stat().st_size > 10_000
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True).stdout.strip()
    assert probe == "16000,1"


def test_ffmpeg_can_draw_ass(cfg):
    assert media.have_subtitle_filter(cfg)
    assert "ffmpeg" in media.ffmpeg_version(cfg).lower()


@pytest.mark.parametrize("key", list(styles.TEMPLATES))
def test_preview_draws_subtitles_on_the_frame(cfg, sample_video, tmp_path, key):
    cfg.ensure_dirs()
    info = media.probe(cfg, sample_video)
    size = (info.width, info.height)
    plain = media.render_preview(cfg, sample_video, info, styles.build_ass([], key, *size), size, 2000, 0, tmp_path / "plain.jpg")
    cue = cues_for()[1]
    delta = 1000 - cue.start_ms
    shifted = st.Cue(cue.start_ms + delta, cue.end_ms + delta, cue.text,
                     [Word(w.text, w.start_ms + delta, w.end_ms + delta) for w in cue.words])
    mid = cue.words[len(cue.words) // 2]
    at = mid.start_ms + 100
    styled = media.render_preview(cfg, sample_video, info, styles.build_ass([shifted], key, *size), size, at, at + delta, tmp_path / f"{key}.jpg")
    assert styled.stat().st_size > plain.stat().st_size  # text adds detail to the picture


def test_preview_accepts_relative_paths(cfg, sample_video, tmp_path, monkeypatch):
    monkeypatch.chdir(sample_video.parent)
    info = media.probe(cfg, sample_video)
    out = media.render_preview(cfg, sample_video.name, info, styles.build_ass(cues_for(), "classic", 640, 360),
                               (640, 360), 1500, 1500, "rel.jpg")
    assert out.exists()


@pytest.mark.parametrize("key", list(styles.TEMPLATES))
def test_render_video_burns_subtitles_and_reports_progress(cfg, sample_video, tmp_path, key):
    cfg.ensure_dirs()
    info = media.probe(cfg, sample_video)
    seen = []
    out = media.render_video(cfg, sample_video, info, styles.build_ass(cues_for(), key, 640, 360), (640, 360),
                             tmp_path / f"{key}.mp4", progress=seen.append)
    result = media.probe(cfg, out)
    assert (result.width, result.height) == (640, 360) and result.has_audio
    assert abs(result.duration - info.duration) < 0.6
    assert seen and seen[-1] == 1.0 and seen == sorted(seen)
    assert not list(tmp_path.glob("*.partial.mp4"))


def test_render_video_can_downscale(cfg, sample_video, tmp_path):
    cfg.ensure_dirs()
    info = media.probe(cfg, sample_video)
    size = media.output_size(info, 180)
    out = media.render_video(cfg, sample_video, info, styles.build_ass(cues_for(), "classic", *size), size, tmp_path / "small.mp4")
    assert media.probe(cfg, out).height == 180


def test_render_video_reports_ffmpeg_errors(cfg, sample_video, tmp_path):
    cfg.ensure_dirs()
    info = media.probe(cfg, sample_video)
    with pytest.raises(MediaError):
        media.render_video(cfg, tmp_path / "missing.mp4", info, "garbage", (640, 360), tmp_path / "o.mp4")


def test_missing_ffmpeg_gives_a_helpful_message(cfg, sample_video):
    from dataclasses import replace

    broken = replace(cfg, ffprobe_path="/no/such/ffprobe")
    with pytest.raises(MediaError, match="FFmpeg"):
        media.probe(broken, sample_video)
    assert shutil.which("ffmpeg")
