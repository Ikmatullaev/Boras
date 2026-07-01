"""Тесты для Part B — Telegram bot module.

Покрывает:
  - AuthManager (RBAC: boss/guard/viewer)
  - CameraRegistry (multi-camera routing)
  - BotAuthStore + CameraSettingsStore (SQLite persistence)
  - Keyboards (inline buttons, callback_data format)
  - TelegramBotService lifecycle (start/stop, is_available)
  - TelegramNotificationProvider (camera_id, public_base_url, Access button)
"""
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

# ─── Auth + RBAC ───────────────────────────────────────────────────────────

from services.telegram_bot.auth import AuthManager, ChatSession, Role


class TestRole:
    def test_role_values(self):
        assert Role.BOSS.value == "boss"
        assert Role.GUARD.value == "guard"
        assert Role.VIEWER.value == "viewer"

    def test_from_string_valid(self):
        assert Role.from_string("boss") == Role.BOSS
        assert Role.from_string("GUARD") == Role.GUARD
        assert Role.from_string("Viewer") == Role.VIEWER

    def test_from_string_invalid_defaults_to_viewer(self):
        assert Role.from_string("admin") == Role.VIEWER
        assert Role.from_string("") == Role.VIEWER
        assert Role.from_string(None) == Role.VIEWER

    def test_permissions_boss(self):
        assert Role.BOSS.can_control_ptz is True
        assert Role.BOSS.can_change_settings is True
        assert Role.BOSS.can_toggle_guard is True
        assert Role.BOSS.can_view_live is True
        assert Role.BOSS.can_manage_users is True

    def test_permissions_guard(self):
        assert Role.GUARD.can_control_ptz is True
        assert Role.GUARD.can_change_settings is False
        assert Role.GUARD.can_toggle_guard is True
        assert Role.GUARD.can_view_live is True
        assert Role.GUARD.can_manage_users is False

    def test_permissions_viewer(self):
        assert Role.VIEWER.can_control_ptz is False
        assert Role.VIEWER.can_change_settings is False
        assert Role.VIEWER.can_toggle_guard is False
        assert Role.VIEWER.can_view_live is True
        assert Role.VIEWER.can_manage_users is False


class TestChatSession:
    def test_default_session_awaits_password(self):
        s = ChatSession(chat_id=123)
        assert s.auth_state == "awaiting_password"
        assert s.is_authorized is False
        assert s.role == Role.VIEWER

    def test_authorized_session(self):
        s = ChatSession(chat_id=123, auth_state="none")
        assert s.is_authorized is True


class TestAuthManager:
    def test_new_session_awaits_password(self):
        am = AuthManager(bot_password="secret")
        s = am.get_session(123)
        assert s.auth_state == "awaiting_password"
        assert s.is_authorized is False

    def test_authenticate_correct_password(self):
        am = AuthManager(bot_password="secret", default_role=Role.GUARD)
        ok = am.authenticate(123, "secret", display_name="Test User")
        assert ok is True
        s = am.get_session(123)
        assert s.is_authorized is True
        assert s.role == Role.GUARD
        assert s.display_name == "Test User"

    def test_authenticate_wrong_password(self):
        am = AuthManager(bot_password="secret")
        ok = am.authenticate(123, "wrong")
        assert ok is False
        s = am.get_session(123)
        assert s.is_authorized is False

    def test_no_password_auto_authorize(self):
        am = AuthManager(bot_password="", default_role=Role.VIEWER)
        ok = am.authenticate(123, "", display_name="Auto")
        assert ok is True
        s = am.get_session(123)
        assert s.is_authorized is True

    def test_pre_authorized_chat_ids_get_boss_role(self):
        am = AuthManager(
            bot_password="secret",
            authorized_chat_ids="123,456",
        )
        s = am.get_session(123)
        assert s.is_authorized is True
        assert s.role == Role.BOSS

    def test_check_permission_authorized_boss(self):
        am = AuthManager(bot_password="", default_role=Role.BOSS)
        am.authenticate(123, "")
        assert am.check_permission(123, "can_change_settings") is True
        assert am.check_permission(123, "can_manage_users") is True

    def test_check_permission_viewer_cannot_control_ptz(self):
        am = AuthManager(bot_password="", default_role=Role.VIEWER)
        am.authenticate(123, "")
        assert am.check_permission(123, "can_control_ptz") is False
        assert am.check_permission(123, "can_view_live") is True

    def test_check_permission_unauthorized_returns_false(self):
        am = AuthManager(bot_password="secret")
        assert am.check_permission(123, "can_view_live") is False

    def test_set_role(self):
        am = AuthManager(bot_password="", default_role=Role.VIEWER)
        am.authenticate(123, "")
        assert am.set_role(123, Role.BOSS) is True
        assert am.get_session(123).role == Role.BOSS

    def test_revoke(self):
        am = AuthManager(bot_password="", default_role=Role.VIEWER)
        am.authenticate(123, "")
        assert am.revoke(123) is True
        assert am.get_session(123).is_authorized is False

    def test_list_authorized(self):
        am = AuthManager(bot_password="", default_role=Role.VIEWER)
        am.authenticate(123, "", display_name="User1")
        am.authenticate(456, "", display_name="User2")
        authorized = am.list_authorized()
        assert len(authorized) == 2
        chat_ids = [u["chat_id"] for u in authorized]
        assert 123 in chat_ids and 456 in chat_ids


