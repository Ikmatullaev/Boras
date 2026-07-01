"""Тесты для B3 — Soft manual override + Bug 1/2/3 fixes.

Логика:
  - manual_override() переводит state_machine в MANUAL и запоминает timestamp
  - Любой новый вызов manual_override() продлевает timeout
  - Если _manual_override_timeout секунд не было новых команд —
    processing loop автоматически:
      1. (опционально) вызывает goto_home() — возврат в центр (Bug 2)
      2. ждёт home_settle_delay секунд (даёт AbsoluteMove отработать)
      3. включает auto_guard → PATROL
  - Если timeout == 0 — soft override отключён (legacy behavior)

Bug 1 fix: manual_override() ВСЕГДА запоминает timestamp, даже если AI
был выключен заранее через toggle_guard. Раньше в этом случае таймер
не запускался и камера оставалась в MANUAL вечно.

Bug 2 fix: после timeout вызывается goto_home() + ptz.stop_zoom(), и
только после home_settle_delay секунд включается auto_guard.

Bug 3 fix: default timeout изменён с 10s на 5s.
"""
import time
from unittest.mock import patch

import pytest

from app_compose import compose_app
from core.state_machine import CraneMode


@pytest.fixture
def runtime():
    """Свежий runtime для каждого теста с FakePTZ (без реальных HTTP запросов).

    compose_app() создаёт реальный CranePTZ который при stop() делает HTTP
    запрос к камере — в тестах это недопустимо (таймаут 2s × много тестов).
    Поэтому подменяем ptz на FakePTZ после compose_app.
    """
    from conftest import FakePTZ
    comps = compose_app()
    rt = comps["runtime"]
    fake_ptz = FakePTZ()
    rt.ptz = fake_ptz
    # Также подменяем в brain и operator, чтобы все ссылки были согласованы
    comps["brain"].ptz = fake_ptz
    comps["operator"].ptz = fake_ptz
    # Для предсказуемости тестов отключаем home_settle_delay
    rt._home_settle_delay = 0.0
    return rt


class TestManualOverrideEntersManualMode:
    """B3: manual_override переводит state_machine в MANUAL."""

    def test_manual_override_enters_manual_mode(self, runtime):
        runtime.state_machine.enable_auto_guard()  # PATROL
        runtime.manual_override()
        assert runtime.state_machine.mode == CraneMode.MANUAL
        assert runtime.state_machine.auto_guard_enabled is False

    def test_manual_override_records_timestamp(self, runtime):
        runtime.state_machine.enable_auto_guard()
        assert runtime._last_manual_command_time is None
        runtime.manual_override()
        assert runtime._last_manual_command_time is not None

    def test_manual_override_emits_event(self, runtime):
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        events = runtime.events.recent()
        assert any(e["name"] == "manual_override" and e["detail"] == "soft" for e in events)

    def test_manual_override_stops_ptz(self, runtime):
        """При переходе из auto-guard в MANUAL — ptz.stop() должен вызываться.
        FakePTZ уже подменён в фикстуре runtime."""
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        # runtime.ptz — это FakePTZ, записывает все вызовы в .calls
        assert any(call[0] == "stop" for call in runtime.ptz.calls), \
            "ptz.stop() should be called during manual_override"


class TestManualOverrideExtendsTimeout:
    """B3: новый вызов manual_override продлевает timeout."""

    def test_second_call_updates_timestamp(self, runtime):
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        first_ts = runtime._last_manual_command_time
        # Небольшая пауза чтобы timestamp точно отличался
        time.sleep(0.01)
        runtime.manual_override()
        second_ts = runtime._last_manual_command_time
        assert second_ts > first_ts


