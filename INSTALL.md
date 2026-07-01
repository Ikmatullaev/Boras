# Boras — Telegram Notifications + Docker + Video Testing

## What's new

### 1. Telegram Alerts (NEW)
- Background NotificationService watches EventLog
- Sends Telegram message on: target_detected, target_lost, error, disconnected
- Includes photo snapshot when person detected
- Rate-limited (default 30s between same-type notifications)
- Resilient — Telegram failures don't affect core pipeline
- Architecture: EventLog → NotificationService → Provider (Telegram/Email/etc.)

### 2. Docker Deployment (NEW)
- Multi-stage Dockerfile (builder + slim runtime)
- docker-compose.yml with auto-restart + healthcheck
- .env.example template for credentials
- .dockerignore to keep image small

### 3. Video Batch Testing (NEW)
- scripts/test_videos.py — runs YOLO on all .mp4 in test_videos/
- Saves annotated videos (with green boxes)
- Generates results.csv + results.json with metrics
- Prints summary table to terminal

## Total tests: 251 passed (was 223, +28 new for notifications)

## How to install

```bash
cd ~/Desktop/Boras
unzip ~/Downloads/boras_telegram_docker.zip
cp -r boras_telegram_docker/* .
cp boras_telegram_docker/.gitignore .
cp boras_telegram_docker/.dockerignore .
cp boras_telegram_docker/.env.example .
rm -rf boras_telegram_docker
python3 -m pytest tests/
```
Expected: `251 passed`

## Setting up Telegram alerts

### 1. Create bot
1. Open Telegram, search for @BotFather
2. Send /newbot, follow prompts
3. Get the bot token (format: "123456789:ABCdefGHIjkl...")

### 2. Get your chat_id
1. Send any message to your new bot
2. Open in browser: https://api.telegram.org/bot<TOKEN>/getUpdates
3. Find "chat":{"id": <NUMBER>} — that's your chat_id

### 3. Configure Boras

Option A — environment variables (recommended):
```bash
export CRANE_TELEGRAM_TOKEN="123456789:ABCdefGHIjkl..."
export CRANE_TELEGRAM_CHAT_ID="123456789"
uvicorn app:app --host 0.0.0.0 --port 8000
```
Notifications auto-enable when both are set.

Option B — config_local.py:
```python
# Add to config_local.py
import os
os.environ["CRANE_TELEGRAM_TOKEN"] = "your_token"
os.environ["CRANE_TELEGRAM_CHAT_ID"] = "your_chat_id"
```

### 4. Test
1. Start server: uvicorn app:app
2. Enable auto-guard in web panel
3. Walk in front of camera
4. Within 2-3 seconds you should get Telegram message with photo

## Video batch testing

### 1. Put videos in test_videos/
```bash
cp /path/to/your/videos/*.mp4 test_videos/
```

### 2. Run
```bash
python3 scripts/test_videos.py
```

### 3. Check results
- test_videos/results.csv — table with metrics per video
- test_videos/results.json — detailed report
- test_videos/annotated/ — videos with YOLO boxes drawn

### Options
```bash
python3 scripts/test_videos.py --model yolov8s.pt    # more accurate model
python3 scripts/test_videos.py --conf 0.3            # lower threshold
python3 scripts/test_videos.py --no-annotate         # skip annotated videos
python3 scripts/test_videos.py --verbose             # per-frame progress
```

## Docker deployment

### 1. Create .env file
```bash
cp .env.example .env
# Edit .env with your credentials
```

### 2. Build and run
```bash
docker compose up
```

### 3. Open browser
http://localhost:8000/

### Notes
- YOLO weights download on first build (adds ~6MB)
- Container auto-restarts on crash
- Healthcheck hits /api/status every 30s
- test_videos/ folder is mounted as volume

## What changed

### New files
- services/notifications/__init__.py
- services/notifications/base.py — NotificationProvider abstract
- services/notifications/telegram_provider.py — Telegram bot integration
- services/notification_service.py — background thread, rate limiting
- tests/test_notifications.py — 28 tests
- scripts/test_videos.py — video batch testing
- test_videos/README.md
- Dockerfile — multi-stage build
- docker-compose.yml
- .dockerignore
- .env.example

### Modified files
- config.py — added NotificationConfig section
- app_compose.py — wires NotificationService
- app.py — starts/stops NotificationService in lifespan
- services/vision_service.py — added get_snapshot() for Telegram photos
- tests/test_app_compose.py — updated for notifications key
- scripts/ci.sh — sets fake env vars for CI
- .gitignore — added .env, .env.local
