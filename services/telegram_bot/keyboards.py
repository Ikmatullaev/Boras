"""Inline-клавиатуры для Telegram-бота.

Все callback_data имеют формат: "<action>:<camera_id>:<param>"
Это позволяет одному callback-хендлеру маршрутизировать действия к нужной камере.

Примеры:
  "sel:cam_1"           — выбрать камеру cam_1
  "move:cam_1:left"     — двинуть камеру cam_1 влево
  "zoom:cam_1:in"       — зум cam_1 вперёд
  "guard:cam_1:toggle"  — переключить AI cam_1
  "live:cam_1:on"       — включить live-stream cam_1
  "settings:cam_1"      — открыть настройки cam_1
  "set:cam_1:timeout:5" — установить manual_override_timeout=5 для cam_1
"""
from typing import List, Optional

try:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    _HAS_AIOGRAM = True
except ImportError:
    _HAS_AIOGRAM = False
    # Stub для случая, когда aiogram не установлен (тесты без бот-зависимостей)
    class InlineKeyboardButton:
        def __init__(self, text: str = "", callback_data: str = "",
                     url: str = None, web_app=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url
            self.web_app = web_app

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard=None):
            self.inline_keyboard = inline_keyboard or []

        def add(self, *buttons):
            self.inline_keyboard.append(list(buttons))

        def row(self, *buttons):
            self.inline_keyboard.append(list(buttons))


def _cb(action: str, camera_id: str, param: str = "") -> str:
    """Build callback_data string. Max length 64 bytes (Telegram limit)."""
    if param:
        return f"{action}:{camera_id}:{param}"
    return f"{action}:{camera_id}"


def main_menu_kb(cameras: list, current_chat_role: str = "viewer") -> InlineKeyboardMarkup:
    """Главное меню со списком камер.

    Args:
        cameras: list of CameraEntry objects
        current_chat_role: role of current user ("boss"/"guard"/"viewer")
    """
    kb = InlineKeyboardMarkup()
    if not cameras:
        kb.add(InlineKeyboardButton(
            text="ℹ️ Камеры не зарегистрированы",
            callback_data="noop:none",
        ))
        return kb
    # Кнопка на каждую камеру
    for cam in cameras:
        # Статус-индикатор: 🟢 AI on, 🔴 AI off, 🟡 MANUAL
        try:
            mode = cam.state_machine.mode.value
            if mode in ("PATROL", "TRACKING"):
                icon = "🟢"
            elif mode == "MANUAL":
                icon = "🟡"
            else:
                icon = "🔴"
        except Exception:
            icon = "❓"
        kb.add(InlineKeyboardButton(
            text=f"{icon} {cam.name}",
            callback_data=_cb("sel", cam.camera_id),
        ))
    # Доп. кнопка для boss — управление пользователями
    if current_chat_role == "boss":
        kb.add(InlineKeyboardButton(
            text="👥 Пользователи",
            callback_data="users:0",
        ))
    return kb


def camera_menu_kb(camera_id: str, role: str = "viewer",
                   ai_enabled: bool = False, live_active: bool = False) -> InlineKeyboardMarkup:
    """Меню конкретной камеры: PTZ, live, настройки, AI toggle.

    Args:
        camera_id: ID камеры
        role: роль пользователя ("boss"/"guard"/"viewer")
        ai_enabled: включён ли auto-guard сейчас
        live_active: активен ли live-stream сейчас
    """
    kb = InlineKeyboardMarkup()

    # PTZ D-pad — только для boss/guard
    if role in ("boss", "guard"):
        kb.row(
            InlineKeyboardButton(text="▲", callback_data=_cb("move", camera_id, "up")),
            InlineKeyboardButton(text="⏹", callback_data=_cb("move", camera_id, "stop")),
        )
        kb.row(
            InlineKeyboardButton(text="◄", callback_data=_cb("move", camera_id, "left")),
            InlineKeyboardButton(text="▼", callback_data=_cb("move", camera_id, "down")),
            InlineKeyboardButton(text="►", callback_data=_cb("move", camera_id, "right")),
        )
        # Zoom
        kb.row(
            InlineKeyboardButton(text="🔍-", callback_data=_cb("zoom", camera_id, "out")),
            InlineKeyboardButton(text="🔍+", callback_data=_cb("zoom", camera_id, "in")),
        )

    # AI toggle — для boss/guard
    if role in ("boss", "guard"):
        ai_text = "🔴 AI: ВЫКЛ" if ai_enabled else "🟢 AI: ВКЛ"
        kb.row(InlineKeyboardButton(
            text=ai_text,
            callback_data=_cb("guard", camera_id, "toggle"),
        ))

    # Live-stream — для всех ролей
    live_text = "⏹ Остановить live" if live_active else "🎥 Live-трансляция"
    kb.row(InlineKeyboardButton(
        text=live_text,
        callback_data=_cb("live", camera_id, "on" if not live_active else "off"),
    ))

    # Snapshot — для всех
    kb.row(InlineKeyboardButton(
        text="📸 Снимок",
        callback_data=_cb("snap", camera_id),
    ))

    # Settings — только для boss
    if role == "boss":
        kb.row(InlineKeyboardButton(
            text="⚙️ Настройки",
            callback_data=_cb("settings", camera_id),
        ))

    # WebApp — если URL задан
    # (WebApp button requires url, not callback_data — caller adds it separately)

    # Back to main menu
    kb.row(InlineKeyboardButton(
        text="↩ Назад",
        callback_data="menu:0",
    ))
    return kb


