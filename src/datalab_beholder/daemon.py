"""Main daemon loop: scan watched paths, attach matched files to items."""

from __future__ import annotations

import fnmatch
import logging
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

from datalab_beholder.client import BeholderClient
from datalab_beholder.config import BeholderConfig, LocalWatchedPath
from datalab_beholder.state import StateStore

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0


def _match_block_type(filename: str, block_patterns: dict[str, str]) -> str | None:
    """Return the block type for the first pattern in ``block_patterns``
    that matches ``filename``, or ``None`` if nothing matches."""
    for pattern, block_type in block_patterns.items():
        if fnmatch.fnmatch(filename, pattern):
            return block_type
    return None


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
    def _build_client(datalab: Any, log_level: str) -> BeholderClient:
        """Construct a client for one configured datalab.

        ``BaseDatalabClient`` reads its API key from the
        ``DATALAB_API_KEY`` env var (or a deployment-prefixed variant)
        during ``__init__``, so we set it from ``DatalabConfig.api_key``
        for the duration of construction — fine because the client
        snapshots the key into its own ``_headers`` dict.
        """
        import os

        prev_env = os.environ.get("DATALAB_API_KEY")
        try:
            if datalab.api_key and datalab.api_key != "your-api-key-here":
                os.environ["DATALAB_API_KEY"] = datalab.api_key
            # Otherwise leave whatever was already in the env so users
            # can keep their key out of the YAML entirely.
            return BeholderClient(
                datalab_api_url=datalab.url,
                log_level=log_level,
            )
        finally:
            if prev_env is None:
                os.environ.pop("DATALAB_API_KEY", None)
            else:
                os.environ["DATALAB_API_KEY"] = prev_env

    @staticmethod
    def _build_clients(config: BeholderConfig) -> dict[str, BeholderClient]:
        """Construct one client per configured datalab."""
        return {
            d.name: BeholderDaemon._build_client(d, config.log_level)
            for d in config.datalabs
        }

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
        4. If the file matched a ``block_patterns`` entry and the item
           doesn't already have a block of that type wired to this
           exact file (matched via the block's own ``file_id``, since
           the file's own record isn't reliably kept in sync), create
           one wired to the newly-uploaded file.
        5. Mark successful uploads synced. A ``304`` reply (the server
           already holds identical content, matched by hash) counts as
           success, so an unchanged-but-retouched file isn't re-uploaded
           forever. Failures are left un-synced and retried next tick.

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
                    if result.get("not_modified"):
                        # Server already holds identical content (it
                        # compares hashes). Marking it synced anyway is
                        # the point: otherwise the entry stays pending
                        # and we re-upload it on every tick.
                        log.info(
                            "%s already up to date on item %s",
                            entry.path,
                            item_id,
                        )
                    else:
                        log.info(
                            "Attached %s -> item %s%s",
                            entry.path,
                            item_id,
                            f" (replaced file {replace_id})" if replace_id else "",
                        )

                    block_type = _match_block_type(file_path.name, wp.block_patterns)
                    file_id = result.get("file_id")
                    if (
                        block_type
                        and file_id
                        and client.find_block_for_file(item, block_type, file_id)
                        is None
                    ):
                        block = client.create_block(
                            item_id=item_id,
                            block_type=block_type,
                            file_id=file_id,
                        )
                        if block is not None:
                            log.info(
                                "Created %s block on item %s for %s",
                                block_type,
                                item_id,
                                entry.path,
                            )

            if synced:
                self._state.mark_synced(wp.name, synced)
            self._state.remove_deleted(wp.name)


# ----------------------------------------------------------------------
# Dry run
# ----------------------------------------------------------------------


@dataclass
class PlannedAction:
    """One thing the daemon would have done, as reported by `dry_run`."""

    watched_path: str
    action: str  # "create_item" | "upload" | "replace" | "create_block" | "skip"
    item_id: str
    path: str = ""
    detail: str = ""


