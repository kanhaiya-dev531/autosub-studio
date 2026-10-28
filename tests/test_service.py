import itertools
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_config, needs_ffmpeg, wait_until

from autosub import media
from autosub.repository import TS_FORMAT
from autosub.service import JobError, JobService
from autosub.transcribe import TranscriptionError

pytestmark = needs_ffmpeg


def upload(service, owner, video, language=None, name="holiday.mp4"):
    job_id, path = service.new_upload_path(name)
    shutil.copy2(video, path)
    return service.create_job(owner, path, name, language, job_id)


def ready_job(service, owner, video):
    job = upload(service, owner, video)
    return wait_until(lambda: (j := service.get_job(job["id"]))["status"] in ("ready", "failed") and j)


def test_upload_is_transcribed_in_the_background(service, owner, sample_video):
    job = upload(service, owner, sample_video, language="auto")
    assert job["status"] in ("uploaded", "transcribing", "ready") and job["language"] is None
    done = wait_until(lambda: (j := service.get_job(job["id"]))["status"] == "ready" and j)
    assert done["progress"] == 100 and done["cue_count"] >= 3 and done["language"] == "en"
    assert (done["width"], done["height"]) == (640, 360)
    cues = service.get_cues(job["id"])
    assert cues[0].words and all(a.end_ms <= b.start_ms for a, b in itertools.pairwise(cues))
    assert service.repo.get_transcript(job["id"])  # kept for auto-sync


def test_video_without_audio_is_ready_immediately(service, owner, silent_video):
    job = upload(service, owner, silent_video)
    assert job["status"] == "ready" and "no audio" in job["message"] and job["cue_count"] == 0


def test_non_video_is_rejected_and_file_removed(service, owner, tmp_path):
    job_id, path = service.new_upload_path("fake.mp4")
    path.write_text("nope")
    with pytest.raises(JobError, match="could not be read"):
        service.create_job(owner, path, "fake.mp4", None, job_id)
    assert not path.exists()


def test_length_limit(tmp_path, sample_video):
    svc = JobService(make_config(tmp_path, max_video_seconds=3))
    try:
        owner = svc.repo and __import__("autosub.accounts", fromlist=["x"]).Accounts(svc.repo).create_guest()["id"]
        with pytest.raises(JobError, match="up to 3 seconds"):
            upload(svc, owner, sample_video)
    finally:
        svc.shutdown()


def test_transcription_failure_marks_job_failed(tmp_path, sample_video):
    class Broken:
        name, audio_codec, max_audio_bytes = "broken", "wav", None

        def transcribe(self, *a, **k):
            raise TranscriptionError("The speech service is down.")

    svc = JobService(make_config(tmp_path), transcriber=Broken())
    try:
        from autosub.accounts import Accounts

        owner = Accounts(svc.repo).create_guest()["id"]
        job = upload(svc, owner, sample_video)
        failed = wait_until(lambda: (j := svc.get_job(job["id"]))["status"] == "failed" and j)
        assert failed["message"] == "The speech service is down."
    finally:
        svc.shutdown()


def test_unexpected_crash_never_leaves_a_job_stuck(tmp_path, sample_video):
    class Crashy:
        name, audio_codec, max_audio_bytes = "crashy", "wav", None

        def transcribe(self, *a, **k):
            raise RuntimeError("boom")

    svc = JobService(make_config(tmp_path), transcriber=Crashy())
    try:
        from autosub.accounts import Accounts

        owner = Accounts(svc.repo).create_guest()["id"]
        job = upload(svc, owner, sample_video)
        failed = wait_until(lambda: (j := svc.get_job(job["id"]))["status"] == "failed" and j)
        assert "boom" in failed["message"]
    finally:
        svc.shutdown()


