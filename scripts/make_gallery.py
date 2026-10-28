"""Create the three style thumbnails shown on the home page (web/static/img/style-*.jpg).

It draws a sunset picture with Pillow, turns it into a short video, and then renders each
subtitle template on a frame with the same code the website uses for its previews.

    python scripts/make_gallery.py
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageChops, ImageDraw, ImageFilter  # noqa: E402

from autosub import media, preview, styles  # noqa: E402
from autosub.config import Config  # noqa: E402
from autosub.subtitles import Cue, redistribute_words  # noqa: E402

WIDTH, HEIGHT = 1280, 720
SENTENCE = "Your words appear exactly when you say them"
OUT_DIR = ROOT / "web" / "static" / "img"


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def draw_scene(path: Path) -> None:
    """A calm dusk landscape: gradient sky, low sun, two hills. Original artwork, no photo licences."""
    top, mid, low = (32, 36, 92), (214, 92, 112), (255, 176, 96)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = ImageDraw.Draw(img)
    horizon = int(HEIGHT * 0.66)
    for y in range(HEIGHT):  # the sky runs behind the hills, so nothing is left unpainted
        t = min(1.0, y / horizon)
        colour = lerp(top, mid, t / 0.6) if t < 0.6 else lerp(mid, low, (t - 0.6) / 0.4)
        px.line([(0, y), (WIDTH, y)], fill=colour)

    # soft warm glow around the sun, added on top of the sky
    glow = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    cx, cy = int(WIDTH * 0.68), horizon - 30
    ImageDraw.Draw(glow).ellipse([cx - 170, cy - 170, cx + 170, cy + 170], fill=(120, 70, 30))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    img = ImageChops.add(img, glow)
    px = ImageDraw.Draw(img)
    px.ellipse([cx - 64, cy - 64, cx + 64, cy + 64], fill=(255, 226, 150))

    def hill(base: int, amp: int, phase: float, colour: tuple[int, int, int]) -> None:
        points: list[tuple[float, float]] = [(0, HEIGHT)]
        for x in range(0, WIDTH + 8, 8):
            points.append((x, base - amp * math.sin(x / 210 + phase) - amp * 0.4 * math.sin(x / 83 + phase * 2)))
        points.append((WIDTH, HEIGHT))
        px.polygon(points, fill=colour)

    hill(horizon + 20, 26, 0.6, (58, 38, 82))
    hill(horizon + 78, 30, 2.4, (28, 22, 54))
    img.save(path, quality=95)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        scene, video = tmp_dir / "scene.png", tmp_dir / "scene.mp4"
        draw_scene(scene)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", str(scene), "-t", "4",
             "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
            check=True,
        )
        cfg = Config.from_env(data_dir=tmp_dir / "data", db_host="", transcriber="mock")
        info = media.probe(cfg, video)
        words = redistribute_words(SENTENCE, 600, 3800)
        cue = Cue(600, 3800, SENTENCE, words)
        for key in styles.TEMPLATES:
            target = OUT_DIR / f"style-{key}.jpg"
            preview.draw_preview(cfg, video, info, [cue], key, target, cue=cue, max_height=360)
            print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
