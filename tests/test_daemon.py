"""Tests for the daemon loop."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from datalab_beholder.config import BeholderConfig
from datalab_beholder.daemon import BeholderDaemon
from tests.conftest import MockTransport, _make_beholder_client


class TestBeholderDaemon:
    def _make_config(self, tmp_path: Path, tmp_tree: Path) -> BeholderConfig:
        return BeholderConfig(
            datalabs=[
                {
                    "name": "test",
                    "url": "https://test.example.org",
                    "api_key": "test-key",
                }
            ],
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
        daemon._running = False

        from datalab_beholder.state import StateStore

        daemon._state = StateStore(config.state_db)
        client = _make_beholder_client(mock_transport, monkeypatch)
        daemon._clients = {d.name: client for d in config.datalabs}
        daemon._clients_by_wp = {wp.name: client for wp in config.watched_paths}
        daemon._daemon_id = daemon._build_daemon_id()
        daemon._watcher = None

        # New status attributes added by the tick/setup refactor
        daemon.last_scan_time = None
        daemon.last_push_time = None
        daemon.pending_count = 0
        daemon.sync_status = "idle"

        return daemon

    def test_daemon_creates(self, tmp_path: Path, tmp_tree: Path, monkeypatch) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)
        assert daemon._daemon_id == "test-data"

    def test_initial_scan(self, tmp_path: Path, tmp_tree: Path, monkeypatch) -> None:
        """Initial scan should perform a full scan and push metadata."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        daemon._initial_scan()

        # Should have made a metadata push
        assert len(transport.requests) == 1

    def test_initial_scan_handles_missing_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Initial scan should handle watched paths that don't exist."""
        config = BeholderConfig(
            datalabs=[
                {
                    "name": "test",
                    "url": "https://test.example.org",
                    "api_key": "test-key",
                }
            ],
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
        daemon._initial_scan()

    def test_push_pending_changes(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Push loop should send accumulated state changes to the server."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        # Seed state via initial scan (consumes one push request)
        daemon._initial_scan()
        transport.requests.clear()

        # Simulate watcher adding a new entry
        from datalab_beholder.scanner import FileEntry

        daemon._state.upsert_entries(
            "test-data",
            [
                FileEntry(
                    path="watcher_new.csv", size=42, modified=9999.0, is_directory=False
                ),
            ],
        )

        daemon._push_pending_changes()

        # Should have pushed the new watcher entry
        assert len(transport.requests) == 1

    def test_push_pending_no_changes(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Push loop should skip push when there are no pending changes."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        # Seed + sync (initial scan pushes and marks synced)
        daemon._initial_scan()
        transport.requests.clear()

        # Push loop should find nothing pending
        daemon._push_pending_changes()
        assert len(transport.requests) == 0

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

    def test_no_pending_after_initial_scan(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """After a successful initial scan, all entries should be marked as synced."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        daemon._initial_scan()
        assert len(transport.requests) == 1

        # After successful sync, no pending changes should remain
        pending = daemon._state.get_pending_changes("test-data")
        assert len(pending) == 0

    def test_setup_runs_initial_scan_and_starts_watcher(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """setup() should scan, start the watcher, and set last_scan_time."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        daemon.setup()

        assert daemon.last_scan_time is not None
        assert daemon._watcher is not None
        assert daemon._running is True
        assert len(transport.requests) == 1

        # Clean up the watcher
        daemon.shutdown()

    def test_tick_can_be_called_independently(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """tick() should execute one loop iteration without errors."""
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

        daemon.setup()
        transport.requests.clear()

        # Calling tick should not raise
        daemon.tick()

        assert daemon.pending_count == 0
        assert daemon.sync_status == "idle"

        daemon.shutdown()

    def test_tick_pushes_when_interval_elapsed(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """tick() should push changes when the metadata interval has elapsed."""
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

        daemon.setup()
        transport.requests.clear()

        # Add a pending entry
        from datalab_beholder.scanner import FileEntry

        daemon._state.upsert_entries(
            "test-data",
            [
                FileEntry(path="new.csv", size=10, modified=9999.0, is_directory=False),
            ],
        )

        # Force the push interval to have elapsed
        daemon._last_push_mono = time.monotonic() - config.sync.metadata_interval - 1

        daemon.tick()

        # Should have pushed
        push_requests = [r for r in transport.requests if r.method == "POST"]
        assert len(push_requests) >= 1
        assert daemon.last_push_time is not None

        daemon.shutdown()

    def test_status_properties_after_setup(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """After setup, config and client should be accessible."""
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        assert daemon.config is config
        assert daemon.clients is daemon._clients

        daemon.setup()
        daemon.shutdown()


class TestMultiDatalabRouting:
    def test_clients_by_wp_routes_per_watched_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from datalab_beholder.config import BeholderConfig
        from datalab_beholder.daemon import BeholderDaemon

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()

        config = BeholderConfig(
            datalabs=[
                {"name": "north", "url": "https://north.example.org", "api_key": "k1"},
                {"name": "south", "url": "https://south.example.org", "api_key": "k2"},
            ],
            watched_paths=[
                {"path": str(tmp_path / "a"), "name": "wp-a", "datalab": "north"},
                {"path": str(tmp_path / "b"), "name": "wp-b", "datalab": "south"},
            ],
            state_db=tmp_path / "state.db",
        )

        # Stub BeholderClient so __init__ doesn't try to connect.
        from datalab_beholder import client as client_module

        constructed: list[str] = []

        class FakeClient:
            def __init__(self, datalab_api_url: str, log_level: str) -> None:
                self.datalab_api_url = datalab_api_url
                constructed.append(datalab_api_url)

        monkeypatch.setattr(client_module, "BeholderClient", FakeClient)
        # daemon.py imported the symbol directly, patch there too
        from datalab_beholder import daemon as daemon_module

        monkeypatch.setattr(daemon_module, "BeholderClient", FakeClient)

        daemon = BeholderDaemon(config)

        assert set(daemon._clients) == {"north", "south"}
        assert (
            daemon._clients_by_wp["wp-a"].datalab_api_url == "https://north.example.org"
        )
        assert (
            daemon._clients_by_wp["wp-b"].datalab_api_url == "https://south.example.org"
        )
        # Each datalab built exactly once.
        assert sorted(constructed) == [
            "https://north.example.org",
            "https://south.example.org",
        ]

    def test_attach_runs_after_push_in_tick(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Attach hook must fire after metadata push, both inside the same tick window."""
        from unittest.mock import MagicMock

        helper = TestBeholderDaemon()
        config = helper._make_config(tmp_path, tmp_tree)
        transport = MockTransport()
        daemon = helper._make_daemon(config, transport, monkeypatch)
        daemon._watcher = None
        daemon._last_push_mono = 0.0
        daemon._last_poll_mono = float("inf")  # skip poll branch

        order: list[str] = []
        daemon._push_pending_changes = MagicMock(
            side_effect=lambda: order.append("push")
        )
        daemon._attach_matched_files = MagicMock(
            side_effect=lambda: order.append("attach")
        )

        daemon.tick()

        assert order == ["push", "attach"]
