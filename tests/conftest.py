"""
Общие фикстуры pytest для проекта Boras.

Все фикстуры спроектированы так, чтобы тесты НЕ зависели от:
  - реальной камеры (RTSP/ONVIF)
  - реальных весов YOLO (yolov8n.pt)
  - сети

Стратегия: чистая логика тестируется через fake-объекты; интеграционные
тесты проводки app.py используют stub ultralytics + переменные окружения,
чтобы импорт app не падал на этапе конфигурации.

ВНИМАНИЕ: env vars и sys.path манипуляции делаются в КОРНЕВОМ conftest.py
(в ~/Desktop/Boras/conftest.py), который выполняется ПЕРВЫМ. Здесь только
фикстуры и stub'ы.
"""
import os
import sys
import types

import numpy as np
import pytest

# Убеждаемся, что корень проекта в sys.path (дублирующая проверка —
# корневой conftest.py уже должен это сделать, но на всякий случай).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ──────────────────────────────────────────────────────────────────────────
#  Stub тяжёлых зависимостей ДО импорта app-модуля
# ──────────────────────────────────────────────────────────────────────────

def _stub_ultralytics_if_missing():
    """Если ultralytics не установлен — подменяем минимальным stub'ом.
    Real YOLO загрузка тяжёлая и требует скачивания весов; для тестов
    проводки это не нужно."""
    if 'ultralytics' in sys.modules:
        return
    stub = types.ModuleType('ultralytics')
    def _fake_yolo(*args, **kwargs):
        # Возвращаем объект с методом track, который тесты могут подменить.
        class _FakeModel:
            def track(self, frame, persist=True, verbose=False, classes=None):
                return []
        return _FakeModel()
    stub.YOLO = _fake_yolo
    sys.modules['ultralytics'] = stub


def _stub_cv2_if_missing():
    """Если cv2 не установлен — подменяем только те функции, которые
    используются в импортируемом коде (imencode, rectangle, putText)."""
    if 'cv2' in sys.modules:
        return
    stub = types.ModuleType('cv2')
    stub.imread = lambda *a, **kw: None
    stub.imwrite = lambda *a, **kw: True
    stub.rectangle = lambda *a, **kw: None
    stub.putText = lambda *a, **kw: None
    stub.FONT_HERSHEY_SIMPLEX = 0
    # imencode возвращает (bool, buffer) — для тестов достаточно (True, None)
    stub.imencode = lambda *a, **kw: (True, None)
    stub.IMWRITE_JPEG_QUALITY = 0
    sys.modules['cv2'] = stub


_stub_ultralytics_if_missing()
_stub_cv2_if_missing()


# ──────────────────────────────────────────────────────────────────────────
#  Fake-объекты для тестирования чистой логики
# ──────────────────────────────────────────────────────────────────────────

class FakeTensor:
    """Имитация torch-тензора: target_manager вызывает .cpu().numpy()."""
    def __init__(self, arr):
        self._arr = np.asarray(arr, dtype=float)
    def cpu(self):
        return self
    def numpy(self):
        return self._arr
    def __len__(self):
        return len(self._arr)


class FakeBoxesObj:
    """Имитация results[0].boxes из ultralytics."""
    def __init__(self, boxes_xyxy=None, ids=None):
        # Если boxes_xyxy=None и ids=None — нет детекций
        if boxes_xyxy is None and ids is None:
            self.id = None
            self.xyxy = FakeTensor([])
        else:
            n = len(boxes_xyxy) if boxes_xyxy is not None else len(ids)
            self.id = FakeTensor(ids if ids is not None else list(range(1, n + 1)))
            self.xyxy = FakeTensor(boxes_xyxy if boxes_xyxy is not None else [])


class FakeResult:
    """Один элемент списка, который возвращает YOLO.track()."""
    def __init__(self, boxes_xyxy=None, ids=None):
        self.boxes = FakeBoxesObj(boxes_xyxy, ids)


