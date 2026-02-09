"""Real-time filesystem event monitoring using watchdog."""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

if TYPE_CHECKING:
    from datalab_beholder.state import StateStore

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5.0


class BeholderEventHandler(FileSystemEventHandler):
    """Handles filesystem events with filtering and debouncing.

    Events are collected into batches and flushed to the state store
    after a debounce window to avoid thrashing during bulk operations.
    """

    def __init__(
        self,
        root: Path,
        watched_path_name: str,
        state: StateStore,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ):
        super().__init__()
        self._root = Path(root).resolve()
        self._watched_path_name = watched_path_name
        self._state = state
        self._include_patterns = include_patterns or ["*"]
        self._exclude_patterns = exclude_patterns or []

        self._pending_events: dict[str, FileSystemEvent] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _matches_filters(self, path: str) -> bool:
        """Check if a path passes include/exclude filters."""
        name = os.path.basename(path)
        if self._exclude_patterns and any(
            fnmatch.fnmatch(name, p) for p in self._exclude_patterns
        ):
            return False
        return any(fnmatch.fnmatch(name, p) for p in self._include_patterns)

    def _rel_path(self, abs_path: str) -> str:
        """Convert absolute path to relative, normalized with forward slashes."""
        rel = os.path.relpath(abs_path, self._root)
        return rel.replace(os.sep, "/")

    def on_created(self, event: FileCreatedEvent | DirCreatedEvent) -> None:
        if not self._matches_filters(event.src_path):
            return
        self._enqueue(event)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        if not self._matches_filters(event.src_path):
            return
        self._enqueue(event)

    def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
        if not self._matches_filters(event.src_path):
            return
        self._enqueue(event)

    def on_moved(self, event: FileMovedEvent | DirMovedEvent) -> None:
        # Treat moves as a delete + create
        if self._matches_filters(event.src_path):
            delete_cls = DirDeletedEvent if event.is_directory else FileDeletedEvent
            self._enqueue(delete_cls(event.src_path))
        if self._matches_filters(event.dest_path):
            create_cls = DirCreatedEvent if event.is_directory else FileCreatedEvent
            self._enqueue(create_cls(event.dest_path))

    def _enqueue(self, event: FileSystemEvent) -> None:
        """Add an event to the pending batch and reset the debounce timer."""
        rel = self._rel_path(event.src_path)
        with self._lock:
            self._pending_events[rel] = event
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        """Process all pending events and update state incrementally."""
        with self._lock:
            events = dict(self._pending_events)
            self._pending_events.clear()
            self._timer = None

        if not events:
            return

        log.info(
            "Processing %d batched filesystem events for %s",
            len(events),
            self._watched_path_name,
        )

        from datalab_beholder.scanner import FileEntry

        upserts: list[FileEntry] = []
        deletes: list[str] = []

        for rel_path, event in events.items():
            abs_path = self._root / rel_path
            if isinstance(event, (FileDeletedEvent, DirDeletedEvent)):
                deletes.append(rel_path)
                continue

            try:
                stat = abs_path.stat()
                upserts.append(
                    FileEntry(
                        path=rel_path,
                        size=stat.st_size if abs_path.is_file() else 0,
                        modified=stat.st_mtime,
                        is_directory=abs_path.is_dir(),
                    )
                )
            except OSError as e:
                log.debug("Cannot stat %s during flush: %s", abs_path, e)

        if upserts:
            self._state.upsert_entries(self._watched_path_name, upserts)
        if deletes:
            self._state.mark_entries_deleted(self._watched_path_name, deletes)

        log.info(
            "Watcher update for %s: %d upserted, %d deleted",
            self._watched_path_name,
            len(upserts),
            len(deletes),
        )


class DirectoryWatcher:
    """Manages watchdog observers for multiple watched paths."""

    def __init__(self, state: StateStore):
        self._state = state
        self._observer = Observer()
        self._handlers: list[BeholderEventHandler] = []

    def watch(
        self,
        path: Path,
        name: str,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        """Add a directory to watch.

        Args:
            path: Directory to watch.
            name: Human-readable name for this watched path.
            include_patterns: Glob patterns for files to include.
            exclude_patterns: Glob patterns for files to exclude.
        """
        handler = BeholderEventHandler(
            root=path,
            watched_path_name=name,
            state=self._state,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        self._handlers.append(handler)
        self._observer.schedule(handler, str(path), recursive=True)
        log.info("Watching directory: %s (%s)", name, path)

    def start(self) -> None:
        """Start the filesystem observer."""
        self._observer.start()
        log.info("Filesystem watcher started")

    def stop(self) -> None:
        """Stop the filesystem observer."""
        self._observer.stop()
        self._observer.join(timeout=5)
        log.info("Filesystem watcher stopped")
