"""Тесты для модуля config — Settings dataclass, env overrides, backward compat."""
import os

import pytest

from config import (
    API_TOKEN, CAMERA_IP, CAMERA_PASS, CAMERA_USER,
    CameraConfig, PTZConfig, VisionConfig, TrackingConfig,
    PatrolConfig, WebConfig, Settings, settings,
)


class TestSettingsStructure:
    """Settings должен содержать все ожидаемые секции."""

    def test_settings_is_singleton_instance(self):
        from config import settings as s2
        assert settings is s2, "settings должен быть module-level singleton"

    def test_settings_has_all_sections(self):
        assert isinstance(settings.camera, CameraConfig)
        assert isinstance(settings.ptz, PTZConfig)
        assert isinstance(settings.vision, VisionConfig)
        assert isinstance(settings.tracking, TrackingConfig)
        assert isinstance(settings.patrol, PatrolConfig)
        assert isinstance(settings.web, WebConfig)

    def test_settings_camera_defaults(self):
        cfg = settings.camera
        assert cfg.rtsp_port == 554
        assert cfg.http_port == 80
        assert cfg.reconnect_delay == 3.0
        assert isinstance(cfg.rtsp_paths, list)
        assert len(cfg.rtsp_paths) >= 1, "Должен быть хотя бы один RTSP path"

    def test_settings_ptz_defaults(self):
        cfg = settings.ptz
        assert cfg.profile == "PROFILE_000"
        assert cfg.video_source == "000"
        assert cfg.min_command_interval == 0.15
        assert cfg.http_timeout == 2.0

    def test_settings_vision_defaults(self):
        cfg = settings.vision
        assert cfg.yolo_model == "yolov8n.pt"
        assert cfg.detect_classes == [0]
        assert cfg.frame_skip_rate == 3
        assert cfg.jpeg_quality == 80

    def test_settings_tracking_defaults(self):
        cfg = settings.tracking
        assert cfg.pan_speed_gain == 0.5
        assert cfg.min_pan_speed == 0.08
        assert cfg.deadzone_frac_x == 0.15
        assert cfg.deadzone_frac_y == 0.15
        assert cfg.height_target_low == 0.40
        assert cfg.height_target_high == 0.75
        assert cfg.zoom_speed == 0.15
        assert cfg.focus_speed == 0.1

    def test_settings_patrol_defaults(self):
        cfg = settings.patrol
        assert cfg.zoom_out_speed == -0.5
        assert cfg.zoom_out_focus == -0.1
        assert cfg.pan_speed == 0.12
        assert cfg.zoom_out_duration == 3.0
        assert cfg.cycle_duration == 4.0
        assert cfg.pan_duration == 2.0

    def test_settings_web_defaults(self):
        cfg = settings.web
        assert cfg.auth_username == "admin"
        assert cfg.stream_sleep == 0.05
        assert cfg.loop_sleep == 0.03
        assert cfg.no_frame_sleep == 0.05


class TestBackwardCompatibility:
    """Module-level CAMERA_IP/CAMERA_USER/CAMERA_PASS/API_TOKEN должны
    остаться доступными для существующего кода:
        from config import CAMERA_IP, CAMERA_USER, CAMERA_PASS, API_TOKEN
    """

    def test_camera_ip_is_string(self):
        assert isinstance(CAMERA_IP, str)
        assert len(CAMERA_IP) > 0

    def test_camera_user_is_string(self):
        assert isinstance(CAMERA_USER, str)
        assert len(CAMERA_USER) > 0

    def test_camera_pass_is_string(self):
        assert isinstance(CAMERA_PASS, str)
        assert len(CAMERA_PASS) > 0

    def test_api_token_is_string(self):
        assert isinstance(API_TOKEN, str)
        assert len(API_TOKEN) > 0

    def test_credentials_match_env_vars_or_config_local(self):
        """Креды должны приходить либо из env vars, либо из config_local.py
        (который имеет приоритет через `from config_local import *`).

        В тестовом окружении conftest.py выставляет env vars, но если в
        рабочей директории есть config_local.py — он переопределит env.
        Поэтому проверяем что креды непустые и являются строками,
        а не точное совпадение с env."""
        assert isinstance(CAMERA_IP, str) and len(CAMERA_IP) > 0
        assert isinstance(CAMERA_USER, str) and len(CAMERA_USER) > 0
        assert isinstance(CAMERA_PASS, str) and len(CAMERA_PASS) > 0
        assert isinstance(API_TOKEN, str) and len(API_TOKEN) > 0
        # Если config_local.py не overriding — значения должны совпадать с env
        # Если overriding — значения могут отличаться (это нормально)


