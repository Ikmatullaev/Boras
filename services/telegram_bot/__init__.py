"""Telegram bot module for Boras — interactive bot with multi-camera support.

Architecture:
    EventLog ─► NotificationService ─► TelegramNotificationProvider (outbound alerts)
                                              ↓
    User ─► TelegramBotService (inbound commands, PTZ control, live, settings)
              ├─ AuthManager (RBAC: boss/guard/viewer)
              ├─ CameraRegistry (multi-camera routing)
              ├─ BotAuthStore + CameraSettingsStore (SQLite persistence)
              └─ WebApp mini-app (Telegram WebAppInfo)

To enable:
    1. pip install aiogram>=3.4  (or use requirements.txt)
    2. Set env vars:
        CRANE_TELEGRAM_TOKEN=...         (from @BotFather)
        CRANE_BOT_PASSWORD=your_secret   (password for new users)
        CRANE_AUTHORIZED_CHAT_IDS=12345  (boss's chat_id, comma-separated)
        CRANE_WEBAPP_URL=https://your-host/webapp
        CRANE_PUBLIC_BASE_URL=https://your-host
    3. Bot auto-starts in app.py lifespan if token + bot_enabled=True
"""
from services.telegram_bot.auth import AuthManager, ChatSession, Role
from services.telegram_bot.bot_service import TelegramBotService
from services.telegram_bot.keyboards import (
    access_button,
    camera_menu_kb,
    main_menu_kb,
    settings_menu_kb,
    user_actions_kb,
    users_menu_kb,
)
from services.telegram_bot.registry import (
    CameraEntry,
    CameraRegistry,
    get_registry,
)
from services.telegram_bot.storage import (
    BotAuthStore,
    CameraSettingsStore,
)

__all__ = [
    "AuthManager",
    "BotAuthStore",
    "CameraEntry",
    "CameraRegistry",
    "CameraSettingsStore",
    "ChatSession",
    "Role",
    "TelegramBotService",
    "access_button",
    "camera_menu_kb",
    "get_registry",
    "main_menu_kb",
    "settings_menu_kb",
    "user_actions_kb",
    "users_menu_kb",
]
