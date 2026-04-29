"""Main daemon loop for metadata sync and file upload."""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any

from datalab_beholder.client import BeholderClient
from datalab_beholder.config import BeholderConfig, LocalWatchedPath
from datalab_beholder.state import StateStore

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0


class BeholderDaemon:
    """Daemon that scans watched paths on layered cadences and pushes
    accumulated metadata to one or more datalab instances.

    Single-threaded by design — `tick()` runs all scan + push + poll work
    on the calling thread, so SQLite never crosses thread boundaries and
    the loop is safe to drive from a CLI sleep loop or a Tkinter
    `root.after` callback.

    Each watched path runs three independent scan cadences:

    * **hot** — re-stat recently-modified files only (cheap, frequent).
    * **warm** — directory-mtime-aware walk; discovers new files in
      active subtrees, skips per-file stats in cold subtrees.
    * **cold** — full walk; ground-truth reconciliation. Optional
      (`scan.cold_interval = null`) for write-once archives.

    The push loop (`metadata_interval`) is independent of the scan loop:
    scans accumulate diffs in state; pushes drain whatever's pending.
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

        # Observable status for the GUI
        self.last_scan_time: float | None = None
        self.last_push_time: float | None = None
        self.pending_count: int = 0
        self.sync_status: str = "idle"  # "idle" | "pushing" | "error"

    @property
    def config(self) -> BeholderConfig:
        return self._config

    @property
    def clients(self) -> dict[str, BeholderClient]:
        """Mapping of datalab name → client. Read-only view for callers."""
        return self._clients

    def _client_for(self, wp: Any) -> BeholderClient:
        return self._clients_by_wp[wp.name]

    @staticmethod
    def _build_clients(config: BeholderConfig) -> dict[str, BeholderClient]:
        return {
            d.name: BeholderClient(
                datalab_api_url=d.url,
                log_level=config.log_level,
            )
            for d in config.datalabs
        }

    def _build_daemon_id(self) -> str:
        names = sorted(wp.name for wp in self._config.watched_paths)
        return "-".join(names).lower().replace(" ", "-")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Register signal handlers and initialise loop timers.

        No scanning happens here — the first `tick()` will trigger the
        appropriate scan tier per watched path based on its registry
        timestamps (which start out NULL → "infinitely stale" → run now).
        """
        log.info("Starting beholder daemon (id=%s)", self._daemon_id)
        log.info(
            "Watching %d path(s): %s",
            len(self._config.watched_paths),
            ", ".join(
                f"{wp.name} ({getattr(wp, 'path', wp.kind)})"
                for wp in self._config.watched_paths
            ),
        )
        log.info(
            "Push intervals: metadata=%ds, file_requests=%ds",
            self._config.sync.metadata_interval,
            self._config.sync.file_request_poll,
        )

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self._handle_signal)

        self._last_push_mono = time.monotonic()
        self._last_poll_mono = time.monotonic()
        self._running = True

    def tick(self) -> None:
        """One iteration of the main loop.

        Per watched path: pick the highest-priority overdue scan tier
        (cold > warm > hot) and run it. Then handle the push and
        file-request loops on their independent cadences.
        """
        now_mono = time.monotonic()
        now_wall = time.time()

        for wp in self._config.watched_paths:
            try:
                kind = self._select_scan_tier(wp, now_wall)
            except Exception:
                log.exception("Error selecting scan tier for %s", wp.name)
                continue
            if kind is None:
                continue
            try:
                self._run_scan(wp, kind)
                self.last_scan_time = now_wall
            except NotImplementedError:
                log.debug("Scan kind %s not implemented for %s", kind, wp.name)
            except Exception:
                log.exception("Error running %s scan for %s", kind, wp.name)
                self.sync_status = "error"

        if now_mono - self._last_push_mono >= self._config.sync.metadata_interval:
            self.sync_status = "pushing"
            try:
                self._push_pending_changes()
                self._attach_matched_files()
                self.last_push_time = now_wall
                self.sync_status = "idle"
            except Exception:
                log.exception("Error in push loop")
                self.sync_status = "error"
            self._last_push_mono = now_mono

        if now_mono - self._last_poll_mono >= self._config.sync.file_request_poll:
            try:
                self._poll_file_requests()
            except Exception:
                log.exception("Error in file-request poll loop")
            self._last_poll_mono = now_mono

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
        """Clean up resources. (No background threads to stop in the
        scan-based design — this is here for symmetry with start().)"""
        log.info("Daemon stopped.")

    def stop(self) -> None:
        log.info("Stopping daemon...")
        self._running = False

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("Received signal %d, shutting down...", signum)
        self.stop()

    # ------------------------------------------------------------------
    # Scan dispatch
    # ------------------------------------------------------------------

    def _select_scan_tier(self, wp: Any, now: float) -> str | None:
        """Return the highest-priority overdue scan tier for `wp`, or
        None if nothing is due.

        NULL timestamps are treated as infinitely stale, so each tier
        fires on its first tick after the daemon starts. Cold supersedes
        warm supersedes hot (the higher-tier scan does the work of the
        lower ones, which is reflected in `update_scan_timestamp`).
        """
        ts = self._state.get_scan_timestamps(wp.name)

        cold_interval = wp.scan.cold_interval
        if cold_interval is not None:
            if ts.cold is None or now - ts.cold >= cold_interval:
                return "cold"

        if ts.warm is None or now - ts.warm >= wp.scan.warm_interval:
            return "warm"

        if ts.hot is None or now - ts.hot >= wp.scan.hot_interval:
            return "hot"

        return None

    def _run_scan(self, wp: Any, kind: str) -> None:
        if kind == "cold":
            diff = wp.cold_scan(self._state)
        elif kind == "warm":
            diff = wp.warm_scan(self._state)
        elif kind == "hot":
            diff = wp.hot_scan(self._state)
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown scan kind: {kind}")

        if diff.has_changes:
            log.info(
                "%s scan of %s: %d new, %d modified, %d deleted",
                kind,
                wp.name,
                len(diff.new),
                len(diff.modified),
                len(diff.deleted),
            )
        else:
            log.debug("%s scan of %s: no changes", kind, wp.name)

    # ------------------------------------------------------------------
    # Push / poll
    # ------------------------------------------------------------------

    def _update_pending_count(self) -> None:
        total = 0
        for wp in self._config.watched_paths:
            total += len(self._state.get_pending_changes(wp.name))
        self.pending_count = total

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

            log.info(
                "Received %d file request(s) from %s",
                len(requests),
                datalab_name,
            )
            for req in requests:
                try:
                    self._handle_file_request(req, client)
                except Exception:
                    log.exception("Error handling file request %s", req.request_id)

    def _handle_file_request(self, req: Any, client: BeholderClient) -> None:
        """Find and upload a requested file using the originating client."""
        for wp in self._config.watched_paths:
            # File uploads only work for local paths today; SSH/Cloud will
            # need their own upload paths when those backends land.
            if not isinstance(wp, LocalWatchedPath):
                continue
            file_path = wp.path / req.path
            if file_path.exists() and file_path.is_file():
                log.info("Uploading %s (request %s)", req.path, req.request_id)
                stat = file_path.stat()
                success = client.upload_file(
                    request_id=req.request_id,
                    file_path=file_path,
                    metadata={"size": stat.st_size, "modified": stat.st_mtime},
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
