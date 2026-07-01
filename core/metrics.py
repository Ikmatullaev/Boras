import time
from collections import deque
from threading import Lock


class RuntimeMetrics:
    """Runtime counters and monitoring for the Boras vision pipeline.

    FPS calculation uses a rolling window (last N frame timestamps) instead
    of lifetime average. Lifetime average is useless after hours of running
    because instantaneous slowdowns (e.g. YOLO inference getting slower)
    are invisible — they're diluted by the long history.

    The rolling window keeps the last `fps_window_size` frame timestamps and
    computes FPS as (window_size - 1) / (newest_timestamp - oldest_timestamp).
    """

    def __init__(self, fps_window_size: int = 60):
        self._lock = Lock()
        self._started_at = time.monotonic()
        self.frames_seen = 0
        self.frames_processed = 0
        self.frames_encoded = 0
        self.detections_count = 0
        self.ptz_commands = 0
        self.errors = 0
        # Rolling window of frame timestamps for FPS calculation
        self._fps_window_size = max(fps_window_size, 2)
        self._frame_timestamps = deque(maxlen=self._fps_window_size)

    def seen_frame(self):
        with self._lock:
            self.frames_seen += 1
            self._frame_timestamps.append(time.monotonic())

    def processed_frame(self):
        with self._lock:
            self.frames_processed += 1

    def encoded_frame(self):
        with self._lock:
            self.frames_encoded += 1

    def detected(self):
        with self._lock:
            self.detections_count += 1

    def ptz_command(self):
        with self._lock:
            self.ptz_commands += 1

    def error(self):
        with self._lock:
            self.errors += 1

    def _rolling_fps(self):
        """Compute FPS from the rolling window of frame timestamps.

        Returns 0.0 if fewer than 2 frames have been seen (can't compute rate).
        """
        if len(self._frame_timestamps) < 2:
            return 0.0
        # (N - 1) intervals between N timestamps
        elapsed = self._frame_timestamps[-1] - self._frame_timestamps[0]
        if elapsed <= 0:
            return 0.0
        return (len(self._frame_timestamps) - 1) / elapsed

    def snapshot(self):
        with self._lock:
            elapsed = max(time.monotonic() - self._started_at, 0.001)
            rolling_fps = self._rolling_fps()
            return {
                "fps": round(rolling_fps, 2),
                "fps_lifetime_avg": round(self.frames_seen / elapsed, 2),
                "fps_window_size": self._fps_window_size,
                "frames_seen": self.frames_seen,
                "frames_processed": self.frames_processed,
                "frames_encoded": self.frames_encoded,
                "detections_count": self.detections_count,
                "ptz_commands": self.ptz_commands,
                "errors": self.errors,
            }
