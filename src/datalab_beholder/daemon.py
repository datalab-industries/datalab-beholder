"""Main daemon loop for metadata sync and file upload."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

from datalab_beholder.client import BeholderClient
from datalab_beholder.config import BeholderConfig
from datalab_beholder.scanner import scan_directory
from datalab_beholder.state import StateStore
from datalab_beholder.watcher import DirectoryWatcher

log = logging.getLogger(__name__)


class BeholderDaemon:
    """Daemon that watches directories and syncs file metadata to datalab.

    Architecture:
    1. Startup: full scan seeds local state, pushes initial snapshot to server.
    2. Watcher: detects file changes in real-time, updates local state.
    3. Push loop: periodically sends accumulated changes to server.
    4. File request loop: polls server for requested files, uploads them.
    """

    def __init__(self, config: BeholderConfig):
        self._config = config
        self._stop_event = threading.Event()
        self._state = StateStore(config.state_db)

        # BaseDatalabClient reads the API key from env vars
        if config.datalab.api_key:
            os.environ["DATALAB_API_KEY"] = config.datalab.api_key
        self._client = BeholderClient(
            datalab_api_url=config.datalab.url,
            log_level=config.log_level,
        )
        self._daemon_id = self._build_daemon_id()
        self._threads: list[threading.Thread] = []
        self._watcher: DirectoryWatcher | None = None

    def _build_daemon_id(self) -> str:
        """Build a daemon ID from watched path names."""
        names = sorted(wp.name for wp in self._config.watched_paths)
        return "-".join(names).lower().replace(" ", "-")

    def start(self) -> None:
        """Start the daemon loops and block until shutdown."""
        log.info("Starting beholder daemon (id=%s)", self._daemon_id)
        log.info(
            "Watching %d path(s): %s",
            len(self._config.watched_paths),
            ", ".join(f"{wp.name} ({wp.path})" for wp in self._config.watched_paths),
        )
        log.info(
            "Sync intervals: metadata=%ds, file_requests=%ds",
            self._config.sync.metadata_interval,
            self._config.sync.file_request_poll,
        )

        # Register signal handlers for graceful shutdown (only works in main thread)
        import threading as _threading

        if _threading.current_thread() is _threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self._handle_signal)

        # 1. Initial full scan + push
        self._initial_scan()

        # 2. Start filesystem watcher
        self._watcher = DirectoryWatcher(self._state)
        for wp in self._config.watched_paths:
            if wp.path.exists():
                self._watcher.watch(
                    path=wp.path,
                    name=wp.name,
                    include_patterns=wp.include_patterns,
                    exclude_patterns=wp.exclude_patterns,
                )
        self._watcher.start()

        # 3. Start periodic background loops
        push_thread = threading.Thread(
            target=self._loop,
            args=(self._push_pending_changes, self._config.sync.metadata_interval),
            name="metadata-push",
            daemon=True,
        )
        file_request_thread = threading.Thread(
            target=self._loop,
            args=(self._poll_file_requests, self._config.sync.file_request_poll),
            name="file-request-poll",
            daemon=True,
        )

        self._threads = [push_thread, file_request_thread]
        for t in self._threads:
            t.start()

        # Block main thread until stop signal
        log.info("Daemon running. Press Ctrl+C to stop.")
        self._stop_event.wait()

        if self._watcher is not None:
            self._watcher.stop()
        log.info("Daemon stopped.")

    def stop(self) -> None:
        """Signal the daemon to stop."""
        log.info("Stopping daemon...")
        self._stop_event.set()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("Received signal %d, shutting down...", signum)
        self.stop()

    def _loop(self, func: callable, interval: float) -> None:
        """Run a function periodically until stop is signaled."""
        while not self._stop_event.is_set():
            self._stop_event.wait(interval)
            if self._stop_event.is_set():
                break
            try:
                func()
            except Exception:
                log.exception("Error in %s loop", func.__name__)

    def _initial_scan(self) -> None:
        """Run a full scan of all watched paths and push the initial snapshot."""
        for wp in self._config.watched_paths:
            try:
                self._scan_and_push(wp)
            except Exception:
                log.exception("Error during initial scan of %s", wp.name)

    def _scan_and_push(self, wp: Any) -> None:
        """Scan a single watched path and push changes to the server."""
        if not wp.path.exists():
            log.warning("Watched path does not exist: %s", wp.path)
            return

        log.debug("Scanning %s (%s)", wp.name, wp.path)
        scan_result = scan_directory(
            root=wp.path,
            name=wp.name,
            include_patterns=wp.include_patterns,
            exclude_patterns=wp.exclude_patterns,
            max_depth=wp.max_depth,
        )
        log.info(
            "Scanned %s: %d files, %d dirs in %dms",
            wp.name,
            scan_result.total_files,
            scan_result.total_directories,
            scan_result.scan_duration_ms,
        )

        diff = self._state.update_from_scan(scan_result)
        if not diff.has_changes:
            log.debug("No changes for %s", wp.name)
            return

        self._push_diff(wp.name, diff)

    def _push_pending_changes(self) -> None:
        """Read accumulated changes from state and push to the server."""
        for wp in self._config.watched_paths:
            try:
                pending = self._state.get_pending_changes(wp.name)
                if not pending:
                    log.debug("No pending changes for %s", wp.name)
                    continue

                log.info("Pushing %d pending entries for %s", len(pending), wp.name)
                entries = [
                    {
                        "path": e.path,
                        "size": e.size,
                        "modified": e.modified,
                        "is_directory": e.is_directory,
                        "status": e.status,
                    }
                    for e in pending
                ]

                success = self._client.push_metadata(
                    daemon_id=self._daemon_id,
                    entries=entries,
                    snapshot_type="diff",
                )

                self._state.log_sync(
                    watched_path_name=wp.name,
                    snapshot_type="diff",
                    entries_sent=len(entries),
                    success=success,
                )

                if success:
                    self._state.mark_synced(wp.name, [e.path for e in pending])
                    self._state.remove_deleted(wp.name)
                    log.info("Pushed %d entries for %s", len(entries), wp.name)
                else:
                    log.warning("Failed to push changes for %s, will retry", wp.name)
            except Exception:
                log.exception("Error pushing changes for %s", wp.name)

    def _push_diff(self, watched_path_name: str, diff: Any) -> None:
        """Push a diff result to the server."""
        entries = [
            {
                "path": e.path,
                "size": e.size,
                "modified": e.modified,
                "is_directory": e.is_directory,
                "status": e.status,
            }
            for e in diff.all_changes
        ]

        log.info(
            "Changes for %s: %d new, %d modified, %d deleted",
            watched_path_name,
            len(diff.new),
            len(diff.modified),
            len(diff.deleted),
        )

        success = self._client.push_metadata(
            daemon_id=self._daemon_id,
            entries=entries,
            snapshot_type=diff.snapshot_type,
        )

        self._state.log_sync(
            watched_path_name=watched_path_name,
            snapshot_type=diff.snapshot_type,
            entries_sent=len(entries),
            success=success,
        )

        if success:
            synced_paths = [e.path for e in diff.all_changes]
            self._state.mark_synced(watched_path_name, synced_paths)
            self._state.remove_deleted(watched_path_name)
            log.info("Synced %d entries for %s", len(entries), watched_path_name)
        else:
            log.warning("Failed to sync metadata for %s, will retry", watched_path_name)

    def _poll_file_requests(self) -> None:
        """Check for pending file requests and upload them."""
        requests = self._client.poll_file_requests(self._daemon_id)
        if not requests:
            return

        log.info("Received %d file request(s)", len(requests))

        for req in requests:
            try:
                self._handle_file_request(req)
            except Exception:
                log.exception("Error handling file request %s", req.request_id)

    def _handle_file_request(self, req: Any) -> None:
        """Find and upload a requested file."""
        # Find the file across all watched paths
        for wp in self._config.watched_paths:
            file_path = wp.path / req.path
            if file_path.exists() and file_path.is_file():
                log.info("Uploading %s (request %s)", req.path, req.request_id)
                stat = file_path.stat()
                success = self._client.upload_file(
                    request_id=req.request_id,
                    file_path=file_path,
                    metadata={
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    },
                )
                if success:
                    log.info("Uploaded %s successfully", req.path)
                else:
                    log.warning("Failed to upload %s", req.path)
                return

        log.warning("Requested file not found: %s", req.path)
