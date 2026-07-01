"""Тесты для EventStore (SQLite persistence)."""
import os
import tempfile
import time

import pytest

from core.event_store import EventStore


@pytest.fixture
def store():
    """Fresh EventStore with temp DB for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        s = EventStore(db_path=db_path)
        yield s
    finally:
        os.unlink(db_path)


class TestEventStoreBasic:
    def test_save_and_get_recent(self, store):
        store.save("test_event", "detail1")
        events = store.get_recent(limit=10)
        assert len(events) == 1
        assert events[0]["name"] == "test_event"
        assert events[0]["detail"] == "detail1"

    def test_get_recent_returns_newest_first(self, store):
        store.save("event_a", "first")
        time.sleep(0.01)
        store.save("event_b", "second")
        events = store.get_recent(limit=10)
        assert len(events) == 2
        assert events[0]["name"] == "event_b"  # newest first
        assert events[1]["name"] == "event_a"

    def test_count_empty(self, store):
        assert store.count() == 0

    def test_count_after_saves(self, store):
        store.save("a", "")
        store.save("b", "")
        store.save("a", "")
        assert store.count() == 3
        assert store.count(name_filter="a") == 2
        assert store.count(name_filter="b") == 1

    def test_limit_respected(self, store):
        for i in range(50):
            store.save(f"event_{i}", "")
        events = store.get_recent(limit=10)
        assert len(events) == 10


class TestEventStoreFiltering:
    def test_name_filter(self, store):
        store.save("target_detected", "person")
        store.save("error", "boom")
        store.save("target_detected", "person2")
        filtered = store.get_recent(limit=10, name_filter="target_detected")
        assert len(filtered) == 2
        assert all(e["name"] == "target_detected" for e in filtered)

    def test_empty_filter_returns_all(self, store):
        store.save("a", "")
        store.save("b", "")
        all_events = store.get_recent(limit=10, name_filter=None)
        assert len(all_events) == 2


class TestEventStoreClear:
    def test_clear_removes_all(self, store):
        store.save("a", "")
        store.save("b", "")
        deleted = store.clear()
        assert deleted == 2
        assert store.count() == 0

    def test_clear_empty_returns_zero(self, store):
        deleted = store.clear()
        assert deleted == 0


class TestEventStorePersistence:
    def test_survives_reopen(self):
        """SQLite должен переживать закрытие и повторное открытие —
        это главное свойство для persistence across server restarts."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            s1 = EventStore(db_path=db_path)
            s1.save("persisted_event", "before_restart")
            # Имитируем рестарт сервера — создаём новый EventStore с тем же файлом
            del s1
            s2 = EventStore(db_path=db_path)
            events = s2.get_recent(limit=10)
            assert len(events) == 1
            assert events[0]["name"] == "persisted_event"
            assert events[0]["detail"] == "before_restart"
        finally:
            os.unlink(db_path)


class TestEventStoreListener:
    def test_listener_integration_with_eventlog(self):
        """EventLog.add_listener должен вызывать EventStore.save на каждый emit."""
        from core.events import EventLog
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            log = EventLog()
            store = EventStore(db_path=db_path)
            log.add_listener(lambda ev: store.save(ev.name, ev.detail, ev.created_at))

            log.emit("target_detected", "person at door")
            log.emit("error", "ptz failed")

            assert store.count() == 2
            events = store.get_recent(limit=10)
            assert events[0]["name"] == "error"
            assert events[1]["name"] == "target_detected"
        finally:
            os.unlink(db_path)


class TestEventStoreThreadSafety:
    def test_concurrent_writes(self, store):
        """SQLite с Lock должен безопасно обрабатывать конкурентные записи."""
        import threading
        errors = []

        def writer(start):
            try:
                for i in range(50):
                    store.save(f"thread_event_{start}_{i}", "")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent write errors: {errors}"
        assert store.count() == 250
