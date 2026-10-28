# First-run checklist

The project was built and tested in an environment where some things could not be run: a real MySQL server, the real Whisper model, the live Groq service, a Docker daemon, and a real desktop window. Everything else (191 automated tests, 3 browser tests that drive the real website in Chromium, real FFmpeg rendering) was run. Tick these off on your own computer, in this order. Each step says what you should see.

## 1. Setup

- [ ] `python scripts/doctor.py` ends with **All good**. If a line says FAIL it tells you what to install.
- [ ] `pytest` shows **191 passed, 3 skipped** (about 30 s). The 3 skipped are the optional browser tests; `pip install playwright`, `playwright install chromium` and they run too (**194 passed**).

## 2. Website with real speech recognition (SQLite)

- [ ] `python run_web.py`, open http://127.0.0.1:5000.
- [ ] Upload a 20-30 second video with clear speech. Progress moves; the first time it downloads the Whisper model (about 150 MB), so wait a few minutes.
- [ ] The lines match what is said, with sensible times. Click a line number: the video jumps there.
- [ ] Pick **Pop**, press **Render video with Pop**, then **Play**. The words light up as spoken.
- [ ] Try a Hindi video with the language set to **Hindi**.
- [ ] **Fix the timing**: shift by +1 s, check the lines moved. Import a deliberately 3 seconds late `.srt` of the same video with *Match timing to the speech* ticked.

## 3. Real MySQL

- [ ] Create the database (`CREATE DATABASE autosub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;`), set the `DB_*` values in `.env`, run `python scripts/init_db.py`: **Ready: MySQL** and four tables.
- [ ] `python run_web.py` again and repeat one upload. In MySQL Workbench or `mysql`, `SELECT * FROM jobs;` shows your project.
- [ ] Database tests on MySQL (see the README for the environment variables): `pytest tests/test_backends.py -v` shows **20 passed** (10 tests on SQLite and 10 on MySQL).

## 4. Desktop app

I could check the logic of the desktop app (24 tests through a stand-in for tkinter) but not how the real window looks. `python run_desktop.py` and look for:

- [ ] The window opens, sizes sensibly, and nothing overlaps. Pick a video: it appears in *Projects* with a percentage that reaches Ready.
- [ ] The three style thumbnails appear.
- [ ] Double-click a line: the edit window opens. **Split at cursor** works. **Save** updates the list.
- [ ] *Timing* menu: shift, stretch, match to speech, import `.srt`.
- [ ] **.srt** saves a file you can open in VLC with the video. **Render video** shows progress and then asks where to save the MP4.
- [ ] If something looks off, the code is in `desktop/app.py` (`_build_layout` for the arrangement).

## 5. Docker (optional)

- [ ] `docker compose up --build`, then http://localhost:5000 works with MySQL running in a container.

## 6. Online deployment

- [ ] Follow [DEPLOY.md](DEPLOY.md). `https://…/healthz` returns `{"ok": true}`; upload a short video, edit, render.
- [ ] Put the live link and the three screenshots in the GitHub README.

## 7. Before you share it

- [ ] Your name in `LICENSE`.
- [ ] Never commit `.env` (it is in `.gitignore`). If a key was ever committed, create a new one.
- [ ] After pushing to GitHub, the **Actions** tab shows a green check.