def settings_menu_kb(camera_id: str) -> InlineKeyboardMarkup:
    """Меню настроек камеры (boss only)."""
    kb = InlineKeyboardMarkup()
    # Timeout settings: - 5 +
    kb.row(
        InlineKeyboardButton(text="➖", callback_data=_cb("set", camera_id, "timeout:-")),
        InlineKeyboardButton(text="⏱ Timeout (5s)", callback_data="noop:none"),
        InlineKeyboardButton(text="➕", callback_data=_cb("set", camera_id, "timeout:+")),
    )
    # Home return toggle
    kb.row(InlineKeyboardButton(
        text="🏠 Возврат в центр: ON",
        callback_data=_cb("set", camera_id, "home_return:toggle"),
    ))
    # Zoom reset toggle
    kb.row(InlineKeyboardButton(
        text="🔍 Сброс зума: ON",
        callback_data=_cb("set", camera_id, "zoom_reset:toggle"),
    ))
    # Settle delay: - 2 +
    kb.row(
        InlineKeyboardButton(text="➖", callback_data=_cb("set", camera_id, "settle:-")),
        InlineKeyboardButton(text="⌛ Settle (2s)", callback_data="noop:none"),
        InlineKeyboardButton(text="➕", callback_data=_cb("set", camera_id, "settle:+")),
    )
    # Frame skip rate: - 3 +
    kb.row(
        InlineKeyboardButton(text="➖", callback_data=_cb("set", camera_id, "fskip:-")),
        InlineKeyboardButton(text="🖼 Frame skip (3)", callback_data="noop:none"),
        InlineKeyboardButton(text="➕", callback_data=_cb("set", camera_id, "fskip:+")),
    )
    # JPEG quality: - 80 +
    kb.row(
        InlineKeyboardButton(text="➖", callback_data=_cb("set", camera_id, "jpeg:-")),
        InlineKeyboardButton(text="📷 JPEG quality (80)", callback_data="noop:none"),
        InlineKeyboardButton(text="➕", callback_data=_cb("set", camera_id, "jpeg:+")),
    )
    # Patrol speed: - 0.12 +
    kb.row(
        InlineKeyboardButton(text="🐢", callback_data=_cb("set", camera_id, "pspd:-")),
        InlineKeyboardButton(text="🔄 Patrol speed", callback_data="noop:none"),
        InlineKeyboardButton(text="🐇", callback_data=_cb("set", camera_id, "pspd:+")),
    )
    # Back to camera menu
    kb.row(InlineKeyboardButton(
        text="↩ Назад к камере",
        callback_data=_cb("sel", camera_id),
    ))
    return kb


def users_menu_kb(authorized_users: list) -> InlineKeyboardMarkup:
    """Меню управления пользователями (boss only)."""
    kb = InlineKeyboardMarkup()
    if not authorized_users:
        kb.add(InlineKeyboardButton(
            text="ℹ️ Нет авторизованных пользователей",
            callback_data="noop:none",
        ))
    else:
        for user in authorized_users:
            role_icon = {"boss": "👑", "guard": "🛡", "viewer": "👀"}.get(
                user["role"], "❓"
            )
            name = user.get("name") or f"chat_id: {user['chat_id']}"
            kb.add(InlineKeyboardButton(
                text=f"{role_icon} {name} ({user['role']})",
                callback_data=f"user:{user['chat_id']}",
            ))
    kb.add(InlineKeyboardButton(
        text="↩ Назад",
        callback_data="menu:0",
    ))
    return kb


def user_actions_kb(target_chat_id: int) -> InlineKeyboardMarkup:
    """Действия над конкретным пользователем (boss only)."""
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(text="👑 Boss", callback_data=f"role:{target_chat_id}:boss"),
        InlineKeyboardButton(text="🛡 Guard", callback_data=f"role:{target_chat_id}:guard"),
        InlineKeyboardButton(text="👀 Viewer", callback_data=f"role:{target_chat_id}:viewer"),
    )
    kb.row(InlineKeyboardButton(
        text="🚫 Отозвать доступ",
        callback_data=f"revoke:{target_chat_id}",
    ))
    kb.row(InlineKeyboardButton(
        text="↩ Назад",
        callback_data="users:0",
    ))
    return kb


def access_button(camera_id: str, web_url: str) -> Optional[InlineKeyboardButton]:
    """Кнопка 'Access to camera' для уведомлений о детекции.

    Возвращает InlineKeyboardButton с URL, либо None если web_url пустой.
    """
    if not web_url:
        return None
    return InlineKeyboardButton(
        text="🎥 Доступ к камере",
        url=web_url,
    )