class TestSoftOverrideAutoReturn:
    """B3 + Bug 2: после timeout — processing loop вызывает goto_home() и
    возвращает PATROL автоматически."""

    def test_check_timeout_returns_to_patrol(self, runtime):
        """Если timeout истёк — _check_manual_override_timeout включает auto_guard.
        С _home_settle_delay=0 (установлено в фикстуре) — возврат мгновенный."""
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        assert runtime.state_machine.mode == CraneMode.MANUAL

        # Имитируем что прошло больше времени чем timeout
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        runtime._check_manual_override_timeout()

        assert runtime.state_machine.mode == CraneMode.PATROL
        assert runtime.state_machine.auto_guard_enabled is True
        assert runtime._last_manual_command_time is None

    def test_check_timeout_emits_expired_event(self, runtime):
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        runtime._check_manual_override_timeout()
        events = runtime.events.recent()
        assert any(e["name"] == "manual_override_expired" for e in events)

    def test_check_timeout_calls_goto_home(self, runtime):
        """Bug 2 fix: после timeout должен вызываться goto_home() для возврата
        в центр."""
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        # Очищаем историю вызовов до проверки
        runtime.ptz.calls.clear()
        runtime._check_manual_override_timeout()
        # Должен быть вызван goto_home
        goto_calls = [c for c in runtime.ptz.calls if c[0] == "goto_home"]
        assert len(goto_calls) >= 1, "goto_home should be called after timeout expiry"

    def test_check_timeout_calls_stop_zoom_when_zoom_reset_enabled(self, runtime):
        """Bug 2 fix: при zoom_reset_on_home=True должен вызываться stop_zoom()
        перед goto_home, чтобы остановить continuous zoom."""
        runtime._zoom_reset_on_home = True
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        runtime.ptz.calls.clear()
        runtime._check_manual_override_timeout()
        stop_zoom_calls = [c for c in runtime.ptz.calls if c[0] == "stop_zoom"]
        assert len(stop_zoom_calls) >= 1, "stop_zoom should be called when zoom_reset_on_home=True"

    def test_check_timeout_no_action_before_expiry(self, runtime):
        """Если timeout ещё не истёк — остаёмся в MANUAL."""
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        # timestamp только что установлен — timeout не истёк
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.MANUAL
        assert runtime._last_manual_command_time is not None

    def test_check_timeout_no_action_when_not_in_manual(self, runtime):
        """Если мы не в MANUAL (например в IDLE) — ничего не делаем."""
        runtime._last_manual_command_time = time.monotonic() - 1000  # очень старое
        # state_machine в IDLE по умолчанию
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.IDLE

    def test_check_timeout_no_action_when_no_manual_command(self, runtime):
        """Если _last_manual_command_time is None — ничего не делаем."""
        runtime.state_machine.enable_auto_guard()  # PATROL
        assert runtime._last_manual_command_time is None
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.PATROL  # не изменилось


class TestSoftOverrideHomeSettleDelay:
    """Bug 2 fix: home_settle_delay — пауза между goto_home() и
    включением auto_guard, чтобы AbsoluteMove успел отработать."""

    def test_settle_delay_delays_enable_auto_guard(self, runtime):
        """Если home_settle_delay > 0 — после goto_home ждём перед PATROL."""
        runtime._home_settle_delay = 1.0
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)

        # Первый вызов — должен вызвать goto_home и начать ждать
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.MANUAL, \
            "Should still be in MANUAL during settle delay"
        assert runtime._home_return_started_at is not None, \
            "home_return_started_at should be set during settle delay"

        # Имитируем что settle delay ещё не прошёл
        runtime._home_return_started_at = time.monotonic() - 0.5  # 0.5s из 1.0s
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.MANUAL, \
            "Should still be in MANUAL if settle delay not yet elapsed"

        # Имитируем что settle delay прошёл
        runtime._home_return_started_at = time.monotonic() - 2.0  # 2.0s > 1.0s
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.PATROL, \
            "Should transition to PATROL after settle delay"
        assert runtime._home_return_started_at is None

    def test_settle_delay_zero_skips_waiting(self, runtime):
        """Если home_settle_delay == 0 — переходим в PATROL сразу после goto_home."""
        runtime._home_settle_delay = 0.0
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        runtime._check_manual_override_timeout()
        assert runtime.state_machine.mode == CraneMode.PATROL
        assert runtime._home_return_started_at is None


class TestSoftOverrideReturnToHomeDisabled:
    """Bug 2 fix: если return_to_home_after_manual=False — goto_home не вызывается."""

    def test_no_goto_home_when_disabled(self, runtime):
        runtime._return_to_home_after_manual = False
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        runtime.ptz.calls.clear()
        runtime._check_manual_override_timeout()
        goto_calls = [c for c in runtime.ptz.calls if c[0] == "goto_home"]
        assert len(goto_calls) == 0, "goto_home should NOT be called when return_to_home_after_manual=False"
        assert runtime.state_machine.mode == CraneMode.PATROL


class TestSoftOverrideDisabledWhen:
    """B3: если timeout == 0 — soft override отключён (legacy behavior)."""

    def test_timeout_zero_disables_soft_override(self, runtime):
        runtime._manual_override_timeout = 0.0
        runtime.state_machine.enable_auto_guard()
        runtime.manual_override()
        # Даже если время сильно в прошлом — _check не должен возвращать
        runtime._last_manual_command_time = time.monotonic() - 1000
        runtime._check_manual_override_timeout()
        # Остаются в MANUAL — soft override отключён
        assert runtime.state_machine.mode == CraneMode.MANUAL