class FakeYOLO:
    """YOLO-модель с программируемым ответом. Тесты могут поменять
    `model.next_result` между вызовами."""
    def __init__(self):
        self.next_result = None  # None → пустой результат (нет детекций)
    def track(self, frame, persist=True, verbose=False, classes=None):
        if self.next_result is None:
            return [FakeResult()]  # boxes.id будет None
        return [self.next_result]


class FakePTZ:
    """Записывает все вызовы move/zoom/focus/stop/goto_home для последующих assert'ов.
    Не делает реальных HTTP-запросов."""
    def __init__(self):
        self.calls = []  # список кортежей: ("move", pan, tilt), ("zoom", s), ...
    def move(self, pan, tilt):
        self.calls.append(("move", float(pan), float(tilt)))
    def zoom(self, speed):
        self.calls.append(("zoom", float(speed)))
    def focus(self, speed):
        self.calls.append(("focus", float(speed)))
    def stop(self):
        self.calls.append(("stop",))
    def stop_pantilt(self):
        self.calls.append(("stop_pantilt",))
    def stop_zoom(self):
        self.calls.append(("stop_zoom",))
    def stop_focus(self):
        self.calls.append(("stop_focus",))
    def goto_home(self, pan=0.0, tilt=0.0, zoom=0.0):
        self.calls.append(("goto_home", float(pan), float(tilt), float(zoom)))

    # Удобные хелперы для тестов
    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None
    def calls_of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeCamera:
    """Камера с программируемым кадром. Не запускает потоков."""
    def __init__(self, frame=None):
        self._frame = frame
        self.status = "stopped"
    def set_frame(self, frame):
        self._frame = frame
    def get_frame(self):
        if self._frame is None:
            return None
        return self._frame.copy()
    def start(self):
        self.status = "live"
    def stop(self):
        self.status = "stopped"


# ──────────────────────────────────────────────────────────────────────────
#  Фикстуры
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_ptz():
    return FakePTZ()


@pytest.fixture
def fake_yolo():
    return FakeYOLO()


@pytest.fixture
def fake_camera():
    return FakeCamera()


@pytest.fixture
def fake_frame():
    """Чёрный кадр 1280x480 (3 канала)."""
    return np.zeros((480, 1280, 3), dtype=np.uint8)


@pytest.fixture
def events():
    from core.events import EventLog
    return EventLog()


@pytest.fixture
def metrics():
    from core.metrics import RuntimeMetrics
    return RuntimeMetrics()


@pytest.fixture
def trace():
    from core.tracking_trace import TrackingTrace
    return TrackingTrace()


@pytest.fixture
def state_machine(events):
    from core.state_machine import CraneStateMachine
    return CraneStateMachine(events=events)


@pytest.fixture
def fake_result_factory():
    """Фабрика FakeResult для тестов TargetManager.
    Использование: fake_result_factory(boxes_xyxy=[[x1,y1,x2,y2], ...])
    """
    def _make(boxes_xyxy=None, ids=None):
        return FakeResult(boxes_xyxy=boxes_xyxy, ids=ids)
    return _make


# ──────────────────────────────────────────────────────────────────────────
#  Фикстура app-модуля для интеграционных тестов проводки (C3)
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app_module():
    """Импортирует модуль app с подменённой YOLO.
    Использует session scope — импорт выполняется один раз за тест-сессию.
    Env vars уже выставлены на уровне модуля conftest (см. выше)."""

    # Подменяем YOLO ДО импорта app, чтобы SecurityBrain не качал веса.
    # app.py выполнит `from ultralytics import YOLO` и получит наш stub.
    import ultralytics
    original_yolo = ultralytics.YOLO
    ultralytics.YOLO = lambda *a, **kw: FakeYOLO()

    try:
        # Импортируем как модуль, чтобы получить ссылки на глобальные объекты.
        import app as app_module
        # Возвращаем YOLO обратно, чтобы другие тесты могли использовать real
        # (хотя в нашем случае все тесты идут через stub).
        return app_module
    finally:
        ultralytics.YOLO = original_yolo
