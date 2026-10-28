# Deploying AutoSub Studio for free

Goal: the website running at a public `https://…` address, using a **MySQL** database, with no paid services.

```
Browser ──► Render (free web service, Docker) ──► MySQL in the cloud (TiDB Cloud or Aiven, free)
                          │
                          └──► Groq (free hosted Whisper) for the speech recognition
```

Why this shape: a free web server is tiny (see "Limits" below), so it cannot run a Whisper model. The deployed site therefore sends the *audio* to Groq's hosted Whisper (`TRANSCRIBER=groq`). On your own computer, and in the desktop app, Whisper runs locally (`TRANSCRIBER=local`) and nothing leaves your machine.

> **Free tiers change.** The numbers below were checked on 24 October 2026 from the providers' own documentation (linked in "Sources"), except where noted. Check each provider's page again before you rely on them, and mention in your report that the deployment uses free tiers.

## 0. Before you start

1. Push the project to a GitHub repository (public is fine; `.env` and `data/` are already in `.gitignore`, so no secrets are uploaded).
2. Put your own name in `LICENSE`.
3. Make sure it works locally first: `python scripts/doctor.py` and `python run_web.py`.

## 1. A free MySQL database

Pick **one**. TiDB Cloud is the simpler match for Render.

### Option A: TiDB Cloud Starter (MySQL-compatible)

