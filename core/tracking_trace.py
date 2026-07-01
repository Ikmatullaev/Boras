"""
Single-slot ring recorder for the most recent tracking-chain decision at each
stage. Designed to be surfaced through /api/status so operators can see exactly
where the chain stopped without grepping server logs.

Stages (in call order):
    yolo              — YOLO produced N boxes
    target            — target_manager.get_group_target returned a box (or None)
    auto_aim          — AutoTracker.auto_aim decided pan/tilt/zoom_in/zoom_out/stop
    ptz_command       — CranePTZ.move/zoom/focus was called with given values
    ptz_http          — _post_ptz/_post_imaging result (sent / throttled / error)

All writes are thread-safe. Reads return a shallow copy.
"""
import time
from threading import Lock


class TrackingTrace:
    STAGES = ("yolo", "target", "auto_aim", "ptz_command", "ptz_http")

    def __init__(self):
        self._lock = Lock()
        self._records = {stage: None for stage in self.STAGES}

    def record(self, stage, **fields):
        if stage not in self.STAGES:
            return
        entry = {"ts": time.monotonic(), **fields}
        with self._lock:
            self._records[stage] = entry

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            out = {}
            for stage, entry in self._records.items():
                if entry is None:
                    out[stage] = None
                    continue
                e = dict(entry)
                e["age_s"] = round(now - e.pop("ts"), 3)
                out[stage] = e
        return out
