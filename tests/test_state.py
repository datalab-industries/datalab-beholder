"""Tests for the SQLite state store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datalab_beholder.scanner import FileEntry, ScanResult, scan_directory
from datalab_beholder.state import StateStore, UnknownWatchedPathError


def _make_scan(name: str, entries: list[FileEntry]) -> ScanResult:
    return ScanResult(
        root_path="/x",
        name=name,
        timestamp=datetime.now(timezone.utc),
        entries=entries,
    )


class TestStateStore:
    def test_create_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = StateStore(db_path)
        assert db_path.exists()
        store.close()

    def test_unregistered_watched_path_raises(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        with pytest.raises(UnknownWatchedPathError):
            store.get_pending_changes("never-registered")
        store.close()

    def test_first_scan_all_new(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        result = scan_directory(tmp_tree, name="test")
        diff = store.update_from_scan(result)

        assert len(diff.new) > 0
        assert len(diff.modified) == 0
        assert len(diff.deleted) == 0
        assert diff.unchanged == 0
        assert diff.has_changes is True
        assert diff.snapshot_type == "full"
        store.close()

    def test_second_scan_no_changes(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        result = scan_directory(tmp_tree, name="test")

        store.update_from_scan(result)
        diff2 = store.update_from_scan(result)

        assert len(diff2.new) == 0
        assert len(diff2.modified) == 0
        assert len(diff2.deleted) == 0
        assert diff2.unchanged > 0
        assert diff2.has_changes is False
        store.close()

    def test_detect_modified(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        (tmp_tree / "file1.csv").write_text("a,b,c\n1,2,3\n4,5,6\n")
        diff = store.update_from_scan(scan_directory(tmp_tree, name="test"))

        modified_paths = {e.path for e in diff.modified}
        assert "file1.csv" in modified_paths
        assert diff.snapshot_type == "diff"
        store.close()

    def test_detect_new_file(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        (tmp_tree / "new_file.csv").write_text("new data")
        diff = store.update_from_scan(scan_directory(tmp_tree, name="test"))

        assert "new_file.csv" in {e.path for e in diff.new}
        store.close()

    def test_detect_deleted(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        (tmp_tree / "notes.txt").unlink()
        diff = store.update_from_scan(scan_directory(tmp_tree, name="test"))

        assert "notes.txt" in {e.path for e in diff.deleted}
        store.close()

    def test_mark_synced(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        store.mark_synced("test", ["file1.csv", "file2.raw"])

        pending_paths = {e.path for e in store.get_pending_changes("test")}
        assert "file1.csv" not in pending_paths
        assert "file2.raw" not in pending_paths
        store.close()

    def test_get_pending_changes(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        pending = store.get_pending_changes("test")
        assert len(pending) > 0
        assert all(e.status == "new" for e in pending)
        store.close()

    def test_log_sync(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        # log_sync uses the shared sync_log table; no registration needed.
        store.log_sync("test-path", "full", 10, True)
        assert store.get_last_sync("test-path") is not None
        assert store.get_last_sync("nonexistent") is None
        store.close()

    def test_context_manager(self, tmp_path: Path) -> None:
        with StateStore(tmp_path / "test.db") as store:
            assert store is not None

    def test_multiple_watched_paths(self, tmp_path: Path, tmp_tree: Path) -> None:
        """Changes to one watched path don't affect another."""
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("path-a")
        store.register_watched_path("path-b")
        store.update_from_scan(scan_directory(tmp_tree, name="path-a"))
        store.update_from_scan(scan_directory(tmp_tree, name="path-b"))

        store.mark_synced("path-a", ["file1.csv"])

        a_paths = {e.path for e in store.get_pending_changes("path-a")}
        b_paths = {e.path for e in store.get_pending_changes("path-b")}
        assert "file1.csv" not in a_paths
        assert "file1.csv" in b_paths
        store.close()

    def test_upsert_entries_new(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        entries = [
            FileEntry(path="a.csv", size=100, modified=1000.0, is_directory=False),
            FileEntry(path="b.csv", size=200, modified=2000.0, is_directory=False),
        ]
        store.upsert_entries("wp", entries)

        pending = store.get_pending_changes("wp")
        assert {e.path for e in pending} == {"a.csv", "b.csv"}
        assert all(e.status == "new" for e in pending)
        store.close()

    def test_upsert_entries_updates_modified(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.upsert_entries(
            "wp",
            [FileEntry(path="a.csv", size=100, modified=1000.0, is_directory=False)],
        )
        store.upsert_entries(
            "wp",
            [FileEntry(path="a.csv", size=999, modified=1000.0, is_directory=False)],
        )

        pending = store.get_pending_changes("wp")
        assert len(pending) == 1
        assert pending[0].status == "modified"
        assert pending[0].size == 999
        store.close()

    def test_upsert_entries_no_false_deletions(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        store.upsert_entries(
            "test",
            [
                FileEntry(
                    path="brand_new.csv", size=10, modified=99.0, is_directory=False
                )
            ],
        )

        pending = store.get_pending_changes("test")
        paths = {e.path for e in pending}
        assert "brand_new.csv" in paths
        assert "file1.csv" in paths
        assert all(e.status == "new" for e in pending)
        store.close()

    def test_mark_entries_deleted(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        store.mark_entries_deleted("test", ["file1.csv", "notes.txt"])

        statuses = {e.path: e.status for e in store.get_pending_changes("test")}
        assert statuses["file1.csv"] == "deleted"
        assert statuses["notes.txt"] == "deleted"
        assert statuses.get("file2.raw") == "new"
        store.close()

    def test_mark_entries_deleted_idempotent(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.upsert_entries(
            "wp",
            [FileEntry(path="a.csv", size=100, modified=1000.0, is_directory=False)],
        )
        store.mark_entries_deleted("wp", ["a.csv"])
        store.mark_entries_deleted("wp", ["a.csv"])

        pending = store.get_pending_changes("wp")
        assert len(pending) == 1
        assert pending[0].status == "deleted"
        store.close()

    def test_remove_deleted(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("test")
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        (tmp_tree / "notes.txt").unlink()
        store.update_from_scan(scan_directory(tmp_tree, name="test"))

        store.mark_synced("test", ["notes.txt"])
        store.remove_deleted("test")

        assert "notes.txt" not in {e.path for e in store.get_pending_changes("test")}
        store.close()


class TestRegistry:
    def test_register_creates_per_path_table(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("digibat")

        cursor = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'files__%'"
        )
        tables = {row["name"] for row in cursor}
        assert "files__digibat" in tables
        store.close()

    def test_register_is_idempotent(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("foo")
        store.register_watched_path("foo")  # no error
        assert store.list_watched_paths() == ["foo"]
        store.close()

    def test_name_sanitisation(self, tmp_path: Path) -> None:
        """Names with non-identifier chars get sanitised for the table name."""
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("My Path-1")

        cursor = store._conn.execute(
            "SELECT table_name FROM watched_paths WHERE name = ?",
            ("My Path-1",),
        )
        row = cursor.fetchone()
        assert row["table_name"] == "my_path_1"
        store.close()

    def test_collision_after_sanitisation_rejected(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("foo-bar")
        with pytest.raises(ValueError, match="already used"):
            store.register_watched_path("foo bar")  # sanitises to same table
        store.close()

    def test_drop_watched_path(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("temp")
        store.upsert_entries(
            "temp",
            [FileEntry(path="x", size=1, modified=1.0, is_directory=False)],
        )

        store.drop_watched_path("temp")

        assert store.list_watched_paths() == []
        with pytest.raises(UnknownWatchedPathError):
            store.get_pending_changes("temp")
        store.close()

    def test_drop_unknown_is_noop(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.drop_watched_path("never-existed")  # no error
        store.close()


class TestScanTimestamps:
    def test_initial_timestamps_are_none(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        ts = store.get_scan_timestamps("wp")
        assert ts.hot is None
        assert ts.warm is None
        assert ts.cold is None
        assert ts.max_dir_mtime is None
        store.close()

    def test_update_hot_only_touches_hot(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.update_scan_timestamp("wp", "hot", 100.0)
        ts = store.get_scan_timestamps("wp")
        assert ts.hot == 100.0
        assert ts.warm is None
        assert ts.cold is None
        store.close()

    def test_update_warm_bumps_hot_too(self, tmp_path: Path) -> None:
        """A warm scan implicitly satisfies the hot tier (it just stat'd
        everything hot would have); bumping warm bumps hot to match."""
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.update_scan_timestamp("wp", "warm", 200.0)
        ts = store.get_scan_timestamps("wp")
        assert ts.hot == 200.0
        assert ts.warm == 200.0
        assert ts.cold is None
        store.close()

    def test_update_cold_bumps_all(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.update_scan_timestamp("wp", "cold", 300.0)
        ts = store.get_scan_timestamps("wp")
        assert ts.hot == 300.0
        assert ts.warm == 300.0
        assert ts.cold == 300.0
        store.close()

    def test_update_max_dir_mtime(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.update_max_dir_mtime("wp", 12345.0)
        assert store.get_scan_timestamps("wp").max_dir_mtime == 12345.0
        store.close()

    def test_unknown_kind_rejected(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        with pytest.raises(ValueError):
            store.update_scan_timestamp("wp", "lukewarm", 1.0)
        store.close()


class TestRecentlyModified:
    def test_filters_by_modified_timestamp(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.upsert_entries(
            "wp",
            [
                FileEntry(path="old.csv", size=1, modified=100.0, is_directory=False),
                FileEntry(path="mid.csv", size=1, modified=500.0, is_directory=False),
                FileEntry(path="new.csv", size=1, modified=900.0, is_directory=False),
            ],
        )

        recent = store.recently_modified_paths("wp", since=400.0)
        assert recent == ["mid.csv", "new.csv"]
        store.close()

    def test_empty_when_no_recent(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        store.register_watched_path("wp")
        store.upsert_entries(
            "wp",
            [FileEntry(path="x", size=1, modified=10.0, is_directory=False)],
        )
        assert store.recently_modified_paths("wp", since=999.0) == []
        store.close()


class TestLegacyMigration:
    def test_old_files_table_is_dropped(self, tmp_path: Path) -> None:
        """Pre-per-path schema's `files` table is dropped on init; the next
        scan re-seeds into per-path tables."""
        db_path = tmp_path / "legacy.db"
        legacy_conn = sqlite3.connect(str(db_path))
        legacy_conn.executescript(
            """
            CREATE TABLE files (
                path TEXT NOT NULL,
                watched_path_name TEXT NOT NULL,
                size INTEGER,
                PRIMARY KEY (path, watched_path_name)
            );
            INSERT INTO files (path, watched_path_name, size)
            VALUES ('legacy.csv', 'old', 42);
            """
        )
        legacy_conn.commit()
        legacy_conn.close()

        # Opening the store should drop the legacy table.
        store = StateStore(db_path)
        cursor = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        assert cursor.fetchone() is None
        store.close()


class TestIdsRoundTrip:
    def test_ids_persisted_through_scan(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        scan = _make_scan(
            "wp",
            [
                FileEntry(
                    path="P011/1111_test.mpr",
                    size=10,
                    modified=1.0,
                    is_directory=False,
                    ids={"group_id": "P011", "item_id": "1111"},
                ),
                FileEntry(
                    path="P012/2222_x.mpr",
                    size=20,
                    modified=2.0,
                    is_directory=False,
                    ids={"item_id": "2222"},
                ),
            ],
        )
        diff = store.update_from_scan(scan)
        assert {e.path: e.ids for e in diff.new} == {
            "P011/1111_test.mpr": {"group_id": "P011", "item_id": "1111"},
            "P012/2222_x.mpr": {"item_id": "2222"},
        }
        assert {e.path: e.ids for e in store.get_pending_changes("wp")} == {
            "P011/1111_test.mpr": {"group_id": "P011", "item_id": "1111"},
            "P012/2222_x.mpr": {"item_id": "2222"},
        }
        store.close()

    def test_ids_default_to_empty_dict(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        scan = _make_scan(
            "wp",
            [FileEntry(path="a.txt", size=1, modified=1.0, is_directory=False)],
        )
        store.update_from_scan(scan)
        pending = store.get_pending_changes("wp")
        assert pending[0].ids == {}
        store.close()
