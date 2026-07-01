"""
config.py — Centralized configuration for Boras Crane Vision.

All tuning parameters live here as dataclass defaults. Credentials continue
to load from environment variables or config_local.py (gitignored).

Module-level CAMERA_IP / CAMERA_USER / CAMERA_PASS / API_TOKEN are preserved
for backward compatibility with existing imports like:
    from config import CAMERA_IP, CAMERA_USER, CAMERA_PASS, API_TOKEN

To override tuning parameters without code changes, set environment variables:
    CRANE_YOLO_MODEL=yolov8s.pt
    CRANE_FRAME_SKIP_RATE=5
    CRANE_AUTH_USERNAME=operator
    CRANE_PTZ_PROFILE=PROFILE_001
    CRANE_MIN_COMMAND_INTERVAL=0.2
    CRANE_PAN_SPEED_GAIN=0.6
    CRANE_MIN_PAN_SPEED=0.1
"""
import os
from dataclasses import dataclass, field
from typing import List


# ─── Credentials (env vars or config_local.py) ─────────────────────────────

CAMERA_IP = os.environ.get("CRANE_CAMERA_IP", "")
CAMERA_USER = os.environ.get("CRANE_CAMERA_USER", "")
CAMERA_PASS = os.environ.get("CRANE_CAMERA_PASS", "")
API_TOKEN = os.environ.get("CRANE_API_TOKEN", "")

# Local-dev override file (not committed) lets you skip exporting env vars.
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass

if not CAMERA_IP or not CAMERA_PASS:
    raise RuntimeError(
        "Camera credentials are not configured.\n"
        "Set CRANE_CAMERA_IP / CRANE_CAMERA_USER / CRANE_CAMERA_PASS as "
        "environment variables, or copy config_local.example.py to "
        "config_local.py and fill in real values (that file is gitignored)."
    )

if not API_TOKEN:
    raise RuntimeError(
        "CRANE_API_TOKEN is not set. Choose a long random string and set "
        "it via env var or config_local.py — it's the password for the "
        "web control panel."
    )


# ─── Tuning parameters (dataclass defaults, env-overridable) ───────────────

@dataclass
class CameraConfig:
    """RTSP connection parameters.

    rtsp_paths is a list of path templates tried in order during connection.
    The first one that opens and returns a frame becomes the working URL.
    Add your camera's specific path here if none of the defaults work.
    """
    rtsp_paths: List[str] = field(default_factory=lambda: [
        "/live/0/MAIN",                                # primary stream (most cameras)
        "/live/0/SUB",                                 # substream fallback
        "/h264/ch1/main/av_stream",                    # Hikvision-style
        "/cam/realmonitor?channel=1&subtype=0",        # Dahua-style
    ])
    rtsp_port: int = 554
    http_port: int = 80
    reconnect_delay: float = 3.0  # seconds between reconnect attempts


@dataclass
class PTZConfig:
    """ONVIF PTZ service parameters."""
    profile: str = "PROFILE_000"             # ONVIF media profile token
    video_source: str = "000"                # ONVIF video source token
    min_command_interval: float = 0.15       # seconds between same-key commands (throttle)
    http_timeout: float = 2.0                # seconds for ONVIF SOAP requests
    # Many cameras don't support ONVIF Imaging service focus control and
    # return HTTP 400. Set to False to skip focus commands entirely
    # (zoom will still work, focus will drift but not break the pipeline).
    enable_focus_control: bool = True


@dataclass
class VisionConfig:
    """YOLO detection and frame processing parameters."""
    yolo_model: str = "yolov8n.pt"           # weights file (yolov8n/s/m/l/x)
    detect_classes: List[int] = field(default_factory=lambda: [0])  # COCO class 0 = person
    frame_skip_rate: int = 3                 # process every Nth frame (1 = every frame)
    jpeg_quality: int = 80                   # MJPEG stream quality (0-100)


@dataclass
class TrackingConfig:
    """AutoTracker aiming parameters.

    All values are in normalized camera-speed units (-1.0 to 1.0) unless noted.
    """
    pan_speed_gain: float = 0.5        # multiplier for offset-to-speed conversion
    min_pan_speed: float = 0.08        # minimum |speed| to overcome deadzone jitter
    deadzone_frac_x: float = 0.15      # fraction of frame width (centered) where no pan happens
    deadzone_frac_y: float = 0.15      # fraction of frame height (centered) where no tilt happens
    height_target_low: float = 0.40    # below this height ratio → zoom in
    height_target_high: float = 0.75   # above this height ratio → zoom out
    zoom_speed: float = 0.15           # continuous zoom speed when adjusting
    focus_speed: float = 0.1           # continuous focus speed when adjusting zoom


@dataclass
class PatrolConfig:
    """SmartPatrol scanning parameters (all times in seconds)."""
    zoom_out_speed: float = -0.5       # zoom speed during initial zoom-out phase
    zoom_out_focus: float = -0.1       # focus speed during initial zoom-out phase
    pan_speed: float = 0.12            # slow pan speed during patrol
    zoom_out_duration: float = 3.0     # seconds to zoom out before starting pan cycle
    cycle_duration: float = 4.0        # seconds per pan-pause cycle
    pan_duration: float = 2.0          # seconds of panning within each cycle (rest is pause)


