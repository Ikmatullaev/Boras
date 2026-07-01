# Dockerfile for Boras AI Security System
#
# Multi-stage build: builder stage installs heavy ML deps (torch, ultralytics),
# final stage copies them into a slim runtime image.
#
# Build:    docker build -t boras .
# Run:      docker run -p 8000:8000 --env-file .env boras
# Compose:  docker compose up

# ─── Builder stage: install dependencies with caching ──────────────────────
FROM python:3.12-slim AS builder

# System deps for OpenCV + ONVIF
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ─── Runtime stage: slim image with only what we need ──────────────────────
FROM python:3.12-slim AS runtime

# Runtime libs for OpenCV (libGL) + libglib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy application code
COPY . .

# YOLO weights download automatically on first run via ultralytics
# Pre-download to avoid delay at runtime (optional, comment out for smaller image)
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" || true

EXPOSE 8000

# Healthcheck: hit /api/status every 30s, allow 5s timeout, 3 retries
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/status', timeout=3)" || exit 1

# Run with uvicorn (no --reload in production)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
