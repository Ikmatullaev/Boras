"""TelegramBotService — интерактивный Telegram-бот для Boras.

Архитектура:
  - Long-polling цикл в отдельном потоке (asyncio.run в отдельном thread)
  - Multi-camera support через CameraRegistry
  - RBAC через AuthManager (boss/guard/viewer)
  - Persistent storage через BotAuthStore + CameraSettingsStore
  - Live-stream: каждые N секунд отправляет JPEG в чат с включённым live
  - WebApp: открытие mini-app в Telegram для продвинутого UI

Команды:
  /start      — начало работы, запрос пароля
  /status     — статус всех камер (для авторизованных)
  /snapshot   — снимок с выбранной камеры
  /help       — справка

Callback queries (inline buttons):
  sel:<cam>       — выбрать камеру (показать camera_menu)
  move:<cam>:<d>  — PTZ move (up/down/left/right/stop)
  zoom:<cam>:<d>  — PTZ zoom (in/out)
  guard:<cam>:t   — toggle auto-guard
  live:<cam>:<on|off> — включить/выключить live-stream
  snap:<cam>      — снимок
  settings:<cam>  — открыть настройки
  set:<cam>:<k>:<v> — изменить настройку
  menu:0          — главное меню
  users:0         — список пользователей (boss)
  user:<chat_id>  — действия над пользователем (boss)
  role:<chat_id>:<r> — изменить роль (boss)
  revoke:<chat_id> — отозвать доступ (boss)

Запуск:
  bot = TelegramBotService(token=..., auth_manager=..., registry=...)
  bot.start()  # spawns background thread with asyncio loop
  ...
  bot.stop()
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crane.telegram.bot")

# Try to import aiogram (optional dependency)
try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command
    from aiogram.types import (
        Message,
        CallbackQuery,
        InlineKeyboardMarkup,
        WebAppInfo,
    )
    from aiogram.exceptions import TelegramNetworkError, TelegramForbiddenError
    _HAS_AIOGRAM = True
except ImportError:
    _HAS_AIOGRAM = False
    Bot = None
    Dispatcher = None
    # Stubs for type hints — allows module to import without aiogram installed.
    # The actual handlers are only registered if _HAS_AIOGRAM is True.
    Message = object
    CallbackQuery = object
    InlineKeyboardMarkup = object
    WebAppInfo = object
    F = None
    Command = None
    logger.info("aiogram not installed — TelegramBotService disabled (notifications still work)")


class TelegramBotService:
    """Background service that runs an interactive Telegram bot.

    Lifecycle:
        bot = TelegramBotService(token, auth_manager, registry, settings_store)
        bot.start()    # spawns background thread with asyncio loop
        ...
        bot.stop()     # signals thread to stop, joins

    The bot runs independently of NotificationService — both can coexist.
    NotificationService sends outbound alerts (target_detected etc.),
    while TelegramBotService handles interactive commands and callbacks.
    """

    def __init__(
        self,
        token: str,
        auth_manager,
        registry,
        settings_store=None,
        webapp_url: str = "",
        public_base_url: str = "",
        camera_id: str = "default",
    ):
        """
        Args:
            token: Telegram bot token from @BotFather
            auth_manager: AuthManager instance (RBAC)
            registry: CameraRegistry with all registered cameras
            settings_store: CameraSettingsStore for persistent settings
            webapp_url: URL of WebApp mini-app (opened in Telegram iframe)
            public_base_url: public base URL of Boras web panel
            camera_id: default camera_id for single-camera setups
        """
        self._token = token
        self._auth = auth_manager
        self._registry = registry
        self._settings_store = settings_store
        self._webapp_url = webapp_url
        self._public_base_url = public_base_url
        self._default_camera_id = camera_id

        self._bot = None
        self._dp = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Live-stream tracking: {chat_id: camera_id}
        self._live_streams = {}
        self._live_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_available(self) -> bool:
        """Whether the bot can run (aiogram installed + token configured)."""
        return _HAS_AIOGRAM and bool(self._token)

    def start(self):
        """Start the bot in a background thread."""
        if not self.is_available:
            logger.warning("TelegramBotService not started: aiogram=%s, token_set=%s",
                           _HAS_AIOGRAM, bool(self._token))
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_thread, daemon=True, name="telegram-bot"
        )
        self._thread.start()
        logger.info("TelegramBotService started")

    def stop(self):
        """Stop the bot gracefully."""
        if not self._running:
            return
        self._running = False
        # Stop live-streams
        with self._live_lock:
            self._live_streams.clear()
        # Signal asyncio loop to stop
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._stop_async(), self._loop).result(timeout=5)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("TelegramBotService stopped")

    def _run_thread(self):
        """Background thread entry point — runs asyncio loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as e:
            logger.error("TelegramBotService thread crashed: %s", e)
        finally:
            self._loop.close()

    async def _run_async(self):
        """Main async entry — creates Bot, Dispatcher, registers handlers, polls."""
        self._bot = Bot(token=self._token)
        self._dp = Dispatcher()
        self._register_handlers()
        # Start live-stream task
        live_task = asyncio.create_task(self._live_stream_loop())
        try:
            await self._dp.start_polling(self._bot)
        finally:
            live_task.cancel()
            try:
                await live_task
            except asyncio.CancelledError:
                pass

    async def _stop_async(self):
        """Stop the dispatcher polling."""
        if self._dp:
            await self._dp.stop_polling()

    def _register_handlers(self):
        """Register all command and callback handlers with the Dispatcher."""
        if not self._dp:
            return
        # Commands
        self._dp.message.register(self._cmd_start, Command("start"))
        self._dp.message.register(self._cmd_help, Command("help"))
        self._dp.message.register(self._cmd_status, Command("status"))
        self._dp.message.register(self._cmd_snapshot, Command("snapshot"))
        # Free-text — password input during auth
        self._dp.message.register(self._handle_text)
        # Callback queries
        self._dp.callback_query.register(self._cb_menu, F.data == "menu:0")
        self._dp.callback_query.register(self._cb_users, F.data == "users:0")
        self._dp.callback_query.register(self._cb_select_camera, F.data.startswith("sel:"))
        self._dp.callback_query.register(self._cb_move, F.data.startswith("move:"))
        self._dp.callback_query.register(self._cb_zoom, F.data.startswith("zoom:"))
        self._dp.callback_query.register(self._cb_guard, F.data.startswith("guard:"))
        self._dp.callback_query.register(self._cb_live, F.data.startswith("live:"))
        self._dp.callback_query.register(self._cb_snap, F.data.startswith("snap:"))
        self._dp.callback_query.register(self._cb_settings, F.data.startswith("settings:"))
        self._dp.callback_query.register(self._cb_set, F.data.startswith("set:"))
        self._dp.callback_query.register(self._cb_user, F.data.startswith("user:"))
        self._dp.callback_query.register(self._cb_role, F.data.startswith("role:"))
        self._dp.callback_query.register(self._cb_revoke, F.data.startswith("revoke:"))
        self._dp.callback_query.register(self._cb_noop, F.data == "noop:none")

    # ─── Command handlers ───────────────────────────────────────────────

    async def _cmd_start(self, message: Message):
        """/start — начало работы. Если не авторизован — запрос пароля."""
        chat_id = message.chat.id
        session = self._auth.get_session(chat_id)
        if session.is_authorized:
            await self._send_main_menu(chat_id)
        else:
            if self._auth._bot_password:
                await message.answer(
                    "🔒 Введите пароль для доступа к системе Boras:"
                )
            else:
                # No password — auto-authorize
                self._auth.authenticate(chat_id, "", display_name=message.from_user.full_name)
                await self._send_main_menu(chat_id)

    async def _cmd_help(self, message: Message):
        chat_id = message.chat.id
        if not self._auth.is_authorized(chat_id):
            await message.answer("🔒 Вы не авторизованы. Отправьте /start")
            return
        help_text = (
            "Boras Security Bot — команды:\n\n"
            "/start — главное меню\n"
            "/status — статус всех камер\n"
            "/snapshot — снимок с камеры по умолчанию\n"
            "/help — эта справка\n\n"
            "Используйте inline-кнопки для управления камерой."
        )
        await message.answer(help_text)

    async def _cmd_status(self, message: Message):
        chat_id = message.chat.id
        if not self._auth.is_authorized(chat_id):
            await message.answer("🔒 Вы не авторизованы. Отправьте /start")
            return
        cameras = self._registry.list_all()
        if not cameras:
            await message.answer("ℹ️ Нет зарегистрированных камер")
            return
        lines = ["📊 Статус камер:\n"]
        for cam in cameras:
            try:
                mode = cam.state_machine.mode.value
                ai = cam.state_machine.auto_guard_enabled
            except Exception:
                mode = "?"
                ai = False
            lines.append(f"📹 {cam.name} ({cam.camera_id})")
            lines.append(f"   Режим: {mode} | AI: {'ON' if ai else 'OFF'}")
            try:
                health = cam.ptz.health()
                ptz_ok = health.get("ptz_reachable", False)
                lines.append(f"   PTZ: {'✅' if ptz_ok else '❌'}")
            except Exception:
                pass
            lines.append("")
        await message.answer("\n".join(lines))

    async def _cmd_snapshot(self, message: Message):
        chat_id = message.chat.id
        if not self._auth.is_authorized(chat_id):
            await message.answer("🔒 Вы не авторизованы. Отправьте /start")
            return
        cam = self._registry.get(self._default_camera_id)
        if not cam:
            await message.answer("❌ Камера не найдена")
            return
        await self._send_snapshot(chat_id, cam)

    async def _handle_text(self, message: Message):
        """Handle free-text messages — password input during auth."""
        chat_id = message.chat.id
        session = self._auth.get_session(chat_id)
        if session.auth_state == "awaiting_password":
            password = message.text.strip()
            ok = self._auth.authenticate(
                chat_id, password,
                display_name=message.from_user.full_name,
            )
            if ok:
                await message.answer("✅ Авторизация успешна!")
                await self._send_main_menu(chat_id)
            else:
                await message.answer("❌ Неверный пароль. Попробуйте ещё раз:")
        else:
            # Already authorized — ignore free text
            pass

    # ─── Callback handlers ──────────────────────────────────────────────

    async def _cb_menu(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.is_authorized(chat_id):
            await callback.answer("🔒 Не авторизован. /start", show_alert=True)
            return
        await self._send_main_menu(chat_id, edit_message=True)
        await callback.answer()

    async def _cb_users(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_manage_users"):
            await callback.answer("🔒 Только boss может управлять пользователями", show_alert=True)
            return
        from services.telegram_bot.keyboards import users_menu_kb
        users = self._auth.list_authorized()
        kb = users_menu_kb(users)
        await callback.message.edit_text(
            "👥 Авторизованные пользователи:",
            reply_markup=kb,
        )
        await callback.answer()

    async def _cb_select_camera(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.is_authorized(chat_id):
            await callback.answer("🔒 Не авторизован. /start", show_alert=True)
            return
        # Parse callback_data: "sel:<camera_id>"
        parts = callback.data.split(":", 1)
        if len(parts) < 2:
            await callback.answer("❌ Неверный callback", show_alert=True)
            return
        cam_id = parts[1]
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        # Save selected camera in session
        session = self._auth.get_session(chat_id)
        session.selected_camera_id = cam_id
        await self._send_camera_menu(chat_id, cam, edit_message=True)
        await callback.answer()

    async def _cb_move(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_control_ptz"):
            await callback.answer("🔒 Нет прав на управление PTZ", show_alert=True)
            return
        # Parse: "move:<cam>:<direction>"
        _, cam_id, direction = callback.data.split(":", 2)
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        # Delegate to runtime.operator (if available) or direct PTZ
        try:
            runtime = cam.runtime
            if hasattr(runtime, "manual_override"):
                runtime.manual_override()
            if direction == "up":
                cam.ptz.move(0, 0.5)
            elif direction == "down":
                cam.ptz.move(0, -0.5)
            elif direction == "left":
                cam.ptz.move(-0.5, 0)
            elif direction == "right":
                cam.ptz.move(0.5, 0)
            elif direction == "stop":
                cam.ptz.stop()
            await callback.answer(f"🎯 {direction}")
        except Exception as e:
            logger.error("PTZ move failed: %s", e)
            await callback.answer("❌ Ошибка PTZ", show_alert=True)

    async def _cb_zoom(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_control_ptz"):
            await callback.answer("🔒 Нет прав на управление PTZ", show_alert=True)
            return
        _, cam_id, direction = callback.data.split(":", 2)
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        try:
            runtime = cam.runtime
            if hasattr(runtime, "manual_override"):
                runtime.manual_override()
            if direction == "in":
                cam.ptz.zoom(0.3)
            elif direction == "out":
                cam.ptz.zoom(-0.3)
            await callback.answer(f"🔍 {direction}")
        except Exception as e:
            logger.error("PTZ zoom failed: %s", e)
            await callback.answer("❌ Ошибка zoom", show_alert=True)

    async def _cb_guard(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_toggle_guard"):
            await callback.answer("🔒 Нет прав на переключение AI", show_alert=True)
            return
        _, cam_id, _ = callback.data.split(":", 2)
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        try:
            result = cam.runtime.toggle_guard()
            status = "включён" if result == "on" else "выключен"
            await callback.answer(f"🟢 AI {status}", show_alert=False)
            # Refresh menu
            await self._send_camera_menu(chat_id, cam, edit_message=True)
        except Exception as e:
            logger.error("Toggle guard failed: %s", e)
            await callback.answer("❌ Ошибка переключения AI", show_alert=True)

    async def _cb_live(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_view_live"):
            await callback.answer("🔒 Нет прав на просмотр live", show_alert=True)
            return
        _, cam_id, action = callback.data.split(":", 2)
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        with self._live_lock:
            if action == "on":
                self._live_streams[chat_id] = cam_id
                await callback.answer("🎥 Live запущен")
            else:
                self._live_streams.pop(chat_id, None)
                await callback.answer("⏹ Live остановлен")
        # Refresh menu
        await self._send_camera_menu(chat_id, cam, edit_message=True)

    async def _cb_snap(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.is_authorized(chat_id):
            await callback.answer("🔒 Не авторизован", show_alert=True)
            return
        _, cam_id = callback.data.split(":", 1)
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        await self._send_snapshot(chat_id, cam)
        await callback.answer()

    async def _cb_settings(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_change_settings"):
            await callback.answer("🔒 Только boss может менять настройки", show_alert=True)
            return
        _, cam_id = callback.data.split(":", 1)
        cam = self._registry.get(cam_id)
        if not cam:
            await callback.answer("❌ Камера не найдена", show_alert=True)
            return
        from services.telegram_bot.keyboards import settings_menu_kb
        # Build dynamic settings menu with current values
        kb = self._build_dynamic_settings_kb(cam_id)
        await callback.message.edit_text(
            f"⚙️ Настройки камеры: {cam.name}\n"
            f"Измените параметры кнопками ниже:",
            reply_markup=kb,
        )
        await callback.answer()

    def _build_dynamic_settings_kb(self, cam_id: str):
        """Build settings keyboard with current values from settings_store."""
        from services.telegram_bot.keyboards import (
            InlineKeyboardButton, InlineKeyboardMarkup, _cb
        )
        kb = InlineKeyboardMarkup()
        # Timeout
        timeout = 5.0
        if self._settings_store:
            timeout = self._settings_store.get(cam_id, "manual_override_timeout", 5.0)
        kb.row(
            InlineKeyboardButton(text="➖", callback_data=_cb("set", cam_id, "timeout:-")),
            InlineKeyboardButton(text=f"⏱ Timeout ({timeout:g}s)", callback_data="noop:none"),
            InlineKeyboardButton(text="➕", callback_data=_cb("set", cam_id, "timeout:+")),
        )
        # Home return
        home_return = True
        if self._settings_store:
            home_return = self._settings_store.get(cam_id, "return_to_home_after_manual", True)
        hr_text = "🏠 Возврат в центр: " + ("ON" if home_return else "OFF")
        kb.row(InlineKeyboardButton(text=hr_text, callback_data=_cb("set", cam_id, "home_return:toggle")))
        # Zoom reset
        zoom_reset = True
        if self._settings_store:
            zoom_reset = self._settings_store.get(cam_id, "zoom_reset_on_home", True)
        zr_text = "🔍 Сброс зума: " + ("ON" if zoom_reset else "OFF")
        kb.row(InlineKeyboardButton(text=zr_text, callback_data=_cb("set", cam_id, "zoom_reset:toggle")))
        # Settle delay
        settle = 2.0
        if self._settings_store:
            settle = self._settings_store.get(cam_id, "home_settle_delay", 2.0)
        kb.row(
            InlineKeyboardButton(text="➖", callback_data=_cb("set", cam_id, "settle:-")),
            InlineKeyboardButton(text=f"⌛ Settle ({settle:g}s)", callback_data="noop:none"),
            InlineKeyboardButton(text="➕", callback_data=_cb("set", cam_id, "settle:+")),
        )
        # Back
        kb.row(InlineKeyboardButton(text="↩ Назад к камере", callback_data=_cb("sel", cam_id)))
        return kb

    async def _cb_set(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_change_settings"):
            await callback.answer("🔒 Только boss может менять настройки", show_alert=True)
            return
        # Parse: "set:<cam>:<key>:<value>"
        parts = callback.data.split(":", 3)
        if len(parts) < 4:
            await callback.answer("❌ Неверный callback", show_alert=True)
            return
        _, cam_id, key, value = parts
        if not self._settings_store:
            await callback.answer("❌ Settings store не настроен", show_alert=True)
            return
        try:
            self._apply_setting_change(cam_id, key, value)
            # Rebuild menu
            kb = self._build_dynamic_settings_kb(cam_id)
            await callback.message.edit_reply_markup(reply_markup=kb)
            await callback.answer("✅ Сохранено")
        except Exception as e:
            logger.error("Setting change failed: %s", e)
            await callback.answer(f"❌ {e}", show_alert=True)

    def _apply_setting_change(self, cam_id: str, key: str, value: str):
        """Apply a setting change to settings_store AND live config.

        This is the bridge between persistent settings (SQLite) and live
        runtime config (settings.operator.* etc).
        """
        cam = self._registry.get(cam_id)
        if not cam:
            raise ValueError(f"Camera {cam_id} not found")

        # Read current value
        if key == "timeout":
            cur = self._settings_store.get(cam_id, "manual_override_timeout", 5.0)
            if value == "+":
                new_val = min(60.0, cur + 1.0)
            elif value == "-":
                new_val = max(0.0, cur - 1.0)
            else:
                new_val = float(value)
            self._settings_store.set(cam_id, "manual_override_timeout", new_val)
            # Apply to live runtime
            cam.runtime._manual_override_timeout = new_val

        elif key == "settle":
            cur = self._settings_store.get(cam_id, "home_settle_delay", 2.0)
            if value == "+":
                new_val = min(10.0, cur + 0.5)
            elif value == "-":
                new_val = max(0.0, cur - 0.5)
            else:
                new_val = float(value)
            self._settings_store.set(cam_id, "home_settle_delay", new_val)
            cam.runtime._home_settle_delay = new_val

        elif key == "home_return":
            cur = self._settings_store.get(cam_id, "return_to_home_after_manual", True)
            new_val = not cur
            self._settings_store.set(cam_id, "return_to_home_after_manual", new_val)
            cam.runtime._return_to_home_after_manual = new_val

        elif key == "zoom_reset":
            cur = self._settings_store.get(cam_id, "zoom_reset_on_home", True)
            new_val = not cur
            self._settings_store.set(cam_id, "zoom_reset_on_home", new_val)
            cam.runtime._zoom_reset_on_home = new_val

        elif key == "fskip":
            from config import settings as cfg
            cur = self._settings_store.get(cam_id, "frame_skip_rate", cfg.vision.frame_skip_rate)
            if value == "+":
                new_val = min(30, cur + 1)
            elif value == "-":
                new_val = max(1, cur - 1)
            else:
                new_val = int(value)
            self._settings_store.set(cam_id, "frame_skip_rate", new_val)
            cam.runtime.frame_skip_rate = new_val

        elif key == "jpeg":
            from config import settings as cfg
            cur = self._settings_store.get(cam_id, "jpeg_quality", cfg.vision.jpeg_quality)
            if value == "+":
                new_val = min(100, cur + 5)
            elif value == "-":
                new_val = max(10, cur - 5)
            else:
                new_val = int(value)
            self._settings_store.set(cam_id, "jpeg_quality", new_val)
            cam.runtime._jpeg_quality = new_val

        elif key == "pspd":
            from config import settings as cfg
            cur = self._settings_store.get(cam_id, "patrol_pan_speed", cfg.patrol.pan_speed)
            if value == "+":
                new_val = min(1.0, cur + 0.02)
            elif value == "-":
                new_val = max(0.02, cur - 0.02)
            else:
                new_val = float(value)
            self._settings_store.set(cam_id, "patrol_pan_speed", new_val)
            cam.runtime.brain.patrol.PAN_SPEED = new_val

        else:
            raise ValueError(f"Unknown setting: {key}")

    async def _cb_user(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_manage_users"):
            await callback.answer("🔒 Только boss", show_alert=True)
            return
        # "user:<chat_id>"
        _, target_str = callback.data.split(":", 1)
        target_chat_id = int(target_str)
        from services.telegram_bot.keyboards import user_actions_kb
        kb = user_actions_kb(target_chat_id)
        session = self._auth.get_session(target_chat_id)
        name = session.display_name or str(target_chat_id)
        await callback.message.edit_text(
            f"👤 {name}\nchat_id: {target_chat_id}\nРоль: {session.role.value}",
            reply_markup=kb,
        )
        await callback.answer()

    async def _cb_role(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_manage_users"):
            await callback.answer("🔒 Только boss", show_alert=True)
            return
        # "role:<chat_id>:<role>"
        _, target_str, role_str = callback.data.split(":", 2)
        target_chat_id = int(target_str)
        from services.telegram_bot.auth import Role
        new_role = Role.from_string(role_str)
        self._auth.set_role(target_chat_id, new_role)
        await callback.answer(f"✅ Роль: {new_role.value}")

    async def _cb_revoke(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        if not self._auth.check_permission(chat_id, "can_manage_users"):
            await callback.answer("🔒 Только boss", show_alert=True)
            return
        # "revoke:<chat_id>"
        _, target_str = callback.data.split(":", 1)
        target_chat_id = int(target_str)
        self._auth.revoke(target_chat_id)
        await callback.answer("🚫 Доступ отозван")

    async def _cb_noop(self, callback: CallbackQuery):
        await callback.answer()

    # ─── Helper methods ─────────────────────────────────────────────────

    async def _send_main_menu(self, chat_id: int, edit_message: bool = False):
        from services.telegram_bot.keyboards import main_menu_kb
        cameras = self._registry.list_all()
        session = self._auth.get_session(chat_id)
        kb = main_menu_kb(cameras, current_chat_role=session.role.value)
        text = "📹 Boras Security\nВыберите камеру:"
        # Add WebApp button if URL configured AND aiogram is available
        if (self._webapp_url and _HAS_AIOGRAM
                and session.role.value in ("boss", "guard", "viewer")):
            from services.telegram_bot.keyboards import InlineKeyboardButton
            try:
                kb.row(InlineKeyboardButton(
                    text="📱 Открыть WebApp",
                    web_app=WebAppInfo(url=self._webapp_url),
                ))
            except Exception:
                pass  # WebAppInfo only works with real aiogram
        if edit_message:
            try:
                # Use bot API to edit message text + reply_markup
                pass  # caller handles
            except Exception:
                pass
        await self._bot.send_message(chat_id, text, reply_markup=kb)

    async def _send_camera_menu(self, chat_id: int, cam, edit_message: bool = False):
        from services.telegram_bot.keyboards import camera_menu_kb
        session = self._auth.get_session(chat_id)
        try:
            ai_enabled = cam.state_machine.auto_guard_enabled
            mode = cam.state_machine.mode.value
        except Exception:
            ai_enabled = False
            mode = "?"
        with self._live_lock:
            live_active = self._live_streams.get(chat_id) == cam.camera_id
        kb = camera_menu_kb(
            camera_id=cam.camera_id,
            role=session.role.value,
            ai_enabled=ai_enabled,
            live_active=live_active,
        )
        text = (
            f"📹 {cam.name}\n"
            f"🆔 {cam.camera_id}\n"
            f"🔄 Режим: {mode}\n"
            f"🟢 AI: {'ON' if ai_enabled else 'OFF'}"
        )
        await self._bot.send_message(chat_id, text, reply_markup=kb)

    async def _send_snapshot(self, chat_id: int, cam):
        try:
            jpeg = cam.snapshot_provider()
            if not jpeg:
                await self._bot.send_message(chat_id, "❌ Нет кадра с камеры")
                return
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            caption = f"📸 {cam.name}\n🕒 {ts}"
            await self._bot.send_photo(chat_id, photo=jpeg, caption=caption)
        except Exception as e:
            logger.error("Snapshot send failed: %s", e)
            await self._bot.send_message(chat_id, f"❌ Ошибка снимка: {e}")

    async def _live_stream_loop(self):
        """Background task: send live frames to chats with active live-stream."""
        from config import settings as cfg
        interval = cfg.notifications.live_stream_interval
        while True:
            try:
                with self._live_lock:
                    active = dict(self._live_streams)
                for chat_id, cam_id in active.items():
                    cam = self._registry.get(cam_id)
                    if not cam:
                        continue
                    try:
                        jpeg = cam.snapshot_provider()
                        if jpeg:
                            await self._bot.send_photo(chat_id, photo=jpeg)
                    except Exception as e:
                        logger.debug("Live stream frame failed for %s: %s", chat_id, e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Live stream loop error: %s", e)
            await asyncio.sleep(interval)