@dataclass
class WebConfig:
    """Web server / auth parameters."""
    auth_username: str = "admin"       # HTTP Basic Auth username (password = API_TOKEN)
    stream_sleep: float = 0.05         # MJPEG generator sleep between frames (seconds)
    loop_sleep: float = 0.03           # processing loop sleep between frames (seconds)
    no_frame_sleep: float = 0.05       # sleep when camera has no frame yet (seconds)


@dataclass
class OperatorConfig:
    """Operator / manual control parameters (B3 soft manual override)."""
    # After a manual command, auto-guard stays disabled for this many seconds.
    # If no further manual command arrives, auto-guard re-enables automatically.
    # Set to 0.0 to disable soft override (legacy behavior: manual kills guard
    # until operator re-toggles).
    # Bug 3 fix: default lowered from 10s to 5s per spec — if a guard forgets
    # the camera in a bad position, the system recovers in 5 seconds instead
    # of 10. Override via CRANE_MANUAL_OVERRIDE_TIMEOUT env var.
    manual_override_timeout: float = 5.0
    # ─── Bug 2 fix: home-return + zoom-reset behavior ───────────────
    # When manual override timeout expires, return camera to home position
    # (pan=0, tilt=0) before re-enabling auto-guard. Prevents patrol from
    # starting wherever operator left the camera pointing.
    return_to_home_after_manual: bool = True
    # When returning to home, also reset zoom to 1x (wide angle). Without
    # this, camera may keep 25x zoom from manual operation and miss the
    # intruder in the narrow field of view.
    zoom_reset_on_home: bool = True
    # How long to wait (seconds) after goto_home() before re-enabling
    # auto-guard. Gives AbsoluteMove time to physically move the camera
    # back to center before patrol/tracking kicks in. 2.0s is a safe default
    # for most PTZ cameras — adjust if your camera moves slower.
    home_settle_delay: float = 2.0


@dataclass
class NotificationConfig:
    """Notifications via external providers (Telegram, etc.).
    Set enabled=False to disable all notifications.
    Telegram requires both token and chat_id to be set.
    """
    enabled: bool = False                  # master switch
    # Telegram bot token from @BotFather (e.g. "123456:ABC-DEF...")
    telegram_token: str = ""
    # Telegram chat ID to send messages to (e.g. "123456789" for private chat)
    telegram_chat_id: str = ""
    # Camera name shown in notification messages
    camera_name: str = "360 camera"
    # Minimum seconds between notifications (rate limit, prevents spam)
    rate_limit_seconds: float = 30.0
    # How often NotificationService polls EventLog for new events (seconds)
    poll_interval: float = 2.0
    # Which event names trigger a notification
    notify_on: tuple = (
        "target_detected",   # PATROL → TRACKING transition
        "target_lost",       # TRACKING → PATROL transition
        "error",             # any system error
        "disconnected",      # RTSP stream lost
    )
    # ─── Multi-camera + Telegram bot settings (Part B) ───────────────
    # Unique camera ID. Used in multi-camera setups to route notifications
    # and Telegram callbacks to the correct camera. Defaults to "default"
    # for single-camera installs; override via CRANE_CAMERA_ID env var.
    camera_id: str = "default"
    # Password required to authorize a new Telegram chat with the bot.
    # When a user sends /start, they're prompted for this password. After
    # successful auth, their chat_id is added to authorized_chat_ids (persisted
    # in SQLite). Leave empty to disable password protection (NOT recommended).
    bot_password: str = ""
    # Comma-separated list of pre-authorized Telegram chat IDs. These chats
    # get full access without entering the password. Useful for the boss.
    # Example: "123456789,987654321"
    authorized_chat_ids: str = ""
    # Role assigned to new chat IDs after password auth. One of: "boss",
    # "guard", "viewer". Boss = full access (settings + live + control).
    # Guard = live + control + notifications. Viewer = notifications + live.
    default_role: str = "viewer"
    # URL of the Telegram WebApp mini-app (served by Boras itself or separate
    # static host). When user clicks "Open camera" in Telegram, this URL opens
    # inside Telegram's WebApp iframe. Example: "https://boras.example.com/webapp"
    webapp_url: str = ""
    # Public base URL of the Boras web panel (for inline "Access" buttons).
    # Example: "https://boras.example.com". Leave empty to use localhost.
    public_base_url: str = ""
    # Whether to enable the Telegram bot (long-polling for /start, callbacks,
    # /status, /snapshot, live-stream, settings menu). When False, only the
    # NotificationService (outbound alerts) runs — no interactive bot.
    bot_enabled: bool = True
    # Interval (seconds) between live-stream frames sent to Telegram chats
    # that requested "live on". Lower = smoother but more API calls + traffic.
    live_stream_interval: float = 2.0
    # Redis URL for bot state (sessions, authorized chats, settings cache).
    # Required for multi-instance deployments. Single-instance can use "".
    # Example: "redis://localhost:6379/0"
    redis_url: str = ""


