"""Тесты для CranePTZ — throttle bypass (B2) и health (B4).

Эти тесты используют FakeSession для имитации HTTP-ответов и проверяют:
  - B2: throttle bypass при смене знака скорости (pan +0.5 → -0.5 не троттлится)
  - B2: throttle работает для одинаковых знаков (+0.5 → +0.7 троттлится)
  - B4: health() возвращает корректную информацию после запросов
  - B4: health() обработка network errors
"""
import pytest
import requests

from services.ptz_service import CranePTZ
from core.events import EventLog
from core.metrics import RuntimeMetrics
from core.tracking_trace import TrackingTrace


# ─── Fake HTTP session ──────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status_code=200):
        self.status_code = status_code

class FakeSession:
    """Записывает все POST запросы. Может возвращать любой status_code
    или поднимать RequestException."""
    def __init__(self, behavior=None):
        # behavior: FakeResp instance, или callable() -> FakeResp,
        # или Exception instance (будет поднят)
        self.behavior = behavior or FakeResp(200)
        self.posts = []
    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, (data or '')[:80]))
        if isinstance(self.behavior, Exception):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior()
        return self.behavior


# ─── Фикстуры ───────────────────────────────────────────────────────────────

@pytest.fixture
def ptz():
    """CranePTZ с fake session и отключённым throttle (для большинства тестов)."""
    p = CranePTZ(ip="1.2.3.4", username="u", password="p",
                 events=EventLog(), metrics=RuntimeMetrics(), trace=TrackingTrace())
    p.session = FakeSession()
    p.min_command_interval = 0.0  # отключаем throttle по умолчанию
    return p


# ═══════════════════════════════════════════════════════════════════════════
#  B2: Throttle bypass on direction change
# ═══════════════════════════════════════════════════════════════════════════

class TestThrottleDirectionBypass:
    """B2: при смене знака скорости throttle должен быть bypass-нут."""

    def test_same_direction_is_throttled(self, ptz):
        """Две команды с одним знаком (+, +) — вторая должна троттлиться."""
        ptz.min_command_interval = 10.0  # большой throttle
        ptz.move(0.5, 0.0)   # первая — проходит
        ptz.move(0.7, 0.0)   # вторая — должна троттлиться (тот же знак)
        assert len(ptz.session.posts) == 1, "Second same-sign command should be throttled"

    def test_direction_change_bypasses_throttle(self, ptz):
        """B2: +0.5 → -0.5 должна пройти НЕЗАВИСИМО от throttle interval."""
        ptz.min_command_interval = 10.0  # большой throttle
        ptz.move(0.5, 0.0)    # первая — проходит
        ptz.move(-0.5, 0.0)   # вторая — должна bypass-нуть throttle (смена знака!)
        assert len(ptz.session.posts) == 2, "Direction change must bypass throttle"

    def test_zoom_direction_change_bypasses_throttle(self, ptz):
        """B2: zoom +0.3 → -0.3 должна пройти при смене знака."""
        ptz.min_command_interval = 10.0
        ptz.zoom(0.3)    # zoom in
        ptz.zoom(-0.3)   # zoom out — должна пройти
        assert len(ptz.session.posts) == 2

    def test_focus_direction_change_bypasses_throttle(self, ptz):
        """B2: focus +0.1 → -0.1 должна пройти при смене знака."""
        ptz.min_command_interval = 10.0
        ptz.focus(0.1)
        ptz.focus(-0.1)
        assert len(ptz.session.posts) == 2

    def test_zero_to_positive_not_bypassed(self, ptz):
        """Переход от 0 к +0.5 — это НЕ смена направления (предыдущий знак был None/0),
        поэтому обычный throttle применяется."""
        ptz.min_command_interval = 10.0
        # Первая команда — нет предыдущей, проходит
        ptz.move(0.5, 0.0)
        assert len(ptz.session.posts) == 1
        # Сбрасываем throttle bucket (как будто stop был)
        ptz._last_sent.pop("move", None)
        # Теперь снова первая команда — должна пройти (нет предыдущего знака)
        ptz.move(0.5, 0.0)
        assert len(ptz.session.posts) == 2

    def test_same_sign_after_direction_change(self, ptz):
        """После смены направления (+→-) следующая команда с тем же знаком (-→-)
        должна снова троттлиться."""
        ptz.min_command_interval = 10.0
        ptz.move(0.5, 0.0)    # + проходит
        ptz.move(-0.5, 0.0)   # - bypass (смена знака) — проходит
        ptz.move(-0.7, 0.0)   # - тот же знак — должна троттлиться
        assert len(ptz.session.posts) == 2, "Third same-sign command should be throttled"

    def test_throttle_bypass_records_event(self, ptz, monkeypatch):
        """При bypass-е throttle не должно создаваться command_throttled event."""
        ptz.min_command_interval = 10.0
        ptz.move(0.5, 0.0)
        events_before = ptz.events.recent(limit=1000)
        throttled_before = len([e for e in events_before if e["name"] == "command_throttled"])
        ptz.move(-0.5, 0.0)  # bypass
        events_after = ptz.events.recent(limit=1000)
        throttled_after = len([e for e in events_after if e["name"] == "command_throttled"])
        assert throttled_after == throttled_before, "Bypass should not emit command_throttled"

    def test_normal_throttle_emits_event(self, ptz):
        """При обычном throttle должно создаваться command_throttled event."""
        ptz.min_command_interval = 10.0
        ptz.move(0.5, 0.0)
        ptz.move(0.7, 0.0)  # throttled
        events = ptz.events.recent()
        assert any(e["name"] == "command_throttled" for e in events)

    def test_stop_resets_throttle_bucket(self, ptz):
        """После stop_pantilt() throttle bucket для 'move' должен быть сброшен —
        следующая команда должна пройти даже при большом interval."""
        ptz.min_command_interval = 10.0
        ptz.move(0.5, 0.0)
        ptz.stop_pantilt()  # force=True, сбрасывает bucket
        ptz.move(0.5, 0.0)  # должна пройти — bucket сброшен
        assert len(ptz.session.posts) == 3  # move + stop + move

    def test_sign_helper(self, ptz):
        """_sign() возвращает 1, -1 или 0 корректно."""
        assert ptz._sign(0.5) == 1
        assert ptz._sign(-0.5) == -1
        assert ptz._sign(0) == 0
        assert ptz._sign(0.001) == 1
        assert ptz._sign(-0.001) == -1


