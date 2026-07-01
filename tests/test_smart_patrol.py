"""Юнит-тесты для SmartPatrol — фазовая машина патрулирования.

Логика SmartPatrol.handle_no_object:
  - При первом вызове: _is_resetting=True, _patrol_state="init", ptz.stop(), tracker.reset()
  - elapsed < 3.0s: _patrol_state="zooming_out" (вызывает zoom(-0.5) + focus(-0.1) один раз)
  - elapsed >= 3.0s: цикл по 4 секунды
      - cycle < 2.0s: _patrol_state="panning" (move(0.12, 0.0) один раз)
      - cycle >= 2.0s: _patrol_state="paused" (stop_pantilt один раз)
"""
import time
from unittest.mock import patch

import pytest

from behavior.patrol import SmartPatrol
from behavior.tracking import AutoTracker
from conftest import FakePTZ


@pytest.fixture
def patrol(fake_ptz):
    tracker = AutoTracker(fake_ptz)
    return SmartPatrol(fake_ptz, tracker)


class TestInitialState:
    def test_initial_state_is_inactive(self, patrol):
        assert patrol.is_active is False
        assert patrol._is_resetting is False
        assert patrol._patrol_state is None

    def test_is_active_reflects_is_resetting(self, patrol):
        patrol._is_resetting = True
        assert patrol.is_active is True
        patrol._is_resetting = False
        assert patrol.is_active is False


class TestReset:
    def test_reset_clears_all_state(self, patrol):
        patrol._is_resetting = True
        patrol._reset_start_time = 123.0
        patrol._patrol_state = "panning"
        patrol.reset()
        assert patrol._is_resetting is False
        assert patrol._reset_start_time is None
        assert patrol._patrol_state is None


class TestHandleNoObjectInit:
    def test_first_call_sets_resetting_and_stops_ptz(self, patrol, fake_ptz):
        """Первый вызов handle_no_object должен: stop PTZ, reset tracker,
        выставить _is_resetting=True.

        Замечание: _patrol_state в ОДНОМ вызове проходит 'init' → 'zooming_out',
        потому что после установки 'init' код продолжает в ветку elapsed<3.0
        и сразу вызывает zoom. Поэтому проверяем _is_resetting и stop(),
        а не _patrol_state == 'init'."""
        patrol.handle_no_object()
        assert patrol._is_resetting is True
        assert patrol._reset_start_time is not None
        # ptz.stop() должен быть вызван (на init-этапе)
        assert len(fake_ptz.calls_of("stop")) == 1
        # И сразу zooming_out (zoom + focus)
        assert patrol._patrol_state == "zooming_out"

    def test_first_call_resets_tracker(self, patrol, fake_ptz):
        """Tracker должен быть сброшен (его _panning и _zoom_state)."""
        # Подготовим tracker с активным состоянием
        patrol.tracker._panning = True
        patrol.tracker._zoom_state = "in"
        patrol.handle_no_object()
        assert patrol.tracker._panning is False
        assert patrol.tracker._zoom_state is None

    def test_first_call_zooms_out_immediately(self, patrol, fake_ptz):
        """В первом вызове сразу срабатывает zooming_out (init→zooming_out
        в одном вызове из-за elapsed<3.0)."""
        patrol.handle_no_object()
        assert len(fake_ptz.calls_of("zoom")) == 1
        _, zoom_speed = fake_ptz.calls_of("zoom")[0]
        assert zoom_speed == -0.5
        assert len(fake_ptz.calls_of("focus")) == 1

    def test_first_call_calls_goto_home(self, patrol, fake_ptz):
        """При потере цели камера должна вернуться в home position (pan=0,tilt=0,zoom=1x)
        перед началом патруля — чтобы не патрулировать из случайного положения."""
        # FakePTZ не имеет goto_home — добавим заглушку
        goto_calls = []
        def fake_goto_home(pan=0.0, tilt=0.0, zoom=0.0):
            goto_calls.append((pan, tilt, zoom))
            return True
        fake_ptz.goto_home = fake_goto_home

        patrol.handle_no_object()
        # goto_home должен быть вызван на init этапе, перед zooming_out
        assert len(goto_calls) == 1
        assert goto_calls[0] == (0.0, 0.0, 0.0)  # home position

    def test_goto_home_failure_doesnt_break_patrol(self, patrol, fake_ptz):
        """Если goto_home падает — patrol должен продолжить работать."""
        def failing_goto_home(pan=0.0, tilt=0.0, zoom=0.0):
            raise RuntimeError("Camera doesn't support AbsoluteMove")
        fake_ptz.goto_home = failing_goto_home

        # Не должно выбросить исключение
        patrol.handle_no_object()
        assert patrol._is_resetting is True
        # Patrol должен продолжить — zooming_out должен сработать
        assert patrol._patrol_state == "zooming_out"


