# Notes for the project report and viva

## In one paragraph

AutoSub Studio converts a video into subtitles. The speech is recognised with Whisper, every word gets a start and end time, and the person can edit the lines, fix the timing (shift, stretch, automatic matching) and export `.srt`. After upload they choose one of three caption templates (Classic, Pop, Neon) like in CapCut; the chosen style is burned into the video with FFmpeg. It exists as a Flask website and a tkinter desktop app that share the same engine and a MySQL database.

## Mapping to the brief (notebook page)

| Brief | Where |
|---|---|
| video to subtitle (.srt) | `autosub/transcribe.py`, `autosub/subtitles.py` (`format_srt`) |
| timestamp detection | word-level timestamps from Whisper, stored in `cues.words_json` |
| sync subtitles | `autosub/subtitles.py`: `shift`, `stretch`, `sync_anchors`, `auto_sync` |
| website and desktop app | `web/` and `desktop/` over the shared `autosub/` |
| extra: templates | `autosub/styles.py`, `autosub/preview.py` |
| MySQL | `schema/mysql_schema.sql`, `autosub/db.py`, `autosub/repository.py` |
| deployed, free | `Dockerfile`, `render.yaml`, `docs/DEPLOY.md` |

## Design decisions worth explaining

- **One engine, two interfaces.** All logic is in `autosub/`; the website and the desktop app only collect input and show results. That is why there are no duplicate rules and why the engine is easy to test.
- **Layers.** `repository.py` is the only place with SQL; `service.py` is the only place that coordinates work; the web routes are thin.
- **MySQL and SQLite.** One small interface (`db.py`) with `?` placeholders translated for each. SQLite makes the first run and the tests effortless; MySQL is the real target. The same tests run against both (`tests/test_backends.py`).
- **Database design.** Four normalised tables, `users`, `jobs`, `cues`, `renders` (the words of a cue are kept as JSON inside the cue row, a deliberate trade for speed); foreign keys with `ON DELETE CASCADE`; `utf8mb4` so every language and emoji are stored; `MEDIUMTEXT` for transcripts because a long video exceeds the 64 KB of `TEXT`.
- **Background jobs.** Transcription and rendering take seconds to minutes, so they run in a thread pool and the page polls for progress. One worker process, because the queue is in memory (a larger system would use Celery or RQ).
- **Same renderer for preview and output.** The thumbnails are real frames from your video with the real subtitle renderer, so what you choose is what you get.
- **Hosted speech recognition when deployed.** A free server cannot run Whisper, so the deployment uses Groq's hosted Whisper. The engine is an interface with three implementations (local, Groq, mock), chosen by one setting.

## Algorithms

- **Grouping words into lines**: break at long pauses, sentence ends, and length limits.
- **Automatic sync**: normalise words, find runs of at least three identical words between the subtitle file and what was heard (`difflib.SequenceMatcher`), then fit `heard = scale * subtitle + offset` by least squares, dropping outliers and re-fitting. `offset` is the shift, `scale` the stretch. Refuses (and says why) when too few words match.
- **ASS captions**: each template generates Advanced SubStation Alpha text with per-word events or `\kf` fills, glow and scale animation. Sizes derive from the video height so portrait and landscape both look right.

## Testing

191 automated tests: unit tests for the subtitle maths, rendering tests with real FFmpeg, database tests, fake speech engines (including a local fake of Groq's HTTP API), every website route (CSRF, ownership, limits), desktop behaviour through a fake toolkit, plus 3 optional browser tests that click through the website in Chromium. CI runs them on every push, with a real MySQL service.

## Security measures

Salted password hashes; parameterised SQL; CSRF tokens; Content-Security-Policy without inline scripts; other users' projects return 404; login rate limit; upload limits; open-redirect protection on `?next=`.

## Likely questions

- **Why Whisper?** Good accuracy including Hindi and other Indian languages, free, works offline, gives word timestamps.
- **Why not store the video in the database?** Large binary files slow a database down; the database holds the paths and everything searchable, the files stay on disk.
- **Why 404 instead of 403 for someone else's project?** A 403 confirms the id exists.
- **What if two people upload at once?** Each upload gets a random id and its own row; at most three videos per person are processed at a time, and the pool runs them in turn.
- **How do you know sync is accurate?** A test makes a subtitle file 3.2 s late and 4 % slow, runs the sync and checks every line is within 60 ms of the truth (37 ms measured).
- **What would you add next?** E-mail verification and password reset, more templates, translation, speaker colours, a proper job queue.
