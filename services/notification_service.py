"""NotificationService — background thread that polls EventLog and dispatches
notifications to all registered providers.

Design decisions:
  - Poll EventLog every N seconds instead of callback pattern.
    Reason: doesn't modify existing EventLog class, isolates failures.
    If Telegram is down, the core pipeline keeps running.
  - Rate limit per event_type (default 30s) to prevent spam.
    A person standing in frame for 60s shouldn't send 60 notifications.
  - Snapshots: when a target_detected event fires, grab the current JPEG
    from VisionRuntime._display_jpeg and attach to the notification.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from config import settings
from core.events import EventLog
from services.notifications import (
    NotificationEvent,
    NotificationProvider,
    TelegramNotificationProvider,
)

logger = logging.getLogger("crane.notifications")


# Maps CraneEvent.name → (NotificationEvent.event_type, message template)
# Events not in this map are ignored (frame_received, move_started, etc.)
_EVENT_MAP = {
    "target_detected": ("target_detected", "Обнаружен человек в кадре"),
    "target_lost":     ("target_lost",     "Цель потеряна — возврат в патруль"),
    "error":           ("error",           "Системная ошибка"),
    "disconnected":    ("disconnected",    "RTSP поток потерян"),
}


class NotificationService:
    """Background thread that watches EventLog and sends notifications.

    Lifecycle:
        svc = NotificationService(events, runtime, providers)
        svc.start()    # spawns background thread
        ...
        svc.stop()     # signals thread to stop, joins

    The thread polls events.recent() every poll_interval seconds, tracks the
    last seen event timestamp, and dispatches new matching events to all
    providers (with per-event-type rate limiting).
    """

    def __init__(
        self,
        events: EventLog,
        snapshot_provider: Optional[Callable[[], Optional[bytes]]] = None,
        providers: Optional[List[NotificationProvider]] = None,
        config=None,
    ):
        cfg = config or settings.notifications
        self._events = events
        self._snapshot_provider = snapshot_provider
        self._cfg = cfg
        # Auto-build providers list if not given
        self._providers: List[NotificationProvider] = providers if providers is not None else self._build_default_providers(cfg)
        # Rate limiting: {event_type: last_sent_monotonic}
        self._last_sent: dict = {}
        # Track the highest event timestamp we've already processed so we
        # don't re-notify on old events when the service restarts.
        self._last_seen_ts: Optional[datetime] = self._init_last_seen_ts()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _build_default_providers(self, cfg) -> List[NotificationProvider]:
        """Construct the default Telegram provider if configured."""
        providers = []
        if cfg.telegram_token and cfg.telegram_chat_id:
            providers.append(TelegramNotificationProvider(
                token=cfg.telegram_token,
                chat_id=cfg.telegram_chat_id,
                camera_name=cfg.camera_name,
                camera_id=cfg.camera_id,
                public_base_url=cfg.public_base_url,
            ))
            logger.info(
                "Telegram notification provider registered "
                "(camera: %s, id: %s, base_url: %s)",
                cfg.camera_name, cfg.camera_id,
                cfg.public_base_url or "(none)"
            )
        return providers

    def _init_last_seen_ts(self) -> Optional[datetime]:
        """On startup, skip all events that already exist in the log.

        Otherwise the service would spam notifications for events that
        happened before it started (e.g. on app restart with a person in frame).
        """
        recent = self._events.recent(limit=1000)
        if not recent:
            return None
        # Find the max created_at
        return max(
            (datetime.fromisoformat(e["created_at"]) for e in recent),
            default=None,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def providers_count(self) -> int:
        return len(self._providers)

    def start(self):
        if self._running:
            return
        if not self._cfg.enabled:
            logger.info("Notifications disabled (settings.notifications.enabled=False)")
            return
        if not self._providers:
            logger.warning("Notifications enabled but no providers configured — skipping start")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="notification-service"
        )
        self._thread.start()
        logger.info("NotificationService started (%d providers, polling every %.1fs)",
                    len(self._providers), self._cfg.poll_interval)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("NotificationService stopped")

    def _run_loop(self):
        """Main polling loop — runs in background thread."""
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                # Defensive: never let the loop die on unexpected errors
                logger.error("NotificationService poll error: %s", e)
            time.sleep(self._cfg.poll_interval)

    def _poll_once(self):
        """Check for new events since last poll and dispatch notifications."""
        recent = self._events.recent(limit=50)
        if not recent:
            return

        new_events = []
        for ev in recent:
            try:
                ev_ts = datetime.fromisoformat(ev["created_at"])
            except (ValueError, KeyError):
                continue
            # Skip events we've already processed
            if self._last_seen_ts and ev_ts <= self._last_seen_ts:
                continue
            # Skip events that don't match notify_on list
            if ev["name"] not in self._cfg.notify_on:
                continue
            new_events.append((ev_ts, ev))

        if not new_events:
            return

        # Update last_seen_ts to the newest event we're about to process
        self._last_seen_ts = max(ts for ts, _ in new_events)

        # Dispatch each new event (subject to per-type rate limit)
        for _, ev in new_events:
            self._dispatch_event(ev)

    def _dispatch_event(self, raw_event: dict):
        """Convert raw CraneEvent to NotificationEvent and send to all providers.

        For target_detected: wait SNAPSHOT_DELAY_SECONDS (default 2.5s) before
        capturing snapshot — gives PTZ camera time to center on the target so
        the photo shows the person clearly, not on the edge of frame.
        """
        name = raw_event["name"]
        if name not in _EVENT_MAP:
            return

        event_type, message = _EVENT_MAP[name]

        # Rate limit per event type
        now = time.monotonic()
        last = self._last_sent.get(event_type, 0)
        if now - last < self._cfg.rate_limit_seconds:
            logger.debug("Rate-limited %s notification (last sent %.1fs ago)",
                         event_type, now - last)
            return
        self._last_sent[event_type] = now

        # Build NotificationEvent
        try:
            ts = datetime.fromisoformat(raw_event["created_at"])
        except (ValueError, KeyError):
            ts = datetime.now()

        snapshot = None
        # Only attach snapshot for target_detected events (photo of intruder)
        if event_type == "target_detected" and self._snapshot_provider:
            # Wait for camera to center on target before capturing photo.
            # Without this, photo is taken at the moment of detection when
            # person is still on the edge of frame — not useful as evidence.
            # 4s gives AutoTracker time to pan/tilt to target and stabilize.
            SNAPSHOT_DELAY = 4.0
            logger.info("Waiting %.1fs before snapshot (camera centering)...", SNAPSHOT_DELAY)
            time.sleep(SNAPSHOT_DELAY)
            try:
                snapshot = self._snapshot_provider()
            except Exception as e:
                logger.warning("Snapshot capture failed: %s", e)
                snapshot = None

        # Extract confidence from event detail if present (e.g. "confidence=0.87")
        confidence_str = ""
        detail = raw_event.get("detail", "")
        if "confidence=" in detail:
            try:
                confidence_str = detail.split("confidence=")[1].split(",")[0].strip()
            except (IndexError, ValueError):
                confidence_str = ""

        notification = NotificationEvent(
            event_type=event_type,
            message=message,
            detail=detail,
            timestamp=ts,
            snapshot=snapshot,
            confidence=confidence_str,
        )

        # Dispatch to all configured providers
        for provider in self._providers:
            if not provider.is_configured():
                continue
            try:
                ok = provider.send(notification)
                if ok:
                    logger.info("Notification sent via %s: %s",
                                provider.name, event_type)
                else:
                    logger.warning("Notification failed via %s: %s",
                                   provider.name, event_type)
            except Exception as e:
                logger.error("Provider %s raised: %s", provider.name, e)
