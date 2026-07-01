"""CameraRegistry — реестр камер для multi-camera support.

Каждая камера в Boras-инстансе регистрируется здесь с уникальным camera_id.
Telegram-бот использует registry, чтобы:
  1. Список камер для пользователя (главное меню)
  2. Маршрутизация PTZ-команд к правильной камере
  3. Маршрутизация notifications с указанием camera_id
  4. Snapshot от правильной камеры при детекции

В single-camera установке registry содержит одну запись с camera_id="default".
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("crane.telegram.registry")


@dataclass
class CameraEntry:
    """Регистрационная запись камеры в registry.

    Содержит ссылки на все сервисы камеры, нужные боту для:
      - snapshot_provider: получить текущий JPEG-кадр
      - ptz_controller: управлять PTZ (move/zoom/focus/stop)
      - runtime: toggle_guard, status, manual_override
      - state_machine: текущий режим (PATROL/TRACKING/MANUAL)
    """
    camera_id: str
    name: str
    snapshot_provider: Callable[[], Optional[bytes]]
    ptz_controller: Any
    runtime: Any
    state_machine: Any
    events: Any = None  # EventLog — для получения recent events
    # Опционально: web-панель этой камеры (для кнопки "Access")
    web_url: str = ""
    # Опционально: webapp URL (Telegram WebApp mini-app)
    webapp_url: str = ""


class CameraRegistry:
    """Реестр всех камер, доступных через Telegram-бота.

    Thread-safe. Камеры добавляются при старте приложения (compose_app)
    через register(). Бот читает registry для построения меню и маршрутизации.
    """

    def __init__(self):
        self._cameras: Dict[str, CameraEntry] = {}
        self._lock = None  # RLock for thread safety

    def register(self, entry: CameraEntry):
        """Register a camera. If camera_id exists — overwrite."""
        if entry.camera_id in self._cameras:
            logger.warning("Camera %s already registered, overwriting", entry.camera_id)
        self._cameras[entry.camera_id] = entry
        logger.info("Camera registered: id=%s name=%s",
                    entry.camera_id, entry.name)

    def unregister(self, camera_id: str):
        """Remove a camera from registry."""
        self._cameras.pop(camera_id, None)

    def get(self, camera_id: str) -> Optional[CameraEntry]:
        """Get a camera entry by ID."""
        return self._cameras.get(camera_id)

    def list_all(self) -> List[CameraEntry]:
        """Return all registered cameras (sorted by camera_id)."""
        return sorted(self._cameras.values(), key=lambda c: c.camera_id)

    def __len__(self):
        return len(self._cameras)

    def __contains__(self, camera_id: str):
        return camera_id in self._cameras


# Global singleton — все Boras-инстансы (если их несколько в одном процессе)
# регистрируют свои камеры сюда. TelegramBotService читает отсюда.
_registry = CameraRegistry()


def get_registry() -> CameraRegistry:
    """Get the global CameraRegistry singleton."""
    return _registry
