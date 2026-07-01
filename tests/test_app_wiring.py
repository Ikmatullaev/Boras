"""Интеграционные тесты проводки app.py (Track C3).

Назначение: гарантировать, что app.py правильно прокидывает общие объекты
(events, metrics, trace) во все компоненты. Эти тесты — регрессионный сет для
бага, исправленного в Phase 2: events/metrics создавались в app.py, но
прокидывались только в VisionRuntime, из-за чего счётчики детекций/PTZ-команд
и лог событий всегда были пустыми.

Если любой из этих тестов падает — кто-то сломал проводку при рефакторинге.
"""
import pytest


@pytest.fixture(scope="module")
def app(app_module):
    """Сам модуль app (импортируется один раз за модуль)."""
    return app_module


class TestEventsWiring:
    """Все компоненты должны разделять ОДИН экземпляр EventLog."""

    def test_state_machine_has_events(self, app):
        assert app.state_machine.events is app.events

    def test_camera_has_events(self, app):
        assert app.camera.events is app.events

    def test_ptz_has_events(self, app):
        assert app.ptz.events is app.events

    def test_brain_has_events(self, app):
        assert app.brain.events is app.events

    def test_all_share_same_events_instance(self, app):
        """Все четыре компонента + runtime должны ссылаться на один и тот же
        объект EventLog (проверка по id)."""
        shared_id = id(app.events)
        assert id(app.state_machine.events) == shared_id
        assert id(app.camera.events) == shared_id
        assert id(app.ptz.events) == shared_id
        assert id(app.brain.events) == shared_id
        assert id(app.runtime.events) == shared_id


class TestMetricsWiring:
    """Все компоненты должны разделять ОДИН экземпляр RuntimeMetrics."""

    def test_camera_has_metrics(self, app):
        assert app.camera.metrics is app.metrics

    def test_ptz_has_metrics(self, app):
        assert app.ptz.metrics is app.metrics

    def test_brain_has_metrics(self, app):
        assert app.brain.metrics is app.metrics

    def test_runtime_has_metrics(self, app):
        assert app.runtime.metrics is app.metrics

    def test_all_share_same_metrics_instance(self, app):
        shared_id = id(app.metrics)
        assert id(app.camera.metrics) == shared_id
        assert id(app.ptz.metrics) == shared_id
        assert id(app.brain.metrics) == shared_id
        assert id(app.runtime.metrics) == shared_id


class TestTraceWiring:
    """trace должен быть прокинут в ptz, brain, brain.tracker, runtime."""

    def test_ptz_has_trace(self, app):
        assert app.ptz.trace is app.trace

    def test_brain_has_trace(self, app):
        assert app.brain.trace is app.trace

    def test_brain_tracker_has_trace(self, app):
        """AutoTracker внутри brain должен получить тот же trace."""
        assert app.brain.tracker.trace is app.trace

    def test_runtime_has_trace(self, app):
        assert app.runtime.trace is app.trace


class TestStateMachineWiring:
    def test_state_machine_is_passed_to_brain(self, app):
        assert app.brain.state_machine is app.state_machine

    def test_state_machine_is_passed_to_runtime(self, app):
        assert app.runtime.state_machine is app.state_machine


class TestComponentReferences:
    """Проверка, что компоненты ссылаются друг на друга правильно."""

    def test_brain_uses_same_ptz_as_app(self, app):
        assert app.brain.ptz is app.ptz

    def test_runtime_uses_same_brain_as_app(self, app):
        assert app.runtime.brain is app.brain

    def test_runtime_uses_same_camera_as_app(self, app):
        assert app.runtime.camera is app.camera

    def test_runtime_uses_same_ptz_as_app(self, app):
        assert app.runtime.ptz is app.ptz

    def test_operator_uses_same_runtime(self, app):
        assert app.operator.runtime is app.runtime

    def test_operator_uses_same_ptz(self, app):
        assert app.operator.ptz is app.ptz