class TestBug1ManualOverrideWhenAIOff:
    """Bug 1 fix: manual_override() ВСЕГДА запоминает timestamp, даже если
    AI был выключен заранее через toggle_guard off.

    Сценарий: охранник выключил AI, повернул камеру вниз, ушёл. Раньше таймер
    не запускался, камера оставалась в MANUAL вечно. Теперь — через timeout
    AI автоматически включится и камера вернётся в центр.
    """

    def test_manual_override_when_ai_off_records_timestamp(self, runtime):
        """AI выключен через toggle_guard → manual_override всё равно запоминает время."""
        runtime.state_machine.enable_auto_guard()  # PATROL
        runtime.toggle_guard()  # выключаем AI → IDLE
        assert runtime.state_machine.mode == CraneMode.IDLE
        assert runtime._last_manual_command_time is None

        # Охранник даёт ручную команду
        runtime.manual_override()
        assert runtime.state_machine.mode == CraneMode.MANUAL
        assert runtime._last_manual_command_time is not None, \
            "Bug 1: timestamp MUST be recorded even when AI was off"

    def test_manual_override_when_ai_off_auto_reenables_after_timeout(self, runtime):
        """После timeout AI должен включиться, даже если был выключен заранее."""
        runtime.state_machine.enable_auto_guard()
        runtime.toggle_guard()  # AI off → IDLE
        runtime.manual_override()  # охранник тронул камеру
        assert runtime.state_machine.mode == CraneMode.MANUAL

        # Имитируем что timeout прошёл
        runtime._last_manual_command_time = time.monotonic() - (runtime._manual_override_timeout + 1)
        runtime._check_manual_override_timeout()

        assert runtime.state_machine.mode == CraneMode.PATROL, \
            "Bug 1: AI should auto-re-enable even if it was off before manual command"
        assert runtime.state_machine.auto_guard_enabled is True

    def test_toggle_guard_off_clears_timer(self, runtime):
        """При ручном выключении AI через toggle_guard (когда AI включён) —
        таймер сбрасывается, чтобы автоматическое re-enable не сработало неожиданно."""
        runtime.state_machine.enable_auto_guard()  # AI on → PATROL
        assert runtime.auto_guard_enabled is True
        # Теперь выключаем AI
        result = runtime.toggle_guard()
        assert result == "off"
        assert runtime._last_manual_command_time is None
        assert runtime._home_return_started_at is None


class TestSoftOverrideConfigIntegration:
    """B3 + Bug 3: настройка из config.py."""

    def test_default_timeout_is_5_seconds(self):
        """Bug 3 fix: default timeout изменён с 10s на 5s."""
        from config import settings
        assert settings.operator.manual_override_timeout == 5.0

    def test_env_override_manual_override_timeout(self, monkeypatch):
        monkeypatch.setenv("CRANE_MANUAL_OVERRIDE_TIMEOUT", "30")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.operator.manual_override_timeout == 30.0
        finally:
            monkeypatch.delenv("CRANE_MANUAL_OVERRIDE_TIMEOUT")
            importlib.reload(config_module)

    def test_return_to_home_after_manual_default_true(self):
        """Bug 2 fix: return_to_home_after_manual по умолчанию True."""
        from config import settings
        assert settings.operator.return_to_home_after_manual is True

    def test_zoom_reset_on_home_default_true(self):
        """Bug 2 fix: zoom_reset_on_home по умолчанию True."""
        from config import settings
        assert settings.operator.zoom_reset_on_home is True

    def test_home_settle_delay_default_2_seconds(self):
        """Bug 2 fix: home_settle_delay по умолчанию 2.0 секунды."""
        from config import settings
        assert settings.operator.home_settle_delay == 2.0

    def test_env_override_return_to_home(self, monkeypatch):
        monkeypatch.setenv("CRANE_RETURN_TO_HOME_AFTER_MANUAL", "0")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.operator.return_to_home_after_manual is False
        finally:
            monkeypatch.delenv("CRANE_RETURN_TO_HOME_AFTER_MANUAL")
            importlib.reload(config_module)

    def test_env_override_zoom_reset_on_home(self, monkeypatch):
        monkeypatch.setenv("CRANE_ZOOM_RESET_ON_HOME", "0")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.operator.zoom_reset_on_home is False
        finally:
            monkeypatch.delenv("CRANE_ZOOM_RESET_ON_HOME")
            importlib.reload(config_module)

    def test_env_override_home_settle_delay(self, monkeypatch):
        monkeypatch.setenv("CRANE_HOME_SETTLE_DELAY", "5.0")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.operator.home_settle_delay == 5.0
        finally:
            monkeypatch.delenv("CRANE_HOME_SETTLE_DELAY")
            importlib.reload(config_module)
