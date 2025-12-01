FROM python:3.11-slim

# Install system dependencies for yt-dlp and ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ ./backend/
COPY static/ ./static/
COPY templates/ ./templates/

# Copy YouTube cookies for authenticated downloads
COPY cookies.txt ./cookies.txt

# Create directories for uploads, transcripts, and data
RUN mkdir -p uploads transcripts data

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run with gunicorn (more workers for RTX 3090 server)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "2", "--timeout", "300", "backend.app:app"]