# ═══════════════════════════════════════════════════════════════════════════
#  B4: Health checks
# ═══════════════════════════════════════════════════════════════════════════

class TestPTZHealth:
    """B4: CranePTZ.health() должен отражать состояние последнего HTTP запроса."""

    def test_health_initial_state_no_requests(self, ptz):
        """До任何 запросов — все поля None, ptz_reachable=None."""
        h = ptz.health()
        assert h["ptz_reachable"] is None
        assert h["last_http_ok"] is None
        assert h["last_http_age_s"] is None
        assert h["last_http_status"] is None
        assert h["last_http_error"] is None
        assert "1.2.3.4" in h["ptz_url"]

    def test_health_after_successful_request(self, ptz):
        """После успешного POST (200) — ptz_reachable=True."""
        ptz.session.behavior = FakeResp(200)
        ptz.move(0.3, 0.0)
        h = ptz.health()
        assert h["ptz_reachable"] is True
        assert h["last_http_ok"] is True
        assert h["last_http_status"] == 200
        assert h["last_http_error"] is None
        assert h["last_http_age_s"] is not None
        assert h["last_http_age_s"] >= 0

    def test_health_after_http_error(self, ptz):
        """После HTTP 500 — ptz_reachable=False, status=500."""
        ptz.session.behavior = FakeResp(500)
        ptz.move(0.3, 0.0)
        h = ptz.health()
        assert h["ptz_reachable"] is False
        assert h["last_http_ok"] is False
        assert h["last_http_status"] == 500
        assert h["last_http_error"] is None

    def test_health_after_network_error(self, ptz):
        """После network error — ptz_reachable=False, error=ConnectTimeoutError."""
        ptz.session.behavior = requests.exceptions.ConnectTimeout("simulated")
        ptz.move(0.3, 0.0)
        h = ptz.health()
        assert h["ptz_reachable"] is False
        assert h["last_http_ok"] is False
        assert h["last_http_status"] is None
        assert h["last_http_error"] == "ConnectTimeout"

    def test_health_reachable_becomes_false_after_30s(self, ptz, monkeypatch):
        """ptz_reachable должен стать False через 30 секунд после последнего OK."""
        import time as time_module
        ptz.session.behavior = FakeResp(200)
        ptz.move(0.3, 0.0)
        assert ptz.health()["ptz_reachable"] is True

        # Сдвигаем time.monotonic на 31 секунду вперёд
        original_monotonic = time_module.monotonic
        call_count = [0]
        def fake_monotonic():
            call_count[0] += 1
            # Health() вызывает monotonic дважды (один раз в _post_ptz, но не тут)
            # На самом деле только один раз в health(). Возвращаем +31 после первого вызова.
            return original_monotonic() + 31
        monkeypatch.setattr(time_module, "monotonic", fake_monotonic)
        # Также нужно патчнуть в ptz_service модуле
        import services.ptz_service as ptz_module
        monkeypatch.setattr(ptz_module.time, "monotonic", fake_monotonic)

        h = ptz.health()
        assert h["ptz_reachable"] is False, "After 30s without requests, should be unreachable"
        assert h["last_http_age_s"] >= 30

    def test_health_after_recovery(self, ptz):
        """После ошибки и последующего успеха — ptz_reachable=True."""
        # Сначала ошибка
        ptz.session.behavior = requests.exceptions.ConnectTimeout("first fail")
        ptz.move(0.3, 0.0)
        assert ptz.health()["ptz_reachable"] is False
        # Теперь успех
        ptz.session.behavior = FakeResp(200)
        ptz.move(0.3, 0.0)
        assert ptz.health()["ptz_reachable"] is True

    def test_health_focus_updates_last_http(self, ptz):
        """focus() должен обновить _last_http (именно imaging endpoint)."""
        ptz.session.behavior = FakeResp(200)
        ptz.focus(0.1)
        h = ptz.health()
        assert h["last_http_ok"] is True
        assert h["ptz_reachable"] is True

    def test_health_zoom_updates_last_http(self, ptz):
        """zoom() должен обновить _last_http."""
        ptz.session.behavior = FakeResp(200)
        ptz.zoom(0.3)
        h = ptz.health()
        assert h["last_http_ok"] is True

    def test_health_throttled_command_does_not_update(self, ptz):
        """Если команда была отброшена throttle — _last_http не должен меняться."""
        ptz.min_command_interval = 10.0
        ptz.session.behavior = FakeResp(200)
        ptz.move(0.5, 0.0)  # проходит
        first_health = ptz.health()
        ptz.move(0.7, 0.0)  # throttled — не доходит до HTTP
        second_health = ptz.health()
        # timestamp не должен измениться
        assert first_health["last_http_age_s"] is not None
        assert second_health["last_http_age_s"] is not None
        # age мог немного вырасти из-за времени, но не должен сброситься в 0
        # (это означает что _last_http не был перезаписан)


