"""Local SQLite state store for tracking file sync status.

Each watched path gets its own `files__<sanitized_name>` table plus a row
in the shared `watched_paths` registry that tracks the last hot/warm/cold
scan timestamps. The shared `sync_log` table records push attempts across
all paths.

The split keeps queries cheap (smaller indexes, no cross-path WHERE
clauses), lets a path's state be wiped independently, and makes room for
per-path schema divergence later (e.g. extra columns for hosted /
cloud-sync paths).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import time

from datalab_beholder.scanner import FileEntry, ScanResult, WarmScanResult

# Registry + sync log are always present. Per-path file tables are created
# on demand by `register_watched_path`.
REGISTRY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS watched_paths (
    name TEXT PRIMARY KEY,
    table_name TEXT NOT NULL UNIQUE,
    last_hot_scan REAL,
    last_warm_scan REAL,
    last_cold_scan REAL,
    last_max_dir_mtime REAL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watched_path_name TEXT,
    timestamp REAL,
    snapshot_type TEXT,
    entries_sent INTEGER,
    success INTEGER
);
"""

# SQLite doesn't bind table names, so we interpolate after sanitising. The
# sanitised name is stored in the registry as the canonical form.
_SANITISE_RE = re.compile(r"[^A-Za-z0-9_]")


def _in_scope_filter(changed_dirs: list[str]):
    """Build a predicate `path -> bool` that returns True if `path` is
    inside any of the directories in `changed_dirs`.

    Empty string in `changed_dirs` means the watched-path root itself was
    rescanned, which scopes the entire tree.
    """
    if "" in changed_dirs:
        return lambda _path: True
    prefixes = tuple(d.rstrip("/") + "/" for d in changed_dirs if d)
    if not prefixes:
        return lambda _path: False
    return lambda path: path.startswith(prefixes)


def _sanitise_name(name: str) -> str:
    sanitised = _SANITISE_RE.sub("_", name).lower()
    if not sanitised or not sanitised[0].isalpha() and sanitised[0] != "_":
        sanitised = "wp_" + sanitised
    return sanitised


@dataclass
class ScanTimestamps:
    """Last-run timestamps for the three scan tiers, plus the warm-scan
    short-circuit anchor.

    Float values are wall-clock unix timestamps; `None` means "never run".
    Callers comparing against monotonic intervals should treat `None` as
    "infinitely stale" (i.e. run the scan now).
    """

    hot: float | None = None
    warm: float | None = None
    cold: float | None = None
    max_dir_mtime: float | None = None


@dataclass
class DiffEntry:
    """A file entry with change status."""

    path: str
    size: int
    modified: float
    is_directory: bool
    status: str  # "new", "modified", "deleted"
    ids: dict[str, str] = field(default_factory=dict)


@dataclass
class DiffResult:
    """Result of comparing a scan against stored state."""

    watched_path_name: str
    new: list[DiffEntry] = field(default_factory=list)
    modified: list[DiffEntry] = field(default_factory=list)
    deleted: list[DiffEntry] = field(default_factory=list)
    unchanged: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.modified or self.deleted)

    @property
    def all_changes(self) -> list[DiffEntry]:
        return self.new + self.modified + self.deleted

    @property
    def snapshot_type(self) -> str:
        """Return 'full' if this is a first scan, 'diff' otherwise."""
        if self.unchanged == 0 and not self.modified and not self.deleted:
            return "full"
        return "diff"


class UnknownWatchedPathError(KeyError):
    """Raised when a method is called for a watched_path_name that hasn't
    been registered with the store."""


