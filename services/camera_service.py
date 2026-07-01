"""
Background thread that owns the single RTSP connection to the camera.

Tries multiple RTSP path templates (from settings.camera.rtsp_paths) in order
until one opens and returns a frame. This makes the stream robust to firmware
updates and camera model changes without code edits.
"""

import logging
import threading
import time

import cv2

from config import settings

logger = logging.getLogger("crane.camera_stream")


class CameraStream:
    def __init__(self, ip, username, password, reconnect_delay=None,
                 events=None, metrics=None, config=None):
        cfg = config or settings.camera
        self.ip = ip
        self.username = username
        self.password = password
        self.reconnect_delay = (
            reconnect_delay if reconnect_delay is not None else cfg.reconnect_delay
        )
        self.events = events
        self.metrics = metrics

        # Build list of RTSP URLs to try (B1: fallback support).
        # Order matters: most likely path first, least likely last.
        self.rtsp_blueprints = [
            f"rtsp://{username}:{password}@{ip}:{cfg.rtsp_port}{path}"
            for path in cfg.rtsp_paths
        ]

        self.working_url = None
        self.cap = None
        self._latest_frame = None
        self._last_frame_time = None  # monotonic время последнего кадра (для health)
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

        # stopped | connecting | live | reconnecting | failed
        self.status = "stopped"
        # Сколько секунд без кадра считать поток "stale" (для health)
        self._stale_threshold = 5.0

    def _find_working_url(self):
        for url in self.rtsp_blueprints:
            path_suffix = url.split(self.ip)[-1]
            logger.info("Probing RTSP path: %s", path_suffix)
            cap = cv2.VideoCapture(url)
            time.sleep(0.5)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    logger.info("Locked stream path: %s", path_suffix)
                    if self.events:
                        self.events.emit("connected", path_suffix)
                    return url, cap
            cap.release()
        return None, None

    def _capture_loop(self):
        while self._running:
            self.status = "connecting"
            if not self.working_url:
                self.working_url, self.cap = self._find_working_url()

            if not self.working_url:
                self.status = "failed"
                logger.warning("No RTSP path responded. Retrying shortly...")
                time.sleep(self.reconnect_delay)
                continue

            self.status = "live"
            while self._running:
                ret, frame = self.cap.read()
                if not ret:
                    logger.warning("Lost frame buffer; reconnecting...")
                    self.status = "reconnecting"
                    if self.events:
                        self.events.emit("disconnected", "lost_frame_buffer")
                    break
                with self._lock:
                    self._latest_frame = frame
                    self._last_frame_time = time.monotonic()
                if self.events:
                    self.events.emit("frame_received")

            if self.cap:
                self.cap.release()
            self.working_url = None
            time.sleep(self.reconnect_delay)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self.cap:
            self.cap.release()
        self.status = "stopped"
        self._last_frame_time = None

    def get_frame(self):
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def health(self):
        """B4: возвращает health-словарь для /api/status.

        Поля:
          rtsp_status  — текущий статус подключения (stopped/connecting/live/...)
          rtsp_healthy — True если статус 'live' и кадр свежий (не старше stale_threshold)
          last_frame_age_s — сколько секунд назад был последний кадр (None если не было)
          working_path — RTSP path который сейчас работает (None если ничего)
        """
        now = time.monotonic()
        with self._lock:
            last_frame_time = self._last_frame_time
            latest_frame_present = self._latest_frame is not None

        last_frame_age = (
            round(now - last_frame_time, 2) if last_frame_time is not None else None
        )
        rtsp_healthy = (
            self.status == "live"
            and latest_frame_present
            and last_frame_age is not None
            and last_frame_age < self._stale_threshold
        )

        return {
            "rtsp_status": self.status,
            "rtsp_healthy": rtsp_healthy,
            "last_frame_age_s": last_frame_age,
            "working_path": self.working_url.split(self.ip)[-1] if self.working_url else None,
        }