- Free "Starter" cluster: 5 GiB of storage and 50 million request units a month, no credit card ([docs](https://docs.pingcap.com/tidbcloud/select-cluster-tier)).
- Create an account, create a Starter cluster, open **Connect** and generate a password. Note the **host**, **port** (4000), **user** (it looks like `abc123.root`) and **password**.
- TLS is required. Set `DB_SSL=true`. TiDB's certificate is signed by a public authority, so nothing else is needed.
- Values for Render: `DB_HOST=<host>`, `DB_PORT=4000`, `DB_USER=<user>`, `DB_PASSWORD=<password>`, `DB_NAME=test` (or `autosub` if you create it).

### Option B: Aiven for MySQL (free plan)

- Free plan: 1 CPU, 1 GB RAM, 1 GB storage, up to 76 connections, one per organisation, no expiry. The service may be **powered off when idle**, so the first visit after a long break can fail until it is running again ([docs](https://aiven.io/docs/products/mysql/concepts/mysql-free-tier)).
- Create the service and note host, port, user (`avnadmin`), password and database (`defaultdb`).
- Aiven signs with its **own** certificate authority. Download `ca.pem` from the service page, save it in the project as `certs/aiven-ca.pem` (it is a public certificate, safe to commit), and set `DB_SSL=true` and `DB_SSL_CA=/app/certs/aiven-ca.pem`.

### Check the connection from your computer

Put the same values in your local `.env` and run:

```powershell
python scripts/init_db.py
python scripts/doctor.py
```

You should see `Ready: MySQL …` and the four table names. This creates the tables; the app would also do it on its own at start-up.

## 2. A Groq API key

- Create a free account at <https://console.groq.com> and make an API key (API Keys page).
- The site uses `whisper-large-v3-turbo`. On the free tier, at the time of checking: files up to 25 MB, 20 requests per minute, 2,000 requests per day, 7,200 audio-seconds per hour and 28,800 per day ([speech-to-text docs](https://console.groq.com/docs/speech-to-text), [rate limits](https://console.groq.com/docs/rate-limits)).
- That is roughly two hours of audio per hour and eight hours per day for the whole site, plenty for a college project and a demo, not for public traffic. `MAX_VIDEO_SECONDS=240` in `render.yaml` keeps single files small.
- The audio of uploaded videos is sent to Groq. The site footer says so when `TRANSCRIBER=groq`.

## 3. The web service on Render

1. Sign in at <https://render.com> with GitHub.
2. **New → Blueprint**, choose your repository. Render reads [`render.yaml`](../render.yaml).
3. It asks for the values marked `sync: false`: `GROQ_API_KEY`, `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`. If you use TiDB, also change `DB_PORT` to `4000` (Dashboard → your service → Environment). If you use Aiven, add `DB_SSL_CA`.
4. Apply. The first build takes a few minutes (it installs FFmpeg in the image).
5. Open `https://<your-service>.onrender.com/healthz`. You should see `{"ok": true}`. Then upload a short video on the home page.

If something fails, open the service's **Logs**. The app prints clear messages (for example which MySQL host it could not reach).

### What the settings do

| Setting | Value | Why |
|---|---|---|
| `TRANSCRIBER` | `groq` | the free server cannot run Whisper |
| `DB_SSL` | `true` | cloud MySQL requires an encrypted connection |
| `SESSION_COOKIE_SECURE` | `true` | the site is served over HTTPS |
| `SECRET_KEY` | generated | signs login cookies; Render creates a random one |
| `MAX_UPLOAD_MB` | `100` | keeps uploads manageable |
| `MAX_VIDEO_SECONDS` | `240` | short clips only on a tiny server |
| `RENDER_MAX_HEIGHT` | `480` | burning in subtitles at 480p is much faster on a weak CPU |
| `JOB_TTL_HOURS` | `24` | projects and files are deleted after a day (privacy and disk space) |
| `WORKERS` | `1` | one video at a time |

## Limits to expect (and to mention in your viva)

- **Cold starts.** A free Render web service goes to sleep after about 15 minutes without visitors and takes a while to wake up. Open the site a minute before you present. The 750 free hours a month are enough for one always-on service. This comes from a third-party review of Render's free plan ([source](https://jwatte.com/blog/render-com-platform-review.md)); Render's own documentation was not reachable when this was written, so check Render's current free-plan documentation yourself.
- **Small machine.** Around 0.1 CPU and 512 MB RAM (same source). Rendering a minute of video can take a while; that is why the render height is capped.
- **Files do not survive restarts.** The disk is temporary. After a restart or redeploy the *subtitles stay in MySQL*, but the uploaded video and rendered files are gone; the workspace then says the original video is no longer stored and offers the `.srt` downloads. That is the reason for `JOB_TTL_HOURS`.
- **A job interrupted by sleep is marked as failed** with a clear message, and the person uploads again.
- **One process.** The job queue is in memory, so gunicorn runs one worker with several threads (see the `Dockerfile`).

## Alternatives

- **Your own computer for the demo.** Run `docker compose up --build` (MySQL included) or `python run_web.py`. For examiners at a table this is often the most reliable option; deploy online as well so the link works on your resume.
- **Oracle Cloud "Always Free" VM** can run the whole `docker compose` stack with local Whisper and no cold starts. Signing up from India needs a credit or debit card for identity checks, RuPay cards are often declined, and the free ARM machines are frequently out of capacity ([a first-hand account](https://dev.to/ashish-codejourney/getting-a-free-oracle-cloud-vps-from-india-the-full-guide-and-the-rupay-wall-nobody-warns-you-4p5o)). Only choose it if you can complete sign-up.
- **Hugging Face Spaces** is not an option for this project: creating Docker Spaces needs a paid plan ([docs](https://huggingface.co/docs/hub/en/spaces-overview)).

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Build fails on Render | Open the build log. Check that `Dockerfile` and `requirements-cloud.txt` are in the repository root. |
| `Could not connect to MySQL at …` | Wrong host, port, user or password, or `DB_SSL` is not `true`. TiDB uses port 4000. Test the same values locally with `python scripts/init_db.py`. |
| `SSL … certificate verify failed` (Aiven) | `DB_SSL_CA` must point to Aiven's `ca.pem`, for example `/app/certs/aiven-ca.pem`. |
| Upload works but the status says *failed*: `GROQ_API_KEY is not set` | Add the key in Render's Environment tab and redeploy. |
| *failed*: a rate-limit message | You reached Groq's free limits. Wait a few minutes, or process a shorter video. |
| First page load is very slow | The free service was asleep. Wait about a minute. |
| Everyone is logged out after each restart | `SECRET_KEY` is empty, so the app makes one on the temporary disk and a new one appears after every restart. Keep the `SECRET_KEY` that `render.yaml` generates (Environment tab) so it stays the same. |

## Sources (checked 24 October 2026)

- Aiven free MySQL: <https://aiven.io/docs/products/mysql/concepts/mysql-free-tier>
- TiDB Cloud cluster tiers: <https://docs.pingcap.com/tidbcloud/select-cluster-tier>
- Groq speech to text: <https://console.groq.com/docs/speech-to-text>; rate limits: <https://console.groq.com/docs/rate-limits>
- Render free web service (third-party review): <https://jwatte.com/blog/render-com-platform-review.md>
- Hugging Face Spaces: <https://huggingface.co/docs/hub/en/spaces-overview>
- Oracle Cloud from India: <https://dev.to/ashish-codejourney/getting-a-free-oracle-cloud-vps-from-india-the-full-guide-and-the-rupay-wall-nobody-warns-you-4p5o>
