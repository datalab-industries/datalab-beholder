"""Tests for the daemon loop."""

from __future__ import annotations

import threading
from pathlib import Path

from datalab_beholder.config import BeholderConfig
from datalab_beholder.daemon import BeholderDaemon
from tests.conftest import MockTransport, _make_beholder_client


class TestBeholderDaemon:
    def _make_config(self, tmp_path: Path, tmp_tree: Path) -> BeholderConfig:
        return BeholderConfig(
            datalab={"url": "https://test.example.org", "api_key": "test-key"},
            watched_paths=[
                {
                    "path": str(tmp_tree),
                    "name": "test-data",
                }
            ],
            sync={"metadata_interval": 1, "file_request_poll": 1},
            state_db=tmp_path / "state.db",
        )

    def _make_daemon(self, config, mock_transport, monkeypatch):
        """Create a daemon with a mocked client."""
        daemon = BeholderDaemon.__new__(BeholderDaemon)
        daemon._config = config
        daemon._stop_event = threading.Event()

        from datalab_beholder.state import StateStore

        daemon._state = StateStore(config.state_db)
        daemon._client = _make_beholder_client(mock_transport, monkeypatch)
        daemon._daemon_id = daemon._build_daemon_id()
        daemon._threads = []
        return daemon

    def test_daemon_creates(self, tmp_path: Path, tmp_tree: Path, monkeypatch) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)
        assert daemon._daemon_id == "test-data"

    def test_initial_sync(self, tmp_path: Path, tmp_tree: Path, monkeypatch) -> None:
        """Daemon should perform an initial metadata sync on start."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        # Run just the sync (not the full daemon loop)
        daemon._sync_metadata()

        # Should have made a metadata push
        assert len(transport.requests) == 1

    def test_sync_handles_missing_path(self, tmp_path: Path, monkeypatch) -> None:
        """Daemon should handle watched paths that don't exist."""
        config = BeholderConfig(
            datalab={"url": "https://test.example.org", "api_key": "test-key"},
            watched_paths=[
                {
                    "path": str(tmp_path / "nonexistent"),
                    "name": "missing",
                }
            ],
            state_db=tmp_path / "state.db",
        )
        transport = MockTransport()
        daemon = self._make_daemon(config, transport, monkeypatch)

        # Should not raise
        daemon._sync_metadata()

    def test_file_request_handling(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        transport.add_response(
            "GET",
            "/api/remote-files/pending",
            json_data={
                "requests": [
                    {"request_id": "req-1", "path": "file1.csv"},
                ]
            },
        )
        transport.add_response(
            "POST",
            "/api/remote-files/upload",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        daemon._poll_file_requests()

        # Should have polled + uploaded
        assert len(transport.requests) == 2

    def test_daemon_stop(self, tmp_path: Path, tmp_tree: Path, monkeypatch) -> None:
        """Daemon should stop cleanly when stop() is called."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        transport.add_response(
            "GET",
            "/api/remote-files/pending",
            json_data={"requests": []},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        # Start daemon in background thread
        thread = threading.Thread(target=daemon.start)
        thread.start()

        # Give it a moment to start
        import time

        time.sleep(0.5)

        # Signal stop
        daemon.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_no_changes_no_push(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """If there are no changes, no metadata should be pushed."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        # First sync pushes everything (all new)
        daemon._sync_metadata()
        assert len(transport.requests) == 1

        # Second sync - no changes
        daemon._sync_metadata()
        # Should still be 1 request (no second push)
        assert len(transport.requests) == 1