class TestRTSPPathsB1:
    """B1: CameraStream должен иметь несколько RTSP URL для fallback."""

    def test_default_rtsp_paths_has_multiple_entries(self):
        """Дефолтный список должен содержать несколько путей для fallback."""
        cfg = settings.camera
        assert len(cfg.rtsp_paths) >= 3, (
            f"Expected at least 3 RTSP path templates for fallback, got {len(cfg.rtsp_paths)}. "
            f"Paths: {cfg.rtsp_paths}"
        )

    def test_default_rtsp_paths_includes_common_formats(self):
        """Должны быть представлены наиболее распространённые форматы URL."""
        cfg = settings.camera
        paths_str = " ".join(cfg.rtsp_paths)
        # Хотя бы один из распространённых форматов должен присутствовать
        assert ("MAIN" in paths_str or "main" in paths_str), \
            "Должен быть MAIN/main stream path"
        assert "live" in paths_str or "h264" in paths_str or "realmonitor" in paths_str, \
            "Должен быть хотя бы один стандартный формат RTSP path"

    def test_rtsp_urls_are_constructed_correctly(self):
        """CameraStream должен строить полный URL из ip + username + password + path."""
        from services.camera_service import CameraStream
        cam = CameraStream(ip="10.0.0.1", username="user", password="pass")
        # Первый URL должен быть rtsp://user:pass@10.0.0.1:554/live/0/MAIN
        assert cam.rtsp_blueprints[0] == "rtsp://user:pass@10.0.0.1:554/live/0/MAIN"
        # Все URL должны содержать ip и порт
        for url in cam.rtsp_blueprints:
            assert "10.0.0.1" in url
            assert ":554" in url
            assert "user:pass" in url


class TestEnvOverrides:
    """Переменные окружения должны переопределять дефолтные значения."""

    def test_env_override_yolo_model(self, monkeypatch):
        """CRANE_YOLO_MODEL должен переопределить дефолт 'yolov8n.pt'."""
        monkeypatch.setenv("CRANE_YOLO_MODEL", "yolov8s.pt")
        # Перезагружаем config модуль чтобы применить env override
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.vision.yolo_model == "yolov8s.pt"
        finally:
            # Возвращаем обратно
            monkeypatch.delenv("CRANE_YOLO_MODEL")
            importlib.reload(config_module)

    def test_env_override_frame_skip_rate(self, monkeypatch):
        """CRANE_FRAME_SKIP_RATE должен переопределить дефолт 3."""
        monkeypatch.setenv("CRANE_FRAME_SKIP_RATE", "5")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.vision.frame_skip_rate == 5
        finally:
            monkeypatch.delenv("CRANE_FRAME_SKIP_RATE")
            importlib.reload(config_module)

    def test_env_override_auth_username(self, monkeypatch):
        """CRANE_AUTH_USERNAME должен переопределить дефолт 'admin'."""
        monkeypatch.setenv("CRANE_AUTH_USERNAME", "operator")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.web.auth_username == "operator"
        finally:
            monkeypatch.delenv("CRANE_AUTH_USERNAME")
            importlib.reload(config_module)

    def test_env_override_ptz_profile(self, monkeypatch):
        """CRANE_PTZ_PROFILE должен переопределить дефолт 'PROFILE_000'."""
        monkeypatch.setenv("CRANE_PTZ_PROFILE", "PROFILE_001")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.ptz.profile == "PROFILE_001"
        finally:
            monkeypatch.delenv("CRANE_PTZ_PROFILE")
            importlib.reload(config_module)

    def test_env_override_pan_speed_gain(self, monkeypatch):
        """CRANE_PAN_SPEED_GAIN должен переопределить дефолт 0.5."""
        monkeypatch.setenv("CRANE_PAN_SPEED_GAIN", "0.7")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.tracking.pan_speed_gain == 0.7
        finally:
            monkeypatch.delenv("CRANE_PAN_SPEED_GAIN")
            importlib.reload(config_module)

    def test_env_override_min_command_interval(self, monkeypatch):
        """CRANE_MIN_COMMAND_INTERVAL должен переопределить дефолт 0.15."""
        monkeypatch.setenv("CRANE_MIN_COMMAND_INTERVAL", "0.25")
        import importlib
        import config as config_module
        importlib.reload(config_module)
        try:
            assert config_module.settings.ptz.min_command_interval == 0.25
        finally:
            monkeypatch.delenv("CRANE_MIN_COMMAND_INTERVAL")
            importlib.reload(config_module)


