import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import API_TOKEN, settings
from app_compose import compose_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("crane.app")

# A2: All component wiring lives in compose_app() now — no more ad-hoc globals.
# This guarantees events/metrics/trace are shared across all components
# (regression fix for Phase 1 bug where they were only passed to VisionRuntime).
_components = compose_app()
events = _components["events"]
metrics = _components["metrics"]
trace = _components["trace"]
state_machine = _components["state_machine"]
camera = _components["camera"]
ptz = _components["ptz"]
brain = _components["brain"]
runtime = _components["runtime"]
operator = _components["operator"]
notifications = _components["notifications"]
event_store = _components["event_store"]
# Part B: Telegram bot components (may be None if aiogram not installed)
telegram_bot = _components.get("telegram_bot")
auth_manager = _components.get("auth_manager")
camera_registry = _components.get("camera_registry")
camera_settings_store = _components.get("camera_settings_store")

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    valid_user = secrets.compare_digest(credentials.username, settings.web.auth_username)
    valid_pass = secrets.compare_digest(credentials.password, API_TOKEN)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime.start()
    notifications.start()
    # Part B: start interactive Telegram bot (if configured)
    if telegram_bot and settings.notifications.bot_enabled:
        telegram_bot.start()
    yield
    # Shutdown: stop bot first, then notifications, then runtime
    if telegram_bot and telegram_bot.is_running:
        telegram_bot.stop()
    notifications.stop()
    runtime.stop()


app = FastAPI(lifespan=lifespan, dependencies=[Depends(require_auth)])


@app.get("/")
def read_root():
    return FileResponse("index.html")


@app.get("/stream")
def stream():
    return StreamingResponse(runtime.mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=--frame")


@app.get("/api/status")
def get_status():
    return runtime.status()


@app.get("/api/history")
def get_history(limit: int = 100, name: str = None):
    """SQLite-backed event history. Survives server restarts.

    Query params:
        limit: number of events to return (default 100, max 1000)
        name:  filter by event name (e.g. "target_detected")
    """
    limit = max(1, min(limit, 1000))
    events = event_store.get_recent(limit=limit, name_filter=name)
    return {
        "total": event_store.count(name_filter=name),
        "returned": len(events),
        "events": events,
    }


@app.delete("/api/history")
def clear_history():
    """Delete all event history. Use with caution."""
    deleted = event_store.clear()
    return {"deleted": deleted}


@app.get("/history")
def history_page():
    """Web UI for browsing event history."""
    return FileResponse("history.html")


@app.get("/api/move")
def manual_move(direction: str):
    # Delegate to OperatorService for consistent manual command handling
    return operator.move(direction)


@app.get("/api/zoom")
def manual_zoom(direction: str):
    # Delegate to OperatorService for zoom control
    return operator.zoom(direction)


@app.get("/api/focus")
def manual_focus(direction: str):
    # Delegate to OperatorService for focus control
    return operator.focus(direction)




@app.post("/api/toggle_guard")
def toggle_guard():
    return {"status": runtime.toggle_guard()}


# ─── Part B: Telegram WebApp endpoints ────────────────────────────────────
# The WebApp mini-app is served at /webapp (HTML/JS) and uses the same
# HTTP Basic Auth as the rest of the panel. Telegram WebAppInfo opens this
# URL inside Telegram's iframe.

@app.get("/webapp")
def webapp_page():
    """Telegram WebApp mini-app — embedded control panel inside Telegram.

    Same HTTP Basic Auth as the main panel. Returns webapp.html.
    """
    return FileResponse("webapp.html")


@app.get("/api/cameras")
def list_cameras():
    """List all registered cameras (multi-camera support).

    Returns list of camera entries with current status. Used by WebApp
    to build camera switcher UI.
    """
    if not camera_registry:
        return {"cameras": []}
    cameras = []
    for cam in camera_registry.list_all():
        try:
            mode = cam.state_machine.mode.value
            ai = cam.state_machine.auto_guard_enabled
        except Exception:
            mode = "?"
            ai = False
        cameras.append({
            "camera_id": cam.camera_id,
            "name": cam.name,
            "mode": mode,
            "auto_guard": ai,
            "web_url": cam.web_url,
        })
    return {"cameras": cameras}


@app.get("/api/settings")
def get_camera_settings(camera_id: str = None):
    """Get settings for a camera. If camera_id not specified — uses default."""
    if not camera_settings_store:
        return {"settings": {}}
    cam_id = camera_id or settings.notifications.camera_id
    return {"camera_id": cam_id, "settings": camera_settings_store.get_all(cam_id)}


@app.post("/api/settings")
def set_camera_setting(camera_id: str = None, key: str = "", value: str = ""):
    """Set a camera setting via API (alternative to Telegram bot settings menu).

    Only keys in CameraSettingsStore.ALLOWED_KEYS are accepted.
    """
    if not camera_settings_store:
        raise HTTPException(status_code=503, detail="Settings store not available")
    cam_id = camera_id or settings.notifications.camera_id
    if not key:
        raise HTTPException(status_code=400, detail="key parameter required")
    # Try to parse value as JSON (bool/int/float/str)
    import json
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value
    try:
        camera_settings_store.set(cam_id, key, parsed)
        return {"status": "ok", "camera_id": cam_id, "key": key, "value": parsed}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
