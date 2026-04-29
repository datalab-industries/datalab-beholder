"""Main daemon loop for metadata sync and file upload."""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any

from datalab_beholder.client import BeholderClient
from datalab_beholder.config import BeholderConfig
from datalab_beholder.scanner import scan_directory
from datalab_beholder.state import StateStore
from datalab_beholder.watcher import DirectoryWatcher

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0


class BeholderDaemon:
    """Daemon that watches directories and syncs file metadata to datalab.

    Runs a single-threaded tick loop on the main thread. The only
    background thread is the watchdog Observer (internal to the library),
    which enqueues events into a lock-protected dict. All state-store
    and network I/O happens on the main thread, avoiding cross-thread
    SQLite access and making the code safe for free-threaded Python.

    Tick loop responsibilities (checked every ~1 s):
    1. Flush watcher events whose debounce/max-wait window has elapsed.
    2. Push accumulated state changes when ``metadata_interval`` elapses.
    3. Poll server for file requests when ``file_request_poll`` elapses.

    The loop body is extracted into :meth:`tick` so that both the CLI
    (``start()``) and the GUI (``root.after``) can drive the same logic.
    """

    def __init__(self, config: BeholderConfig):
        self._config = config
        self._running = False
        self._state = StateStore(config.state_db)
        for wp in config.watched_paths:
            self._state.register_watched_path(wp.name)

        self._clients: dict[str, BeholderClient] = self._build_clients(config)
        # Routing table: each watched path resolves to exactly one client.
        # Cross-field validation in BeholderConfig guarantees wp.datalab is set
        # and references a real datalab name.
        self._clients_by_wp: dict[str, BeholderClient] = {
            wp.name: self._clients[wp.datalab]
            for wp in config.watched_paths
            if wp.datalab
        }

        self._daemon_id = self._build_daemon_id()
        self._watcher: DirectoryWatcher | None = None

        # Observable status for the GUI
        self.last_scan_time: float | None = None
        self.last_push_time: float | None = None
        self.pending_count: int = 0
        self.sync_status: str = "idle"  # "idle" | "pushing" | "error"

    @property
    def config(self) -> BeholderConfig:
        """The daemon's configuration."""
        return self._config

    @property
    def clients(self) -> dict[str, BeholderClient]:
        """Mapping of datalab name → client. Read-only view for callers."""
        return self._clients

    def _client_for(self, wp: Any) -> BeholderClient:
        """Return the BeholderClient that owns ``wp``."""
        return self._clients_by_wp[wp.name]

    @staticmethod
    def _build_clients(config: BeholderConfig) -> dict[str, BeholderClient]:
        """Construct one BeholderClient per configured datalab."""
        return {
            d.name: BeholderClient(
                datalab_api_url=d.url,
                log_level=config.log_level,
            )
            for d in config.datalabs
        }

    def _build_daemon_id(self) -> str:
        """Build a daemon ID from watched path names."""
        names = sorted(wp.name for wp in self._config.watched_paths)
        return "-".join(names).lower().replace(" ", "-")

    def setup(self) -> None:
        """Run initial scan, start the filesystem watcher, and prepare timers.

        Call this once before the first :meth:`tick`. ``start()`` calls it
        automatically for the CLI path.
        """
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
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self._handle_signal)

        # 1. Initial full scan + push
        self._initial_scan()
        self.last_scan_time = time.time()

        # 2. Start filesystem watcher (local paths only; SSH/Cloud will be
        #    handled by the layered scan loop in the next refactor)
        from datalab_beholder.config import LocalWatchedPath

        self._watcher = DirectoryWatcher(self._state)
        for wp in self._config.watched_paths:
            if not isinstance(wp, LocalWatchedPath):
                continue
            if wp.path.exists():
                self._watcher.watch(
                    path=wp.path,
                    name=wp.name,
                    include_patterns=wp.include_patterns,
                    exclude_patterns=wp.exclude_patterns,
                )
        self._watcher.start()

        # 3. Initialise monotonic timers for interval checks
        self._last_push_mono = time.monotonic()
        self._last_poll_mono = time.monotonic()
        self._running = True

        # Update pending count after initial scan
        self._update_pending_count()

    def tick(self) -> None:
        """One iteration of the main loop.

        Flushes watcher events, pushes changes when the metadata interval
        elapses, and polls for file requests when the poll interval elapses.
        Safe to call from ``root.after()`` in Tkinter.
        """
        # Flush watcher events if debounce/max-wait elapsed
        if self._watcher is not None:
            self._watcher.flush_if_ready()

        now = time.monotonic()

        # Push accumulated changes
        if now - self._last_push_mono >= self._config.sync.metadata_interval:
            self.sync_status = "pushing"
            try:
                self._push_pending_changes()
                self._attach_matched_files()
                self.last_push_time = time.time()
                self.sync_status = "idle"
            except Exception:
                log.exception("Error in push loop")
                self.sync_status = "error"
            self._last_push_mono = now

        # Poll for file requests
        if now - self._last_poll_mono >= self._config.sync.file_request_poll:
            try:
                self._poll_file_requests()
            except Exception:
                log.exception("Error in file-request poll loop")
            self._last_poll_mono = now

        self._update_pending_count()

    def start(self) -> None:
        """CLI entry: setup + tick loop. Blocks until ``stop()`` is called."""
        self.setup()

        log.info("Daemon running. Press Ctrl+C to stop.")
        try:
            while self._running:
                time.sleep(TICK_SECONDS)
                self.tick()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop the watcher and clean up resources."""
        if self._watcher is not None:
            self._watcher.stop()
        log.info("Daemon stopped.")

    def stop(self) -> None:
        """Signal the daemon to stop."""
        log.info("Stopping daemon...")
        self._running = False

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("Received signal %d, shutting down...", signum)
        self.stop()

    def _update_pending_count(self) -> None:
        """Refresh the pending-change counter from the state store."""
        total = 0
        for wp in self._config.watched_paths:
            total += len(self._state.get_pending_changes(wp.name))
        self.pending_count = total

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
            id_patterns=wp.id_patterns,
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

        self._push_diff(wp, diff)

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

                success = self._client_for(wp).push_metadata(
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

    def _push_diff(self, wp: Any, diff: Any) -> None:
        """Push a diff result to the server."""
        watched_path_name = wp.name
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

        success = self._client_for(wp).push_metadata(
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
        """Check every configured datalab for pending file requests."""
        for datalab_name, client in self._clients.items():
            try:
                requests = client.poll_file_requests(self._daemon_id)
            except Exception:
                log.exception("Error polling file requests from %s", datalab_name)
                continue
            if not requests:
                continue

            log.info("Received %d file request(s) from %s", len(requests), datalab_name)
            for req in requests:
                try:
                    self._handle_file_request(req, client)
                except Exception:
                    log.exception("Error handling file request %s", req.request_id)

    def _handle_file_request(self, req: Any, client: BeholderClient) -> None:
        """Find and upload a requested file using the originating client."""
        for wp in self._config.watched_paths:
            file_path = wp.path / req.path
            if file_path.exists() and file_path.is_file():
                log.info("Uploading %s (request %s)", req.path, req.request_id)
                stat = file_path.stat()
                success = client.upload_file(
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

    def _attach_matched_files(self) -> None:
        """Attach pending files with an extracted item_id to their datalab item.

        Body intentionally left as a stub for the user to fill in. Iterates
        per watched path, picks the right client via :meth:`_client_for`,
        reads pending entries via ``state.get_pending_changes``, filters for
        ``e.ids.get("item_id")``, performs the upload, and on success calls
        ``state.mark_synced`` for that path. Failures are left un-synced and
        retried on the next tick.
        """
