"""Click through the website in a real browser (optional).

These tests start the site on a local port with the mock speech engine and drive Chromium with Playwright.
They are skipped automatically when Playwright or its browser is not installed. To run them:

    pip install playwright
    playwright install chromium
    pytest tests/test_browser.py
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import make_config, needs_ffmpeg

sync_api = pytest.importorskip("playwright.sync_api")

from werkzeug.serving import make_server  # noqa: E402

from autosub.service import JobService  # noqa: E402
from web import create_app  # noqa: E402

pytestmark = needs_ffmpeg


@pytest.fixture
def site(tmp_path):
    cfg = make_config(tmp_path)
    service = JobService(cfg)
    server = make_server("127.0.0.1", 0, create_app(cfg, service), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    service.shutdown()


@pytest.fixture
def browser():
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as exc:  # the browser binary is not installed
            pytest.skip(f"Chromium is not available: {exc}")
        yield chromium
        chromium.close()


def watch(page) -> list[str]:
    """Collect everything the browser complains about (console errors, CSP violations, uncaught exceptions)."""
    problems: list[str] = []
    page.on("console", lambda m: problems.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    return problems


def until(page, expression: str, timeout: float = 15.0) -> None:
    """Wait until a JavaScript expression is true.

    Playwright's own wait_for_function evaluates a string with eval(), which the site's Content-Security-Policy
    (no 'unsafe-eval') rightly forbids, so the page is polled with evaluate() instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"Timed out waiting for: {expression}")


def test_home_page_demo_and_gallery(site, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    problems = watch(page)
    page.goto(site + "/")

    seen = set()
    for _ in range(25):  # the demo caption lights up word after word
        seen.add(page.evaluate(
            "[...document.getElementById('demo-caption').children].map(x => x.textContent + (x.classList.contains('on') ? '*' : '')).join(' ')"))
        page.wait_for_timeout(150)
    assert len(seen) >= 4

    page.click('.seg [data-template="neon"]')
    assert "cap--neon" in page.get_attribute("#demo-caption", "class")
    assert page.eval_on_selector_all(".style-grid img", "imgs => imgs.every(i => i.complete && i.naturalWidth > 0)")
    assert problems == []


def test_phone_width_has_no_sideways_scroll(site, browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    for path in ("/", "/dashboard", "/login", "/register"):
        page.goto(site + path)
        assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0, path


def test_upload_edit_sync_and_render(site, browser, sample_video):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    problems = watch(page)

    # upload from the dashboard
    page.goto(site + "/dashboard")
    page.set_input_files("#file", str(sample_video))
    page.wait_for_url("**/p/*")
    page.wait_for_selector("#ws-body:not([hidden])", timeout=60_000)
    page.wait_for_selector(".cue")
    assert page.inner_text("#status-chip") == "Ready"
    cues = page.locator(".cue").count()
    assert cues >= 3
    until(page, "[...document.querySelectorAll('img[data-preview]')].every(i => i.complete && i.naturalWidth > 0)", 60)

    # the live caption follows the playhead
    page.evaluate("const v = document.getElementById('video'); v.muted = true; v.currentTime = 1.0; v.play()")
    until(page, "document.getElementById('live-caption').children.length > 0", 10)
    page.evaluate("document.getElementById('video').pause()")

    # edit, autosave, reload
    page.locator(".cue textarea").first.fill("Edited by the browser test")
    until(page, "document.getElementById('save-state').textContent === 'All changes saved'")
    page.reload()
    page.wait_for_selector(".cue")
    assert page.locator(".cue textarea").first.input_value() == "Edited by the browser test"

    # add and delete a line
    page.click("#add-line")
    assert page.locator(".cue").count() == cues + 1
    page.locator('.cue [data-act="delete"]').last.click()
    assert page.locator(".cue").count() == cues

    # choosing a style updates the .ass link
    page.locator(".tpl", has_text="Neon").click()
    assert page.get_attribute("#download-ass", "href").endswith("template=neon")

    # shift everything by half a second
    page.locator("#sync summary").click()
    page.fill("#shift-seconds", "0.5")
    page.click("#shift-apply")
    until(page, "!/^(|Working…)$/.test(document.getElementById('sync-result').textContent.trim())")
    assert "0.50" in page.inner_text("#sync-result")

    # download the .srt
    with page.expect_download() as download:
        page.click("text=Download .srt")
    assert download.value.suggested_filename.endswith(".srt")

    # render with a style and get a playable link
    page.locator(".tpl", has_text="Pop").click()
    page.click("#render-btn")
    page.wait_for_selector("#renders li a", timeout=120_000)
    assert "Pop style" in page.inner_text("#renders")

    assert problems == []
