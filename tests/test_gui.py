"""Tests for the GUI module.

These tests verify that the GUI classes can be instantiated and destroyed
without error. They require a display (real or virtual) — if unavailable
they are skipped.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from datalab_beholder.config import BeholderConfig

# Skip entire module if no display is available
try:
    _root = tk.Tk()
    _root.destroy()
    HAS_DISPLAY = True
except tk.TclError:
    HAS_DISPLAY = False

pytestmark = pytest.mark.skipif(not HAS_DISPLAY, reason="No display available")


@pytest.fixture
def config_file(tmp_path: Path, tmp_tree: Path) -> Path:
    """Write a minimal config YAML and return its path."""
    import yaml

    config_dict = {
        "datalab": {
            "url": "https://test.example.org",
            "api_key": "test-key",
        },
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

        app = BeholderGUI(config_file)
        # Verify window exists
        assert app.winfo_exists()
        app.destroy()
