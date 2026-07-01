# Boras — Autonomous Perimeter Monitoring Platform

**Why this exists:** Traditional CCTV cameras only record — they don't understand what they see. Boras turns a standard ONVIF PTZ camera into an autonomous security agent that detects people, tracks their movement in real-time, patrols the perimeter when idle, and alerts the owner via Telegram with photo evidence. Built for construction sites, residential perimeters, and any location requiring active visual monitoring without a human operator.

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![Tests](https://img.shields.io/badge/tests-263%20passing-brightgreen.svg)
![YOLOv8](https://img.shields.io/badge/YOLOv8-s-orange.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

---

## Key Features

- **Real-time Detection** — YOLOv8 person tracking with configurable frame skip rate
- **Auto-Guard Mode** — Smooth pan/tilt/zoom aiming with deadzone suppression
- **Smart Patrol** — Automatic perimeter scanning (zoom-out → pan → pause cycle) when no targets
- **Telegram Alerts** — Instant notifications with photo snapshots when person detected
- **Event History** — SQLite-backed persistent log, survives server restarts
- **Soft Manual Override** — Manual commands pause auto-guard for 10s, then auto-resume
- **Multi-URL RTSP Fallback** — Tries 4 RTSP path templates (MAIN/SUB/Hikvision/Dahua)
- **PTZ Throttle Bypass** — Instant response on direction change (no 150ms lag)
- **Connection Health** — Separate RTSP and PTZ HTTP status monitoring
- **Tracking Trace** — 5-stage debug recorder (YOLO → target → auto_aim → ptz_command → ptz_http)
- **Rolling FPS** — Last-N-frames window for accurate performance monitoring
- **Configurable** — All tuning parameters in `Settings` dataclass, env-overridable
- **263 Tests** — Full pytest suite running in ~7s without camera/network/YOLO weights

---

## AI Model Benchmarks

Tested on 10 real 4K construction site videos using `scripts/test_videos.py`:

| Video | Resolution | Duration | Detection Rate | Avg Confidence | Max People | Processing FPS |
|-------|-----------|----------|----------------|----------------|------------|----------------|
| Test1 | 2160x3840 | 5.9s | 100.0% | 0.488 | 10 | 14.7 |
| Test2 | 3840x2160 | 12.8s | 100.0% | 0.572 | 10 | 10.5 |
| Test3 | 3840x2160 | 19.8s | 100.0% | 0.671 | 9 | 10.0 |
| Test5 | 3840x2160 | 10.7s | 95.8% | 0.564 | 4 | 11.8 |
| Test6 | 3840x2160 | 10.2s | 99.6% | 0.483 | 10 | 10.8 |
| Test7 | 3840x2160 | 8.6s | 98.1% | 0.371 | 15 | 11.4 |
| Test8 | 2160x3840 | 61.9s | 100.0% | 0.532 | 18 | 11.4 |
| Test9 | 3840x2160 | 25.4s | 100.0% | 0.596 | 10 | 10.7 |
| Test10 | 3840x2160 | 12.3s | 80.2% | 0.503 | 5 | 14.0 |
| Test11 | 3840x2160 | 29.1s | 100.0% | 0.647 | 8 | 12.8 |

**Summary:**
- **Average detection rate:** 97.4%
- **Average confidence:** 0.543
- **Average processing speed:** 11.8 FPS (on CPU, 4K input)
- **Max simultaneous people detected:** 18

### Model Comparison

| Model | Detection Rate | Avg Confidence | CPU FPS | Use Case |
|-------|---------------|----------------|---------|----------|
| yolov8n | Lower (not benchmarked) | Lower | 18.5 | Too weak for 4K/distant objects |
| **yolov8s** | **97.4%** | **0.543** | **11.8** | **Recommended for CPU deployment** |
| yolov8m | Higher (not benchmarked) | Higher | ~6 | GPU server recommended |

**Conclusion:** `yolov8n` completely fails on 4K construction site footage (0% detection). `yolov8s` achieves 97.4% detection rate at 11.8 FPS on CPU — sufficient for real-time PTZ tracking. For 24/7 production deployment on a GPU server, `yolov8m` is recommended for higher accuracy.

---

## Architecture

```
                     ┌──────────────────────────────────────────┐
                     │              compose_app()                │
                     │         (DI container in app.py)          │
                     └──────────────────────────────────────────┘
                                       │
         ┌─────────────────┬───────────┼───────────┬─────────────────┐
         ▼                 ▼           ▼           ▼                 ▼
   ┌──────────┐     ┌──────────┐ ┌──────────┐ ┌──────────┐   ┌──────────┐
   │  Camera  │     │   PTZ    │ │  Brain   │ │  State   │   │ Operator │
   │  Stream  │     │  Service │ │  (YOLO)  │ │  Machine │   │ Service  │
   └────┬─────┘     └────┬─────┘ └────┬─────┘ └────┬─────┘   └────┬─────┘
        │                │            │            │              │
        │   shared       │   shared   │   shared   │              │
        │   events,      │   events,  │   events,  │              │
        │   metrics,     │   metrics, │   metrics, │              │
        │   trace ◄──────┴────────────┴────────────┘              │
        │                                                            │
        │           ┌────────────────────────────────────┐         │
        │           │           EventLog                  │         │
        │           │  ┌──────────────────────────────┐  │         │
        │           │  │  Listeners:                   │  │         │
        │           │  │  • EventStore (SQLite)        │  │         │
        │           │  │  • NotificationService (TG)   │  │         │
        │           │  └──────────────────────────────┘  │         │
        │           └────────────────────────────────────┘         │
        ▼                                                            ▼
   RTSP stream                                                  /api/move
   /api/status                                                  /api/zoom
   /stream                                                      /api/focus
                                                                /api/toggle_guard
```

All components share the same `EventLog`, `RuntimeMetrics`, and `TrackingTrace` instances. `EventLog` broadcasts to `EventStore` (SQLite persistence) and `NotificationService` (Telegram alerts) via listener pattern — neither breaks the core pipeline on failure.

---

## State Machine

```
                 ┌──────┐
                 │ IDLE │ ◄─────────────────────────┐
                 └──┬───┘                           │
                    │ enable_auto_guard             │ disable_auto_guard
                    ▼                               │
                 ┌──────┐                           │
        ┌──────► │PATROL│ ◄─────────────┐           │
        │        └──┬───┘               │           │
        │           │ target_detected   │ target_   │
        │           ▼                   │ lost      │
        │        ┌──────┐               │           │
        │        │TRACK │ ──────────────┘           │
        │        │ ING  │                           │
        │        └──────┘                           │
        │                                           │
        │   manual command (10s timeout)            │
        │   ┌──────┐                                 │
        └───┤MANUAL│ ────────────────────────────────┘
            └──────┘
              │
              │ timeout expired (auto-return)
              └────────────────────────────────────► PATROL
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- PTZ camera with ONVIF support
- RTSP stream access

### Installation

```bash
git clone https://github.com/umarkhanovv/Boras.git
cd Boras

python -m venv ai_env
source ai_env/bin/activate    # Linux/macOS
# ai_env\Scripts\activate     # Windows

pip install -r requirements.txt
pip install pytest
```

### Configuration

```bash
cp config_local.example.py config_local.py
```

Edit `config_local.py`:

```python
CAMERA_IP = "10.0.0.1"                    # your camera IP
CAMERA_USER = "admin"                     # camera ONVIF username
CAMERA_PASS = "your-camera-password"      # camera password

# Web panel password (NOT the camera password)
API_TOKEN = "your-long-random-string"
```

> `config_local.py` is gitignored — credentials never get committed.

### Run

```bash
uvicorn app:app --reload
```

Open http://127.0.0.1:8000/ — login with `admin` / your `API_TOKEN`.

---

## Telegram Alerts Setup

1. **Create bot:** Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts. Get the bot token.

2. **Get chat_id:** Send any message to your new bot, then open in browser:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Find `"chat":{"id": <NUMBER>}` — that's your chat_id.

3. **Configure Boras:**
   ```bash
   export CRANE_TELEGRAM_TOKEN="your_bot_token"
   export CRANE_TELEGRAM_CHAT_ID="your_chat_id"
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```
   Notifications auto-enable when both are set.

4. **Test:** Enable auto-guard in web panel, walk in front of camera. Within 2-3 seconds you'll receive a Telegram message with photo snapshot.

**Notification triggers:**
- `target_detected` — person entered frame (with photo)
- `target_lost` — person left frame, returning to patrol
- `error` — system error
- `disconnected` — RTSP stream lost

Rate limited: max 1 notification per 30s per event type (configurable).

---

## Deployment

### Option A: Docker (recommended for production)

```bash
# Create .env file with credentials
cp .env.example .env
# Edit .env with your camera IP, password, Telegram token, etc.

# Build and run
docker compose up
```

- Multi-stage Dockerfile (slim runtime image)
- Auto-restart on crash (`restart: unless-stopped`)
- Healthcheck every 30s
- YOLO weights cached in volume (no re-download on restart)
- `test_videos/` mounted as volume for batch testing

### Option B: Direct uvicorn (development)

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

### Option C: systemd service (Linux server)

Create `/etc/systemd/system/boras.service`:
```ini
[Unit]
Description=Boras AI Security System
After=network.target

[Service]
Type=simple
User=boras
WorkingDirectory=/opt/boras
EnvironmentFile=/opt/boras/.env
ExecStart=/opt/boras/ai_env/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable boras
sudo systemctl start boras
```

---

## Hardware Recommendations

| Platform | Model | Expected FPS | Use Case |
|----------|-------|-------------|----------|
| Raspberry Pi 5 | yolov8n | ~5 FPS | Testing only (too weak for 4K) |
| Mini PC (Intel N100) | yolov8s | ~12 FPS | Home, small perimeter |
| **Server with GPU (RTX 3060+)** | **yolov8s/m** | **30+ FPS** | **Construction site, 24/7** |
| Jetson Orin Nano | yolov8s | ~25 FPS | Edge device at camera |

**For production 24/7 deployment:** Server with GPU + yolov8s or yolov8m. CPU-only deployment works for prototyping but won't keep up with 4K streams in real-time.

---

## Configuration

All tuning parameters live in `config.py` as a `Settings` dataclass with 8 sections:

| Section | Purpose |
|---|---|
| `camera` | RTSP paths, ports, reconnect delay |
| `ptz` | ONVIF profile, throttle interval, HTTP timeout, focus control toggle |
| `vision` | YOLO model, detect classes, frame skip rate, JPEG quality |
| `tracking` | Pan/tilt/zoom speeds, deadzone, height ratio targets |
| `patrol` | Patrol scan timings, pan speed |
| `web` | Auth username, MJPEG/loop sleep intervals |
| `operator` | Soft manual override timeout |
| `notifications` | Telegram token, chat_id, rate limit, poll interval |

Override via environment variables:

```bash
export CRANE_YOLO_MODEL=yolov8s.pt              # YOLO model (n/s/m/l/x)
export CRANE_FRAME_SKIP_RATE=5                  # process every 5th frame
export CRANE_AUTH_USERNAME=operator             # web panel username
export CRANE_PTZ_PROFILE=PROFILE_001            # ONVIF profile token
export CRANE_PAN_SPEED_GAIN=0.6                 # faster panning
export CRANE_MANUAL_OVERRIDE_TIMEOUT=30         # soft override timeout
export CRANE_DISABLE_FOCUS=1                    # disable focus (unsupported cameras)
export CRANE_TELEGRAM_TOKEN="..."               # Telegram bot token
export CRANE_TELEGRAM_CHAT_ID="..."             # Telegram chat ID
```

---

## API Reference

### `GET /`
Operator console UI (`index.html`). Requires HTTP Basic Auth.

### `GET /stream`
MJPEG video stream with YOLO annotations.

### `GET /api/status`
Full system state as JSON — includes `metrics`, `events`, `tracking_trace`, `connection_health`.

### `GET /api/history?limit=100&name=target_detected`
SQLite-backed event history. Survives server restarts.

### `DELETE /api/history`
Clear all event history.

### `GET /history`
Web UI for browsing event history with filters and auto-refresh.

### `GET /api/move?direction=left|right|up|down|stop`
Manual PTZ control. Triggers soft manual override.

### `GET /api/zoom?direction=in|out|stop`
Manual zoom control.

### `GET /api/focus?direction=near|far|stop`
Manual focus control (disabled if `CRANE_DISABLE_FOCUS=1`).

### `POST /api/toggle_guard`
Toggle auto-guard on/off.

---

## Testing

```bash
# Run all 263 tests
python -m pytest tests/

# Full CI check (compile + tests + e2e smoke test)
bash scripts/ci.sh

# End-to-end smoke test (no camera needed)
python scripts/e2e_trace_check.py

# Video batch testing — runs YOLO on all .mp4 in test_videos/
python scripts/test_videos.py --model yolov8s.pt --conf 0.5
```

Tests run in ~7 seconds without requiring a camera, network, or YOLO weights. Stubs for `ultralytics` and `cv2` are auto-loaded in `tests/conftest.py`.

---

## Project Structure

```
Boras/
├── app.py                       # FastAPI entry, HTTP Basic Auth, endpoints
├── app_compose.py               # DI container (compose_app)
├── config.py                    # Settings dataclass (8 sections)
├── config_local.example.py      # Credentials template
├── conftest.py                  # Pytest root config
├── history.html                 # Event history web UI
├── index.html                   # Operator Console UI
├── Dockerfile                   # Multi-stage build
├── docker-compose.yml           # Production deployment
├── .env.example                 # Docker env template
├── core/
│   ├── events.py                # EventLog + listener pattern
│   ├── event_store.py           # SQLite persistent storage
│   ├── metrics.py               # RuntimeMetrics (rolling FPS)
│   ├── state_machine.py         # CraneStateMachine (IDLE/PATROL/TRACKING/MANUAL)
│   └── tracking_trace.py        # 5-stage debug recorder
├── behavior/
│   ├── tracking.py              # AutoTracker (pan/tilt/zoom aim)
│   └── patrol.py                # SmartPatrol (scan cycle)
├── services/
│   ├── camera_service.py        # RTSP stream + 4 URL fallbacks + health()
│   ├── ptz_service.py           # ONVIF PTZ (SOAP, throttle bypass, health())
│   ├── vision_service.py        # SecurityBrain + VisionRuntime
│   ├── target_manager.py        # Group target from YOLO boxes
│   ├── operator_service.py      # Manual commands
│   ├── notification_service.py  # Background thread, rate limiting
│   └── notifications/
│       ├── base.py              # NotificationProvider abstract
│       └── telegram_provider.py # Telegram bot integration
├── tests/                       # 263 tests
└── scripts/
    ├── ci.sh                    # CI: compileall → pytest → e2e
    ├── e2e_trace_check.py       # Smoke test (no camera)
    └── test_videos.py           # Video batch testing tool
```

---

## Dependencies

- `fastapi` 0.138 — Web framework
- `uvicorn` 0.49 — ASGI server
- `opencv-python` 4.13 — RTSP capture + frame encoding
- `ultralytics` 8.4 — YOLOv8 inference
- `torch` 2.12 + `torchvision` 0.27 — PyTorch backend
- `requests` 2.34 — ONVIF SOAP + Telegram API client

See `requirements.txt` for pinned versions.

---

## Troubleshooting

Use `/api/status` to diagnose issues:

| Symptom | Cause | Fix |
|---|---|---|
| `camera_status: "failed"` | RTSP unreachable | Check IP/credentials/firewall |
| `connection_health.ptz.ptz_reachable: false` | ONVIF HTTP unreachable | Verify port 80 open on camera |
| `connection_health.ptz.last_http_status: 401` | Wrong ONVIF credentials | Check `CAMERA_USER`/`CAMERA_PASS` |
| `tracking_trace.yolo.boxes: 0` | YOLO finds no person | Check lighting, try `yolov8s.pt` |
| `tracking_trace.ptz_http.ok: false` | Camera rejected SOAP | Check `CRANE_PTZ_PROFILE` |
| `metrics.errors` growing | Recurring errors | Check `events` array + `/history` page |
| Telegram not sending | Token/chat_id wrong | Test via `https://api.telegram.org/bot<TOKEN>/getUpdates` |

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Run tests: `bash scripts/ci.sh`
4. Commit changes: `git commit -m 'Add amazing feature'`
5. Push to branch: `git push origin feature/amazing-feature`
6. Open a Pull Request

---

## License

Distributed under the MIT License.

---

## Author

**Umarkhanov Askhat**

---

## Acknowledgments

- [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics) — Real-time object detection
- [ONVIF](https://www.onvif.org/) — Open network video interface standard
- [FastAPI](https://fastapi.tiangolo.com/) — Modern Python web framework