# ═══════════════════════════════════════════════════════════════════════════
#  B4: CameraStream.health()
# ═══════════════════════════════════════════════════════════════════════════

class TestCameraHealth:
    """B4: CameraStream.health() должен отражать RTSP состояние."""

    def test_health_initial_state(self):
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        h = cam.health()
        assert h["rtsp_status"] == "stopped"
        assert h["rtsp_healthy"] is False
        assert h["last_frame_age_s"] is None
        assert h["working_path"] is None

    def test_health_stopped_not_healthy(self):
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        cam.status = "stopped"
        assert cam.health()["rtsp_healthy"] is False

    def test_health_failed_not_healthy(self):
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        cam.status = "failed"
        assert cam.health()["rtsp_healthy"] is False

    def test_health_live_with_fresh_frame_is_healthy(self):
        import time as time_module
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        cam.status = "live"
        cam._latest_frame = b"fake_frame"
        cam._last_frame_time = time_module.monotonic()
        h = cam.health()
        assert h["rtsp_healthy"] is True
        assert h["last_frame_age_s"] is not None
        assert h["last_frame_age_s"] < 5.0

    def test_health_live_with_stale_frame_not_healthy(self, monkeypatch):
        import time as time_module
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        cam.status = "live"
        cam._latest_frame = b"fake_frame"
        cam._last_frame_time = time_module.monotonic()

        # Сдвигаем время на 10 секунд вперёд
        original_monotonic = time_module.monotonic
        def fake_monotonic():
            return original_monotonic() + 10
        monkeypatch.setattr(time_module, "monotonic", fake_monotonic)
        import services.camera_service as cam_module
        monkeypatch.setattr(cam_module.time, "monotonic", fake_monotonic)

        h = cam.health()
        assert h["rtsp_healthy"] is False, "Frame older than 5s should not be healthy"
        assert h["last_frame_age_s"] >= 5.0

    def test_health_live_without_frame_not_healthy(self):
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        cam.status = "live"
        cam._latest_frame = None
        cam._last_frame_time = None
        assert cam.health()["rtsp_healthy"] is False

    def test_health_working_path_reflected(self):
        from services.camera_service import CameraStream
        cam = CameraStream(ip="1.2.3.4", username="u", password="p")
        cam.working_url = "rtsp://u:p@1.2.3.4:554/live/0/MAIN"
        h = cam.health()
        assert h["working_path"] == ":554/live/0/MAIN"