def dry_run(
    config: BeholderConfig,
    clients: dict[str, BeholderClient] | None = None,
) -> list[PlannedAction]:
    """Simulate one full scan + attach pass without changing anything.

    Walks each local watched path with a full (cold-style) scan,
    classifies the results against the state DB opened read-only (a
    missing DB classifies everything as new), then probes the datalab
    server with GET-only requests to report exactly what the daemon
    would do on its next attach pass. Neither the state DB nor the
    server is written to.

    A datalab that can't be reached is not fatal: the scan and local
    classification still run, pending files are reported with an
    ``attach_unknown`` action, and the connection failure is logged as
    an error.

    ``clients`` may be injected (used by tests); otherwise one client
    per configured datalab is constructed as in the real daemon. A
    datalab name missing from an injected mapping is treated as
    unreachable.
    """
    from datalab_beholder.scanner import scan_directory
    from datalab_beholder.state import DiffEntry, DiffResult

    if clients is None:
        clients = {}
        for d in config.datalabs:
            try:
                clients[d.name] = BeholderDaemon._build_client(d, config.log_level)
            except Exception as e:
                log.error(
                    "Cannot connect to datalab %r at %s: %s — scans will "
                    "still run, but server-side state for its watched "
                    "paths is unknown",
                    d.name,
                    d.url,
                    e,
                )

    state: StateStore | None = None
    if config.state_db.exists():
        state = StateStore(config.state_db, read_only=True)
    else:
        log.info(
            "No state DB at %s — treating every matched file as new",
            config.state_db,
        )

    actions: list[PlannedAction] = []
    try:
        for wp in config.watched_paths:
            if not isinstance(wp, LocalWatchedPath):
                log.info(
                    "Skipping %s: only local paths supported (kind=%s)",
                    wp.name,
                    wp.kind,
                )
                continue

            # A scan that cannot run at all is fatal: a dry run against a
            # missing/mistyped path would otherwise report "0 files
            # matched", which reads as "nothing to do" instead of
            # "misconfigured".
            if not wp.path.is_dir():
                raise FileNotFoundError(
                    f"watched path {wp.name!r} cannot be scanned: {wp.path} "
                    "does not exist or is not a directory"
                )

            log.info("Dry-run scan of %s (%s)", wp.name, wp.path)
            scan = scan_directory(
                wp.path,
                name=wp.name,
                include_patterns=wp.include_patterns,
                exclude_patterns=wp.exclude_patterns,
                id_patterns=wp.id_patterns,
                item_id_template=wp.item_id_template,
                collection_id_template=wp.collection_id_template,
                max_depth=wp.max_depth,
            )
            log.info(
                "%s: %d file(s) matched patterns in %d ms "
                "(run with --log-level debug to see every skipped file)",
                wp.name,
                scan.total_files,
                scan.scan_duration_ms,
            )

            if state is not None:
                diff = state.classify_scan(scan)
            else:
                diff = DiffResult(watched_path_name=wp.name)
                diff.new = [
                    DiffEntry(
                        path=e.path,
                        size=e.size,
                        modified=e.modified,
                        is_directory=False,
                        status="new",
                        ids=dict(e.ids),
                    )
                    for e in scan.entries
                ]

            log.info(
                "%s vs local state: %d new, %d modified, %d unchanged, %d deleted",
                wp.name,
                len(diff.new),
                len(diff.modified),
                diff.unchanged,
                len(diff.deleted),
            )
            if diff.deleted:
                log.info(
                    "%s: %d deleted file(s) would be pruned from local state "
                    "(no server change)",
                    wp.name,
                    len(diff.deleted),
                )

            pending = diff.new + diff.modified
            attachable = [e for e in pending if e.ids.get("item_id")]
            if len(pending) != len(attachable):
                log.info(
                    "%s: %d pending file(s) have no item_id and would not be attached",
                    wp.name,
                    len(pending) - len(attachable),
                )
            if not attachable:
                continue

            # Config validation guarantees wp.datalab resolves to a real
            # datalab name (see BeholderConfig.validate_datalab_refs);
            # it can still be absent from `clients` if unreachable.
            assert wp.datalab is not None
            client = clients.get(wp.datalab)
            if client is None:
                log.error(
                    "%s: datalab %r is unreachable — %d pending file(s) "
                    "would be attached, but whether items/files/blocks "
                    "would be created or replaced is unknown",
                    wp.name,
                    wp.datalab,
                    len(attachable),
                )
                for entry in attachable:
                    log.info(
                        "would attach %s to item %s (server state unknown)",
                        entry.path,
                        entry.ids["item_id"],
                    )
                    actions.append(
                        PlannedAction(
                            wp.name,
                            "attach_unknown",
                            entry.ids["item_id"],
                            entry.path,
                            "server unreachable",
                        )
                    )
                continue

            item_cache: dict[str, dict[str, Any] | None] = {}
            would_create: set[str] = set()

            for entry in attachable:
                item_id = entry.ids["item_id"]
                filename = entry.path.rsplit("/", 1)[-1]

                if item_id not in item_cache:
                    item_cache[item_id] = client.fetch_item(item_id)
                item = item_cache[item_id]

                replace_id: str | None = None
                if item is None:
                    if not wp.item_type:
                        log.warning(
                            "would skip %s: item %s does not exist and no "
                            "item_type is configured to create it",
                            entry.path,
                            item_id,
                        )
                        actions.append(
                            PlannedAction(
                                wp.name,
                                "skip",
                                item_id,
                                entry.path,
                                "item missing, not creatable",
                            )
                        )
                        continue
                    if item_id not in would_create:
                        would_create.add(item_id)
                        log.info(
                            "would create item %s (type=%s, collection=%s, group=%s)",
                            item_id,
                            wp.item_type,
                            entry.ids.get("collection_id"),
                            entry.ids.get("group_id"),
                        )
                        actions.append(
                            PlannedAction(
                                wp.name,
                                "create_item",
                                item_id,
                                detail=f"type={wp.item_type}",
                            )
                        )
                else:
                    replace_id = client.find_existing_file_id(item, filename)

                if replace_id:
                    log.info(
                        "would replace file %s on item %s with %s",
                        replace_id,
                        item_id,
                        entry.path,
                    )
                    actions.append(
                        PlannedAction(
                            wp.name,
                            "replace",
                            item_id,
                            entry.path,
                            f"file_id={replace_id}",
                        )
                    )
                else:
                    log.info(
                        "would upload %s as a new file on item %s",
                        entry.path,
                        item_id,
                    )
                    actions.append(
                        PlannedAction(wp.name, "upload", item_id, entry.path)
                    )

                block_type = _match_block_type(filename, wp.block_patterns)
                if block_type:
                    # A block can only already exist for a file that is
                    # already attached (i.e. the replace case); a new
                    # upload gets a fresh file_id, so the daemon would
                    # always create a block for it.
                    has_block = (
                        item is not None
                        and replace_id is not None
                        and client.find_block_for_file(item, block_type, replace_id)
                        is not None
                    )
                    if has_block:
                        log.debug(
                            "item %s already has a %s block for %s",
                            item_id,
                            block_type,
                            entry.path,
                        )
                    else:
                        log.info(
                            "would create %s block on item %s for %s",
                            block_type,
                            item_id,
                            entry.path,
                        )
                        actions.append(
                            PlannedAction(
                                wp.name,
                                "create_block",
                                item_id,
                                entry.path,
                                detail=block_type,
                            )
                        )
    finally:
        if state is not None:
            state.close()

    return actions
