"""Tests for the SQLite state store."""

from __future__ import annotations

from pathlib import Path

from datalab_beholder.scanner import scan_directory
from datalab_beholder.state import StateStore


class TestStateStore:
    def test_create_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = StateStore(db_path)
        assert db_path.exists()
        store.close()

    def test_first_scan_all_new(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
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
        result = scan_directory(tmp_tree, name="test")

        store.update_from_scan(result)
        # Feed the same scan result again — identical entries mean zero changes.
        # (We don't re-scan because on Windows, directory mtimes can shift by
        # sub-second amounts between rapid stat() calls.)
        diff2 = store.update_from_scan(result)

        assert len(diff2.new) == 0
        assert len(diff2.modified) == 0
        assert len(diff2.deleted) == 0
        assert diff2.unchanged > 0
        assert diff2.has_changes is False
        store.close()

    def test_detect_modified(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        # Modify a file
        (tmp_tree / "file1.csv").write_text("a,b,c\n1,2,3\n4,5,6\n")

        result2 = scan_directory(tmp_tree, name="test")
        diff = store.update_from_scan(result2)

        modified_paths = {e.path for e in diff.modified}
        assert "file1.csv" in modified_paths
        assert diff.snapshot_type == "diff"
        store.close()

    def test_detect_new_file(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        # Add a new file
        (tmp_tree / "new_file.csv").write_text("new data")

        result2 = scan_directory(tmp_tree, name="test")
        diff = store.update_from_scan(result2)

        new_paths = {e.path for e in diff.new}
        assert "new_file.csv" in new_paths
        store.close()

    def test_detect_deleted(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        # Delete a file
        (tmp_tree / "notes.txt").unlink()

        result2 = scan_directory(tmp_tree, name="test")
        diff = store.update_from_scan(result2)

        deleted_paths = {e.path for e in diff.deleted}
        assert "notes.txt" in deleted_paths
        store.close()

    def test_mark_synced(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        store.mark_synced("test", ["file1.csv", "file2.raw"])

        pending = store.get_pending_changes("test")
        pending_paths = {e.path for e in pending}
        assert "file1.csv" not in pending_paths
        assert "file2.raw" not in pending_paths
        store.close()

    def test_get_pending_changes(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        pending = store.get_pending_changes("test")
        assert len(pending) > 0
        assert all(e.status == "new" for e in pending)
        store.close()

    def test_log_sync(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "test.db")

        store.log_sync("test-path", "full", 10, True)
        last = store.get_last_sync("test-path")
        assert last is not None

        assert store.get_last_sync("nonexistent") is None
        store.close()

    def test_context_manager(self, tmp_path: Path) -> None:
        with StateStore(tmp_path / "test.db") as store:
            assert store is not None

    def test_multiple_watched_paths(self, tmp_path: Path, tmp_tree: Path) -> None:
        """Changes to one watched path don't affect another."""
        store = StateStore(tmp_path / "test.db")
        result1 = scan_directory(tmp_tree, name="path-a")
        result2 = scan_directory(tmp_tree, name="path-b")

        store.update_from_scan(result1)
        store.update_from_scan(result2)

        store.mark_synced("path-a", ["file1.csv"])

        pending_a = store.get_pending_changes("path-a")
        pending_b = store.get_pending_changes("path-b")

        a_paths = {e.path for e in pending_a}
        b_paths = {e.path for e in pending_b}
        assert "file1.csv" not in a_paths
        assert "file1.csv" in b_paths
        store.close()

    def test_upsert_entries_new(self, tmp_path: Path) -> None:
        """upsert_entries inserts new rows without deletion detection."""
        from datalab_beholder.scanner import FileEntry

        store = StateStore(tmp_path / "test.db")
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
        """upsert_entries marks changed entries as modified."""
        from datalab_beholder.scanner import FileEntry

        store = StateStore(tmp_path / "test.db")
        store.upsert_entries(
            "wp",
            [
                FileEntry(path="a.csv", size=100, modified=1000.0, is_directory=False),
            ],
        )
        # Upsert same path with new size
        store.upsert_entries(
            "wp",
            [
                FileEntry(path="a.csv", size=999, modified=1000.0, is_directory=False),
            ],
        )

        pending = store.get_pending_changes("wp")
        assert len(pending) == 1
        assert pending[0].status == "modified"
        assert pending[0].size == 999
        store.close()

    def test_upsert_entries_no_false_deletions(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        """upsert_entries should not mark missing entries as deleted."""
        from datalab_beholder.scanner import FileEntry

        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        # Upsert only one new file — existing entries must stay untouched
        store.upsert_entries(
            "test",
            [
                FileEntry(
                    path="brand_new.csv", size=10, modified=99.0, is_directory=False
                ),
            ],
        )

        pending = store.get_pending_changes("test")
        # The original files should still be pending (status=new from initial scan)
        # plus the new file — no deletions
        paths = {e.path for e in pending}
        assert "brand_new.csv" in paths
        assert "file1.csv" in paths
        assert all(e.status in ("new",) for e in pending)
        store.close()

    def test_mark_entries_deleted(self, tmp_path: Path, tmp_tree: Path) -> None:
        """mark_entries_deleted marks specific entries as deleted."""
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        store.mark_entries_deleted("test", ["file1.csv", "notes.txt"])

        pending = store.get_pending_changes("test")
        statuses = {e.path: e.status for e in pending}
        assert statuses["file1.csv"] == "deleted"
        assert statuses["notes.txt"] == "deleted"
        # Other files still new
        assert statuses.get("file2.raw") == "new"
        store.close()

    def test_mark_entries_deleted_idempotent(self, tmp_path: Path) -> None:
        """Marking an already-deleted entry as deleted is a no-op."""
        from datalab_beholder.scanner import FileEntry

        store = StateStore(tmp_path / "test.db")
        store.upsert_entries(
            "wp",
            [
                FileEntry(path="a.csv", size=100, modified=1000.0, is_directory=False),
            ],
        )
        store.mark_entries_deleted("wp", ["a.csv"])
        store.mark_entries_deleted("wp", ["a.csv"])  # again

        pending = store.get_pending_changes("wp")
        assert len(pending) == 1
        assert pending[0].status == "deleted"
        store.close()

    def test_remove_deleted(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "test.db")
        result = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result)

        (tmp_tree / "notes.txt").unlink()
        result2 = scan_directory(tmp_tree, name="test")
        store.update_from_scan(result2)

        # Mark deleted entries as synced, then remove them
        store.mark_synced("test", ["notes.txt"])
        store.remove_deleted("test")

        pending = store.get_pending_changes("test")
        assert "notes.txt" not in {e.path for e in pending}
        store.close()


class TestIdsRoundTrip:
    def test_ids_persisted_through_scan(self, tmp_path: Path) -> None:
        from datalab_beholder.scanner import FileEntry, ScanResult
        from datalab_beholder.state import StateStore
        from datetime import datetime, timezone

        store = StateStore(tmp_path / "s.db")
        scan = ScanResult(
            root_path="/x",
            name="wp",
            timestamp=datetime.now(timezone.utc),
            entries=[
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

        pending = store.get_pending_changes("wp")
        assert {e.path: e.ids for e in pending} == {
            "P011/1111_test.mpr": {"group_id": "P011", "item_id": "1111"},
            "P012/2222_x.mpr": {"item_id": "2222"},
        }
        store.close()

    def test_ids_default_to_empty_dict(self, tmp_path: Path) -> None:
        from datalab_beholder.scanner import FileEntry, ScanResult
        from datalab_beholder.state import StateStore
        from datetime import datetime, timezone

        store = StateStore(tmp_path / "s.db")
        scan = ScanResult(
            root_path="/x",
            name="wp",
            timestamp=datetime.now(timezone.utc),
            entries=[
                FileEntry(path="a.txt", size=1, modified=1.0, is_directory=False),
            ],
        )
        store.update_from_scan(scan)
        pending = store.get_pending_changes("wp")
        assert pending[0].ids == {}
        store.close()

    def test_legacy_db_without_ids_column_migrated(self, tmp_path: Path) -> None:
        """A pre-migration DB (no ids_json column) must be upgradable in place."""
        import sqlite3
        from datalab_beholder.state import StateStore

        db = tmp_path / "legacy.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE files (path TEXT, watched_path_name TEXT, size INTEGER, "
                "modified REAL, last_synced REAL, status TEXT DEFAULT 'new', "
                "PRIMARY KEY (path, watched_path_name))"
            )
            conn.execute(
                "INSERT INTO files (path, watched_path_name, size, modified, status) "
                "VALUES ('old.txt', 'wp', 1, 1.0, 'new')"
            )

        store = StateStore(db)
        pending = store.get_pending_changes("wp")
        assert len(pending) == 1
        assert pending[0].ids == {}
        store.close()
