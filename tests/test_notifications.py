"""Тесты для NotificationService и TelegramNotificationProvider."""
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.events import EventLog
from services.notifications import (
    NotificationEvent,
    NotificationProvider,
    TelegramNotificationProvider,
)
from services.notification_service import NotificationService, _EVENT_MAP


# ─── Fake providers для тестов ─────────────────────────────────────────────

class FakeProvider(NotificationProvider):
    """Записывает все отправленные события в self.sent для проверок."""
    def __init__(self, name="fake", configured=True, should_succeed=True):
        self._name = name
        self._configured = configured
        self._should_succeed = should_succeed
        self.sent = []
    @property
    def name(self):
        return self._name
    def is_configured(self):
        return self._configured
    def send(self, event):
        self.sent.append(event)
        return self._should_succeed


class FailingProvider(NotificationProvider):
    """Provider который всегда поднимает исключение — для проверки resilience."""
    @property
    def name(self):
        return "failing"
    def send(self, event):
        raise RuntimeError("Simulated provider crash")


# ─── NotificationEvent / NotificationProvider ──────────────────────────────

class TestNotificationEvent:
    def test_event_creation_minimal(self):
        ev = NotificationEvent(event_type="target_detected", message="Test")
        assert ev.event_type == "target_detected"
        assert ev.message == "Test"
        assert ev.detail == ""
        assert ev.snapshot is None
        assert ev.timestamp is not None

    def test_event_with_snapshot(self):
        ev = NotificationEvent(
            event_type="target_detected",
            message="Test",
            snapshot=b"fake_jpeg_bytes",
        )
        assert ev.snapshot == b"fake_jpeg_bytes"


class TestNotificationProviderAbstract:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            NotificationProvider()

    def test_subclass_must_implement_send(self):
        # Missing send() and name property → can't instantiate
        class Incomplete(NotificationProvider):
            pass
        with pytest.raises(TypeError):
            Incomplete()


# ─── TelegramNotificationProvider ──────────────────────────────────────────

