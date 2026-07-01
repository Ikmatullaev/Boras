"""Тесты для app_compose.compose_app() (A2) + SecurityBrain cleanup (A3)."""
import pytest

from app_compose import compose_app
from core.events import EventLog
from core.metrics import RuntimeMetrics
from core.tracking_trace import TrackingTrace
from core.state_machine import CraneStateMachine
from services.camera_service import CameraStream
from services.ptz_service import CranePTZ
from services.vision_service import SecurityBrain, VisionRuntime
from services.operator_service import OperatorService


@pytest.fixture(scope="module")
def components():
    """Module-scoped — compose_app один раз для всех тестов модуля.
    env vars уже выставлены в conftest.py."""
    return compose_app()


class TestComposeAppReturnsAllComponents:
    """A2: compose_app должен вернуть все ожидаемые компоненты."""

    def test_returns_dict_with_all_keys(self, components):
        # Part B добавила bot_* ключи (могут быть None если aiogram не установлен)
        expected_keys = {
            "events", "metrics", "trace", "event_store",
            "state_machine", "camera", "ptz", "brain", "runtime",
            "operator", "notifications",
            # Part B: Telegram bot components
            "bot_auth_store", "camera_settings_store", "auth_manager",
            "camera_registry", "telegram_bot",
        }
        assert set(components.keys()) == expected_keys

    def test_events_is_eventlog(self, components):
        assert isinstance(components["events"], EventLog)

    def test_metrics_is_runtimemetrics(self, components):
        assert isinstance(components["metrics"], RuntimeMetrics)

    def test_trace_is_trackingtrace(self, components):
        assert isinstance(components["trace"], TrackingTrace)

    def test_state_machine_is_crane_state_machine(self, components):
        assert isinstance(components["state_machine"], CraneStateMachine)

    def test_camera_is_camera_stream(self, components):
        assert isinstance(components["camera"], CameraStream)

    def test_ptz_is_crane_ptz(self, components):
        assert isinstance(components["ptz"], CranePTZ)

    def test_brain_is_security_brain(self, components):
        assert isinstance(components["brain"], SecurityBrain)

    def test_runtime_is_vision_runtime(self, components):
        assert isinstance(components["runtime"], VisionRuntime)

    def test_operator_is_operator_service(self, components):
        assert isinstance(components["operator"], OperatorService)


class TestComposeAppWiring:
    """A2: Все компоненты должны разделять общие events/metrics/trace.
    Это регрессионный тест для Phase 1 бага."""

    def test_all_components_share_events(self, components):
        shared_id = id(components["events"])
        assert id(components["state_machine"].events) == shared_id
        assert id(components["camera"].events) == shared_id
        assert id(components["ptz"].events) == shared_id
        assert id(components["brain"].events) == shared_id
        assert id(components["runtime"].events) == shared_id

    def test_all_components_share_metrics(self, components):
        shared_id = id(components["metrics"])
        assert id(components["camera"].metrics) == shared_id
        assert id(components["ptz"].metrics) == shared_id
        assert id(components["brain"].metrics) == shared_id
        assert id(components["runtime"].metrics) == shared_id

    def test_ptz_brain_runtime_share_trace(self, components):
        shared_id = id(components["trace"])
        assert id(components["ptz"].trace) == shared_id
        assert id(components["brain"].trace) == shared_id
        assert id(components["brain"].tracker.trace) == shared_id
        assert id(components["runtime"].trace) == shared_id

    def test_state_machine_passed_to_brain_and_runtime(self, components):
        assert components["brain"].state_machine is components["state_machine"]
        assert components["runtime"].state_machine is components["state_machine"]

    def test_brain_uses_same_ptz(self, components):
        assert components["brain"].ptz is components["ptz"]

    def test_runtime_uses_same_brain_camera_ptz(self, components):
        assert components["runtime"].brain is components["brain"]
        assert components["runtime"].camera is components["camera"]
        assert components["runtime"].ptz is components["ptz"]

    def test_operator_uses_same_runtime_ptz(self, components):
        assert components["operator"].runtime is components["runtime"]
        assert components["operator"].ptz is components["ptz"]


class TestComposeAppIsolation:
    """A2: Каждый вызов compose_app должен создавать НОВЫЕ инстансы
    (чтобы тесты не мутировали shared state)."""

    def test_two_calls_return_different_instances(self):
        c1 = compose_app()
        c2 = compose_app()
        assert c1["events"] is not c2["events"]
        assert c1["metrics"] is not c2["metrics"]
        assert c1["state_machine"] is not c2["state_machine"]
        assert c1["runtime"] is not c2["runtime"]


# ═══════════════════════════════════════════════════════════════════════════
#  A3: SecurityBrain cleanup — proxy properties removed
# ═══════════════════════════════════════════════════════════════════════════

class TestSecurityBrainProxyRemoval:
    """A3: proxy properties и wrapper methods удалены из SecurityBrain.
    Доступ к tracker/patrol состоянию — через brain.tracker.* / brain.patrol.*"""

    def test_no_panning_proxy_property(self, components):
        brain = components["brain"]
        assert not hasattr(brain, "_panning"), \
            "A3: _panning proxy property should be removed. Use brain.tracker._panning."

    def test_no_zoom_state_proxy_property(self, components):
        brain = components["brain"]
        assert not hasattr(brain, "_zoom_state")

    def test_no_is_resetting_proxy_property(self, components):
        brain = components["brain"]
        assert not hasattr(brain, "_is_resetting")

    def test_no_reset_start_time_proxy_property(self, components):
        brain = components["brain"]
        assert not hasattr(brain, "_reset_start_time")

    def test_no_patrol_state_proxy_property(self, components):
        brain = components["brain"]
        assert not hasattr(brain, "_patrol_state")

    def test_no_legacy_class_attributes(self, components):
        """A3: PAN_SPEED_GAIN и т.д. были legacy re-exports из AutoTracker."""
        brain = components["brain"]
        assert not hasattr(SecurityBrain, "PAN_SPEED_GAIN")
        assert not hasattr(SecurityBrain, "MIN_PAN_SPEED")
        assert not hasattr(SecurityBrain, "DEADZONE_FRAC_X")
        assert not hasattr(SecurityBrain, "HEIGHT_TARGET_LOW")
        assert not hasattr(SecurityBrain, "ZOOM_SPEED")

    def test_no_proxy_methods(self, components):
        """A3: auto_aim, _force_stop_zoom, _stop_all, _handle_no_object
        были wrapper-методами которые просто делегировали в tracker."""
        brain = components["brain"]
        assert not hasattr(brain, "auto_aim")
        assert not hasattr(brain, "_force_stop_zoom")
        assert not hasattr(brain, "_stop_all")
        assert not hasattr(brain, "_handle_no_object")

    def test_tracker_accessible_directly(self, components):
        """A3: доступ к tracker — через brain.tracker (а не через proxy)."""
        brain = components["brain"]
        assert hasattr(brain, "tracker")
        assert hasattr(brain.tracker, "_panning")
        assert hasattr(brain.tracker, "_zoom_state")

    def test_patrol_accessible_directly(self, components):
        """A3: доступ к patrol — через brain.patrol (а не через proxy)."""
        brain = components["brain"]
        assert hasattr(brain, "patrol")
        assert hasattr(brain.patrol, "_is_resetting")
        assert hasattr(brain.patrol, "_patrol_state")
