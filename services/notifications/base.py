"""Base classes for notification providers.

A NotificationProvider is anything that can deliver a security alert to an
external channel (Telegram, Email, SMS, webhook, mobile app push, etc.).

The NotificationService owns the rate-limiting and event filtering logic;
providers just format and send.
"""
import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class NotificationEvent:
    """A normalized event that providers render into a message.

    Attributes:
        event_type: one of "target_detected", "target_lost", "error",
                    "disconnected", "state_changed".
        message:    human-readable short message ("Обнаружен человек").
        detail:     extra context (HTTP status, error class, etc.).
        timestamp:  when the underlying CraneEvent was emitted.
        snapshot:   optional JPEG bytes of the current frame, for photo alerts.
        confidence: optional detection confidence string ("0.87") for target_detected.
    """
    event_type: str
    message: str
    detail: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    snapshot: Optional[bytes] = None
    confidence: str = ""


class NotificationProvider(abc.ABC):
    """Abstract base for all notification providers.

    Subclasses MUST implement send(). They SHOULD NOT raise on network errors —
    instead log and return False so the NotificationService can continue
    processing future events. Raising breaks the background thread.
    """

    @abc.abstractmethod
    def send(self, event: NotificationEvent) -> bool:
        """Deliver the event. Returns True on success, False on failure.

        Implementations should be idempotent and never raise.
        """
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Provider name for logging ("telegram", "email", etc.)."""
        raise NotImplementedError

    def is_configured(self) -> bool:
        """Whether this provider has enough config to actually send.

        Override in subclasses. Default True for providers that don't need
        external config (e.g. a webhook to localhost).
        """
        return True
