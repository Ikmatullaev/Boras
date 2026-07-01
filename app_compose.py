"""
app_compose.py — Dependency injection container for the Boras application.

Centralizes the wiring of all components (state_machine, camera, ptz, brain,
runtime, operator) with shared events/metrics/trace. This replaces the
ad-hoc global initialization that lived in app.py and was the original source
of the "events not wired into all components" bug (Phase 1 critical finding).

Part B additions:
  - Registers the camera in the global CameraRegistry (multi-camera support)
  - Creates BotAuthStore + CameraSettingsStore (SQLite persistence)
  - Creates AuthManager (RBAC: boss/guard/viewer)
  - Creates TelegramBotService (interactive bot with PTZ control, live, settings)
  - Returns all bot components in the components dict

Usage:
    from app_compose import compose_app
    components = compose_app()
    runtime = components['runtime']
    app = create_fastapi_app(components)

Or via the convenience function:
    from app_compose import build_app
    app = build_app()  # returns FastAPI app with everything wired
"""
import logging
from typing import Dict, Any

from config import API_TOKEN, CAMERA_IP, CAMERA_PASS, CAMERA_USER, settings
from core.events import EventLog
from core.event_store import EventStore
from core.metrics import RuntimeMetrics
from core.state_machine import CraneStateMachine
from core.tracking_trace import TrackingTrace
from services.camera_service import CameraStream
from services.notification_service import NotificationService
from services.operator_service import OperatorService
from services.ptz_service import CranePTZ
from services.vision_service import SecurityBrain, VisionRuntime

logger = logging.getLogger("crane.app")


def compose_app(
    camera_ip: str = None,
    camera_user: str = None,
    camera_pass: str = None,
    api_token: str = None,
    settings_override=None,
) -> Dict[str, Any]:
    """Build all application components with shared events/metrics/trace.

    All four critical components (state_machine, camera, ptz, brain) receive
    the SAME events/metrics/trace instances — this is the regression fix for
    the Phase 1 bug where they were only passed to VisionRuntime.

    Note: lights functionality was removed — the camera has its own light
    sensors and manages IR/White light automatically.

    Args:
        camera_ip / camera_user / camera_pass / api_token:
            Optional overrides. Defaults come from config module.
        settings_override:
            Optional Settings instance for testing (overrides the singleton).

    Returns:
        Dict with keys: events, metrics, trace, event_store, state_machine,
        camera, ptz, brain, runtime, operator, notifications,
        plus Part B keys: bot_auth_store, camera_settings_store,
        auth_manager, camera_registry, telegram_bot.
    """
    cfg = settings_override or settings
    ip = camera_ip or CAMERA_IP
    user = camera_user or CAMERA_USER
    password = camera_pass or CAMERA_PASS
    token = api_token or API_TOKEN  # noqa: F841 — kept for completeness

    # Shared singletons — ALL components get the SAME instance.
    # This is the key fix: previously app.py only passed these to VisionRuntime.
    events = EventLog()
    metrics = RuntimeMetrics()
    trace = TrackingTrace()

    # SQLite-backed event store — persists events across server restarts.
    # Every emit() will now also write to SQLite via this listener.
    event_store = EventStore(db_path="events.db")
    events.add_listener(lambda ev: event_store.save(ev.name, ev.detail, ev.created_at))

    state_machine = CraneStateMachine(events=events)
    camera = CameraStream(
        ip=ip, username=user, password=password,
        events=events, metrics=metrics,
    )
    ptz = CranePTZ(
        ip=ip, username=user, password=password,
        events=events, metrics=metrics, trace=trace,
    )
    brain = SecurityBrain(
        ptz, state_machine=state_machine, events=events,
        metrics=metrics, trace=trace,
    )
    runtime = VisionRuntime(
        camera=camera,
        brain=brain,
        ptz=ptz,
        state_machine=state_machine,
        events=events,
        metrics=metrics,
        trace=trace,
    )
    operator = OperatorService(runtime, ptz, logger)

    # NotificationService — watches events for target_detected/target_lost/error
    # and sends Telegram alerts (or any other configured provider).
    # Snapshot provider captures the current JPEG frame for photo alerts.
    notifications = NotificationService(
        events=events,
        snapshot_provider=runtime.get_snapshot,
    )

    # ─── Part B: Telegram bot (interactive) ─────────────────────────────
    # Multi-camera registry — register this camera so TelegramBotService can
    # route PTZ commands and notifications to it.
    bot_components = {}
    try:
        from services.telegram_bot import (
            AuthManager,
            BotAuthStore,
            CameraEntry,
            CameraRegistry,
            CameraSettingsStore,
            Role,
            TelegramBotService,
            get_registry,
        )

        # SQLite persistence for bot state (authorized chats + camera settings)
        bot_auth_store = BotAuthStore(db_path="boras_bot.db")
        camera_settings_store = CameraSettingsStore(db_path="boras_bot.db")

        # RBAC: boss/guard/viewer roles
        auth_manager = AuthManager(
            bot_password=cfg.notifications.bot_password,
            default_role=Role.from_string(cfg.notifications.default_role),
            authorized_chat_ids=cfg.notifications.authorized_chat_ids,
            store=bot_auth_store,
        )

        # Register camera in global registry
        registry = get_registry()
        # Construct web_url for this camera (used for "Access" button)
        web_url = ""
        if cfg.notifications.public_base_url:
            web_url = cfg.notifications.public_base_url.rstrip("/")
        registry.register(CameraEntry(
            camera_id=cfg.notifications.camera_id,
            name=cfg.notifications.camera_name,
            snapshot_provider=runtime.get_snapshot,
            ptz_controller=ptz,
            runtime=runtime,
            state_machine=state_machine,
            events=events,
            web_url=web_url,
            webapp_url=cfg.notifications.webapp_url,
        ))

        # Interactive Telegram bot service
        telegram_bot = TelegramBotService(
            token=cfg.notifications.telegram_token,
            auth_manager=auth_manager,
            registry=registry,
            settings_store=camera_settings_store,
            webapp_url=cfg.notifications.webapp_url,
            public_base_url=cfg.notifications.public_base_url,
            camera_id=cfg.notifications.camera_id,
        )

        bot_components = {
            "bot_auth_store": bot_auth_store,
            "camera_settings_store": camera_settings_store,
            "auth_manager": auth_manager,
            "camera_registry": registry,
            "telegram_bot": telegram_bot,
        }
        logger.info(
            "Telegram bot components initialized "
            "(camera_id=%s, bot_enabled=%s, available=%s)",
            cfg.notifications.camera_id,
            cfg.notifications.bot_enabled,
            telegram_bot.is_available,
        )
    except ImportError as e:
        logger.warning(
            "Telegram bot module not available (aiogram not installed?): %s. "
            "Bot service disabled — notifications still work.", e
        )
        bot_components = {
            "bot_auth_store": None,
            "camera_settings_store": None,
            "auth_manager": None,
            "camera_registry": None,
            "telegram_bot": None,
        }

    return {
        "events": events,
        "metrics": metrics,
        "trace": trace,
        "event_store": event_store,
        "state_machine": state_machine,
        "camera": camera,
        "ptz": ptz,
        "brain": brain,
        "runtime": runtime,
        "operator": operator,
        "notifications": notifications,
        **bot_components,
    }
