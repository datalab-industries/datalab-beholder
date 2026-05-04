"""Main daemon loop: scan watched paths, attach matched files to items."""

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
    """Daemon that scans watched paths on layered cadences and attaches
    files whose path matched an ``id_pattern`` to their datalab item.

    Single-threaded by design — `tick()` runs all scan + attach work on
    the calling thread, so SQLite never crosses thread boundaries and
    the loop is safe to drive from a CLI sleep loop or a Tkinter
    `root.after` callback.

    Each watched path runs three independent scan cadences:

    * **hot** — re-stat recently-modified files only (cheap, frequent).
    * **warm** — directory-mtime-aware walk; discovers new files in
      active subtrees, skips per-file stats in cold subtrees.
    * **cold** — full walk; ground-truth reconciliation. Optional
      (`scan.cold_interval = null`) for write-once archives.

    Attaching runs on its own cadence (``sync.metadata_interval``)
    independent of the scan loop: scans accumulate diffs in state, the
    attach pass drains whatever is pending and has an ``item_id``.
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
        self.last_attach_time: float | None = None
        self.pending_count: int = 0
        self.sync_status: str = "idle"  # "idle" | "attaching" | "error"

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
        """Construct one client per configured datalab.

        ``BaseDatalabClient`` reads its API key from the
        ``DATALAB_API_KEY`` env var (or a deployment-prefixed variant)
        during ``__init__``, so we set it from ``DatalabConfig.api_key``
        per-construction. With multiple datalabs this just walks the
        list and re-sets the env var each time — fine because each
        client snapshots the key into its own ``_headers`` dict.
        """
        import os

        clients: dict[str, BeholderClient] = {}
        prev_env = os.environ.get("DATALAB_API_KEY")
        try:
            for d in config.datalabs:
                if d.api_key and d.api_key != "your-api-key-here":
                    os.environ["DATALAB_API_KEY"] = d.api_key
                # Otherwise leave whatever was already in the env so
                # users can keep their key out of the YAML entirely.
                clients[d.name] = BeholderClient(
                    datalab_api_url=d.url,
                    log_level=config.log_level,
                )
        finally:
            if prev_env is None:
                os.environ.pop("DATALAB_API_KEY", None)
            else:
                os.environ["DATALAB_API_KEY"] = prev_env
        return clients

    def _build_daemon_id(self) -> str:
        names = sorted(wp.name for wp in self._config.watched_paths)
        return "-".join(names).lower().replace(" ", "-")

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
        log.info("Attach interval: %ds", self._config.sync.metadata_interval)

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self._handle_signal)

        self._last_attach_mono = (
            time.monotonic() - self._config.sync.metadata_interval
        )  # so attach runs on the first tick
        self._running = True

    def tick(self) -> None:
        """One iteration of the main loop.

        Per watched path: pick the highest-priority overdue scan tier
        (cold > warm > hot) and run it. Then, on its own cadence, attach
        any pending files whose path matched an ``item_id`` regex.
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

        if now_mono - self._last_attach_mono >= self._config.sync.metadata_interval:
            self.sync_status = "attaching"
            try:
                self._attach_matched_files()
                self.last_attach_time = now_wall
                self.sync_status = "idle"
            except Exception:
                log.exception("Error in attach loop")
                self.sync_status = "error"
            self._last_attach_mono = now_mono

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
    # Attach
    # ------------------------------------------------------------------

    def _update_pending_count(self) -> None:
        total = 0
        for wp in self._config.watched_paths:
            total += len(self._state.get_pending_changes(wp.name))
        self.pending_count = total

    def _attach_matched_files(self) -> None:
        """Attach pending files whose path matched an ``item_id`` regex.

        Per watched path:

        1. Pull pending entries from state and keep the ones with
           ``status in {new, modified}`` and ``ids["item_id"]`` set.
        2. Per item id, fetch the item once (creating it if missing
           and ``item_type`` is configured) and re-use that snapshot
           for every file on that item — saves a round-trip per file
           in the common case of many files sharing one item id.
        3. For each attachable file, look up an existing attachment
           with the same basename. If found, upload with
           ``replace_file_id`` to overwrite in place; otherwise upload
           as a new attachment.
        4. Mark successful uploads synced. Failures are left un-synced
           and retried on the next tick.

        File uploads only support local paths today; SSH/Cloud entries
        are skipped with a debug log until those backends grow upload
        paths of their own.
        """
        for wp in self._config.watched_paths:
            if not isinstance(wp, LocalWatchedPath):
                log.debug("Skipping attach for %s: only local paths supported", wp.name)
                continue

            pending = self._state.get_pending_changes(wp.name)
            attachable = [
                e
                for e in pending
                if e.status in ("new", "modified") and e.ids.get("item_id")
            ]
            if not attachable:
                continue

            client = self._client_for(wp)
            log.info("Attaching %d file(s) for %s", len(attachable), wp.name)

            # Per-pass cache: item_id → item dict (or None if the item
            # couldn't be ensured). Avoids re-querying for each file on
            # the same item.
            item_cache: dict[str, dict[str, Any] | None] = {}

            synced: list[str] = []
            for entry in attachable:
                file_path = wp.path / entry.path
                item_id = entry.ids["item_id"]

                if item_id not in item_cache:
                    if wp.item_type:
                        item_cache[item_id] = client.ensure_item(
                            item_id=item_id,
                            item_type=wp.item_type,
                            collection_id=entry.ids.get("collection_id"),
                            group_id=entry.ids.get("group_id"),
                        )
                    else:
                        # No item_type configured → don't create, only
                        # attach if the item already exists.
                        item_cache[item_id] = client.fetch_item(item_id)

                item = item_cache[item_id]
                if item is None:
                    log.warning(
                        "Skipping %s: item %s not found and not creatable",
                        entry.path,
                        item_id,
                    )
                    continue

                replace_id = client.find_existing_file_id(item, file_path.name)
                result = client.attach_file(
                    item_id=item_id,
                    file_path=file_path,
                    replace_file_id=replace_id,
                )
                if result is not None:
                    synced.append(entry.path)
                    log.info(
                        "Attached %s → item %s%s",
                        entry.path,
                        item_id,
                        f" (replaced file {replace_id})" if replace_id else "",
                    )

            if synced:
                self._state.mark_synced(wp.name, synced)
            self._state.remove_deleted(wp.name)
