"""
End-to-end smoke test for the new tracking-chain instrumentation.

Wires up the real SecurityBrain + AutoTracker + CranePTZ with a fake camera
frame and a fake HTTP session, then prints what would appear in /api/status.

This does NOT require a camera, network, or YOLO weights — it stubs the YOLO
model output to simulate a single detected person and the requests.Session to
simulate HTTP 200 responses.

Run:  python3 scripts/e2e_trace_check.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

# Stub ultralytics + cv2 if not installed, so we can import SecurityBrain
# without the heavy ML stack. We replace YOLO with our fake below anyway.
if 'ultralytics' not in sys.modules:
    sys.modules['ultralytics'] = type(sys)('ultralytics')
    sys.modules['ultralytics'].YOLO = lambda *a, **kw: None
if 'cv2' not in sys.modules:
    import types
    cv2_stub = types.ModuleType('cv2')
    cv2_stub.imread = lambda *a, **kw: None
    cv2_stub.imwrite = lambda *a, **kw: True
    cv2_stub.rectangle = lambda *a, **kw: None
    cv2_stub.putText = lambda *a, **kw: None
    cv2_stub.FONT_HERSHEY_SIMPLEX = 0
    cv2_stub.imencode = lambda *a, **kw: (True, None)
    sys.modules['cv2'] = cv2_stub

from core.events import EventLog
from core.metrics import RuntimeMetrics
from core.state_machine import CraneStateMachine
from core.tracking_trace import TrackingTrace
from services.ptz_service import CranePTZ
from services.vision_service import SecurityBrain


# ---- Fake YOLO result -------------------------------------------------------
# YOLO returns torch-like tensors; target_manager calls .cpu().numpy() on xyxy.
class FakeTensor:
    def __init__(self, arr): self._arr = np.asarray(arr)
    def cpu(self): return self
    def numpy(self): return self._arr
    def __len__(self): return len(self._arr)

class FakeBoxesObj:
    id = FakeTensor([1])
    xyxy = FakeTensor([[400.0, 100.0, 600.0, 400.0]])  # one person box

class FakeResult:
    boxes = FakeBoxesObj()

class FakeYOLO:
    def track(self, frame, persist=True, verbose=False, classes=None):
        return [FakeResult()]


# ---- Fake HTTP session ------------------------------------------------------
class FakeResp:
    status_code = 200

class FakeSession:
    def __init__(self): self.posts = []
    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, (data or '')[:80]))
        return FakeResp()


def main():
    events = EventLog()
    metrics = RuntimeMetrics()
    trace = TrackingTrace()
    state_machine = CraneStateMachine(events=events)

    ptz = CranePTZ(ip='1.2.3.4', username='u', password='p',
                   events=events, metrics=metrics, trace=trace)
    ptz.session = FakeSession()
    ptz.min_command_interval = 0.0  # disable throttle for clean test

    # Build brain but swap in the fake YOLO before process_frame runs
    brain = SecurityBrain(ptz, state_machine=state_machine,
                          events=events, metrics=metrics, trace=trace)
    brain.model = FakeYOLO()

    # Simulate a 1280x480 frame
    frame = np.zeros((480, 1280, 3), dtype=np.uint8)

    # Enable auto-guard so process_frame goes through tracking
    state_machine.enable_auto_guard()

    print("=== Before process_frame ===")
    print("mode:", state_machine.mode.value)
    print()

    brain.process_frame(frame)

    print("=== After process_frame ===")
    print("mode:", state_machine.mode.value)
    print("metrics:", metrics.snapshot())
    print()
    print("events:")
    for e in events.recent():
        print(f"  - {e['name']}: {e['detail']}")
    print()
    print("tracking_trace:")
    import json
    print(json.dumps(trace.snapshot(), indent=2, default=str))
    print()
    print(f"HTTP POSTs sent to camera: {len(ptz.session.posts)}")
    for url, body in ptz.session.posts:
        print(f"  -> {url}")
        print(f"     {body}...")

    # ---- Scenario 2: target off-center, should trigger pan + HTTP POST ----
    print()
    print("=" * 60)
    print("Scenario 2: target far off-center (dx > deadzone)")
    print("=" * 60)
    FakeBoxesObj.xyxy = FakeTensor([[1100.0, 100.0, 1300.0, 400.0]])  # cx=1200
    ptz.session.posts.clear()
    brain.process_frame(frame)

    print()
    print("tracking_trace (post scenario 2):")
    import json
    print(json.dumps(trace.snapshot(), indent=2, default=str))
    print()
    print(f"HTTP POSTs sent to camera: {len(ptz.session.posts)}")
    for url, body in ptz.session.posts:
        print(f"  -> {url}")
        print(f"     {body}...")


if __name__ == "__main__":
    main()
