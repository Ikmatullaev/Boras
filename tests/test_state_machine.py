"""Юнит-тесты для CraneStateMachine."""
import pytest

from core.state_machine import CraneMode, CraneStateMachine


class TestInitialState:
    def test_initial_mode_is_idle(self):
        sm = CraneStateMachine()
        assert sm.mode == CraneMode.IDLE

    def test_auto_guard_disabled_initially(self):
        sm = CraneStateMachine()
        assert sm.auto_guard_enabled is False


class TestAutoGuardProperty:
    @pytest.mark.parametrize("mode,expected", [
        (CraneMode.IDLE, False),
        (CraneMode.PATROL, True),
        (CraneMode.TRACKING, True),
        (CraneMode.MANUAL, False),
    ])
    def test_auto_guard_enabled_by_mode(self, mode, expected):
        sm = CraneStateMachine()
        sm.mode = mode  # прямой выставление для теста
        assert sm.auto_guard_enabled is expected


class TestTransitions:
    def test_enable_auto_guard_transitions_to_patrol(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        assert sm.mode == CraneMode.PATROL

    def test_enable_auto_guard_emits_state_changed(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        recent = events.recent()
        assert any(e["name"] == "state_changed" and "IDLE->PATROL" in e["detail"] for e in recent)

    def test_disable_auto_guard_transitions_to_idle(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        sm.disable_auto_guard()
        assert sm.mode == CraneMode.IDLE

    def test_disable_auto_guard_emits_state_changed(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        events.recent()  # сбросим view
        sm.disable_auto_guard()
        recent = events.recent()
        assert any(e["name"] == "state_changed" and "PATROL->IDLE" in e["detail"] for e in recent)

    def test_enter_tracking_only_when_auto_guard_enabled(self, events):
        """Если auto_guard выключен (IDLE/MANUAL), enter_tracking не должен
        переводить в TRACKING."""
        sm = CraneStateMachine(events=events)
        # IDLE → enter_tracking должен быть no-op
        sm.enter_tracking()
        assert sm.mode == CraneMode.IDLE

    def test_enter_tracking_when_patrol(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()  # PATROL
        sm.enter_tracking()
        assert sm.mode == CraneMode.TRACKING

    def test_enter_tracking_when_tracking_is_noop(self, events):
        """Если уже в TRACKING — переход не должен генерировать событие."""
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        sm.enter_tracking()
        # Считаем события state_changed до повторного вызова
        before = len([e for e in events.recent(limit=1000) if e["name"] == "state_changed"])
        sm.enter_tracking()  # повторный вызов (no-op)
        after = len([e for e in events.recent(limit=1000) if e["name"] == "state_changed"])
        # Количество state_changed не должно увеличиться
        assert after == before, f"Expected no new state_changed, got {after - before} new events"

    def test_enter_patrol_only_when_auto_guard_enabled(self, events):
        sm = CraneStateMachine(events=events)
        # IDLE → enter_patrol должен быть no-op
        sm.enter_patrol()
        assert sm.mode == CraneMode.IDLE

    def test_enter_patrol_when_tracking(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        sm.enter_tracking()
        sm.enter_patrol()
        assert sm.mode == CraneMode.PATROL

    def test_enter_manual_from_any_state(self, events):
        """MANUAL доступен из любого состояния — оператор всегда может
        перехватить управление."""
        for initial_mode in [CraneMode.IDLE, CraneMode.PATROL, CraneMode.TRACKING]:
            sm = CraneStateMachine(events=events)
            sm.mode = initial_mode
            sm.enter_manual()
            assert sm.mode == CraneMode.MANUAL, f"Failed from {initial_mode}"

    def test_enter_manual_emits_state_changed(self, events):
        sm = CraneStateMachine(events=events)
        sm.enter_manual()
        recent = events.recent()
        assert any(e["name"] == "state_changed" and "IDLE->MANUAL" in e["detail"] for e in recent)

    def test_same_mode_transition_is_noop_no_event(self, events):
        """Переход в тот же режим не должен генерировать событие."""
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()  # PATROL
        before = len([e for e in events.recent(limit=1000) if e["name"] == "state_changed"])
        sm.transition(CraneMode.PATROL)  # попытка перейти в тот же режим
        after = len([e for e in events.recent(limit=1000) if e["name"] == "state_changed"])
        assert after == before, f"Expected no new state_changed, got {after - before} new events"


class TestTransitionDetail:
    def test_transition_includes_detail_in_event(self, events):
        sm = CraneStateMachine(events=events)
        sm.enable_auto_guard()
        recent = events.recent()
        matching = [e for e in recent if e["name"] == "state_changed"]
        assert matching
        assert "auto_guard_enabled" in matching[-1]["detail"]

    def test_transition_without_detail_omits_colon(self, events):
        sm = CraneStateMachine(events=events)
        sm.transition(CraneMode.MANUAL)  # без detail
        recent = events.recent()
        matching = [e for e in recent if e["name"] == "state_changed"]
        assert matching
        # В detail не должно быть ":" в конце (detail пустой)
        assert not matching[-1]["detail"].endswith(": ")


class TestNoEventsNoCrash:
    def test_state_machine_without_events_does_not_crash(self):
        """Если events=None — переходы не должны падать."""
        sm = CraneStateMachine()  # events=None
        sm.enable_auto_guard()
        sm.enter_tracking()
        sm.enter_manual()
        assert sm.mode == CraneMode.MANUAL
