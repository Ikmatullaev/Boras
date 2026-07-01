from config import settings


class AutoTracker:
    # Class attributes sourced from settings — serve as defaults and keep
    # backward compatibility with tests that reference AutoTracker.PAN_SPEED_GAIN.
    # Override at instance level by passing `config=` to __init__.
    PAN_SPEED_GAIN = settings.tracking.pan_speed_gain
    MIN_PAN_SPEED = settings.tracking.min_pan_speed
    DEADZONE_FRAC_X = settings.tracking.deadzone_frac_x
    DEADZONE_FRAC_Y = settings.tracking.deadzone_frac_y
    HEIGHT_TARGET_LOW = settings.tracking.height_target_low
    HEIGHT_TARGET_HIGH = settings.tracking.height_target_high
    ZOOM_SPEED = settings.tracking.zoom_speed
    FOCUS_SPEED = settings.tracking.focus_speed

    def __init__(self, ptz_controller, trace=None, config=None):
        self.ptz = ptz_controller
        self.trace = trace
        self._panning = False
        self._zoom_state = None
        # Allow runtime/test override of all tuning params
        if config is not None:
            self.PAN_SPEED_GAIN = config.pan_speed_gain
            self.MIN_PAN_SPEED = config.min_pan_speed
            self.DEADZONE_FRAC_X = config.deadzone_frac_x
            self.DEADZONE_FRAC_Y = config.deadzone_frac_y
            self.HEIGHT_TARGET_LOW = config.height_target_low
            self.HEIGHT_TARGET_HIGH = config.height_target_high
            self.ZOOM_SPEED = config.zoom_speed
            self.FOCUS_SPEED = config.focus_speed

    def reset(self):
        self._panning = False
        self._zoom_state = None

    def auto_aim(self, cx, cy, group_height, frame_width, frame_height):
        # Convert all YOLO/numpy inputs to Python float at the entry point.
        # Without this, dx/dy/speed_x/speed_y are numpy.float32 and fail
        # isinstance(value, (int, float)) check in CranePTZ._safe_speed,
        # producing "invalid_tilt_speed" / "invalid_pan_speed" errors.
        cx = float(cx)
        cy = float(cy)
        group_height = float(group_height)
        frame_width = float(frame_width)
        frame_height = float(frame_height)

        dx = cx - (frame_width / 2)
        dy = cy - (frame_height / 2)

        deadzone_x = frame_width * self.DEADZONE_FRAC_X
        deadzone_y = frame_height * self.DEADZONE_FRAC_Y

        if abs(dx) > deadzone_x:
            self._force_stop_zoom()

            speed_x = (dx / (frame_width / 2)) * self.PAN_SPEED_GAIN
            if abs(speed_x) < self.MIN_PAN_SPEED:
                speed_x = self.MIN_PAN_SPEED if speed_x > 0 else -self.MIN_PAN_SPEED

            self.ptz.move(float(speed_x), 0.0)
            self._panning = True
            self._trace("pan", speed_x=round(float(speed_x), 3), tilt=0.0,
                        dx=int(dx), deadzone_x=int(deadzone_x))
            return

        if abs(dy) > deadzone_y:
            self._force_stop_zoom()

            speed_y = (dy / (frame_height / 2)) * self.PAN_SPEED_GAIN
            if abs(speed_y) < self.MIN_PAN_SPEED:
                speed_y = self.MIN_PAN_SPEED if speed_y > 0 else -self.MIN_PAN_SPEED

            # Tilt axis is inverted: target below center (dy>0) requires
            # negative tilt speed on this camera.
            self.ptz.move(0.0, float(-speed_y))
            self._panning = True
            self._trace("tilt", pan=0.0, speed_y=round(float(speed_y), 3),
                        sent_tilt=round(float(-speed_y), 3),
                        dy=int(dy), deadzone_y=int(deadzone_y))
            return

        if self._panning:
            self.ptz.stop_pantilt()
            self._panning = False

        ratio = group_height / frame_height

        if ratio < self.HEIGHT_TARGET_LOW:
            if self._zoom_state != "in":
                self.ptz.zoom(float(self.ZOOM_SPEED))
                self.ptz.focus(float(self.FOCUS_SPEED))
                self._zoom_state = "in"
                self._trace("zoom_in", ratio=round(float(ratio), 3),
                            zoom_speed=self.ZOOM_SPEED, focus_speed=self.FOCUS_SPEED)
        elif ratio > self.HEIGHT_TARGET_HIGH:
            if self._zoom_state != "out":
                self.ptz.zoom(float(-self.ZOOM_SPEED))
                self.ptz.focus(float(-self.FOCUS_SPEED))
                self._zoom_state = "out"
                self._trace("zoom_out", ratio=round(float(ratio), 3),
                            zoom_speed=-self.ZOOM_SPEED, focus_speed=-self.FOCUS_SPEED)
        else:
            self._force_stop_zoom()
            self._trace("hold", ratio=round(float(ratio), 3),
                        lo=self.HEIGHT_TARGET_LOW, hi=self.HEIGHT_TARGET_HIGH)

    def _trace(self, decision, **fields):
        if self.trace:
            self.trace.record("auto_aim", decision=decision, **fields)

    def _force_stop_zoom(self):
        if self._zoom_state is not None:
            self.ptz.stop_zoom()
            self.ptz.stop_focus()
            self._zoom_state = None

    def _stop_all(self):
        self.ptz.stop()
        self.reset()
