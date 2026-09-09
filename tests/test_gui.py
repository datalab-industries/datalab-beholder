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
