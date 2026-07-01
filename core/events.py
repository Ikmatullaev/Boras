from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, List


@dataclass(frozen=True)
class CraneEvent:
    name: str
    created_at: datetime
    detail: str = ""


def make_event(name, detail=""):
    return CraneEvent(name=name, detail=detail, created_at=datetime.now(timezone.utc))


class EventLog:
    """In-memory ring buffer for recent events + optional persistence listeners.

    Listeners are called on every emit() — used by EventStore (SQLite) and
    NotificationService (Telegram) without modifying this class's core logic.
    """

    def __init__(self, maxlen=100):
        self._events = deque(maxlen=maxlen)
        self._lock = Lock()
        self._listeners: List[Callable[[CraneEvent], None]] = []

    def emit(self, name, detail=""):
        event = make_event(name, detail)
        with self._lock:
            self._events.append(event)
        # Notify listeners outside the lock to avoid deadlocks
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                # Never let a listener break the emit pipeline
                import logging
                logging.getLogger("crane.events").error(
                    "Event listener failed: %s", e
                )
        return event

    def add_listener(self, listener: Callable[[CraneEvent], None]):
        """Register a callback invoked on every emit()."""
        self._listeners.append(listener)

    def recent(self, limit=20):
        with self._lock:
            events = list(self._events)[-limit:]
        return [
            {
                **asdict(event),
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
