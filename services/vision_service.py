import logging
import threading
import time

import cv2
from ultralytics import YOLO

from behavior.patrol import SmartPatrol
from behavior.tracking import AutoTracker
from config import settings
from core.metrics import RuntimeMetrics
from services.target_manager import TargetManager

logger = logging.getLogger("crane.brain")


class SecurityBrain:
    """
    YOLO detection plus patrol/tracking behavior.

    A3 cleanup: removed 7 property pairs that proxied _panning/_zoom_state/
    _is_resetting/_reset_start_time/_patrol_state from inner tracker/patrol
    objects. They were migration debt — no caller in the codebase used them.
    Also removed the PAN_SPEED_GAIN/etc class attributes (legacy re-exports
    of AutoTracker constants) and the auto_aim/_force_stop_zoom/_stop_all
    wrapper methods that just delegated to tracker. If you need tracker
    state, access brain.tracker.* directly.
    """

    def __init__(self, ptz_controller, state_machine=None, events=None, metrics=None, trace=None, config=None):
        cfg = config or settings.vision
        self.model = YOLO(cfg.yolo_model)
        self._detect_classes = cfg.detect_classes
        self.ptz = ptz_controller
        self.state_machine = state_machine
        self.events = events
        self.metrics = metrics
        self.trace = trace
        self.last_frame = None
        self.target_manager = TargetManager()
        self.tracker = AutoTracker(ptz_controller, trace=trace)
        self.patrol = SmartPatrol(ptz_controller, self.tracker)
        self._target_visible = False

    def process_frame(self, frame):
        frame_height, frame_width = frame.shape[:2]
        results = self.model.track(frame, persist=True, verbose=False, classes=self._detect_classes)

        # Stage: YOLO → target_manager. Record box count and group target.
        try:
            boxes_obj = results[0].boxes
            yolo_count = 0 if boxes_obj.id is None else len(boxes_obj.id)
        except Exception:
            yolo_count = -1
        if self.trace:
            self.trace.record("yolo", boxes=yolo_count)

        target = self.target_manager.get_group_target(results)
        if self.trace:
            if target is None:
                self.trace.record("target", target=None)
            else:
                cx, cy = target.center
                self.trace.record(
                    "target",
                    target="group",
                    cx=round(float(cx), 1),
                    cy=round(float(cy), 1),
                    height=int(target.height),
                    frame_w=int(frame_width),
                    frame_h=int(frame_height),
                )

        if target is not None:
            if self.metrics:
                self.metrics.detected()
            if self.events and not self._target_visible:
                # Extract confidence from YOLO results for notification
                try:
                    boxes = results[0].boxes
                    if boxes is not None and hasattr(boxes, 'conf') and boxes.conf is not None:
                        confs = boxes.conf.cpu().numpy()
                        avg_conf = float(confs.mean()) if len(confs) > 0 else 0.0
                    else:
                        avg_conf = 0.0
                except Exception:
                    avg_conf = 0.0
                self.events.emit("target_detected", f"confidence={avg_conf:.2f}")
            self._target_visible = True

            if self.patrol.is_active:
                self.ptz.stop()
                self.patrol.reset()
                self.tracker.reset()

            if self.state_machine:
                self.state_machine.enter_tracking()

            cx, cy = target.center
            logger.debug(
                "track: target cx=%.1f cy=%.1f h=%d frame=%dx%d",
                cx, cy, target.height, frame_width, frame_height,
            )
            self.tracker.auto_aim(cx, cy, target.height, frame_width, frame_height)
            self.target_manager.annotate(frame, target)
        else:
            if self._target_visible:
                self.ptz.stop()
                if self.events:
                    self.events.emit("target_lost")
            self._target_visible = False
            if self.state_machine:
                self.state_machine.enter_patrol()
            self.patrol.handle_no_object()

        self.last_frame = frame
        return frame