class TestTelegramProvider:
    def test_is_configured_with_token_and_chat_id(self):
        p = TelegramNotificationProvider(token="abc", chat_id="123")
        assert p.is_configured() is True

    def test_not_configured_without_token(self):
        p = TelegramNotificationProvider(token="", chat_id="123")
        assert p.is_configured() is False

    def test_not_configured_without_chat_id(self):
        p = TelegramNotificationProvider(token="abc", chat_id="")
        assert p.is_configured() is False

    def test_name_property(self):
        p = TelegramNotificationProvider(token="abc", chat_id="123")
        assert p.name == "telegram"

    def test_send_returns_false_when_not_configured(self):
        p = TelegramNotificationProvider(token="", chat_id="")
        ev = NotificationEvent(event_type="error", message="test")
        assert p.send(ev) is False

    def test_format_text_includes_emoji_and_camera_name(self):
        p = TelegramNotificationProvider(token="abc", chat_id="123", camera_name="Front Door")
        ev = NotificationEvent(
            event_type="target_detected",
            message="Обнаружен человек",
            detail="confidence: 87%",
        )
        text = p._format_text(ev)
        assert "🚨" in text
        assert "Front Door" in text
        assert "Обнаружен человек" in text or "confidence: 87%" in text

    def test_send_text_success(self):
        """Проверяем что send() вызывает requests.post и возвращает True при 200."""
        p = TelegramNotificationProvider(token="fake_token", chat_id="fake_chat")
        ev = NotificationEvent(event_type="error", message="Test error")
        with patch("services.notifications.telegram_provider.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            result = p.send(ev)
        assert result is True
        assert mock_post.called

    def test_send_text_http_error_returns_false(self):
        """При HTTP 400 — возвращаем False, не падаем."""
        p = TelegramNotificationProvider(token="fake_token", chat_id="fake_chat")
        ev = NotificationEvent(event_type="error", message="Test error")
        with patch("services.notifications.telegram_provider.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = "Bad Request"
            mock_post.return_value = mock_resp
            result = p.send(ev)
        assert result is False

    def test_send_network_error_returns_false(self):
        """При network exception — возвращаем False, не падаем."""
        import requests
        p = TelegramNotificationProvider(token="fake_token", chat_id="fake_chat")
        ev = NotificationEvent(event_type="error", message="Test error")
        with patch("services.notifications.telegram_provider.requests.post",
                   side_effect=requests.exceptions.ConnectionError("no network")):
            result = p.send(ev)
        assert result is False

    def test_send_photo_with_snapshot(self):
        """При наличии snapshot — sendPhoto вызывается."""
        p = TelegramNotificationProvider(token="fake_token", chat_id="fake_chat")
        ev = NotificationEvent(
            event_type="target_detected",
            message="Person detected",
            snapshot=b"fake_jpeg_bytes",
        )
        with patch("services.notifications.telegram_provider.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            result = p.send(ev)
        assert result is True
        # Должен быть вызван sendPhoto endpoint
        called_url = mock_post.call_args[0][0]
        assert "sendPhoto" in called_url

    def test_send_photo_fallback_to_text_on_error(self):
        """Если sendPhoto падает (HTTP != 200) — fallback на sendMessage."""
        p = TelegramNotificationProvider(token="fake_token", chat_id="fake_chat")
        ev = NotificationEvent(
            event_type="target_detected",
            message="Person detected",
            snapshot=b"fake_jpeg_bytes",
        )
        with patch("services.notifications.telegram_provider.requests.post") as mock_post:
            # First call (sendPhoto) fails, second (sendMessage fallback) succeeds
            fail_resp = MagicMock()
            fail_resp.status_code = 400
            fail_resp.text = "Bad Request"
            ok_resp = MagicMock()
            ok_resp.status_code = 200
            mock_post.side_effect = [fail_resp, ok_resp]
            result = p.send(ev)
        assert result is True
        assert mock_post.call_count == 2  # photo + fallback text


# ─── NotificationService ───────────────────────────────────────────────────

@pytest.fixture
def events_log():
    return EventLog()


@pytest.fixture
def fake_provider():
    return FakeProvider()


@pytest.fixture
def notification_service(events_log, fake_provider):
    """NotificationService с fake provider и быстрым poll_interval для тестов."""
    from config import NotificationConfig
    cfg = NotificationConfig(
        enabled=True,
        rate_limit_seconds=0.0,  # отключаем rate limit в тестах
        poll_interval=0.05,      # быстро опрашиваем
        notify_on=("target_detected", "target_lost", "error", "disconnected"),
    )
    return NotificationService(
        events=events_log,
        providers=[fake_provider],
        config=cfg,
    )


class TestNotificationServiceLifecycle:
    def test_does_not_start_when_disabled(self, events_log, fake_provider):
        from config import NotificationConfig
        cfg = NotificationConfig(enabled=False)
        svc = NotificationService(
            events=events_log, providers=[fake_provider], config=cfg,
        )
        svc.start()
        assert svc.is_running is False
        svc.stop()

    def test_does_not_start_without_providers(self, events_log):
        from config import NotificationConfig
        cfg = NotificationConfig(enabled=True)
        svc = NotificationService(
            events=events_log, providers=[], config=cfg,
        )
        svc.start()
        assert svc.is_running is False  # нет провайдеров
        svc.stop()

    def test_start_stop_lifecycle(self, notification_service):
        notification_service.start()
        assert notification_service.is_running is True
        time.sleep(0.2)  # даём потоку поработать
        notification_service.stop()
        assert notification_service.is_running is False


class TestNotificationServiceEventDispatch:
    def test_target_detected_triggers_notification(self, notification_service, fake_provider, events_log):
        notification_service.start()
        try:
            events_log.emit("target_detected", "")
            time.sleep(0.3)  # ждём пока poll цикл отработает
            assert len(fake_provider.sent) == 1
            assert fake_provider.sent[0].event_type == "target_detected"
        finally:
            notification_service.stop()

    def test_target_lost_triggers_notification(self, notification_service, fake_provider, events_log):
        notification_service.start()
        try:
            events_log.emit("target_lost", "")
            time.sleep(0.3)
            assert len(fake_provider.sent) == 1
            assert fake_provider.sent[0].event_type == "target_lost"
        finally:
            notification_service.stop()

    def test_error_event_triggers_notification(self, notification_service, fake_provider, events_log):
        notification_service.start()
        try:
            events_log.emit("error", "ptz_http_500:move")
            time.sleep(0.3)
            assert len(fake_provider.sent) == 1
            assert "ptz_http_500" in fake_provider.sent[0].detail
        finally:
            notification_service.stop()

    def test_ignored_events_not_notified(self, notification_service, fake_provider, events_log):
        """frame_received, move_started и т.д. не должны триггерить notification."""
        notification_service.start()
        try:
            events_log.emit("frame_received", "")
            events_log.emit("move_started", "pan=0.5")
            events_log.emit("state_changed", "IDLE->PATROL")
            time.sleep(0.3)
            assert len(fake_provider.sent) == 0
        finally:
            notification_service.stop()


class TestNotificationServiceRateLimit:
    def test_rate_limit_prevents_spam(self, events_log, fake_provider):
        from config import NotificationConfig
        cfg = NotificationConfig(
            enabled=True,
            rate_limit_seconds=0.5,  # 500ms rate limit
            poll_interval=0.05,
        )
        svc = NotificationService(
            events=events_log, providers=[fake_provider], config=cfg,
        )
        svc.start()
        try:
            # Отправляем 3 события target_detected подряд
            events_log.emit("target_detected", "")
            time.sleep(0.1)
            events_log.emit("target_detected", "")
            time.sleep(0.1)
            events_log.emit("target_detected", "")
            time.sleep(0.2)
            # Должно отправиться только 1 (rate limit)
            assert len(fake_provider.sent) == 1
        finally:
            svc.stop()

    def test_different_event_types_not_rate_limited_against_each_other(self, events_log, fake_provider):
        """Rate limit применяется per event_type — target_detected и error
        не должны блокировать друг друга."""
        from config import NotificationConfig
        cfg = NotificationConfig(
            enabled=True,
            rate_limit_seconds=10.0,  # большой rate limit
            poll_interval=0.05,
        )
        svc = NotificationService(
            events=events_log, providers=[fake_provider], config=cfg,
        )
        svc.start()
        try:
            events_log.emit("target_detected", "")
            events_log.emit("error", "some_error")
            time.sleep(0.3)
            # Оба должны отправиться — разные event_type
            assert len(fake_provider.sent) == 2
        finally:
            svc.stop()


class TestNotificationServiceResilience:
    def test_failing_provider_does_not_crash_service(self, events_log):
        """Если provider поднимает исключение — сервис должен продолжить работу."""
        from config import NotificationConfig
        failing = FailingProvider()
        ok_provider = FakeProvider(name="ok")
        cfg = NotificationConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            poll_interval=0.05,
        )
        svc = NotificationService(
            events=events_log,
            providers=[failing, ok_provider],
            config=cfg,
        )
        svc.start()
        try:
            events_log.emit("target_detected", "")
            time.sleep(0.3)
            # Сервис не упал, ok_provider получил событие
            assert svc.is_running is True
            assert len(ok_provider.sent) == 1
        finally:
            svc.stop()


class TestNotificationServiceStartupSkipsOldEvents:
    def test_does_not_notify_on_preexisting_events(self, events_log, fake_provider):
        """При старте сервиса события которые уже в логе не должны триггерить
        уведомления (иначе при рестарте с человеком в кадре будет спам)."""
        # Сначала добавляем событие БЕЗ запущенного сервиса
        events_log.emit("target_detected", "")
        time.sleep(0.05)

        from config import NotificationConfig
        cfg = NotificationConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            poll_interval=0.05,
        )
        svc = NotificationService(
            events=events_log, providers=[fake_provider], config=cfg,
        )
        svc.start()
        try:
            time.sleep(0.3)
            # Старое событие не должно отправиться
            assert len(fake_provider.sent) == 0
        finally:
            svc.stop()


class TestNotificationServiceSnapshot:
    def test_snapshot_attached_to_target_detected(self, events_log, fake_provider, monkeypatch):
        """При target_detected — вызывается snapshot_provider и прикрепляется к event.
        Monkeypatch time.sleep в notification_service чтобы пропустить 2.5s snapshot delay."""
        # Replace only notification_service's time.sleep to skip the 2.5s snapshot delay.
        # We can't replace all time.sleep because poll_interval also uses it.
        import services.notification_service as ns_module

        real_sleep = ns_module.time.sleep

        def fast_sleep(seconds):
            # Skip only long sleeps (the 2.5s snapshot delay); keep short ones (poll)
            if seconds >= 2.0:
                real_sleep(0.01)
            else:
                real_sleep(seconds)

        monkeypatch.setattr(ns_module.time, "sleep", fast_sleep)

        snapshot_calls = []
        def snapshot_provider():
            snapshot_calls.append(True)
            return b"fake_jpeg"
        from config import NotificationConfig
        cfg = NotificationConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            poll_interval=0.05,
        )
        svc = NotificationService(
            events=events_log,
            providers=[fake_provider],
            snapshot_provider=snapshot_provider,
            config=cfg,
        )
        svc.start()
        try:
            events_log.emit("target_detected", "")
            # Wait enough for: poll (0.05) + fast_sleep(0.01) + snapshot + send
            real_sleep(0.5)
            assert len(fake_provider.sent) == 1
            assert snapshot_calls == [True]
            assert fake_provider.sent[0].snapshot == b"fake_jpeg"
        finally:
            svc.stop()

    def test_snapshot_not_attached_to_other_events(self, events_log, fake_provider):
        """Только target_detected получает snapshot — error/target_lost без фото."""
        snapshot_calls = []
        def snapshot_provider():
            snapshot_calls.append(True)
            return b"fake_jpeg"
        from config import NotificationConfig
        cfg = NotificationConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            poll_interval=0.05,
        )
        svc = NotificationService(
            events=events_log,
            providers=[fake_provider],
            snapshot_provider=snapshot_provider,
            config=cfg,
        )
        svc.start()
        try:
            events_log.emit("error", "test")
            time.sleep(0.3)
            assert len(fake_provider.sent) == 1
            assert snapshot_calls == []  # snapshot не вызывался
            assert fake_provider.sent[0].snapshot is None
        finally:
            svc.stop()
