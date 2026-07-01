"""
SQLite-backed persistent event store for Boras.

Survives server restarts — events are written to disk immediately.
Used for: audit logs, debugging, reporting, "what happened while I was away".

Schema:
    events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL  -- ISO 8601 UTC
    )

Indexes on created_at and name for fast queries.
"""
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger("crane.event_store")


class EventStore:
    """Thread-safe SQLite event store. One writer, many readers.

    Uses a single connection guarded by a Lock — SQLite handles concurrent
    reads natively but we serialize writes to avoid "database is locked" errors.
    """

    def __init__(self, db_path: str = "events.db"):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Create table + indexes if not exists. Called once on startup."""
        try:
            with self._lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        detail TEXT,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_name ON events(name)"
                )
                conn.commit()
                conn.close()
            logger.info("EventStore initialized at %s", self._db_path)
        except sqlite3.Error as e:
            logger.error("EventStore init failed: %s", e)

    def save(self, name: str, detail: str = "", created_at: Optional[datetime] = None):
        """Insert a single event. Non-blocking on failure — never break pipeline.

        Filters out high-frequency events that would flood the database:
        frame_received fires ~20x/second and is useless for audit logs.
        """
        # Don't persist high-frequency noise events
        if name in ("frame_received",):
            return
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        ts = created_at.isoformat()
        try:
            with self._lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                conn.execute(
                    "INSERT INTO events (name, detail, created_at) VALUES (?, ?, ?)",
                    (name, detail, ts),
                )
                conn.commit()
                conn.close()
        except sqlite3.Error as e:
            logger.warning("EventStore save failed: %s", e)

    def get_recent(self, limit: int = 100, name_filter: Optional[str] = None) -> List[dict]:
        """Return last N events, newest first. Optional filter by event name."""
        try:
            with self._lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                if name_filter:
                    rows = conn.execute(
                        "SELECT id, name, detail, created_at FROM events "
                        "WHERE name = ? ORDER BY id DESC LIMIT ?",
                        (name_filter, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, name, detail, created_at FROM events "
                        "ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                conn.close()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.warning("EventStore query failed: %s", e)
            return []

    def count(self, name_filter: Optional[str] = None) -> int:
        """Total event count, optional filter by name."""
        try:
            with self._lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                if name_filter:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE name = ?", (name_filter,)
                    ).fetchone()
                else:
                    row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
                conn.close()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.warning("EventStore count failed: %s", e)
            return 0

    def clear(self) -> int:
        """Delete all events. Returns deleted count. Use with caution."""
        try:
            with self._lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                count_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                conn.execute("DELETE FROM events")
                conn.commit()
                conn.close()
            return count_before
        except sqlite3.Error as e:
            logger.warning("EventStore clear failed: %s", e)
            return 0
