FROM python:3.11-slim

# Install system dependencies for yt-dlp, ffmpeg, and Whisper
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install PyTorch CPU version first (smaller, faster for CPU-only servers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install other requirements
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./backend/
COPY static/ ./static/
COPY templates/ ./templates/

# Copy YouTube cookies for authenticated downloads (optional)
COPY cookies.txt* ./

# Create directories for uploads, transcripts, and data
RUN mkdir -p uploads transcripts data

# Environment variables for local Whisper
# Set USE_LOCAL_WHISPER=true to use local model instead of OpenAI API
# WHISPER_MODEL options: tiny (fastest), base, small (default), medium, large (best quality)
ENV USE_LOCAL_WHISPER=false
ENV WHISPER_MODEL=small

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run with gunicorn
# IMPORTANT: Use single worker (--workers 1) to share transcription_status dict in memory
# Multiple workers have separate memory spaces, causing status polling to fail
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "600", "backend.app:app"]
