"""Юнит-тесты для TrackingTrace."""
import time

import pytest

from core.tracking_trace import TrackingTrace


class TestRecordAndSnapshot:
    def test_initial_snapshot_all_stages_none(self):
        trace = TrackingTrace()
        snap = trace.snapshot()
        for stage in TrackingTrace.STAGES:
            assert snap[stage] is None

    def test_record_single_stage(self):
        trace = TrackingTrace()
        trace.record("yolo", boxes=3)
        snap = trace.snapshot()
        assert snap["yolo"] is not None
        assert snap["yolo"]["boxes"] == 3
        # Другие стадии всё ещё None
        assert snap["target"] is None
        assert snap["auto_aim"] is None

    def test_record_all_stages(self):
        trace = TrackingTrace()
        trace.record("yolo", boxes=1)
        trace.record("target", target="group", cx=320.0, cy=240.0)
        trace.record("auto_aim", decision="pan", speed_x=0.23)
        trace.record("ptz_command", kind="move", pan=0.23, tilt=0.0)
        trace.record("ptz_http", service="ptz", key="move", sent=True, http=200, ok=True)
        snap = trace.snapshot()
        for stage in TrackingTrace.STAGES:
            assert snap[stage] is not None, f"Stage {stage} should be recorded"

    def test_record_overwrites_previous(self):
        """Повторная запись в ту же стадию перезаписывает предыдущую."""
        trace = TrackingTrace()
        trace.record("yolo", boxes=1)
        trace.record("yolo", boxes=5)
        snap = trace.snapshot()
        assert snap["yolo"]["boxes"] == 5

    def test_unknown_stage_ignored(self):
        """Запись в неизвестную стадию должна молча игнорироваться."""
        trace = TrackingTrace()
        trace.record("unknown_stage", foo="bar")
        snap = trace.snapshot()
        # Ничего не должно появиться
        for stage in TrackingTrace.STAGES:
            assert snap[stage] is None


class TestAgeS:
    def test_age_s_increases_over_time(self):
        trace = TrackingTrace()
        trace.record("yolo", boxes=1)
        snap1 = trace.snapshot()
        time.sleep(0.05)
        snap2 = trace.snapshot()
        assert snap2["yolo"]["age_s"] > snap1["yolo"]["age_s"]

    def test_age_s_is_rounded(self):
        trace = TrackingTrace()
        trace.record("yolo", boxes=1)
        snap = trace.snapshot()
        # age_s должен быть округлён до 3 знаков
        assert isinstance(snap["yolo"]["age_s"], float)
        # Проверяем, что не более 3 знаков после запятой
        assert round(snap["yolo"]["age_s"], 3) == snap["yolo"]["age_s"]


class TestSnapshotIsolation:
    """snapshot() должен возвращать копию — мутация возвращённого dict
    не должна влиять на внутреннее состояние."""

    def test_snapshot_returns_independent_copy(self):
        trace = TrackingTrace()
        trace.record("yolo", boxes=3)
        snap = trace.snapshot()
        # Мутируем возвращённый dict
        snap["yolo"]["boxes"] = 999
        snap["yolo"]["injected"] = True
        # Внутреннее состояние не должно измениться
        snap2 = trace.snapshot()
        assert snap2["yolo"]["boxes"] == 3
        assert "injected" not in snap2["yolo"]


class TestThreadSafety:
    """TrackingTrace используется из processing_loop (writer) и /api/status (reader)
    одновременно — должен быть потокобезопасным."""

    def test_concurrent_writes_and_reads(self):
        import threading
        trace = TrackingTrace()
        errors = []

        def writer():
            try:
                for i in range(100):
                    trace.record("yolo", boxes=i)
                    trace.record("auto_aim", decision="pan", i=i)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    snap = trace.snapshot()
                    # Просто читаем — не должно упасть
                    assert "yolo" in snap
                    assert "auto_aim" in snap
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert errors == [], f"Concurrent access errors: {errors}"
        # Финальное состояние должно быть консистентным
        snap = trace.snapshot()
        assert snap["yolo"]["boxes"] == 99  # последнее записанное значение


class TestStagesConstant:
    def test_stages_in_expected_order(self):
        """Порядок стадий важен — он отражает порядок вызовов в цепочке."""
        assert TrackingTrace.STAGES == (
            "yolo",
            "target",
            "auto_aim",
            "ptz_command",
            "ptz_http",
        )
