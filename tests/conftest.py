"""Shared test fixtures. The tests need FFmpeg (with libass) but no speech model and no MySQL server."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autosub.config import Config
from autosub.service import JobService

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="FFmpeg and FFprobe are not installed")


def _make_video(path: Path, width: int, height: int, seconds: int = 6, audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"gradients=s={width}x{height}:d={seconds}:c0=0x1d2671:c1=0xc33764:speed=0.02"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    if not HAVE_FFMPEG:
        pytest.skip("FFmpeg is not installed")
    return _make_video(tmp_path_factory.mktemp("media") / "landscape.mp4", 640, 360)


@pytest.fixture(scope="session")
def portrait_video(tmp_path_factory) -> Path:
    if not HAVE_FFMPEG:
        pytest.skip("FFmpeg is not installed")
    return _make_video(tmp_path_factory.mktemp("media") / "portrait.mp4", 360, 640)


@pytest.fixture(scope="session")
def silent_video(tmp_path_factory) -> Path:
    if not HAVE_FFMPEG:
        pytest.skip("FFmpeg is not installed")
    return _make_video(tmp_path_factory.mktemp("media") / "silent.mp4", 320, 180, audio=False)


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(data_dir=tmp_path / "data", db_host="", transcriber="mock", secret_key="test-secret", workers=1)
    values.update(overrides)
    return Config.from_env(**values)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def service(cfg):
    svc = JobService(cfg)
    yield svc
    svc.shutdown()


def wait_until(predicate, timeout: float = 60.0, interval: float = 0.1):
    """Poll `predicate` until it returns something truthy (used for background jobs)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("Timed out waiting for a background job")


@pytest.fixture
def owner(service):
    from autosub.accounts import Accounts

    return Accounts(service.repo).create_guest()["id"]


def copy_video(src: Path, dst_dir: Path, name: str = "clip.mp4") -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    shutil.copy2(src, dst)
    return dst


os.environ.pop("DB_HOST", None)  # tests always use SQLite
