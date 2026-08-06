"""Tests for the directory scanner."""

from __future__ import annotations

from pathlib import Path

import pytest

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

    def test_skip_dirs_true_omits_directory_entries(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, skip_dirs=True)

        dirs = {e.path for e in result.entries if e.is_directory}
        assert dirs == set()
        # Directories are still recursed into and counted in statistics.
        assert result.total_directories > 0
        assert "subdir/file3.csv" in {e.path for e in result.entries}

    def test_skip_dirs_false_includes_directory_entries(self, tmp_tree: Path) -> None:
        result = scan_directory(tmp_tree, skip_dirs=False)

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

    def test_skip_reasons_logged_at_debug(self, tmp_tree: Path, caplog) -> None:
        """Every non-matching file is debug-logged with the reason, so a
        dry run at debug level explains exactly why files were skipped."""
        with caplog.at_level("DEBUG", logger="datalab_beholder.scanner"):
            scan_directory(
                tmp_tree,
                include_patterns=["*.csv"],
                exclude_patterns=["*.tmp"],
                id_patterns=[r"(?P<item_id>file1)\.csv$"],
            )

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            m.startswith("skip temp/scratch.tmp: excluded by pattern '*.tmp'")
            for m in messages
        )
        assert any(
            m.startswith("skip notes.txt: does not match include_patterns")
            for m in messages
        )
        assert any(
            m.startswith("skip subdir/file3.csv: no id_pattern matched")
            for m in messages
        )

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


class TestIdPatterns:
    @pytest.fixture
    def digibat_tree(self, tmp_path: Path) -> Path:
        """Mirror of tests/examples/digibat layout."""
        root = tmp_path / "digibat"
        (root / "P011").mkdir(parents=True)
        (root / "P011" / "1111_test.mpr").write_text("data")
        (root / "P011" / "test.mpr").write_text("no id prefix")
        (root / "P012" / "subdir").mkdir(parents=True)
        (root / "P012" / "subdir" / "2222-x.mpr").write_text("nested")
        (root / "xyz").mkdir()
        (root / "xyz" / "1111-test.mpr").write_text("wrong project dir")
        return root

    def test_id_patterns_extract_named_groups(self, digibat_tree: Path) -> None:
        result = scan_directory(
            digibat_tree,
            include_patterns=["*.mpr"],
            id_patterns=[r"^(?P<group_id>P[0-9]{3,4})/(?P<item_id>[0-9]+)[-_].*\.mpr$"],
        )

        by_path = {e.path: e for e in result.entries}
        assert "P011/1111_test.mpr" in by_path
        assert by_path["P011/1111_test.mpr"].ids == {
            "group_id": "P011",
            "item_id": "1111",
        }

        # Files that don't match the pattern are skipped.
        assert "P011/test.mpr" not in by_path
        assert "xyz/1111-test.mpr" not in by_path
        # Top-level-anchored regex should not match nested project files.
        assert "P012/subdir/2222-x.mpr" not in by_path

    def test_empty_id_patterns_passes_everything(self, digibat_tree: Path) -> None:
        result = scan_directory(digibat_tree, include_patterns=["*.mpr"])

        paths = {e.path for e in result.entries}
        assert "P011/1111_test.mpr" in paths
        assert "P011/test.mpr" in paths
        assert "xyz/1111-test.mpr" in paths
        assert all(e.ids == {} for e in result.entries)

    def test_id_patterns_serialised_in_to_dict(self, digibat_tree: Path) -> None:
        result = scan_directory(
            digibat_tree,
            include_patterns=["*.mpr"],
            id_patterns=[r"^(?P<group_id>P[0-9]{3,4})/(?P<item_id>[0-9]+)[-_].*\.mpr$"],
        )
        d = result.to_dict()
        match = next(e for e in d["entries"] if e["path"] == "P011/1111_test.mpr")
        assert match["ids"] == {"group_id": "P011", "item_id": "1111"}
