FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to read YouTube's player code
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt requirements-local-whisper.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional offline transcription: docker compose build --build-arg LOCAL_WHISPER=true
ARG LOCAL_WHISPER=false
RUN if [ "$LOCAL_WHISPER" = "true" ]; then \
        pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
        && pip install --no-cache-dir -r requirements-local-whisper.txt; \
    fi

COPY backend/ ./backend/
COPY static/ ./static/
COPY templates/ ./templates/

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown app:app /app/data
USER app

ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

# A single worker on purpose: job status lives in this process's memory
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--worker-tmp-dir", "/dev/shm", "backend.app:app"]