class TestStatusPayloadShape:
    """Гарантируем, что /api/status возвращает ожидаемую структуру."""

    def test_status_has_required_fields(self, app):
        status = app.runtime.status()
        assert "camera_status" in status
        assert "auto_guard" in status
        assert "mode" in status
        assert "metrics" in status
        assert "events" in status

    def test_status_includes_tracking_trace(self, app):
        """Регрессионный тест: tracking_trace должен быть в /api/status.
        Это поле было добавлено в Phase 2 — без него оператор не видит,
        где остановилась цепочка отслеживания."""
        status = app.runtime.status()
        assert "tracking_trace" in status
        # tracking_trace не должен быть None (trace прокинут в runtime)
        assert status["tracking_trace"] is not None

    def test_status_tracking_trace_has_all_stages(self, app):
        status = app.runtime.status()
        trace = status["tracking_trace"]
        from core.tracking_trace import TrackingTrace
        for stage in TrackingTrace.STAGES:
            assert stage in trace, f"Stage {stage} missing from tracking_trace"

    def test_status_mode_value_is_valid(self, app):
        from core.state_machine import CraneMode
        status = app.runtime.status()
        assert status["mode"] in [m.value for m in CraneMode]

    def test_status_metrics_has_expected_counters(self, app):
        status = app.runtime.status()
        metrics = status["metrics"]
        for key in ("fps", "fps_lifetime_avg", "fps_window_size",
                    "frames_seen", "frames_processed", "frames_encoded",
                    "detections_count", "ptz_commands", "errors"):
            assert key in metrics, f"Metric {key} missing"

    def test_status_has_connection_health(self, app):
        """B4: /api/status должен содержать connection_health с rtsp и ptz."""
        status = app.runtime.status()
        assert "connection_health" in status
        assert "rtsp" in status["connection_health"]
        assert "ptz" in status["connection_health"]

    def test_connection_health_rtsp_has_expected_fields(self, app):
        status = app.runtime.status()
        rtsp = status["connection_health"]["rtsp"]
        for key in ("rtsp_status", "rtsp_healthy", "last_frame_age_s", "working_path"):
            assert key in rtsp, f"RTSP health field {key} missing"

    def test_connection_health_ptz_has_expected_fields(self, app):
        status = app.runtime.status()
        ptz = status["connection_health"]["ptz"]
        for key in ("ptz_reachable", "last_http_ok", "last_http_age_s",
                    "last_http_status", "last_http_error", "ptz_url"):
            assert key in ptz, f"PTZ health field {key} missing"

    def test_connection_health_rtsp_matches_camera_health(self, app):
        """connection_health.rtsp должен совпадать с camera.health()."""
        status = app.runtime.status()
        assert status["connection_health"]["rtsp"] == app.camera.health()

    def test_connection_health_ptz_matches_ptz_health(self, app):
        """connection_health.ptz должен совпадать с ptz.health()."""
        status = app.runtime.status()
        assert status["connection_health"]["ptz"] == app.ptz.health()


class TestEventsActuallyFire:
    """Доказательство того, что проводка работает на практике: события реально
    доходят до EventLog и видны в /api/status."""

    def test_state_machine_transition_visible_in_events(self, app):
        """При переходе состояния — событие должно появиться в event log,
        который вернёт /api/status."""
        initial_events_count = len(app.events.recent(limit=1000))
        app.state_machine.enable_auto_guard()  # должен эмитить state_changed
        recent = app.events.recent(limit=1000)
        # Должно быть больше событий, чем до вызова
        assert len(recent) > initial_events_count
        # Последнее событие должно быть state_changed
        assert recent[-1]["name"] == "state_changed"
        # И оно должно быть видно в /api/status
        status = app.runtime.status()
        assert any(e["name"] == "state_changed" for e in status["events"])
        # Чистим за собой
        app.state_machine.disable_auto_guard()


class TestNoDuplicateInstances:
    """Защита от бага: кто-то может случайно создать второй EventLog или
    RuntimeMetrics внутри компонента. Проверяем, что такое не произошло."""

    def test_no_second_eventlog_created(self, app):
        """Если внутри SecurityBrain или другого компонента кто-то создаст
        свой EventLog — он НЕ будет равен app.events, и тест упадёт."""
        # brain.events должен ссылаться на app.events, не быть новым инстансом
        assert app.brain.events is app.events
        # Дополнительно:emit в brain.events должен быть виден в app.events
        app.brain.events.emit("test_event", "from_test")
        recent = app.events.recent()
        assert any(e["name"] == "test_event" for e in recent)

    def test_no_second_metrics_created(self, app):
        """ptz_command в ptz.metrics должен увеличивать app.metrics.ptz_commands."""
        initial_count = app.metrics.snapshot()["ptz_commands"]
        app.ptz.metrics.ptz_command()
        new_count = app.metrics.snapshot()["ptz_commands"]
        assert new_count == initial_count + 1
