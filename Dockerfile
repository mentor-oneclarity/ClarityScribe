# Multi-stage Dockerfile for AI Meeting Bot
# Stage 1: Python build environment
FROM python:3.14-slim-bookworm AS builder

WORKDIR /app

# Install system dependencies for Playwright and audio
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libglib2.0-0 \
    libx11-6 \
    libxext6 \
    libxrandr2 \
    libdbus-1-3 \
    libxshmfence1 \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# Copy requirements and install Python dependencies + bake Chromium into image
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install chromium


# Stage 2: Runtime environment
FROM python:3.14-slim-bookworm

WORKDIR /app

# Install runtime dependencies + curl (required for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2 \
    libportaudio2 \
    libglib2.0-0 \
    libx11-6 \
    libxext6 \
    libxrandr2 \
    libdbus-1-3 \
    libxshmfence1 \
    ca-certificates \
    curl \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg \
    xvfb \
    x11-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment and baked Playwright browsers from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/playwright-browsers /opt/playwright-browsers

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# Install all Chromium system dependencies as root (playwright handles the full list)
RUN /opt/venv/bin/playwright install-deps chromium

# Create non-root user
RUN useradd -m -u 1000 appuser

# Pre-create /tmp/.X11-unix as root with sticky-bit permissions.
# Xvfb requires this directory to exist when running as non-root (euid != 0).
# Without this, Xvfb fails silently: Chrome connects to a broken display,
# audio pipeline stays disabled, and all transcription returns "empty".
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# Copy application code
COPY --chown=appuser:appuser . .

# Create necessary directories
RUN mkdir -p uploads bot/bot_browser_profile logs && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:6050/health || exit 1

# Expose port
EXPOSE 6050

# Run startup script
CMD ["bash", "startup.sh"]