# ─── CameraRegistry ────────────────────────────────────────────────────────

from services.telegram_bot.registry import CameraEntry, CameraRegistry, get_registry


class TestCameraRegistry:
    def test_empty_registry(self):
        reg = CameraRegistry()
        assert len(reg) == 0
        assert reg.list_all() == []
        assert "any_id" not in reg

    def test_register_and_get(self):
        reg = CameraRegistry()
        entry = CameraEntry(
            camera_id="cam_1",
            name="Front Door",
            snapshot_provider=lambda: b"fake",
            ptz_controller=MagicMock(),
            runtime=MagicMock(),
            state_machine=MagicMock(),
        )
        reg.register(entry)
        assert len(reg) == 1
        assert "cam_1" in reg
        assert reg.get("cam_1") == entry

    def test_register_overwrites_existing(self):
        reg = CameraRegistry()
        entry1 = CameraEntry(
            camera_id="cam_1", name="Old", snapshot_provider=lambda: None,
            ptz_controller=MagicMock(), runtime=MagicMock(), state_machine=MagicMock(),
        )
        entry2 = CameraEntry(
            camera_id="cam_1", name="New", snapshot_provider=lambda: None,
            ptz_controller=MagicMock(), runtime=MagicMock(), state_machine=MagicMock(),
        )
        reg.register(entry1)
        reg.register(entry2)
        assert reg.get("cam_1").name == "New"

    def test_unregister(self):
        reg = CameraRegistry()
        entry = CameraEntry(
            camera_id="cam_1", name="Test", snapshot_provider=lambda: None,
            ptz_controller=MagicMock(), runtime=MagicMock(), state_machine=MagicMock(),
        )
        reg.register(entry)
        reg.unregister("cam_1")
        assert len(reg) == 0
        assert reg.get("cam_1") is None

    def test_list_all_sorted(self):
        reg = CameraRegistry()
        for cid in ["cam_3", "cam_1", "cam_2"]:
            reg.register(CameraEntry(
                camera_id=cid, name=cid, snapshot_provider=lambda: None,
                ptz_controller=MagicMock(), runtime=MagicMock(), state_machine=MagicMock(),
            ))
        result = reg.list_all()
        assert [c.camera_id for c in result] == ["cam_1", "cam_2", "cam_3"]

    def test_global_registry_singleton(self):
        reg1 = get_registry()
        reg2 = get_registry()
        assert reg1 is reg2


# ─── SQLite persistence ────────────────────────────────────────────────────

from services.telegram_bot.storage import BotAuthStore, CameraSettingsStore