@dataclass
class Settings:
    """Top-level settings container. Access sections via settings.camera,
    settings.ptz, settings.vision, settings.tracking, settings.patrol, settings.web,
    settings.operator, settings.notifications.
    """
    camera: CameraConfig = field(default_factory=CameraConfig)
    ptz: PTZConfig = field(default_factory=PTZConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    patrol: PatrolConfig = field(default_factory=PatrolConfig)
    web: WebConfig = field(default_factory=WebConfig)
    operator: OperatorConfig = field(default_factory=OperatorConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


# Singleton instance — import this everywhere as `from config import settings`
settings = Settings()


# ─── Env overrides for key tuning parameters ───────────────────────────────
# Allows changing tuning without code edits. Credentials are handled above
# (they MUST come from env/config_local for security).

def _env_str(name, default):
    val = os.environ.get(name)
    return val if val is not None else default

def _env_int(name, default):
    val = os.environ.get(name)
    return int(val) if val is not None else default

def _env_float(name, default):
    val = os.environ.get(name)
    return float(val) if val is not None else default


# Apply env overrides to singleton settings instance
settings.vision.yolo_model = _env_str("CRANE_YOLO_MODEL", settings.vision.yolo_model)
settings.vision.frame_skip_rate = _env_int("CRANE_FRAME_SKIP_RATE", settings.vision.frame_skip_rate)
settings.web.auth_username = _env_str("CRANE_AUTH_USERNAME", settings.web.auth_username)
settings.ptz.profile = _env_str("CRANE_PTZ_PROFILE", settings.ptz.profile)
settings.ptz.min_command_interval = _env_float("CRANE_MIN_COMMAND_INTERVAL", settings.ptz.min_command_interval)
settings.tracking.pan_speed_gain = _env_float("CRANE_PAN_SPEED_GAIN", settings.tracking.pan_speed_gain)
settings.tracking.min_pan_speed = _env_float("CRANE_MIN_PAN_SPEED", settings.tracking.min_pan_speed)
settings.operator.manual_override_timeout = _env_float("CRANE_MANUAL_OVERRIDE_TIMEOUT", settings.operator.manual_override_timeout)
# Bug 2 fix: home-return + zoom-reset settings (env-overridable)
settings.operator.return_to_home_after_manual = os.environ.get(
    "CRANE_RETURN_TO_HOME_AFTER_MANUAL", "1" if settings.operator.return_to_home_after_manual else "0"
).lower() in ("1", "true", "yes", "on")
settings.operator.zoom_reset_on_home = os.environ.get(
    "CRANE_ZOOM_RESET_ON_HOME", "1" if settings.operator.zoom_reset_on_home else "0"
).lower() in ("1", "true", "yes", "on")
settings.operator.home_settle_delay = _env_float("CRANE_HOME_SETTLE_DELAY", settings.operator.home_settle_delay)

# Notifications — Telegram alerts
_tg_token = os.environ.get("CRANE_TELEGRAM_TOKEN", "")
_tg_chat_id = os.environ.get("CRANE_TELEGRAM_CHAT_ID", "")
if _tg_token:
    settings.notifications.telegram_token = _tg_token
if _tg_chat_id:
    settings.notifications.telegram_chat_id = _tg_chat_id
# Auto-enable notifications if both token and chat_id are set via env
if _tg_token and _tg_chat_id:
    settings.notifications.enabled = True
# Allow explicit enable/disable override
_tg_enabled = os.environ.get("CRANE_NOTIFICATIONS_ENABLED")
if _tg_enabled is not None:
    settings.notifications.enabled = _tg_enabled.lower() in ("1", "true", "yes", "on")

# ─── Part B: Multi-camera + Telegram bot env overrides ───────────────────
settings.notifications.camera_id = _env_str("CRANE_CAMERA_ID", settings.notifications.camera_id)
settings.notifications.bot_password = _env_str("CRANE_BOT_PASSWORD", settings.notifications.bot_password)
settings.notifications.authorized_chat_ids = _env_str(
    "CRANE_AUTHORIZED_CHAT_IDS", settings.notifications.authorized_chat_ids
)
settings.notifications.default_role = _env_str("CRANE_DEFAULT_ROLE", settings.notifications.default_role)
settings.notifications.webapp_url = _env_str("CRANE_WEBAPP_URL", settings.notifications.webapp_url)
settings.notifications.public_base_url = _env_str("CRANE_PUBLIC_BASE_URL", settings.notifications.public_base_url)
_tg_bot_enabled = os.environ.get("CRANE_BOT_ENABLED")
if _tg_bot_enabled is not None:
    settings.notifications.bot_enabled = _tg_bot_enabled.lower() in ("1", "true", "yes", "on")
settings.notifications.live_stream_interval = _env_float(
    "CRANE_LIVE_STREAM_INTERVAL", settings.notifications.live_stream_interval
)
settings.notifications.redis_url = _env_str("CRANE_REDIS_URL", settings.notifications.redis_url)
settings.notifications.camera_name = _env_str("CRANE_CAMERA_NAME", settings.notifications.camera_name)
