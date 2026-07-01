import time

from config import settings


class SmartPatrol:
    # Class attributes sourced from settings — defaults for backward compat.
    ZOOM_OUT_SPEED = settings.patrol.zoom_out_speed
    ZOOM_OUT_FOCUS = settings.patrol.zoom_out_focus
    PAN_SPEED = settings.patrol.pan_speed
    ZOOM_OUT_DURATION = settings.patrol.zoom_out_duration
    CYCLE_DURATION = settings.patrol.cycle_duration
    PAN_DURATION = settings.patrol.pan_duration

    def __init__(self, ptz_controller, tracker, config=None):
        self.ptz = ptz_controller
        self.tracker = tracker
        self._is_resetting = False
        self._reset_start_time = None
        self._patrol_state = None
        # Allow runtime/test override of all patrol params
        if config is not None:
            self.ZOOM_OUT_SPEED = config.zoom_out_speed
            self.ZOOM_OUT_FOCUS = config.zoom_out_focus
            self.PAN_SPEED = config.pan_speed
            self.ZOOM_OUT_DURATION = config.zoom_out_duration
            self.CYCLE_DURATION = config.cycle_duration
            self.PAN_DURATION = config.pan_duration

    @property
    def is_active(self):
        return self._is_resetting

    def reset(self):
        self._is_resetting = False
        self._reset_start_time = None
        self._patrol_state = None

    def handle_no_object(self):
        if not self._is_resetting:
            self._is_resetting = True
            self._reset_start_time = time.monotonic()
            self.ptz.stop()
            self.tracker.reset()
            # Return camera to home position (pan=0, tilt=0, zoom=1x) so patrol
            # doesn't start from a random position the camera ended up in while
            # tracking (e.g. looking down at the floor). Best-effort — if camera
            # doesn't support AbsoluteMove, silently continue.
            if hasattr(self.ptz, "goto_home"):
                try:
                    self.ptz.goto_home()
                except Exception:
                    pass  # never let goto_home break patrol
            self._patrol_state = "init"

        elapsed = time.monotonic() - self._reset_start_time

        if elapsed < self.ZOOM_OUT_DURATION:
            if self._patrol_state != "zooming_out":
                self.ptz.zoom(self.ZOOM_OUT_SPEED)
                self.ptz.focus(self.ZOOM_OUT_FOCUS)
                self._patrol_state = "zooming_out"
        else:
            spin_time = elapsed - self.ZOOM_OUT_DURATION
            cycle = spin_time % self.CYCLE_DURATION

            if cycle < self.PAN_DURATION:
                if self._patrol_state != "panning":
                    self.ptz.stop_zoom()
                    self.ptz.stop_focus()
                    self.ptz.move(self.PAN_SPEED, 0.0)
                    self._patrol_state = "panning"
            else:
                if self._patrol_state != "paused":
                    self.ptz.stop_pantilt()
                    self._patrol_state = "paused"
