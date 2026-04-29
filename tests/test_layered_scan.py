"""End-to-end smoke tests for LocalWatchedPath.{hot,warm,cold}_scan."""

from __future__ import annotations

import os
import time
from pathlib import Path

from datalab_beholder.config import LocalWatchedPath
from datalab_beholder.state import StateStore


def _make(name: str, root: Path, **scan_overrides) -> LocalWatchedPath:
    return LocalWatchedPath(
        path=root,
        name=name,
        datalab="d",
        scan=scan_overrides or {},  # type: ignore[arg-type]
    )


def _bump_mtime(path: Path, seconds_ahead: float) -> None:
    """Force a file's mtime forward — bypasses sub-second clock issues."""
    target = time.time() + seconds_ahead
    os.utime(path, (target, target))


class TestColdScan:
    def test_cold_scan_seeds_state(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        wp = _make("wp", tmp_tree)

        diff = wp.cold_scan(store)

        paths = {e.path for e in diff.new}
        assert "file1.csv" in paths
        ts = store.get_scan_timestamps("wp")
        assert ts.cold is not None
        # cold subsumes warm + hot.
        assert ts.warm == ts.cold
        assert ts.hot == ts.cold

    def test_cold_scan_detects_in_place_modification(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        wp = _make("wp", tmp_tree)
        wp.cold_scan(store)

        target = tmp_tree / "file1.csv"
        target.write_text("rewritten")
        _bump_mtime(target, 10)

        diff = wp.cold_scan(store)
        assert "file1.csv" in {e.path for e in diff.modified}


class TestWarmScan:
    def test_warm_scan_discovers_new_file(self, tmp_path: Path, tmp_tree: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        wp = _make("wp", tmp_tree)

        # Seed via cold scan, anchor warm short-circuit.
        wp.cold_scan(store)

        # Drop a new file into a subdir; bump the dir's mtime so the
        # warm scan recognises it as changed.
        new_file = tmp_tree / "subdir" / "freshly_added.csv"
        new_file.write_text("hi")
        _bump_mtime(tmp_tree / "subdir", 10)

        diff = wp.warm_scan(store)
        assert "subdir/freshly_added.csv" in {e.path for e in diff.new}

    def test_warm_scan_skips_unchanged_dirs(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        """In-place file rewrites without a parent-dir mtime bump are
        invisible to the warm scan — that's by design; cold catches them."""
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        wp = _make("wp", tmp_tree)
        wp.cold_scan(store)

        target = tmp_tree / "file1.csv"
        target.write_text("rewritten")
        _bump_mtime(target, 10)
        # Explicitly do NOT bump the parent dir's mtime.
        _bump_mtime(tmp_tree, -100)

        diff = wp.warm_scan(store)
        # File1 is in the (unchanged) root dir, so warm doesn't see the rewrite.
        assert "file1.csv" not in {e.path for e in diff.modified}


class TestHotScan:
    def test_hot_scan_picks_up_recent_modification(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        wp = _make("wp", tmp_tree, hot_window=86400)
        wp.cold_scan(store)
        store.mark_synced("wp", [e.path for e in store.get_pending_changes("wp")])

        target = tmp_tree / "file1.csv"
        target.write_text("rewritten")
        _bump_mtime(target, 10)
        # The hot scan reads recently_modified_paths(since=now-hot_window).
        # The file's stored modified timestamp is from the cold scan, so it
        # already qualifies — we just need it to differ on size/mtime.

        diff = wp.hot_scan(store)
        assert "file1.csv" in {e.path for e in diff.modified}

    def test_hot_scan_marks_missing_file_deleted(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        wp = _make("wp", tmp_tree)
        wp.cold_scan(store)

        (tmp_tree / "file1.csv").unlink()

        diff = wp.hot_scan(store)
        assert "file1.csv" in {e.path for e in diff.deleted}

    def test_hot_scan_ignores_files_outside_window(
        self, tmp_path: Path, tmp_tree: Path
    ) -> None:
        store = StateStore(tmp_path / "s.db")
        store.register_watched_path("wp")
        # 1-second window means almost every file is "stale" for the hot tier.
        wp = _make("wp", tmp_tree, hot_window=1)
        wp.cold_scan(store)
        # Force every file's stored mtime way into the past so none are
        # eligible.
        store._conn.execute("UPDATE files__wp SET modified = 0")
        store._conn.commit()

        diff = wp.hot_scan(store)
        assert not diff.has_changes