class StateStore:
    """SQLite-backed state store for tracking file metadata and sync status.

    Each watched path is registered up front via `register_watched_path`;
    that creates the per-path file table. All subsequent calls take the
    `watched_path_name` and route to the right table internally.
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(REGISTRY_SCHEMA)
        self._wipe_legacy_files_table()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Registry / migration
    # ------------------------------------------------------------------

    def _wipe_legacy_files_table(self) -> None:
        """Drop the old single `files` table from pre-per-path schema, if
        it exists. The next scan on each watched path will re-seed."""
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        if cursor.fetchone() is not None:
            self._conn.execute("DROP TABLE files")
            self._conn.commit()

    def register_watched_path(self, name: str) -> None:
        """Idempotently register a watched path, creating its file table.

        Resolves the canonical table name via `_sanitise_name` and stores
        it in the registry. Raises if a different name already maps to the
        same sanitised table name (i.e. two registered paths would collide).
        """
        table = _sanitise_name(name)

        existing = self._conn.execute(
            "SELECT name, table_name FROM watched_paths WHERE name = ? OR table_name = ?",
            (name, table),
        ).fetchone()
        if existing is not None:
            if existing["name"] != name:
                raise ValueError(
                    f"watched_path name {name!r} sanitises to table {table!r}, "
                    f"which is already used by registered path "
                    f"{existing['name']!r}; pick a more distinct name."
                )
            # Already registered with the same name; nothing to do.
            return

        self._conn.execute(
            "INSERT INTO watched_paths (name, table_name) VALUES (?, ?)",
            (name, table),
        )
        self._conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS files__{table} (
                path TEXT PRIMARY KEY,
                size INTEGER,
                modified REAL,
                last_seen REAL,
                last_synced REAL,
                status TEXT DEFAULT 'new',
                ids_json TEXT DEFAULT '{{}}'
            );
            CREATE INDEX IF NOT EXISTS idx_{table}_modified
                ON files__{table}(modified);
            CREATE INDEX IF NOT EXISTS idx_{table}_status
                ON files__{table}(status);
            """
        )
        self._conn.commit()

    def drop_watched_path(self, name: str) -> None:
        """Drop a watched path's file table and registry entry. Mainly
        useful for tests and config-time pruning."""
        table = self._table_for(name, allow_missing=True)
        if table is None:
            return
        self._conn.execute(f"DROP TABLE IF EXISTS files__{table}")
        self._conn.execute("DELETE FROM watched_paths WHERE name = ?", (name,))
        self._conn.commit()

    def list_watched_paths(self) -> list[str]:
        cursor = self._conn.execute("SELECT name FROM watched_paths ORDER BY name")
        return [row["name"] for row in cursor]

    def _table_for(self, name: str, allow_missing: bool = False) -> str | None:
        row = self._conn.execute(
            "SELECT table_name FROM watched_paths WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            if allow_missing:
                return None
            raise UnknownWatchedPathError(
                f"watched_path {name!r} is not registered; "
                f"call register_watched_path() first"
            )
        return row["table_name"]

    # ------------------------------------------------------------------
    # Scan / diff routines
    # ------------------------------------------------------------------

    def update_from_scan(self, scan_result: ScanResult) -> DiffResult:
        """Compare scan result against stored state and update the database.

        Args:
            scan_result: The result of a directory scan.

        Returns:
            DiffResult with new, modified, deleted, and unchanged counts.
        """
        watched_name = scan_result.name
        table = self._table_for(watched_name)
        diff = DiffResult(watched_path_name=watched_name)
        now = time()

        cursor = self._conn.execute(
            f"SELECT path, size, modified, status, ids_json FROM files__{table}"
        )
        existing = {row["path"]: dict(row) for row in cursor}
        seen_paths: set[str] = set()

        for entry in scan_result.entries:
            seen_paths.add(entry.path)
            prev = existing.get(entry.path)
            ids_json = json.dumps(entry.ids)

            if prev is None:
                diff.new.append(
                    DiffEntry(
                        path=entry.path,
                        size=entry.size,
                        modified=entry.modified,
                        is_directory=entry.is_directory,
                        status="new",
                        ids=dict(entry.ids),
                    )
                )
                self._conn.execute(
                    f"INSERT INTO files__{table} "
                    "(path, size, modified, last_seen, status, ids_json) "
                    "VALUES (?, ?, ?, ?, 'new', ?)",
                    (entry.path, entry.size, entry.modified, now, ids_json),
                )
            elif prev["size"] != entry.size or prev["modified"] != entry.modified:
                diff.modified.append(
                    DiffEntry(
                        path=entry.path,
                        size=entry.size,
                        modified=entry.modified,
                        is_directory=entry.is_directory,
                        status="modified",
                        ids=dict(entry.ids),
                    )
                )
                self._conn.execute(
                    f"UPDATE files__{table} "
                    "SET size = ?, modified = ?, last_seen = ?, "
                    "    status = 'modified', ids_json = ? "
                    "WHERE path = ?",
                    (entry.size, entry.modified, now, ids_json, entry.path),
                )
            else:
                diff.unchanged += 1
                self._conn.execute(
                    f"UPDATE files__{table} SET last_seen = ? WHERE path = ?",
                    (now, entry.path),
                )

        for path, data in existing.items():
            if path not in seen_paths and data["status"] != "deleted":
                diff.deleted.append(
                    DiffEntry(
                        path=path,
                        size=data["size"],
                        modified=data["modified"],
                        is_directory=False,
                        status="deleted",
                        ids=json.loads(data.get("ids_json") or "{}"),
                    )
                )
                self._conn.execute(
                    f"UPDATE files__{table} SET status = 'deleted' WHERE path = ?",
                    (path,),
                )

        self._conn.commit()
        return diff

    def update_from_warm_scan(self, warm: WarmScanResult) -> DiffResult:
        """Diff a warm scan's partial output against state.

        Files outside the `changed_dirs` scope are not touched (they're
        assumed unchanged because their parent directory's mtime didn't
        move). Within the scope, behaviour matches `update_from_scan`:
        new files are inserted, modified files are updated, files that
        were in state but missing from the scan are marked deleted.
        """
        watched_name = warm.name
        table = self._table_for(watched_name)
        diff = DiffResult(watched_path_name=watched_name)
        now = time()

        cursor = self._conn.execute(
            f"SELECT path, size, modified, status, ids_json FROM files__{table}"
        )
        existing = {row["path"]: dict(row) for row in cursor}
        observed = {entry.path: entry for entry in warm.entries}
        in_scope = _in_scope_filter(warm.changed_dirs)

        for path, entry in observed.items():
            prev = existing.get(path)
            ids_json = json.dumps(entry.ids)
            if prev is None:
                diff.new.append(
                    DiffEntry(
                        path=path,
                        size=entry.size,
                        modified=entry.modified,
                        is_directory=entry.is_directory,
                        status="new",
                        ids=dict(entry.ids),
                    )
                )
                self._conn.execute(
                    f"INSERT INTO files__{table} "
                    "(path, size, modified, last_seen, status, ids_json) "
                    "VALUES (?, ?, ?, ?, 'new', ?)",
                    (path, entry.size, entry.modified, now, ids_json),
                )
            elif prev["size"] != entry.size or prev["modified"] != entry.modified:
                diff.modified.append(
                    DiffEntry(
                        path=path,
                        size=entry.size,
                        modified=entry.modified,
                        is_directory=entry.is_directory,
                        status="modified",
                        ids=dict(entry.ids),
                    )
                )
                self._conn.execute(
                    f"UPDATE files__{table} "
                    "SET size = ?, modified = ?, last_seen = ?, "
                    "    status = 'modified', ids_json = ? "
                    "WHERE path = ?",
                    (entry.size, entry.modified, now, ids_json, path),
                )
            else:
                diff.unchanged += 1
                self._conn.execute(
                    f"UPDATE files__{table} SET last_seen = ? WHERE path = ?",
                    (now, path),
                )

        for path, data in existing.items():
            if path in observed or data["status"] == "deleted":
                continue
            if not in_scope(path):
                continue
            diff.deleted.append(
                DiffEntry(
                    path=path,
                    size=data["size"],
                    modified=data["modified"],
                    is_directory=False,
                    status="deleted",
                    ids=json.loads(data.get("ids_json") or "{}"),
                )
            )
            self._conn.execute(
                f"UPDATE files__{table} SET status = 'deleted' WHERE path = ?",
                (path,),
            )

        self._conn.commit()
        return diff

    def update_from_targeted_stats(
        self,
        watched_path_name: str,
        observed: list[FileEntry],
        missing: list[str],
    ) -> DiffResult:
        """Diff a targeted set of file stats against state.

        Only the paths in `observed` (still present, with fresh stat) and
        `missing` (raised FileNotFoundError) are authoritative. Any other
        file in state is left alone. Used by the hot scan, which only
        stats the recently-modified subset of known files.
        """
        table = self._table_for(watched_path_name)
        diff = DiffResult(watched_path_name=watched_path_name)
        now = time()

        observed_paths = {e.path for e in observed}
        relevant_paths = list(observed_paths | set(missing))
        if not relevant_paths:
            return diff

        placeholders = ",".join("?" for _ in relevant_paths)
        cursor = self._conn.execute(
            f"SELECT path, size, modified, status, ids_json FROM files__{table} "
            f"WHERE path IN ({placeholders})",
            relevant_paths,
        )
        existing = {row["path"]: dict(row) for row in cursor}

        for entry in observed:
            prev = existing.get(entry.path)
            ids_json = json.dumps(entry.ids)
            if prev is None:
                diff.new.append(
                    DiffEntry(
                        path=entry.path,
                        size=entry.size,
                        modified=entry.modified,
                        is_directory=entry.is_directory,
                        status="new",
                        ids=dict(entry.ids),
                    )
                )
                self._conn.execute(
                    f"INSERT INTO files__{table} "
                    "(path, size, modified, last_seen, status, ids_json) "
                    "VALUES (?, ?, ?, ?, 'new', ?)",
                    (entry.path, entry.size, entry.modified, now, ids_json),
                )
            elif prev["size"] != entry.size or prev["modified"] != entry.modified:
                diff.modified.append(
                    DiffEntry(
                        path=entry.path,
                        size=entry.size,
                        modified=entry.modified,
                        is_directory=entry.is_directory,
                        status="modified",
                        ids=dict(entry.ids),
                    )
                )
                self._conn.execute(
                    f"UPDATE files__{table} "
                    "SET size = ?, modified = ?, last_seen = ?, "
                    "    status = 'modified', ids_json = ? "
                    "WHERE path = ?",
                    (entry.size, entry.modified, now, ids_json, entry.path),
                )
            else:
                diff.unchanged += 1
                self._conn.execute(
                    f"UPDATE files__{table} SET last_seen = ? WHERE path = ?",
                    (now, entry.path),
                )

        for path in missing:
            prev = existing.get(path)
            if prev is None or prev["status"] == "deleted":
                continue
            diff.deleted.append(
                DiffEntry(
                    path=path,
                    size=prev["size"],
                    modified=prev["modified"],
                    is_directory=False,
                    status="deleted",
                    ids=json.loads(prev.get("ids_json") or "{}"),
                )
            )
            self._conn.execute(
                f"UPDATE files__{table} SET status = 'deleted' WHERE path = ?",
                (path,),
            )

        self._conn.commit()
        return diff

    def upsert_entries(self, watched_path_name: str, entries: list[FileEntry]) -> None:
        """Insert or update individual file entries without deletion detection."""
        table = self._table_for(watched_path_name)
        now = time()
        for entry in entries:
            existing = self._conn.execute(
                f"SELECT size, modified FROM files__{table} WHERE path = ?",
                (entry.path,),
            ).fetchone()
            ids_json = json.dumps(entry.ids)
            if existing is None:
                self._conn.execute(
                    f"INSERT INTO files__{table} "
                    "(path, size, modified, last_seen, status, ids_json) "
                    "VALUES (?, ?, ?, ?, 'new', ?)",
                    (entry.path, entry.size, entry.modified, now, ids_json),
                )
            elif (
                existing["size"] != entry.size or existing["modified"] != entry.modified
            ):
                self._conn.execute(
                    f"UPDATE files__{table} "
                    "SET size = ?, modified = ?, last_seen = ?, "
                    "    status = 'modified', ids_json = ? "
                    "WHERE path = ?",
                    (entry.size, entry.modified, now, ids_json, entry.path),
                )
        self._conn.commit()

    def mark_entries_deleted(self, watched_path_name: str, paths: list[str]) -> None:
        table = self._table_for(watched_path_name)
        for path in paths:
            self._conn.execute(
                f"UPDATE files__{table} SET status = 'deleted' "
                "WHERE path = ? AND status != 'deleted'",
                (path,),
            )
        self._conn.commit()

    def mark_synced(self, watched_path_name: str, paths: list[str]) -> None:
        table = self._table_for(watched_path_name)
        now = time()
        for path in paths:
            self._conn.execute(
                f"UPDATE files__{table} SET last_synced = ?, status = 'synced' "
                "WHERE path = ?",
                (now, path),
            )
        self._conn.commit()

    def remove_deleted(self, watched_path_name: str) -> None:
        table = self._table_for(watched_path_name)
        self._conn.execute(
            f"DELETE FROM files__{table} "
            "WHERE status = 'deleted' AND last_synced IS NOT NULL"
        )
        self._conn.commit()

    def get_pending_changes(self, watched_path_name: str) -> list[DiffEntry]:
        table = self._table_for(watched_path_name)
        cursor = self._conn.execute(
            f"SELECT path, size, modified, status, ids_json FROM files__{table} "
            "WHERE status != 'synced'"
        )
        return [
            DiffEntry(
                path=row["path"],
                size=row["size"],
                modified=row["modified"],
                is_directory=False,
                status=row["status"],
                ids=json.loads(row["ids_json"] or "{}"),
            )
            for row in cursor
        ]

    def recently_modified_paths(
        self, watched_path_name: str, since: float
    ) -> list[str]:
        """Return paths whose `modified` timestamp is newer than `since`.

        Used by the hot scan to pick the small set of files worth re-stat'ing.
        """
        table = self._table_for(watched_path_name)
        cursor = self._conn.execute(
            f"SELECT path FROM files__{table} WHERE modified > ? ORDER BY path",
            (since,),
        )
        return [row["path"] for row in cursor]

    # ------------------------------------------------------------------
    # Per-tier scan timestamps
    # ------------------------------------------------------------------

    def get_scan_timestamps(self, watched_path_name: str) -> ScanTimestamps:
        row = self._conn.execute(
            "SELECT last_hot_scan, last_warm_scan, last_cold_scan, last_max_dir_mtime "
            "FROM watched_paths WHERE name = ?",
            (watched_path_name,),
        ).fetchone()
        if row is None:
            raise UnknownWatchedPathError(
                f"watched_path {watched_path_name!r} is not registered"
            )
        return ScanTimestamps(
            hot=row["last_hot_scan"],
            warm=row["last_warm_scan"],
            cold=row["last_cold_scan"],
            max_dir_mtime=row["last_max_dir_mtime"],
        )

    def update_scan_timestamp(
        self, watched_path_name: str, kind: str, ts: float
    ) -> None:
        if kind not in ("hot", "warm", "cold"):
            raise ValueError(f"unknown scan kind: {kind!r}")
        column = f"last_{kind}_scan"
        # Cold supersedes warm, warm supersedes hot — bumping a higher tier
        # bumps the lower tiers too, so they don't re-fire immediately.
        if kind == "cold":
            sql = (
                "UPDATE watched_paths "
                "SET last_cold_scan = ?, last_warm_scan = ?, last_hot_scan = ? "
                "WHERE name = ?"
            )
            self._conn.execute(sql, (ts, ts, ts, watched_path_name))
        elif kind == "warm":
            sql = (
                "UPDATE watched_paths "
                "SET last_warm_scan = ?, last_hot_scan = ? "
                "WHERE name = ?"
            )
            self._conn.execute(sql, (ts, ts, watched_path_name))
        else:
            sql = f"UPDATE watched_paths SET {column} = ? WHERE name = ?"
            self._conn.execute(sql, (ts, watched_path_name))
        self._conn.commit()

    def update_max_dir_mtime(self, watched_path_name: str, mtime: float) -> None:
        """Record the maximum directory mtime seen during the last warm or
        cold scan; the next warm scan uses it to short-circuit unchanged
        subtrees."""
        self._conn.execute(
            "UPDATE watched_paths SET last_max_dir_mtime = ? WHERE name = ?",
            (mtime, watched_path_name),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Sync log (shared across paths)
    # ------------------------------------------------------------------

    def log_sync(
        self,
        watched_path_name: str,
        snapshot_type: str,
        entries_sent: int,
        success: bool,
    ) -> None:
        self._conn.execute(
            "INSERT INTO sync_log "
            "(watched_path_name, timestamp, snapshot_type, entries_sent, success) "
            "VALUES (?, ?, ?, ?, ?)",
            (watched_path_name, time(), snapshot_type, entries_sent, int(success)),
        )
        self._conn.commit()

    def get_last_sync(self, watched_path_name: str) -> float | None:
        cursor = self._conn.execute(
            "SELECT timestamp FROM sync_log "
            "WHERE watched_path_name = ? AND success = 1 "
            "ORDER BY timestamp DESC LIMIT 1",
            (watched_path_name,),
        )
        row = cursor.fetchone()
        return row["timestamp"] if row else None
