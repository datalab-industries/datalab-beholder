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
        # Scan again without changes
        result2 = scan_directory(tmp_tree, name="test")
        diff2 = store.update_from_scan(result2)

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
