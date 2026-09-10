"""Tests for the GUI module.

These tests verify that the GUI classes can be instantiated and destroyed
without error. They require tkinter and a working Tcl/Tk installation —
skipped otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip entire module when tkinter is not installed (Ubuntu CI, some Mac builds)
tk = pytest.importorskip("tkinter")


@pytest.fixture
def config_file(tmp_path: Path, tmp_tree: Path) -> Path:
    """Write a minimal config YAML and return its path."""
    import yaml

    config_dict = {
        "datalabs": [
            {
                "name": "test",
                "url": "https://test.example.org",
                "api_key": "test-key",
            }
        ],
        "watched_paths": [
            {"path": str(tmp_tree), "name": "test-data"},
        ],
        "sync": {"metadata_interval": 9999, "file_request_poll": 9999},
        "state_db": str(tmp_path / "state.db"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_dict))
    return path


class TestBeholderGUI:
    def test_gui_creates_and_destroys(self, config_file: Path, monkeypatch) -> None:
        """The GUI window should create and destroy without error."""
        from datalab_api._base import BaseDatalabClient

        from datalab_beholder.client import BeholderClient

        monkeypatch.setattr(BaseDatalabClient, "_detect_api_url", lambda self: None)
        monkeypatch.setenv("DATALAB_API_KEY", "test-key")

        def mock_get_info(self):
            self.info = {
                "attributes": {
                    "available_api_versions": ["0.1.0"],
                    "server_version": "0.1.0",
                }
            }
            return self.info

        def mock_get_block_info(self):
            self.block_info = []
            return self.block_info

        monkeypatch.setattr(BeholderClient, "get_info", mock_get_info)
        monkeypatch.setattr(BeholderClient, "get_block_info", mock_get_block_info)

        from datalab_beholder.gui import BeholderGUI

        try:
            app = BeholderGUI(config_file)
        except tk.TclError as exc:
            pytest.skip(f"Tcl/Tk not usable: {exc}")

        assert app.winfo_exists()
        app.destroy()


@pytest.fixture
def rich_config_file(tmp_path: Path, tmp_tree: Path) -> Path:
    """A config exercising fields the settings dialog does not model."""
    import yaml

    config_dict = {
        "version": 1,
        "datalabs": [
            {
                "name": "test",
                "url": "https://test.example.org",
                "api_key": "test-key",
                "elevate_permissions": True,
            }
        ],
        "watched_paths": [
            {
                "kind": "local",
                "path": str(tmp_tree),
                "name": "test-data",
                "datalab": "test",
                "item_type": "cells",
                "include_patterns": ["*.mpr"],
                "exclude_patterns": ["*.tmp"],
                "id_patterns": [r"(?P<item_id>[0-9]+)\.mpr$"],
                "item_id_template": "{item_id}",
                "block_patterns": {"*.mpr": "cycle"},
                "max_depth": 3,
                "scan": {"hot_interval": 30, "warm_interval": 600},
            },
        ],
        "sync": {"metadata_interval": 900},
        "reset_scan_clocks_on_startup": True,
        "log_level": "debug",
        "state_db": str(tmp_path / "state.db"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_dict))
    return path


class TestSettingsDialog:
    """The settings dialog edits a subset of the schema, so the parts it does
    not model must survive a save untouched."""

    @staticmethod
    def _dialog(config_file: Path):
        """Build a real SettingsDialog on a stand-in parent.

        The parent only needs `reload_config`, which the dialog calls after a
        successful save; recording it lets the tests assert the reload.
        """
        from datalab_beholder.config import load_config
        from datalab_beholder.gui import SettingsDialog, _init_fonts

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            pytest.skip(f"Tcl/Tk not usable: {exc}")
        root.update()
        _init_fonts()

        root.reloaded = []  # type: ignore[attr-defined]
        root.reload_config = root.reloaded.append  # type: ignore[attr-defined]

        config = load_config(config_file)
        return root, SettingsDialog(root, config_file, config), config

    def test_dialog_opens(self, config_file: Path) -> None:
        """Regression: building a datalab row refreshes the path dropdowns,
        so `_path_rows` has to exist before the first row is added."""
        root, dialog, _ = self._dialog(config_file)
        try:
            assert dialog.winfo_exists()
            assert len(dialog._datalab_rows) == 1
        finally:
            root.destroy()

    def test_save_preserves_unmodelled_fields(self, rich_config_file: Path) -> None:
        """Saving with no edits must be a no-op on disk."""
        import yaml

        before = yaml.safe_load(rich_config_file.read_text())
        root, dialog, _ = self._dialog(rich_config_file)
        try:
            dialog._save()
            # A save must refresh the parent's copy, or reopening the dialog
            # would show — and re-save — the pre-save values.
            assert root.reloaded == [rich_config_file]
        finally:
            root.destroy()
        after = yaml.safe_load(rich_config_file.read_text())

        wp_before, wp_after = before["watched_paths"][0], after["watched_paths"][0]
        for key in (
            "kind",
            "item_type",
            "include_patterns",
            "exclude_patterns",
            "id_patterns",
            "item_id_template",
            "block_patterns",
            "max_depth",
            "scan",
        ):
            assert wp_after[key] == wp_before[key], f"{key} was not preserved"

        assert after["version"] == before["version"]
        assert after["state_db"] == before["state_db"]
        assert after["reset_scan_clocks_on_startup"] is True
        assert after["log_level"] == "debug"
        assert after["datalabs"][0]["elevate_permissions"] is True

    def test_elevate_permissions_is_editable(self, rich_config_file: Path) -> None:
        """The sudo checkbox round-trips to the YAML."""
        import yaml

        root, dialog, _ = self._dialog(rich_config_file)
        try:
            assert dialog._datalab_rows[0]["elevate_permissions"].get() is True
            dialog._datalab_rows[0]["elevate_permissions"].set(False)
            dialog._save()
        finally:
            root.destroy()

        after = yaml.safe_load(rich_config_file.read_text())
        assert after["datalabs"][0]["elevate_permissions"] is False

    def test_add_path_keeps_patterns(self, config_file: Path, tmp_path: Path) -> None:
        """Regression: AddPathDialog collected include/exclude patterns into
        StringVars that nothing ever read."""
        root, dialog, _ = self._dialog(config_file)
        try:
            dialog.add_path(
                "new-path",
                str(tmp_path),
                include_patterns=["*.mpr"],
                exclude_patterns=["*.tmp"],
            )
            original = dialog._path_rows[-1]["original"]
        finally:
            root.destroy()

        assert original["include_patterns"] == ["*.mpr"]
        assert original["exclude_patterns"] == ["*.tmp"]


class TestDaemonPresence:
    """Pure logic: how a heartbeat maps onto what the window may do.

    Getting this wrong is what would let a GUI launched at startup fight an
    already-running daemon for the same state database.
    """

    @staticmethod
    def _hb(**kw):
        import time as _time

        from datalab_beholder.state import Heartbeat

        defaults = dict(
            pid=999999,
            hostname="elsewhere",
            daemon_id="d",
            started_at=0.0,
            last_tick=_time.time(),
            status="idle",
        )
        defaults.update(kw)
        return Heartbeat(**defaults)

    def test_no_heartbeat_means_nothing_running(self):
        from datalab_beholder.gui import DaemonPresence

        presence = DaemonPresence(None, driving_locally=False)
        assert presence.kind == DaemonPresence.NONE
        assert not presence.blocks_start

    def test_live_foreign_heartbeat_blocks_start(self):
        from datalab_beholder.gui import DaemonPresence

        presence = DaemonPresence(self._hb(), driving_locally=False)
        assert presence.kind == DaemonPresence.EXTERNAL
        assert presence.blocks_start
        text, _ = presence.describe()
        assert "999999" in text

    def test_stale_heartbeat_does_not_block_start(self):
        """A crashed daemon must not lock the GUI out forever."""
        import time as _time

        from datalab_beholder.gui import DaemonPresence

        presence = DaemonPresence(
            self._hb(last_tick=_time.time() - 3600), driving_locally=False
        )
        assert presence.kind == DaemonPresence.STALE
        assert not presence.blocks_start

    def test_own_heartbeat_is_local(self):
        import os

        from datalab_beholder.gui import DaemonPresence

        hb = self._hb(pid=os.getpid(), hostname=__import__("socket").gethostname())
        presence = DaemonPresence(hb, driving_locally=False)
        assert presence.kind == DaemonPresence.LOCAL
        assert not presence.blocks_start

    def test_driving_locally_wins_over_foreign_claim(self):
        """While this window owns a daemon it reports itself, not the row it
        is about to overwrite."""
        from datalab_beholder.gui import DaemonPresence

        presence = DaemonPresence(self._hb(), driving_locally=True)
        assert presence.kind == DaemonPresence.LOCAL


class TestStateObserver:
    def test_missing_database_is_not_an_error(self, tmp_path):
        """The GUI may start before any daemon has ever run."""
        from datalab_beholder.gui import StateObserver

        observer = StateObserver(tmp_path / "nope.db")
        snap = observer.poll()
        assert snap.available is False
        assert snap.summaries == []

    def test_picks_up_database_created_later(self, tmp_path, tmp_tree):
        """An observer started first must notice the daemon when it appears."""
        from datalab_beholder.gui import StateObserver
        from datalab_beholder.scanner import scan_directory
        from datalab_beholder.state import StateStore

        db = tmp_path / "state.db"
        observer = StateObserver(db)
        assert observer.poll().available is False

        store = StateStore(db)
        store.register_watched_path("wp")
        store.update_from_scan(
            scan_directory(
                tmp_tree, name="wp", include_patterns=["*"], exclude_patterns=[]
            )
        )
        store.beat("rig", "idle")
        store.close()

        snap = observer.poll()
        assert snap.available is True
        assert snap.heartbeat is not None
        assert snap.total_pending > 0
        assert snap.last_scan is None or isinstance(snap.last_scan, float)
        observer.close()

    def test_set_path_reopens(self, tmp_path):
        from datalab_beholder.gui import StateObserver
        from datalab_beholder.state import StateStore

        first, second = tmp_path / "a.db", tmp_path / "b.db"
        for db in (first, second):
            store = StateStore(db)
            store.register_watched_path(db.stem)
            store.close()

        observer = StateObserver(first)
        assert [s.name for s in observer.poll().summaries] == ["a"]
        observer.set_path(second)
        assert [s.name for s in observer.poll().summaries] == ["b"]
        observer.close()


class TestWatchedPathDialog:
    """The editor is the only route to most of the schema, so it has to
    round-trip every field it shows."""

    @staticmethod
    def _dialog(entry):
        from datalab_beholder.gui import WatchedPathDialog, _init_fonts

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            pytest.skip(f"Tcl/Tk not usable: {exc}")
        root.update()
        _init_fonts()

        # The editor only reads `_datalab_names` off its parent.
        parent = tk.Toplevel(root)
        parent._datalab_names = lambda: ["main"]  # type: ignore[attr-defined]

        captured: list[dict] = []
        dialog = WatchedPathDialog(parent, entry, captured.append)
        return root, dialog, captured

    RICH = {
        "kind": "local",
        "name": "echem",
        "path": "/tmp",
        "datalab": "main",
        "item_type": "cells",
        "include_patterns": ["*.mpr", "*.nda"],
        "exclude_patterns": ["*.tmp"],
        "id_patterns": [r"^(?P<item_id>[0-9]+)\.mpr$"],
        "item_id_template": "{item_id}",
        "block_patterns": {"*.mpr": "cycle"},
        "max_depth": 8,
        "scan": {
            "hot_interval": 30,
            "warm_interval": 900,
            "cold_interval": 43200,
            "hot_window": 7200,
        },
    }

    def test_roundtrips_every_field(self):
        root, dialog, captured = self._dialog(dict(self.RICH))
        try:
            dialog._commit()
        finally:
            root.destroy()

        assert captured, "dialog did not commit"
        out = captured[0]
        for key, value in self.RICH.items():
            assert out[key] == value, f"{key} did not round-trip"

    def test_preserves_unknown_keys(self):
        """A config written by a newer beholder must survive an edit."""
        entry = dict(self.RICH, some_future_field={"a": 1})
        root, dialog, captured = self._dialog(entry)
        try:
            dialog._commit()
        finally:
            root.destroy()
        assert captured[0]["some_future_field"] == {"a": 1}

    def test_blank_cold_interval_disables_it(self):
        root, dialog, captured = self._dialog(dict(self.RICH))
        try:
            dialog._cold_var.set("")
            dialog._commit()
        finally:
            root.destroy()
        assert captured[0]["scan"]["cold_interval"] is None

    def test_invalid_id_pattern_is_rejected(self, monkeypatch):
        """Validation runs against the real schema, so a regex with no
        item_id group is caught here rather than at next startup."""
        from datalab_beholder import gui as gui_module

        errors: list[tuple] = []
        monkeypatch.setattr(
            gui_module.messagebox, "showerror", lambda *a, **k: errors.append(a)
        )
        root, dialog, captured = self._dialog(dict(self.RICH))
        try:
            dialog._id_patterns_text.delete("1.0", "end")
            dialog._id_patterns_text.insert("1.0", r"^(?P<group_id>[0-9]+)$")
            dialog._commit()
        finally:
            root.destroy()

        assert not captured, "invalid path should not commit"
        assert errors and "item_id" in str(errors[0])

    def test_ssh_kind_requires_host(self, monkeypatch):
        from datalab_beholder import gui as gui_module

        errors: list[tuple] = []
        monkeypatch.setattr(
            gui_module.messagebox, "showerror", lambda *a, **k: errors.append(a)
        )
        root, dialog, captured = self._dialog(dict(self.RICH))
        try:
            dialog._kind_var.set("ssh")
            dialog._commit()
        finally:
            root.destroy()
        assert not captured
        assert errors and "SSH host" in str(errors[0])

    def test_block_pattern_syntax_error_is_reported(self, monkeypatch):
        from datalab_beholder import gui as gui_module

        errors: list[tuple] = []
        monkeypatch.setattr(
            gui_module.messagebox, "showerror", lambda *a, **k: errors.append(a)
        )
        root, dialog, captured = self._dialog(dict(self.RICH))
        try:
            dialog._block_patterns_text.delete("1.0", "end")
            dialog._block_patterns_text.insert("1.0", "*.mpr cycle")
            dialog._commit()
        finally:
            root.destroy()
        assert not captured
        assert errors and "Block patterns" in str(errors[0])


class TestSettingsCoverage:
    def test_state_db_is_editable(self, config_file: Path, tmp_path: Path) -> None:
        """`state_db` previously had no UI at all."""
        import yaml

        root, dialog, _ = TestSettingsDialog._dialog(config_file)
        try:
            dialog._state_db_var.set(str(tmp_path / "moved.db"))
            dialog._save()
        finally:
            root.destroy()

        after = yaml.safe_load(config_file.read_text())
        assert after["state_db"] == str(tmp_path / "moved.db")

    def test_editing_a_path_writes_through_to_yaml(
        self, rich_config_file: Path
    ) -> None:
        """Round-trip: edit via the path editor, save, re-read."""
        import yaml

        root, dialog, _ = TestSettingsDialog._dialog(rich_config_file)
        try:
            row = dialog._path_rows[0]
            updated = dict(row["original"])
            updated["item_type"] = "samples"
            updated["scan"] = dict(updated.get("scan", {}), hot_interval=11)
            dialog._apply_path_edit(row, updated)
            dialog._save()
        finally:
            root.destroy()

        after = yaml.safe_load(rich_config_file.read_text())
        assert after["watched_paths"][0]["item_type"] == "samples"
        assert after["watched_paths"][0]["scan"]["hot_interval"] == 11
