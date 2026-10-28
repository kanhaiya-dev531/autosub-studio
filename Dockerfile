# AutoSub Studio website. Build for hosting (small, uses Groq for speech recognition):
#   docker build -t autosub .
# Build with the local Whisper engine included:
#   docker build --build-arg REQUIREMENTS=requirements.txt -t autosub-local .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# FFmpeg (with libass for burning subtitles) and fontconfig (libass finds fonts through it)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ARG REQUIREMENTS=requirements-cloud.txt
COPY requirements.txt requirements-cloud.txt ./
RUN pip install -r ${REQUIREMENTS}

COPY . .
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app

ENV DATA_DIR=/data \
    PORT=10000
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '10000'), timeout=4)"

# One worker process on purpose: the job queue and progress live in memory, so every request must reach the
# same process. Threads give concurrency for uploads, polling and downloads.
CMD ["sh", "-c", "exec gunicorn wsgi:app --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 120 --access-logfile -"]
