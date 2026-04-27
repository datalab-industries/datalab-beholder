"""Local SQLite state store for tracking file sync status."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import time

from datalab_beholder.scanner import FileEntry, ScanResult

SCHEMA = """\
CREATE TABLE IF NOT EXISTS files (
    path TEXT NOT NULL,
    watched_path_name TEXT NOT NULL,
    size INTEGER,
    modified REAL,
    last_synced REAL,
    status TEXT DEFAULT 'new',
    ids_json TEXT DEFAULT '{}',
    PRIMARY KEY (path, watched_path_name)
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


class StateStore:
    """SQLite-backed state store for tracking file metadata and sync status."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def update_from_scan(self, scan_result: ScanResult) -> DiffResult:
        """Compare scan result against stored state and update the database.

        Args:
            scan_result: The result of a directory scan.

        Returns:
            DiffResult with new, modified, deleted, and unchanged counts.
        """
        watched_name = scan_result.name
        diff = DiffResult(watched_path_name=watched_name)

        # Get existing entries for this watched path
        cursor = self._conn.execute(
            "SELECT path, size, modified, status, ids_json FROM files "
            "WHERE watched_path_name = ?",
            (watched_name,),
        )
        existing = {row["path"]: dict(row) for row in cursor}

        # Track which paths are still present
        seen_paths: set[str] = set()

        for entry in scan_result.entries:
            seen_paths.add(entry.path)
            prev = existing.get(entry.path)

            ids_json = json.dumps(entry.ids)

            if prev is None:
                # New file
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
                    "INSERT INTO files (path, watched_path_name, size, modified, status, ids_json) "
                    "VALUES (?, ?, ?, ?, 'new', ?)",
                    (entry.path, watched_name, entry.size, entry.modified, ids_json),
                )
            elif prev["size"] != entry.size or prev["modified"] != entry.modified:
                # Modified file
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
                    "UPDATE files SET size = ?, modified = ?, status = 'modified', ids_json = ? "
                    "WHERE path = ? AND watched_path_name = ?",
                    (entry.size, entry.modified, ids_json, entry.path, watched_name),
                )
            else:
                diff.unchanged += 1

        # Find deleted entries
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
                    "UPDATE files SET status = 'deleted' "
                    "WHERE path = ? AND watched_path_name = ?",
                    (path, watched_name),
                )

        self._conn.commit()
        return diff

    def upsert_entries(self, watched_path_name: str, entries: list[FileEntry]) -> None:
        """Insert or update individual file entries without deletion detection.

        Used by the watcher for incremental updates — only touches the entries
        provided, leaving all other rows untouched.
        """
        for entry in entries:
            existing = self._conn.execute(
                "SELECT size, modified FROM files "
                "WHERE path = ? AND watched_path_name = ?",
                (entry.path, watched_path_name),
            ).fetchone()

            ids_json = json.dumps(entry.ids)

            if existing is None:
                self._conn.execute(
                    "INSERT INTO files (path, watched_path_name, size, modified, status, ids_json) "
                    "VALUES (?, ?, ?, ?, 'new', ?)",
                    (
                        entry.path,
                        watched_path_name,
                        entry.size,
                        entry.modified,
                        ids_json,
                    ),
                )
            elif (
                existing["size"] != entry.size or existing["modified"] != entry.modified
            ):
                self._conn.execute(
                    "UPDATE files SET size = ?, modified = ?, status = 'modified', ids_json = ? "
                    "WHERE path = ? AND watched_path_name = ?",
                    (
                        entry.size,
                        entry.modified,
                        ids_json,
                        entry.path,
                        watched_path_name,
                    ),
                )
        self._conn.commit()

    def mark_entries_deleted(self, watched_path_name: str, paths: list[str]) -> None:
        """Mark specific file entries as deleted.

        Used by the watcher when it receives delete events.
        """
        for path in paths:
            self._conn.execute(
                "UPDATE files SET status = 'deleted' "
                "WHERE path = ? AND watched_path_name = ? AND status != 'deleted'",
                (path, watched_path_name),
            )
        self._conn.commit()

    def mark_synced(self, watched_path_name: str, paths: list[str]) -> None:
        """Mark files as successfully synced to the server.

        Args:
            watched_path_name: The watched path these files belong to.
            paths: List of relative file paths that were synced.
        """
        now = time()
        for path in paths:
            self._conn.execute(
                "UPDATE files SET last_synced = ?, status = 'synced' "
                "WHERE path = ? AND watched_path_name = ?",
                (now, path, watched_path_name),
            )
        self._conn.commit()

    def remove_deleted(self, watched_path_name: str) -> None:
        """Remove entries marked as deleted after they've been synced.

        Args:
            watched_path_name: The watched path to clean up.
        """
        self._conn.execute(
            "DELETE FROM files WHERE status = 'deleted' AND watched_path_name = ? "
            "AND last_synced IS NOT NULL",
            (watched_path_name,),
        )
        self._conn.commit()

    def get_pending_changes(self, watched_path_name: str) -> list[DiffEntry]:
        """Get entries that haven't been synced yet.

        Args:
            watched_path_name: The watched path to query.

        Returns:
            List of DiffEntry objects with pending changes.
        """
        cursor = self._conn.execute(
            "SELECT path, size, modified, status, ids_json FROM files "
            "WHERE watched_path_name = ? AND status != 'synced'",
            (watched_path_name,),
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

    def log_sync(
        self,
        watched_path_name: str,
        snapshot_type: str,
        entries_sent: int,
        success: bool,
    ) -> None:
        """Record a sync attempt in the log.

        Args:
            watched_path_name: The watched path that was synced.
            snapshot_type: "full" or "diff".
            entries_sent: Number of entries included in the sync.
            success: Whether the sync succeeded.
        """
        self._conn.execute(
            "INSERT INTO sync_log (watched_path_name, timestamp, snapshot_type, entries_sent, success) "
            "VALUES (?, ?, ?, ?, ?)",
            (watched_path_name, time(), snapshot_type, entries_sent, int(success)),
        )
        self._conn.commit()

    def get_last_sync(self, watched_path_name: str) -> float | None:
        """Get the timestamp of the last successful sync.

        Args:
            watched_path_name: The watched path to query.

        Returns:
            Unix timestamp of last sync, or None if never synced.
        """
        cursor = self._conn.execute(
            "SELECT timestamp FROM sync_log "
            "WHERE watched_path_name = ? AND success = 1 "
            "ORDER BY timestamp DESC LIMIT 1",
            (watched_path_name,),
        )
        row = cursor.fetchone()
        return row["timestamp"] if row else None