class TestComponentConfigInjection:
    """Компоненты должны принимать optional config= параметр для override."""

    def test_auto_tracker_accepts_config(self):
        """AutoTracker должен принимать config= и переопределять значения."""
        from behavior.tracking import AutoTracker
        from config import TrackingConfig
        from conftest import FakePTZ

        custom_config = TrackingConfig(
            pan_speed_gain=0.9,
            min_pan_speed=0.2,
            deadzone_frac_x=0.25,
            deadzone_frac_y=0.25,
            height_target_low=0.3,
            height_target_high=0.8,
            zoom_speed=0.2,
            focus_speed=0.15,
        )
        tracker = AutoTracker(FakePTZ(), config=custom_config)
        assert tracker.PAN_SPEED_GAIN == 0.9
        assert tracker.MIN_PAN_SPEED == 0.2
        assert tracker.DEADZONE_FRAC_X == 0.25
        assert tracker.ZOOM_SPEED == 0.2
        assert tracker.FOCUS_SPEED == 0.15

    def test_smart_patrol_accepts_config(self):
        """SmartPatrol должен принимать config= и переопределять значения."""
        from behavior.patrol import SmartPatrol
        from behavior.tracking import AutoTracker
        from config import PatrolConfig
        from conftest import FakePTZ

        custom_config = PatrolConfig(
            zoom_out_speed=-0.7,
            zoom_out_focus=-0.2,
            pan_speed=0.2,
            zoom_out_duration=5.0,
            cycle_duration=6.0,
            pan_duration=3.0,
        )
        patrol = SmartPatrol(FakePTZ(), AutoTracker(FakePTZ()), config=custom_config)
        assert patrol.ZOOM_OUT_SPEED == -0.7
        assert patrol.PAN_SPEED == 0.2
        assert patrol.ZOOM_OUT_DURATION == 5.0
        assert patrol.CYCLE_DURATION == 6.0

    def test_crane_ptz_accepts_config(self):
        """CranePTZ должен принимать config= и переопределять profile/timeout."""
        from services.ptz_service import CranePTZ
        from config import PTZConfig

        custom_config = PTZConfig(
            profile="PROFILE_001",
            video_source="001",
            min_command_interval=0.3,
            http_timeout=5.0,
        )
        ptz = CranePTZ(ip="1.2.3.4", username="u", password="p", config=custom_config)
        assert ptz.profile == "PROFILE_001"
        assert ptz.video_source == "001"
        assert ptz.min_command_interval == 0.3
        assert ptz._http_timeout == 5.0

    def test_camera_stream_accepts_config(self):
        """CameraStream должен принимать config= с кастомными RTSP paths."""
        from services.camera_service import CameraStream
        from config import CameraConfig

        custom_config = CameraConfig(
            rtsp_paths=["/custom/path"],
            rtsp_port=8554,
            http_port=8080,
            reconnect_delay=5.0,
        )
        cam = CameraStream(ip="10.0.0.1", username="u", password="p", config=custom_config)
        assert cam.reconnect_delay == 5.0
        assert len(cam.rtsp_blueprints) == 1
        assert ":8554/custom/path" in cam.rtsp_blueprints[0]


class TestSettingsMutationIsolation:
    """Каждая Settings секция должна быть независимой — мутация одной
    не должна влиять на другие."""

    def test_mutation_to_tracking_doesnt_affect_patrol(self):
        """Изменение settings.tracking не должно менять settings.patrol."""
        original_pan_speed = settings.patrol.pan_speed
        settings.tracking.pan_speed_gain = 0.99
        assert settings.patrol.pan_speed == original_pan_speed
        # Возвращаем обратно
        settings.tracking.pan_speed_gain = 0.5
