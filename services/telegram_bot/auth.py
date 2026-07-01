"""RBAC — Role-Based Access Control для Telegram-бота.

Три роли:
  - boss:    полный доступ (настройки + live + управление + уведомления)
  - guard:   live + управление PTZ + уведомления (без настроек)
  - viewer:  только просмотр live + уведомления (без управления PTZ)

Авторизация:
  1. При /start пользователь вводит пароль (CRANE_BOT_PASSWORD).
  2. После успешной авторизации его chat_id записывается в SQLite с ролью
     default_role (по умолчанию "viewer").
  3. Boss может повысить роль другого chat_id командой /promote.

В памяти (in-process) храним маппинг chat_id → ChatSession.
Persistent storage — SQLite (services/telegram_bot/auth_store.py).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional, Set


class Role(str, Enum):
    """Роли пользователей в Telegram-боте."""
    BOSS = "boss"      # полный доступ
    GUARD = "guard"    # live + PTZ control, без настроек
    VIEWER = "viewer"  # только live + notifications, без PTZ control

    @classmethod
    def from_string(cls, value: str) -> "Role":
        """Безопасное создание Role из строки. Unknown → VIEWER."""
        try:
            return cls(value.lower())
        except (ValueError, AttributeError):
            return cls.VIEWER

    @property
    def can_control_ptz(self) -> bool:
        """Управление PTZ (move/zoom/focus) — только boss и guard."""
        return self in (Role.BOSS, Role.GUARD)

    @property
    def can_change_settings(self) -> bool:
        """Изменение настроек камеры — только boss."""
        return self == Role.BOSS

    @property
    def can_toggle_guard(self) -> bool:
        """Включение/выключение AI — только boss и guard."""
        return self in (Role.BOSS, Role.GUARD)

    @property
    def can_view_live(self) -> bool:
        """Просмотр live-stream — все роли."""
        return True

    @property
    def can_manage_users(self) -> bool:
        """Управление пользователями (promote/revoke) — только boss."""
        return self == Role.BOSS


@dataclass
class ChatSession:
    """In-memory сессия Telegram-чата.

    Хранит:
      - chat_id: Telegram chat ID (int)
      - role: роль пользователя (Role enum)
      - display_name: имя пользователя (для отображения в меню)
      - auth_state: состояние процесса авторизации
        "none" — авторизован
        "awaiting_password" — ждём ввода пароля
      - selected_camera_id: ID выбранной камеры (для multi-camera setups)
      - live_stream_active: запущен ли live-stream в этот чат
      - last_interaction: timestamp последнего действия (для GC неактивных сессий)
    """
    chat_id: int
    role: Role = Role.VIEWER
    display_name: str = ""
    auth_state: str = "awaiting_password"
    selected_camera_id: Optional[str] = None
    live_stream_active: bool = False
    last_interaction: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_authorized(self) -> bool:
        return self.auth_state == "none"


class AuthManager:
    """Управление авторизацией и ролями пользователей.

    In-memory кэш ChatSession + persistent storage через AuthStore (SQLite).
    При старте бота загружает все chat_id из AuthStore и создаёт сессии.
    """

    def __init__(self, bot_password: str = "", default_role: Role = Role.VIEWER,
                 authorized_chat_ids: str = "", store=None):
        """
        Args:
            bot_password: пароль для авторизации новых пользователей.
                          Если пустой — авторизация отключена (все допускаются).
            default_role: роль, присваиваемая после успешной авторизации.
            authorized_chat_ids: comma-separated chat IDs с пред-авторизованным
                                 доступом (например, для босса). Эти chat_id
                                 получают роль BOSS автоматически.
            store: optional AuthStore для persistence. Если None — только
                   in-memory (тесты).
        """
        self._bot_password = bot_password
        self._default_role = default_role
        self._sessions: Dict[int, ChatSession] = {}
        self._store = store
        # Pre-authorized chat IDs (boss role)
        self._pre_authorized: Set[int] = set()
        if authorized_chat_ids:
            for raw in authorized_chat_ids.split(","):
                raw = raw.strip()
                if raw:
                    try:
                        self._pre_authorized.add(int(raw))
                    except ValueError:
                        continue
        # Load persisted sessions from store
        if self._store is not None:
            self._load_from_store()

    def _load_from_store(self):
        """Load all persisted chat_ids from AuthStore."""
        if self._store is None:
            return
        for record in self._store.list_all():
            chat_id = record["chat_id"]
            role = Role.from_string(record["role"])
            self._sessions[chat_id] = ChatSession(
                chat_id=chat_id,
                role=role,
                display_name=record.get("display_name", ""),
                auth_state="none",
            )

    def get_session(self, chat_id: int) -> ChatSession:
        """Получить сессию по chat_id. Если нет — создать новую (awaiting_password)."""
        if chat_id not in self._sessions:
            # Pre-authorized chat_ids get boss role without password
            if chat_id in self._pre_authorized:
                session = ChatSession(
                    chat_id=chat_id,
                    role=Role.BOSS,
                    display_name="",
                    auth_state="none",
                )
                self._sessions[chat_id] = session
                self._persist(session)
            else:
                self._sessions[chat_id] = ChatSession(
                    chat_id=chat_id,
                    role=self._default_role,
                    auth_state="awaiting_password" if self._bot_password else "none",
                )
        session = self._sessions[chat_id]
        session.last_interaction = datetime.now(timezone.utc)
        return session

    def is_authorized(self, chat_id: int) -> bool:
        return self.get_session(chat_id).is_authorized

    def has_role(self, chat_id: int, role: Role) -> bool:
        return self.get_session(chat_id).role == role

    def check_permission(self, chat_id: int, permission: str) -> bool:
        """Check if chat_id has the given permission.

        Args:
            permission: one of "can_control_ptz", "can_change_settings",
                       "can_toggle_guard", "can_view_live", "can_manage_users"
        """
        session = self.get_session(chat_id)
        if not session.is_authorized:
            return False
        return getattr(session.role, permission, False)

    def authenticate(self, chat_id: int, password: str, display_name: str = "") -> bool:
        """Try to authenticate a chat_id with a password.

        Returns True if password matches (or if password auth is disabled).
        On success, persists the session to AuthStore.
        """
        session = self.get_session(chat_id)
        # If no password configured — auto-authorize
        if not self._bot_password:
            session.auth_state = "none"
            session.display_name = display_name
            session.role = self._default_role
            self._persist(session)
            return True
        # Check password
        if password == self._bot_password:
            session.auth_state = "none"
            session.display_name = display_name
            session.role = self._default_role
            self._persist(session)
            return True
        return False

    def set_role(self, chat_id: int, role: Role) -> bool:
        """Change role for a chat_id. Returns True if session exists."""
        session = self.get_session(chat_id)
        if not session.is_authorized:
            return False
        session.role = role
        self._persist(session)
        return True

    def revoke(self, chat_id: int) -> bool:
        """Revoke authorization for a chat_id. Returns True if was authorized."""
        if chat_id in self._sessions:
            self._sessions[chat_id].auth_state = "awaiting_password"
            if self._store is not None:
                try:
                    self._store.delete(chat_id)
                except Exception:
                    pass
            return True
        return False

    def list_authorized(self) -> list:
        """Return list of all authorized sessions (for /users command)."""
        return [
            {"chat_id": s.chat_id, "role": s.role.value, "name": s.display_name}
            for s in self._sessions.values() if s.is_authorized
        ]

    def _persist(self, session: ChatSession):
        """Persist session to AuthStore if available."""
        if self._store is None:
            return
        try:
            self._store.upsert(
                chat_id=session.chat_id,
                role=session.role.value,
                display_name=session.display_name,
            )
        except Exception:
            # Persistence failures shouldn't break the bot
            pass