class TestZoomOutPhase:
    """elapsed < 3.0s — фаза zoom-out."""

    def test_zoom_out_phase_calls_zoom_and_focus_once(self, patrol, fake_ptz):
        """На первом вызове после init — zooming_out, вызывает zoom(-0.5) и focus(-0.1)."""
        # Имитируем: первый вызов уже сделал init, прошло 0.5 секунды
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()  # init, _reset_start_time=100.0

            mock_time.return_value = 100.5  # elapsed=0.5 < 3.0
            patrol.handle_no_object()  # zooming_out

        assert patrol._patrol_state == "zooming_out"
        assert len(fake_ptz.calls_of("zoom")) == 1
        _, zoom_speed = fake_ptz.calls_of("zoom")[0]
        assert zoom_speed == -0.5
        assert len(fake_ptz.calls_of("focus")) == 1
        _, focus_speed = fake_ptz.calls_of("focus")[0]
        assert focus_speed == -0.1

    def test_zoom_out_does_not_repeat_on_subsequent_calls(self, patrol, fake_ptz):
        """Если _patrol_state уже 'zooming_out' — повторных zoom-команд не должно быть."""
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()
            mock_time.return_value = 100.5
            patrol.handle_no_object()  # zooming_out
            mock_time.return_value = 101.0
            patrol.handle_no_object()  # всё ещё zooming_out
            mock_time.return_value = 101.5
            patrol.handle_no_object()  # всё ещё zooming_out

        assert len(fake_ptz.calls_of("zoom")) == 1, "zoom should only fire once in zooming_out phase"


class TestPanPhase:
    """elapsed >= 3.0s, cycle < 2.0s — фаза panning."""

    def test_pan_phase_calls_move_and_stops_zoom(self, patrol, fake_ptz):
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()  # init
            mock_time.return_value = 100.5
            patrol.handle_no_object()  # zooming_out
            # Переходим в pan-фазу: elapsed=3.5 → spin_time=0.5 → cycle=0.5 < 2.0
            mock_time.return_value = 103.5
            patrol.handle_no_object()  # panning

        assert patrol._patrol_state == "panning"
        # Должен быть stop_zoom + stop_focus (при переходе из zooming_out)
        assert len(fake_ptz.calls_of("stop_zoom")) >= 1
        assert len(fake_ptz.calls_of("stop_focus")) >= 1
        # И move(0.12, 0.0)
        moves = fake_ptz.calls_of("move")
        assert len(moves) == 1
        _, pan, tilt = moves[0]
        assert pan == 0.12
        assert tilt == 0.0

    def test_pan_does_not_repeat_move_on_subsequent_calls(self, patrol, fake_ptz):
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()
            mock_time.return_value = 100.5
            patrol.handle_no_object()  # zooming_out
            mock_time.return_value = 103.5
            patrol.handle_no_object()  # panning
            mock_time.return_value = 104.0
            patrol.handle_no_object()  # всё ещё panning
            mock_time.return_value = 104.5
            patrol.handle_no_object()  # всё ещё panning (cycle=1.5 < 2.0)

        # Только одна move-команда за всю pan-фазу
        assert len(fake_ptz.calls_of("move")) == 1


class TestPausePhase:
    """elapsed >= 3.0s, cycle >= 2.0s — фаза paused."""

    def test_pause_phase_calls_stop_pantilt(self, patrol, fake_ptz):
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()  # init
            mock_time.return_value = 100.5
            patrol.handle_no_object()  # zooming_out
            mock_time.return_value = 103.5
            patrol.handle_no_object()  # panning (cycle=0.5)
            # cycle >= 2.0 → paused: elapsed=105.5 → spin_time=2.5 → cycle=2.5
            mock_time.return_value = 105.5
            patrol.handle_no_object()

        assert patrol._patrol_state == "paused"
        assert len(fake_ptz.calls_of("stop_pantilt")) >= 1

    def test_pause_does_not_repeat_stop_pantilt(self, patrol, fake_ptz):
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()
            mock_time.return_value = 100.5
            patrol.handle_no_object()
            mock_time.return_value = 103.5
            patrol.handle_no_object()  # panning
            mock_time.return_value = 105.5
            patrol.handle_no_object()  # paused
            mock_time.return_value = 106.0
            patrol.handle_no_object()  # всё ещё paused (cycle=3.0%4=3.0 >= 2.0)

        # Только один stop_pantilt за всё время pause-фазы
        # (предыдущий panning мог вызвать stop_pantilt при переходе — нужно аккуратно)
        # Реально: stop_pantilt вызывается только когда _patrol_state != "paused".
        # Поэтому все последующие paused-вызовы его не повторяют.
        stop_pantilt_count = len(fake_ptz.calls_of("stop_pantilt"))
        assert stop_pantilt_count == 1, f"Expected 1 stop_pantilt, got {stop_pantilt_count}"


class TestCycleRestart:
    """После 4 секунд (cycle > 4.0 → cycle=0.0) должен снова перейти в panning."""

    def test_cycle_restarts_to_panning_after_4s(self, patrol, fake_ptz):
        with patch('behavior.patrol.time.monotonic') as mock_time:
            mock_time.return_value = 100.0
            patrol.handle_no_object()  # init
            mock_time.return_value = 100.5
            patrol.handle_no_object()  # zooming_out
            mock_time.return_value = 103.5
            patrol.handle_no_object()  # panning (cycle=0.5)
            mock_time.return_value = 105.5
            patrol.handle_no_object()  # paused (cycle=2.5)
            # cycle=4.5%4=0.5 → panning снова
            mock_time.return_value = 107.5
            patrol.handle_no_object()

        assert patrol._patrol_state == "panning"
        # Должен быть второй move (первый был в фазе panning, второй — после cycle restart)
        assert len(fake_ptz.calls_of("move")) == 2
