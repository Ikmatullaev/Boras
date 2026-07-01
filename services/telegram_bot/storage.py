"""SQLite-backed persistence для Telegram-бота.

Две таблицы:
  - authorized_chats: chat_id → role, display_name, authorized_at
  - camera_settings:  camera_id → setting_key → setting_value
                       (JSON-serializable значения)

Используется стандартная библиотека sqlite3 — без новых зависимостей.
Thread-safe через один lock на соединение.
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("crane.telegram.store")


class BotAuthStore:
    """SQLite storage для authorized chat_ids и их ролей."""

    def __init__(self, db_path: str = "boras_bot.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Create tables if not exist."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS authorized_chats (
                        chat_id        INTEGER PRIMARY KEY,
                        role           TEXT NOT NULL DEFAULT 'viewer',
                        display_name   TEXT NOT NULL DEFAULT '',
                        authorized_at  TEXT NOT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    def upsert(self, chat_id: int, role: str, display_name: str = ""):
        """Insert or update a chat_id with role + name."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO authorized_chats "
                    "(chat_id, role, display_name, authorized_at) "
                    "VALUES (?, ?, ?, ?)",
                    (chat_id, role, display_name,
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

    def delete(self, chat_id: int):
        """Remove a chat_id from authorized list."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM authorized_chats WHERE chat_id = ?",
                             (chat_id,))
                conn.commit()
            finally:
                conn.close()

    def get(self, chat_id: int) -> Optional[Dict]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM authorized_chats WHERE chat_id = ?",
                    (chat_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_all(self) -> List[Dict]:
        """Return all authorized chats."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM authorized_chats ORDER BY authorized_at DESC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()


class CameraSettingsStore:
    """SQLite storage для per-camera настроек, изменяемых через Telegram-бота.

    Хранит key-value пары. Значения сериализуются в JSON.
    При запуске бота загружает все настройки для camera_id в memory cache.
    """

    # Allowed setting keys (whitelist). Trying to set unknown key raises ValueError.
    ALLOWED_KEYS = {
        # OperatorConfig
        "manual_override_timeout",
        "return_to_home_after_manual",
        "zoom_reset_on_home",
        "home_settle_delay",
        # VisionConfig
        "yolo_model",
        "frame_skip_rate",
        "jpeg_quality",
        # PatrolConfig
        "patrol_pan_speed",
        "patrol_cycle_duration",
        "patrol_pan_duration",
        # TrackingConfig
        "pan_speed_gain",
        "deadzone_frac_x",
        "deadzone_frac_y",
        # NotificationConfig
        "rate_limit_seconds",
        "live_stream_interval",
        # WebConfig
        "auth_username",
    }

    def __init__(self, db_path: str = "boras_bot.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}  # camera_id → {key: value}
        self._init_db()
        self._load_all()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS camera_settings (
                        camera_id   TEXT NOT NULL,
                        key         TEXT NOT NULL,
                        value       TEXT NOT NULL,
                        updated_at  TEXT NOT NULL,
                        PRIMARY KEY (camera_id, key)
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    def _load_all(self):
        """Load all settings into memory cache on startup."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT camera_id, key, value FROM camera_settings"
                ).fetchall()
                for row in rows:
                    cam_id = row["camera_id"]
                    if cam_id not in self._cache:
                        self._cache[cam_id] = {}
                    try:
                        self._cache[cam_id][row["key"]] = json.loads(row["value"])
                    except (json.JSONDecodeError, TypeError):
                        self._cache[cam_id][row["key"]] = row["value"]
            finally:
                conn.close()

    def get(self, camera_id: str, key: str, default: Any = None) -> Any:
        """Get a setting value. Returns from memory cache."""
        if key not in self.ALLOWED_KEYS:
            raise ValueError(f"Unknown setting key: {key}")
        return self._cache.get(camera_id, {}).get(key, default)

    def get_all(self, camera_id: str) -> Dict[str, Any]:
        """Get all settings for a camera_id."""
        return dict(self._cache.get(camera_id, {}))

    def set(self, camera_id: str, key: str, value: Any):
        """Set a setting value. Persists to SQLite + updates cache."""
        if key not in self.ALLOWED_KEYS:
            raise ValueError(f"Unknown setting key: {key}")
        # Serialize to JSON for storage
        value_json = json.dumps(value)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO camera_settings "
                    "(camera_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                    (camera_id, key, value_json,
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
        # Update cache
        if camera_id not in self._cache:
            self._cache[camera_id] = {}
        self._cache[camera_id][key] = value

    def delete(self, camera_id: str, key: str):
        """Delete a setting. Returns to default."""
        if key not in self.ALLOWED_KEYS:
            raise ValueError(f"Unknown setting key: {key}")
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "DELETE FROM camera_settings WHERE camera_id = ? AND key = ?",
                    (camera_id, key),
                )
                conn.commit()
            finally:
                conn.close()
        if camera_id in self._cache:
            self._cache[camera_id].pop(key, None)

    def list_cameras(self) -> List[str]:
        """Return list of camera_ids that have at least one setting."""
        return list(self._cache.keys())