def test_job_left_over_from_a_dead_server_is_marked_failed(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(TS_FORMAT)
    service.db.execute("UPDATE jobs SET status = 'transcribing', updated_at = ? WHERE id = ?", (old, job["id"]))
    assert service.get_job(job["id"])["status"] == "failed"


def test_editing_keeps_word_timing_when_only_times_change(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    first = service.get_cues(job["id"])[0]
    items = [{"orig": 0, "start": first.start_ms + 500, "end": first.end_ms + 500, "text": first.text}]
    saved = service.save_cues(job["id"], items)
    assert saved[0].words[0].start_ms == first.words[0].start_ms + 500


def test_editing_validates_input(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    with pytest.raises(JobError, match=r"Line 1.*after the start"):
        service.save_cues(job["id"], [{"start": 2000, "end": 1000, "text": "x"}])
    with pytest.raises(JobError, match="Line 1"):
        service.save_cues(job["id"], [{"start": "abc", "end": 1000, "text": "x"}])
    with pytest.raises(JobError, match="Line 1"):
        service.save_cues(job["id"], [{"text": "x"}])
    assert service.save_cues(job["id"], [{"start": 0, "end": 900, "text": "  "}]) == []  # blank lines vanish


def test_sync_offset_stretch_anchors(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    before = service.get_cues(job["id"])
    after, msg = service.apply_sync(job["id"], "offset", delta_ms=400)
    assert after[0].start_ms == before[0].start_ms + 400 and "+0.40" in msg
    after, _ = service.apply_sync(job["id"], "stretch", factor=1.1)
    assert after[0].start_ms == round((before[0].start_ms + 400) * 1.1)
    last = service.get_cues(job["id"])[-1].start_ms
    after, _ = service.apply_sync(job["id"], "anchors", first="0:01.000", last=f"{last // 1000 + 2}")
    assert after[0].start_ms == 1000
    for mode, params in (("offset", {}), ("stretch", {"factor": 9}), ("anchors", {"first": "1", "last": "0.5"}), ("warp", {})):
        with pytest.raises(JobError):
            service.apply_sync(job["id"], mode, **params)
    with pytest.raises(JobError, match="before the start"):
        service.apply_sync(job["id"], "offset", delta_ms=-10_000_000)


def test_auto_sync_repairs_a_shifted_project(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    original = service.get_cues(job["id"])
    service.apply_sync(job["id"], "offset", delta_ms=2500)
    fixed, message = service.apply_sync(job["id"], "auto")
    assert "Matched" in message
    assert max(abs(a.start_ms - b.start_ms) for a, b in zip(fixed, original)) < 150


def test_import_srt_with_and_without_auto_sync(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    original = service.get_cues(job["id"])
    late = "".join(
        f"{i}\n{media_ts(c.start_ms + 3000)} --> {media_ts(c.end_ms + 3000)}\n{c.text}\n\n" for i, c in enumerate(original, 1)
    )
    cues, message = service.import_srt(job["id"], late, auto_sync=True)
    assert "Matched" in message and abs(cues[0].start_ms - original[0].start_ms) < 150
    cues, message = service.import_srt(job["id"], late)
    assert message.startswith("Imported") and cues[0].start_ms == original[0].start_ms + 3000
    with pytest.raises(JobError):
        service.import_srt(job["id"], "garbage")
    foreign = "1\n00:00:01,000 --> 00:00:03,000\nbilkul alag bhasha mein likha hua text\n"
    with pytest.raises(JobError, match="Too few words"):
        service.import_srt(job["id"], foreign, auto_sync=True)


def media_ts(ms):
    from autosub.subtitles import ms_to_srt

    return ms_to_srt(ms)


def test_exports(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    assert service.export_srt(job["id"]).startswith("1\n00:00:00,")
    assert service.export_vtt(job["id"]).startswith("WEBVTT")
    assert "Welcome" in service.export_txt(job["id"])
    ass = service.export_ass(job["id"], "pop")
    assert "PlayResX: 640" in ass and "Dialogue:" in ass
    with pytest.raises(ValueError):
        service.export_ass(job["id"], "nope")


@pytest.mark.parametrize("template", ["classic", "pop", "neon"])
def test_preview_is_cached_and_cleared_by_edits(service, owner, sample_video, template):
    job = ready_job(service, owner, sample_video)
    path = service.ensure_preview(job["id"], template)
    assert path.exists() and path.suffix == ".jpg"
    stamp = path.stat().st_mtime_ns
    assert service.ensure_preview(job["id"], template) == path and path.stat().st_mtime_ns == stamp
    service.apply_sync(job["id"], "offset", delta_ms=100)
    assert not path.exists()


def test_render_produces_a_playable_mp4(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    render = service.start_render(job["id"], "pop")
    done = wait_until(lambda: (r := service.get_render(render["id"]))["status"] in ("done", "failed") and r, timeout=120)
    assert done["status"] == "done" and done["progress"] == 100, done
    info = media.probe(service.cfg, done["output_path"])
    assert info.has_audio and (info.width, info.height) == (640, 360)
    assert [r["id"] for r in service.list_renders(job["id"])] == [render["id"]]


def test_render_respects_height_cap(tmp_path, sample_video):
    svc = JobService(make_config(tmp_path, render_max_height=180))
    try:
        from autosub.accounts import Accounts

        owner = Accounts(svc.repo).create_guest()["id"]
        job = upload(svc, owner, sample_video)
        wait_until(lambda: svc.get_job(job["id"])["status"] == "ready")
        render = svc.start_render(job["id"], "classic")
        done = wait_until(lambda: (r := svc.get_render(render["id"]))["status"] in ("done", "failed") and r, timeout=120)
        assert media.probe(svc.cfg, done["output_path"]).height == 180
    finally:
        svc.shutdown()


def test_render_refuses_bad_requests(service, owner, sample_video, silent_video):
    with pytest.raises(JobError, match="no longer exists"):
        service.start_render("0" * 32, "pop")
    empty = upload(service, owner, silent_video)
    with pytest.raises(JobError, match="no subtitle lines"):
        service.start_render(empty["id"], "pop")
    job = ready_job(service, owner, sample_video)
    with pytest.raises(ValueError):
        service.start_render(job["id"], "sparkle")


def test_expired_video_gives_a_clear_message(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    service.video_file(job).unlink()
    with pytest.raises(JobError, match="no longer stored"):
        service.start_render(job["id"], "pop")
    with pytest.raises(JobError, match="no longer stored"):
        service.ensure_preview(job["id"], "pop")
    assert service.export_srt(job["id"])  # subtitles themselves are still there


def test_delete_removes_files_and_rows(service, owner, sample_video):
    job = ready_job(service, owner, sample_video)
    service.ensure_preview(job["id"], "classic")
    video = service.video_file(job)
    service.delete_job(job["id"])
    assert service.get_job(job["id"]) is None and not video.exists()
    assert not (service.cfg.previews_dir / job["id"]).exists()


def test_files_outside_the_uploads_folder_are_never_deleted(service, owner, sample_video, tmp_path):
    mine = tmp_path / "precious.mp4"
    shutil.copy2(sample_video, mine)
    job = service.create_job(owner, mine, "precious.mp4")
    wait_until(lambda: service.get_job(job["id"])["status"] == "ready")
    service.delete_job(job["id"])
    assert mine.exists()  # the desktop app uses the user's own file in place


def test_old_projects_are_purged_when_a_ttl_is_set(tmp_path, sample_video):
    svc = JobService(make_config(tmp_path, job_ttl_hours=1))
    try:
        from autosub.accounts import Accounts

        owner = Accounts(svc.repo).create_guest()["id"]
        job = upload(svc, owner, sample_video)
        wait_until(lambda: svc.get_job(job["id"])["status"] == "ready")
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(TS_FORMAT)
        svc.db.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (old, job["id"]))
        svc.purge_expired()
        assert svc.get_job(job["id"]) is None
    finally:
        svc.shutdown()


def test_portrait_video_is_probed_and_previewed(service, owner, portrait_video):
    job = ready_job(service, owner, portrait_video)
    assert (job["width"], job["height"]) == (360, 640)
    assert service.ensure_preview(job["id"], "neon").exists()
