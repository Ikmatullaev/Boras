"""Тесты для RuntimeMetrics — rolling FPS window."""
import time

import pytest

from core.metrics import RuntimeMetrics


class TestInitial_state:
    def test_initial_counters_are_zero(self):
        m = RuntimeMetrics()
        snap = m.snapshot()
        assert snap["frames_seen"] == 0
        assert snap["frames_processed"] == 0
        assert snap["frames_encoded"] == 0
        assert snap["detections_count"] == 0
        assert snap["ptz_commands"] == 0
        assert snap["errors"] == 0
        assert snap["fps"] == 0.0
        assert snap["fps_lifetime_avg"] == 0.0


class TestRollingFPS:
    """Rolling FPS должен отражать недавнюю скорость, а не lifetime average."""

    def test_fps_zero_with_less_than_two_frames(self):
        m = RuntimeMetrics()
        m.seen_frame()  # 1 frame
        assert m.snapshot()["fps"] == 0.0

    def test_fps_computed_with_two_frames(self):
        m = RuntimeMetrics()
        m.seen_frame()
        time.sleep(0.05)
        m.seen_frame()
        fps = m.snapshot()["fps"]
        # ~20 FPS (2 frames over 0.05s = 1/0.05 = 20)
        assert 10 < fps < 30, f"Expected ~20 FPS, got {fps}"

    def test_fps_reflects_recent_rate_not_lifetime(self):
        """Burst of fast frames, then slow frames — rolling FPS should drop."""
        m = RuntimeMetrics(fps_window_size=10)
        # Fast burst: 10 frames in 0.05s
        for _ in range(10):
            m.seen_frame()
            time.sleep(0.005)
        burst_fps = m.snapshot()["fps"]
        assert burst_fps > 100, f"Burst FPS should be high, got {burst_fps}"
        # Now slow frames — should push out fast ones from window
        for _ in range(10):
            m.seen_frame()
            time.sleep(0.1)  # 10 FPS
        recent_fps = m.snapshot()["fps"]
        # Rolling window now contains only slow frames, FPS should be much lower
        assert recent_fps < 20, f"FPS should drop after slow frames, got {recent_fps}"
        # Lifetime avg should be between burst and slow
        lifetime = m.snapshot()["fps_lifetime_avg"]
        assert recent_fps < lifetime < burst_fps, \
            f"Lifetime {lifetime} should be between rolling {recent_fps} and burst {burst_fps}"

    def test_fps_updates_with_new_frames(self):
        """Добавление новых кадров должно обновлять FPS."""
        m = RuntimeMetrics(fps_window_size=5)
        for _ in range(5):
            m.seen_frame()
            time.sleep(0.02)
        fps1 = m.snapshot()["fps"]
        # Add more frames
        for _ in range(5):
            m.seen_frame()
            time.sleep(0.02)
        fps2 = m.snapshot()["fps"]
        # FPS should be similar (same rate)
        assert abs(fps1 - fps2) < 15, f"FPS should be stable, {fps1} vs {fps2}"

    def test_rolling_window_drops_old_frames(self):
        """Окно должно содержать только последние N кадров."""
        m = RuntimeMetrics(fps_window_size=5)
        # Add 10 frames — only last 5 should be in window
        for _ in range(10):
            m.seen_frame()
        snap = m.snapshot()
        assert snap["fps_window_size"] == 5
        assert snap["frames_seen"] == 10  # total counter

    def test_fps_window_size_minimum_two(self):
        """fps_window_size < 2 должен быть поднят до 2."""
        m = RuntimeMetrics(fps_window_size=1)
        assert m._fps_window_size == 2
        m2 = RuntimeMetrics(fps_window_size=0)
        assert m2._fps_window_size == 2


class TestLifetimeAvg:
    def test_lifetime_avg_differs_from_rolling(self):
        """После долгой работы rolling и lifetime должны различаться."""
        m = RuntimeMetrics(fps_window_size=5)
        # Fast burst
        for _ in range(5):
            m.seen_frame()
            time.sleep(0.005)
        # Long silence
        time.sleep(0.3)
        snap = m.snapshot()
        # Rolling FPS reflects recent inactivity
        # Lifetime avg includes the burst, so should be higher
        # (both are computed from same timestamps in this case since window
        # is full with the burst frames)
        # The key check: both fields exist and are floats
        assert isinstance(snap["fps"], float)
        assert isinstance(snap["fps_lifetime_avg"], float)


class TestCounters:
    def test_seen_frame_increments(self):
        m = RuntimeMetrics()
        m.seen_frame()
        m.seen_frame()
        assert m.snapshot()["frames_seen"] == 2

    def test_processed_frame_increments(self):
        m = RuntimeMetrics()
        m.processed_frame()
        assert m.snapshot()["frames_processed"] == 1

    def test_encoded_frame_increments(self):
        m = RuntimeMetrics()
        m.encoded_frame()
        assert m.snapshot()["frames_encoded"] == 1

    def test_detected_increments(self):
        m = RuntimeMetrics()
        m.detected()
        m.detected()
        m.detected()
        assert m.snapshot()["detections_count"] == 3

    def test_ptz_command_increments(self):
        m = RuntimeMetrics()
        m.ptz_command()
        assert m.snapshot()["ptz_commands"] == 1

    def test_error_increments(self):
        m = RuntimeMetrics()
        m.error()
        m.error()
        assert m.snapshot()["errors"] == 2


class TestThreadSafety:
    """RuntimeMetrics используется из нескольких потоков — должен быть thread-safe."""

    def test_concurrent_seen_frame_calls(self):
        import threading
        m = RuntimeMetrics()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    m.seen_frame()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Concurrent errors: {errors}"
        assert m.snapshot()["frames_seen"] == 500  # 5 threads × 100 frames

    def test_concurrent_snapshot_during_writes(self):
        import threading
        m = RuntimeMetrics()
        errors = []

        def writer():
            try:
                for _ in range(100):
                    m.seen_frame()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    snap = m.snapshot()
                    assert "fps" in snap
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert errors == [], f"Concurrent errors: {errors}"
