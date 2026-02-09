"""Tests for the directory scanner."""

from __future__ import annotations

from pathlib import Path

from datalab_beholder.scanner import scan_directory


class TestScanDirectory:
    def test_basic_scan(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, name="test")

        assert result.name == "test"
        assert result.root_path == str(tmp_tree)
        assert result.total_files > 0
        assert result.total_directories > 0
        assert result.total_size > 0
        assert result.scan_duration_ms >= 0

    def test_finds_all_files(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree)

        paths = {e.path for e in result.entries if not e.is_directory}
        assert "file1.csv" in paths
        assert "file2.raw" in paths
        assert "notes.txt" in paths
        assert "subdir/file3.csv" in paths
        assert "subdir/deep/file4.dat" in paths
        assert "temp/scratch.tmp" in paths

    def test_finds_directories(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree)

        dirs = {e.path for e in result.entries if e.is_directory}
        assert "subdir" in dirs
        assert "subdir/deep" in dirs
        assert "temp" in dirs

    def test_include_patterns(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, include_patterns=["*.csv"])

        file_paths = {e.path for e in result.entries if not e.is_directory}
        assert "file1.csv" in file_paths
        assert "subdir/file3.csv" in file_paths
        assert "file2.raw" not in file_paths
        assert "notes.txt" not in file_paths

    def test_exclude_patterns(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, exclude_patterns=["*.tmp"])

        file_paths = {e.path for e in result.entries if not e.is_directory}
        assert "temp/scratch.tmp" not in file_paths
        assert "file1.csv" in file_paths

    def test_exclude_directory(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, exclude_patterns=["temp"])

        all_paths = {e.path for e in result.entries}
        assert "temp" not in all_paths
        assert "temp/scratch.tmp" not in all_paths

    def test_max_depth_zero(self, tmp_tree: Path) -> None:
        """max_depth=0 should only scan the root level, no recursion."""
        result = scan_directory(tmp_tree, max_depth=0)

        file_paths = {e.path for e in result.entries if not e.is_directory}
        assert "file1.csv" in file_paths
        # Should not recurse into subdirectories
        assert "subdir/file3.csv" not in file_paths
        assert "subdir/deep/file4.dat" not in file_paths

    def test_max_depth_one(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, max_depth=1)

        file_paths = {e.path for e in result.entries if not e.is_directory}
        assert "file1.csv" in file_paths
        assert "subdir/file3.csv" in file_paths
        # Should not recurse deeper
        assert "subdir/deep/file4.dat" not in file_paths

    def test_file_metadata(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree)

        csv_entry = next(e for e in result.entries if e.path == "file1.csv")
        assert csv_entry.size > 0
        assert csv_entry.modified > 0
        assert csv_entry.is_directory is False

        raw_entry = next(e for e in result.entries if e.path == "file2.raw")
        assert raw_entry.size == 1024

    def test_empty_directory(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        result = scan_directory(empty, name="empty-test")
        assert result.total_files == 0
        assert result.total_directories == 0
        assert len(result.entries) == 0

    def test_nonexistent_raises(self, tmp_path: Path) -> None:
        """Scanning a nonexistent directory should still return a result (empty)."""
        result = scan_directory(tmp_path / "does-not-exist")
        assert result.total_files == 0

    def test_to_dict(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, name="dict-test")
        d = result.to_dict()

        assert d["name"] == "dict-test"
        assert "entries" in d
        assert "statistics" in d
        assert d["statistics"]["total_files"] == result.total_files

    def test_forward_slash_paths(self, tmp_tree: Path) -> None:
        """All paths should use forward slashes regardless of OS."""
        result = scan_directory(tmp_tree)

        for entry in result.entries:
            assert "\\" not in entry.path

    def test_default_name(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree)
        assert result.name == tmp_tree.name
