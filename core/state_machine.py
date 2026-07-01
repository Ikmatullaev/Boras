from enum import Enum


class CraneMode(str, Enum):
    IDLE = "IDLE"
    PATROL = "PATROL"
    TRACKING = "TRACKING"
    MANUAL = "MANUAL"


class CraneStateMachine:
    def __init__(self, events=None):
        self.mode = CraneMode.IDLE
        self.events = events

    @property
    def auto_guard_enabled(self):
        return self.mode in {CraneMode.PATROL, CraneMode.TRACKING}

    def transition(self, mode, detail=""):
        if self.mode == mode:
            return
        previous = self.mode
        self.mode = mode
        if self.events:
            self.events.emit("state_changed", f"{previous.value}->{mode.value}{(': ' + detail) if detail else ''}")

    def enable_auto_guard(self):
        self.transition(CraneMode.PATROL, "auto_guard_enabled")

    def disable_auto_guard(self):
        self.transition(CraneMode.IDLE, "auto_guard_disabled")

    def enter_tracking(self):
        if self.auto_guard_enabled:
            self.transition(CraneMode.TRACKING, "target_detected")

    def enter_patrol(self):
        if self.auto_guard_enabled:
            self.transition(CraneMode.PATROL, "target_lost")

    def enter_manual(self):
        self.transition(CraneMode.MANUAL, "manual_override")