@pytest.fixture
def temp_db():
    """Temporary SQLite DB file for tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


class TestBotAuthStore:
    def test_upsert_and_get(self, temp_db):
        store = BotAuthStore(db_path=temp_db)
        store.upsert(123, "boss", "Test User")
        record = store.get(123)
        assert record is not None
        assert record["chat_id"] == 123
        assert record["role"] == "boss"
        assert record["display_name"] == "Test User"

    def test_get_nonexistent_returns_none(self, temp_db):
        store = BotAuthStore(db_path=temp_db)
        assert store.get(999) is None

    def test_delete(self, temp_db):
        store = BotAuthStore(db_path=temp_db)
        store.upsert(123, "viewer")
        store.delete(123)
        assert store.get(123) is None

    def test_list_all(self, temp_db):
        store = BotAuthStore(db_path=temp_db)
        store.upsert(123, "boss")
        store.upsert(456, "viewer")
        all_records = store.list_all()
        assert len(all_records) == 2

    def test_upsert_replaces_existing(self, temp_db):
        store = BotAuthStore(db_path=temp_db)
        store.upsert(123, "viewer", "Old")
        store.upsert(123, "boss", "New")
        record = store.get(123)
        assert record["role"] == "boss"
        assert record["display_name"] == "New"


class TestCameraSettingsStore:
    def test_set_and_get(self, temp_db):
        store = CameraSettingsStore(db_path=temp_db)
        store.set("cam_1", "manual_override_timeout", 10.0)
        assert store.get("cam_1", "manual_override_timeout") == 10.0

    def test_get_nonexistent_returns_default(self, temp_db):
        store = CameraSettingsStore(db_path=temp_db)
        assert store.get("cam_1", "manual_override_timeout", default=5.0) == 5.0

    def test_get_all_for_camera(self, temp_db):
        store = CameraSettingsStore(db_path=temp_db)
        store.set("cam_1", "manual_override_timeout", 7.0)
        store.set("cam_1", "home_settle_delay", 3.0)
        all_settings = store.get_all("cam_1")
        assert all_settings["manual_override_timeout"] == 7.0
        assert all_settings["home_settle_delay"] == 3.0

    def test_unknown_key_raises(self, temp_db):
        store = CameraSettingsStore(db_path=temp_db)
        with pytest.raises(ValueError):
            store.set("cam_1", "unknown_key", "value")
        with pytest.raises(ValueError):
            store.get("cam_1", "unknown_key")

    def test_delete(self, temp_db):
        store = CameraSettingsStore(db_path=temp_db)
        store.set("cam_1", "manual_override_timeout", 7.0)
        store.delete("cam_1", "manual_override_timeout")
        assert store.get("cam_1", "manual_override_timeout") is None

    def test_persistence_across_instances(self, temp_db):
        """Settings written by one store instance should be readable by another."""
        store1 = CameraSettingsStore(db_path=temp_db)
        store1.set("cam_1", "manual_override_timeout", 15.0)
        # Create a new store instance (simulates app restart)
        store2 = CameraSettingsStore(db_path=temp_db)
        assert store2.get("cam_1", "manual_override_timeout") == 15.0

    def test_allowed_keys_whitelist(self, temp_db):
        """Verify ALLOWED_KEYS contains expected settings."""
        expected = {
            "manual_override_timeout", "return_to_home_after_manual",
            "zoom_reset_on_home", "home_settle_delay",
            "frame_skip_rate", "jpeg_quality", "patrol_pan_speed",
            "rate_limit_seconds", "live_stream_interval",
        }
        assert expected.issubset(CameraSettingsStore.ALLOWED_KEYS)


# ─── AuthManager with persistence ──────────────────────────────────────────

class TestAuthManagerWithStore:
    def test_loads_from_store_on_init(self, temp_db):
        """AuthManager should load persisted chat_ids from store on startup."""
        store = BotAuthStore(db_path=temp_db)
        store.upsert(123, "boss", "Boss User")
        # Create AuthManager with the store — should load chat_id=123
        am = AuthManager(
            bot_password="any",
            store=store,
        )
        s = am.get_session(123)
        assert s.is_authorized is True
        assert s.role == Role.BOSS
        assert s.display_name == "Boss User"

    def test_persists_after_authenticate(self, temp_db):
        store = BotAuthStore(db_path=temp_db)
        am = AuthManager(bot_password="secret", store=store)
        am.authenticate(456, "secret", display_name="New User")
        # Should be persisted
        record = store.get(456)
        assert record is not None
        assert record["role"] == "viewer"


# ─── Keyboards ─────────────────────────────────────────────────────────────

from services.telegram_bot.keyboards import (
    access_button, camera_menu_kb, main_menu_kb, settings_menu_kb,
    user_actions_kb, users_menu_kb,
)


class TestKeyboards:
    def test_main_menu_empty_cameras(self):
        kb = main_menu_kb([], current_chat_role="boss")
        assert kb.inline_keyboard is not None

    def test_main_menu_with_cameras(self):
        cam = MagicMock()
        cam.camera_id = "cam_1"
        cam.name = "Front"
        cam.state_machine.mode.value = "PATROL"
        kb = main_menu_kb([cam], current_chat_role="boss")
        assert len(kb.inline_keyboard) >= 1

    def test_camera_menu_viewer_no_ptz(self):
        """Viewer role should not see PTZ controls."""
        kb = camera_menu_kb("cam_1", role="viewer", ai_enabled=False, live_active=False)
        # Find any PTZ button by checking callback_data prefix
        has_ptz = any(
            btn.callback_data.startswith("move:")
            for row in kb.inline_keyboard
            for btn in row
        )
        assert has_ptz is False, "Viewer should not see PTZ controls"

    def test_camera_menu_guard_has_ptz(self):
        """Guard role should see PTZ controls."""
        kb = camera_menu_kb("cam_1", role="guard", ai_enabled=False, live_active=False)
        has_ptz = any(
            btn.callback_data.startswith("move:")
            for row in kb.inline_keyboard
            for btn in row
        )
        assert has_ptz is True

    def test_camera_menu_boss_has_settings(self):
        """Boss role should see Settings button."""
        kb = camera_menu_kb("cam_1", role="boss", ai_enabled=False, live_active=False)
        has_settings = any(
            btn.callback_data.startswith("settings:")
            for row in kb.inline_keyboard
            for btn in row
        )
        assert has_settings is True

    def test_camera_menu_viewer_no_settings(self):
        """Viewer role should NOT see Settings button."""
        kb = camera_menu_kb("cam_1", role="viewer", ai_enabled=False, live_active=False)
        has_settings = any(
            btn.callback_data.startswith("settings:")
            for row in kb.inline_keyboard
            for btn in row
        )
        assert has_settings is False

    def test_settings_menu(self):
        kb = settings_menu_kb("cam_1")
        assert kb.inline_keyboard is not None

    def test_users_menu_empty(self):
        kb = users_menu_kb([])
        assert kb.inline_keyboard is not None

    def test_users_menu_with_users(self):
        users = [
            {"chat_id": 123, "role": "boss", "name": "Boss"},
            {"chat_id": 456, "role": "viewer", "name": "Viewer"},
        ]
        kb = users_menu_kb(users)
        assert len(kb.inline_keyboard) >= 2

    def test_user_actions_kb(self):
        kb = user_actions_kb(123)
        assert kb.inline_keyboard is not None

    def test_access_button_with_url(self):
        btn = access_button("cam_1", "https://example.com")
        assert btn is not None
        assert btn.url == "https://example.com"

    def test_access_button_no_url_returns_none(self):
        btn = access_button("cam_1", "")
        assert btn is None


# ─── TelegramBotService lifecycle ──────────────────────────────────────────

from services.telegram_bot.bot_service import TelegramBotService


class TestTelegramBotService:
    def test_is_available_no_token(self):
        am = AuthManager(bot_password="test")
        reg = CameraRegistry()
        svc = TelegramBotService(
            token="", auth_manager=am, registry=reg,
        )
        assert svc.is_available is False

    def test_is_available_with_token(self):
        am = AuthManager(bot_password="test")
        reg = CameraRegistry()
        svc = TelegramBotService(
            token="fake_token", auth_manager=am, registry=reg,
        )
        # aiogram might not be installed in test env — is_available depends on it
        # Just check it doesn't crash
        assert isinstance(svc.is_available, bool)

    def test_start_without_token_does_nothing(self):
        am = AuthManager(bot_password="test")
        reg = CameraRegistry()
        svc = TelegramBotService(
            token="", auth_manager=am, registry=reg,
        )
        svc.start()  # should be no-op
        assert svc.is_running is False

    def test_stop_when_not_running(self):
        am = AuthManager(bot_password="test")
        reg = CameraRegistry()
        svc = TelegramBotService(
            token="", auth_manager=am, registry=reg,
        )
        svc.stop()  # should not raise


# ─── TelegramNotificationProvider (Part B updates) ─────────────────────────

from services.notifications import NotificationEvent, TelegramNotificationProvider


class TestTelegramProviderPartB:
    def test_format_text_includes_camera_id(self):
        """Bug B: notification messages should include camera_id."""
        p = TelegramNotificationProvider(
            token="abc", chat_id="123",
            camera_name="Front Door", camera_id="cam_3",
        )
        ev = NotificationEvent(event_type="target_detected", message="Test")
        text = p._format_text(ev)
        assert "cam_3" in text
        assert "Front Door" in text

    def test_no_inline_keyboard_without_base_url(self):
        p = TelegramNotificationProvider(
            token="abc", chat_id="123",
            camera_name="Test", camera_id="cam_1",
            public_base_url="",
        )
        kb = p._build_inline_keyboard()
        assert kb is None

    def test_inline_keyboard_with_base_url(self):
        p = TelegramNotificationProvider(
            token="abc", chat_id="123",
            camera_name="Test", camera_id="cam_1",
            public_base_url="https://boras.example.com",
        )
        kb = p._build_inline_keyboard()
        assert kb is not None
        assert "inline_keyboard" in kb
        assert kb["inline_keyboard"][0][0]["url"] == "https://boras.example.com"

    def test_send_text_with_inline_keyboard(self):
        """When public_base_url is set, sendMessage should include reply_markup."""
        p = TelegramNotificationProvider(
            token="fake_token", chat_id="fake_chat",
            camera_name="Test", camera_id="cam_1",
            public_base_url="https://boras.example.com",
        )
        ev = NotificationEvent(event_type="error", message="Test error")
        with patch("services.notifications.telegram_provider.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            result = p.send(ev)
        assert result is True
        # Verify reply_markup was included in the payload
        call_kwargs = mock_post.call_args[1]["json"]
        assert "reply_markup" in call_kwargs
        assert "inline_keyboard" in call_kwargs["reply_markup"]