class VisionRuntime:
    def __init__(self, camera, brain, ptz, state_machine, events, metrics,
                 frame_skip_rate=None, trace=None, config=None):
        cfg = config or settings.vision
        web_cfg = settings.web
        self.camera = camera
        self.brain = brain
        self.ptz = ptz
        self.state_machine = state_machine
        self.frame_skip_rate = (
            frame_skip_rate if frame_skip_rate is not None else cfg.frame_skip_rate
        )
        self._jpeg_quality = cfg.jpeg_quality
        self._loop_sleep = web_cfg.loop_sleep
        self._no_frame_sleep = web_cfg.no_frame_sleep
        self._stream_sleep = web_cfg.stream_sleep
        # B3: soft manual override — auto-return to PATROL after timeout
        self._manual_override_timeout = settings.operator.manual_override_timeout
        self._last_manual_command_time = None  # monotonic timestamp
        self.events = events
        self.metrics = metrics or RuntimeMetrics()
        self.trace = trace
        self._display_lock = threading.Lock()
        self._display_jpeg = None
        self._processing_running = False
        self._thread = None
        # ─── Bug 2 fix: home-return + zoom-reset behavior ───────────────
        # When manual override timeout expires, optionally return camera to
        # home position (pan=0, tilt=0, zoom=1x) before re-enabling auto-guard.
        # This prevents the camera from continuing to patrol from wherever
        # the operator left it pointing (e.g. looking at the floor).
        self._return_to_home_after_manual = settings.operator.return_to_home_after_manual
        self._zoom_reset_on_home = settings.operator.zoom_reset_on_home
        # How long to wait (seconds) after goto_home() before re-enabling
        # auto-guard. Gives AbsoluteMove time to physically move the camera
        # back to center before patrol/tracking kicks in.
        self._home_settle_delay = settings.operator.home_settle_delay
        # Timestamp when we last issued a goto_home() during manual-override
        # expiry. Used to enforce the settle delay.
        self._home_return_started_at = None

    @property
    def auto_guard_enabled(self):
        return self.state_machine.auto_guard_enabled

    def start(self):
        self.camera.start()
        self._processing_running = True
        self._thread = threading.Thread(target=self._processing_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._processing_running = False
        self.camera.stop()
        self.state_machine.disable_auto_guard()

    def _processing_loop(self):
        frame_count = 0
        while self._processing_running:
            frame = self.camera.get_frame()
            if frame is None:
                time.sleep(self._no_frame_sleep)
                continue

            # B3: проверяем, не истекло ли время soft manual override
            self._check_manual_override_timeout()

            self.metrics.seen_frame()

            if self.auto_guard_enabled:
                frame_count += 1
                if frame_count % self.frame_skip_rate == 0:
                    frame = self.brain.process_frame(frame)
                    self.metrics.processed_frame()
            else:
                frame_count = 0

            ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality])
            if ok:
                with self._display_lock:
                    self._display_jpeg = buffer.tobytes()
                self.metrics.encoded_frame()
            time.sleep(self._loop_sleep)

    def _check_manual_override_timeout(self):
        """B3 + Bug 1 fix: если находимся в MANUAL режиме и timeout истёк —
        возвращаемся в PATROL.

        Логика (исправленная):
          - Если _manual_override_timeout == 0 — soft override отключён (legacy behavior)
          - Если _last_manual_command_time is None — не в manual режиме
          - Если state_machine не в MANUAL — ничего не делаем
          - Если timeout истёк:
              1. (опционально) Вызвать goto_home() — вернуть камеру в центр
              2. Подождать home_settle_delay секунд (даём AbsoluteMove отработать)
              3. Включить auto_guard → PATROL

        Bug 1 fix: В отличие от старой версии, этот метод работает даже если
        auto_guard был выключен ДО ручной команды (через toggle_guard off).
        Решение: проверяем не auto_guard_enabled, а state_machine.mode == MANUAL.
        Любая ручная команда теперь ставит state_machine в MANUAL, и timeout
        всегда сработает — даже если AI был выключен заранее.

        State machine during settle delay:
          - First call after timeout: emit event, call goto_home(), set
            _home_return_started_at. Stay in MANUAL.
          - Subsequent calls: if settle delay not yet elapsed → stay in MANUAL.
          - When settle delay elapsed → enable_auto_guard(), clear state.
        """
        if self._manual_override_timeout <= 0:
            return  # soft override отключён
        if self._last_manual_command_time is None:
            return  # не в manual режиме
        if self.state_machine.mode.value != "MANUAL":
            # Уже не в manual — сбрасываем таймер
            self._last_manual_command_time = None
            self._home_return_started_at = None
            return

        # ─── Phase 1: Settle delay in progress ────────────────────────
        # Если уже начали возврат в home — проверяем, прошёл ли settle delay.
        if self._home_return_started_at is not None:
            settle_elapsed = time.monotonic() - self._home_return_started_at
            if settle_elapsed < self._home_settle_delay:
                return  # ещё ждём, пока AbsoluteMove физически переместит камеру
            # Settle прошёл — сбрасываем и переходим к включению AI
            self._home_return_started_at = None
            self._last_manual_command_time = None
            self.state_machine.enable_auto_guard()
            return

        # ─── Phase 2: Check if timeout expired ────────────────────────
        elapsed = time.monotonic() - self._last_manual_command_time
        if elapsed < self._manual_override_timeout:
            return  # ещё не время

        # ─── Timeout истёк ─────────────────────────────────────────────
        if self.events:
            self.events.emit(
                "manual_override_expired",
                f"after_{self._manual_override_timeout}s"
            )

        # Шаг 1 (Bug 2 fix): вернуть камеру в home position (опционально)
        if self._return_to_home_after_manual and hasattr(self.ptz, "goto_home"):
            try:
                if self._zoom_reset_on_home:
                    # Сначала stop_zoom чтобы остановить continuous zoom,
                    # потом AbsoluteMove в (0, 0, 0) = центр, 1x.
                    self.ptz.stop_zoom()
                self.ptz.goto_home(pan=0.0, tilt=0.0, zoom=0.0)
            except Exception as e:
                logger.warning("goto_home during manual override expiry failed: %s", e)

        # Шаг 2 (Bug 2 fix): если включён settle delay — запускаем ожидание.
        # Если settle delay == 0 — сразу переходим к PATROL.
        if self._return_to_home_after_manual and self._home_settle_delay > 0:
            self._home_return_started_at = time.monotonic()
            return  # ждём следующий цикл processing_loop

        # Шаг 3: включить auto_guard → PATROL (при settle_delay == 0)
        self._last_manual_command_time = None
        self.state_machine.enable_auto_guard()

    def mjpeg_generator(self):
        while True:
            with self._display_lock:
                frame = self._display_jpeg
            if frame is not None:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(self._stream_sleep)

    def get_snapshot(self) -> bytes:
        """Return the latest encoded JPEG frame as bytes (or empty bytes if none).

        Used by NotificationService to attach a photo to Telegram alerts.
        Thread-safe — uses the same _display_lock as mjpeg_generator.
        """
        with self._display_lock:
            return self._display_jpeg or b""

    def status(self):
        return {
            "camera_status": self.camera.status,
            "auto_guard": self.auto_guard_enabled,
            "mode": self.state_machine.mode.value,
            "metrics": self.metrics.snapshot(),
            "events": self.events.recent() if self.events else [],
            "tracking_trace": self.trace.snapshot() if self.trace else None,
            "connection_health": {
                "rtsp": self.camera.health(),
                "ptz": self.ptz.health(),
            },
        }

    def manual_override(self):
        """B3 + Bug 1 fix: Soft manual override.

        Любая ручная команда (move/zoom/focus) переводит state_machine в MANUAL
        и запоминает время. Processing loop проверяет периодически — если
        _manual_override_timeout секунд не было новых ручных команд, auto-guard
        автоматически включается снова (с возвратом в home position).

        Bug 1 fix: В старой версии, если auto_guard был ВЫКЛЮЧЕН (через
        toggle_guard off) до ручной команды, условие `if self.auto_guard_enabled`
        было ложным, и state_machine.enter_manual() вызывался, но таймер не
        запускался корректно — камера оставалась в MANUAL вечно.

        Новая логика:
          1. ВСЕГДА запоминаем/продлеваем _last_manual_command_time
          2. ВСЕГДА переводим state_machine в MANUAL
          3. Если auto_guard был включен — выключаем его и останавливаем PTZ
          4. Если уже выключен — просто продлеваем таймер

        Любой новый вызов manual_override() продлевает timeout.
        """
        # Шаг 1: если AI был включен — выключаем и останавливаем движение
        if self.auto_guard_enabled:
            self.state_machine.disable_auto_guard()
            self.ptz.stop()
        # Шаг 2: ВСЕГДА переводим в MANUAL — даже если были в IDLE
        self.state_machine.enter_manual()
        # Шаг 3: ВСЕГДА запоминаем/продлеваем время последней ручной команды
        self._last_manual_command_time = time.monotonic()
        # Сбрасываем флаг ожидания settle delay (если был)
        self._home_return_started_at = None
        if self.events:
            self.events.emit("manual_override", "soft")

    def toggle_guard(self):
        if self.auto_guard_enabled:
            self.state_machine.disable_auto_guard()
            self.ptz.stop()
            # Bug 1 fix: при ручном выключении AI сбрасываем таймер, чтобы
            # automatic re-enable не сработал неожиданно. Но если после этого
            # будет ручная команда (move/zoom) — manual_override() снова
            # запустит таймер, и через timeout AI включится автоматически.
            self._last_manual_command_time = None
            self._home_return_started_at = None
            return "off"

        # Enabling auto-guard: return camera to home position first so patrol
        # doesn't start from wherever the operator left it pointing.
        if self._return_to_home_after_manual and hasattr(self.ptz, "goto_home"):
            try:
                if self._zoom_reset_on_home:
                    self.ptz.stop_zoom()
                self.ptz.goto_home()
            except Exception:
                pass
        self.state_machine.enable_auto_guard()
        return "on"
