"""Tests for the filesystem watcher event handling."""

from __future__ import annotations

import time
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
)

from datalab_beholder.state import StateStore
from datalab_beholder.watcher import BeholderEventHandler, DirectoryWatcher


class TestBeholderEventHandler:
    """Test that filesystem events flow through the handler into the state store."""

    def _make_handler(
        self, root: Path, state: StateStore, name: str = "test"
    ) -> BeholderEventHandler:
        handler = BeholderEventHandler(
            root=root,
            watched_path_name=name,
            state=state,
        )
        return handler

    def test_create_event_upserts(self, tmp_path: Path) -> None:
        """Created files should be upserted into state after flush."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "new.csv").write_text("hello")

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        handler.on_created(FileCreatedEvent(str(data / "new.csv")))
        handler.flush()

        pending = state.get_pending_changes("test")
        assert {e.path for e in pending} == {"new.csv"}
        assert pending[0].status == "new"
        state.close()

    def test_modify_event_upserts(self, tmp_path: Path) -> None:
        """Modified files should be upserted into state after flush."""
        data = tmp_path / "data"
        data.mkdir()
        f = data / "existing.csv"
        f.write_text("original")

        state = StateStore(tmp_path / "test.db")
        # Seed the file into state first
        from datalab_beholder.scanner import FileEntry

        state.upsert_entries(
            "test",
            [
                FileEntry(
                    path="existing.csv", size=8, modified=1000.0, is_directory=False
                ),
            ],
        )

        # Now modify
        f.write_text("modified content that is longer")
        handler = self._make_handler(data, state)
        handler.on_modified(FileModifiedEvent(str(f)))
        handler.flush()

        pending = state.get_pending_changes("test")
        entry = next(e for e in pending if e.path == "existing.csv")
        assert entry.status == "modified"
        state.close()

    def test_delete_event_marks_deleted(self, tmp_path: Path) -> None:
        """Deleted files should be marked deleted in state after flush."""
        data = tmp_path / "data"
        data.mkdir()

        state = StateStore(tmp_path / "test.db")
        from datalab_beholder.scanner import FileEntry

        state.upsert_entries(
            "test",
            [
                FileEntry(
                    path="gone.csv", size=10, modified=1000.0, is_directory=False
                ),
            ],
        )

        handler = self._make_handler(data, state)
        handler.on_deleted(FileDeletedEvent(str(data / "gone.csv")))
        handler.flush()

        pending = state.get_pending_changes("test")
        entry = next(e for e in pending if e.path == "gone.csv")
        assert entry.status == "deleted"
        state.close()

    def test_exclude_pattern_filters(self, tmp_path: Path) -> None:
        """Events for excluded patterns should be ignored."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "file.tmp").write_text("temp")

        state = StateStore(tmp_path / "test.db")
        handler = BeholderEventHandler(
            root=data,
            watched_path_name="test",
            state=state,
            exclude_patterns=["*.tmp"],
        )

        handler.on_created(FileCreatedEvent(str(data / "file.tmp")))
        handler.flush()

        pending = state.get_pending_changes("test")
        assert len(pending) == 0
        state.close()

    def test_include_pattern_filters(self, tmp_path: Path) -> None:
        """Only events matching include patterns should be processed."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "file.csv").write_text("data")
        (data / "file.txt").write_text("text")

        state = StateStore(tmp_path / "test.db")
        handler = BeholderEventHandler(
            root=data,
            watched_path_name="test",
            state=state,
            include_patterns=["*.csv"],
        )

        handler.on_created(FileCreatedEvent(str(data / "file.csv")))
        handler.on_created(FileCreatedEvent(str(data / "file.txt")))
        handler.flush()

        pending = state.get_pending_changes("test")
        assert {e.path for e in pending} == {"file.csv"}
        state.close()

    def test_debounce_batches_events(self, tmp_path: Path) -> None:
        """Multiple rapid events for the same path should be coalesced."""
        data = tmp_path / "data"
        data.mkdir()
        f = data / "file.csv"
        f.write_text("final content")

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        # Simulate rapid create -> modify -> modify
        handler._enqueue(FileCreatedEvent(str(f)))
        handler._enqueue(FileModifiedEvent(str(f)))
        handler._enqueue(FileModifiedEvent(str(f)))

        # Only one entry should be pending for this path
        assert len(handler._pending_events) == 1

        handler.flush()

        pending = state.get_pending_changes("test")
        assert len(pending) == 1
        state.close()

    def test_flush_if_ready_respects_debounce(self, tmp_path: Path) -> None:
        """flush_if_ready should not flush if debounce window hasn't elapsed."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "a.csv").write_text("aaa")

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        handler._enqueue(FileCreatedEvent(str(data / "a.csv")))

        # Immediately calling flush_if_ready — debounce hasn't elapsed
        handler.flush_if_ready()

        pending = state.get_pending_changes("test")
        assert len(pending) == 0  # not flushed yet
        assert len(handler._pending_events) == 1  # still queued
        state.close()

    def test_flush_if_ready_after_debounce(self, tmp_path: Path, monkeypatch) -> None:
        """flush_if_ready should flush once debounce window elapses."""
        import datalab_beholder.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "DEBOUNCE_SECONDS", 0.05)

        data = tmp_path / "data"
        data.mkdir()
        (data / "a.csv").write_text("aaa")

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        handler._enqueue(FileCreatedEvent(str(data / "a.csv")))
        time.sleep(0.1)

        handler.flush_if_ready()

        pending = state.get_pending_changes("test")
        assert {e.path for e in pending} == {"a.csv"}
        state.close()

    def test_max_flush_interval(self, tmp_path: Path, monkeypatch) -> None:
        """Max-wait should force flush even if debounce keeps resetting."""
        import datalab_beholder.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "MAX_FLUSH_INTERVAL", 0.05)
        monkeypatch.setattr(watcher_mod, "DEBOUNCE_SECONDS", 60.0)  # won't trigger

        data = tmp_path / "data"
        data.mkdir()
        (data / "a.csv").write_text("aaa")

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        handler._enqueue(FileCreatedEvent(str(data / "a.csv")))
        time.sleep(0.1)

        # Debounce is 60s so it wouldn't trigger, but max-wait has elapsed
        handler.flush_if_ready()

        pending = state.get_pending_changes("test")
        assert {e.path for e in pending} == {"a.csv"}
        state.close()

    def test_dir_created_event(self, tmp_path: Path) -> None:
        """Directory creation events should be upserted with is_directory=True."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "subdir").mkdir()

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        handler.on_created(DirCreatedEvent(str(data / "subdir")))
        handler.flush()

        pending = state.get_pending_changes("test")
        assert len(pending) == 1
        assert pending[0].path == "subdir"
        state.close()

    def test_stat_failure_skips_entry(self, tmp_path: Path) -> None:
        """If a file disappears between event and flush, it should be skipped."""
        data = tmp_path / "data"
        data.mkdir()
        f = data / "ephemeral.csv"
        f.write_text("brief")

        state = StateStore(tmp_path / "test.db")
        handler = self._make_handler(data, state)

        handler.on_created(FileCreatedEvent(str(f)))
        # Delete before flush
        f.unlink()
        handler.flush()

        pending = state.get_pending_changes("test")
        assert len(pending) == 0
        state.close()


class TestDirectoryWatcher:
    def test_watch_and_start_stop(self, tmp_path: Path) -> None:
        """Watcher should start and stop without error."""
        data = tmp_path / "data"
        data.mkdir()

        state = StateStore(tmp_path / "test.db")
        watcher = DirectoryWatcher(state)
        watcher.watch(data, name="test")
        watcher.start()

        # Give it a moment
        time.sleep(0.1)

        watcher.stop()
        state.close()

    def test_flush_if_ready_delegates(self, tmp_path: Path, monkeypatch) -> None:
        """DirectoryWatcher.flush_if_ready should check all handlers."""
        import datalab_beholder.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "DEBOUNCE_SECONDS", 0.05)

        data = tmp_path / "data"
        data.mkdir()
        (data / "file.csv").write_text("data")

        state = StateStore(tmp_path / "test.db")
        watcher = DirectoryWatcher(state)
        watcher.watch(data, name="test")

        # Enqueue an event directly on the handler
        watcher._handlers[0]._enqueue(FileCreatedEvent(str(data / "file.csv")))
        time.sleep(0.1)

        watcher.flush_if_ready()

        pending = state.get_pending_changes("test")
        assert {e.path for e in pending} == {"file.csv"}
        state.close()
