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
                    # short hot_window so tests using time-based gating work
                    "scan": {
                        "hot_interval": 0,
                        "warm_interval": 0,
                        "cold_interval": 0,
                    },
                }
            ],
            sync={"metadata_interval": 1, "file_request_poll": 1},
            state_db=tmp_path / "state.db",
        )

    def _make_daemon(self, config, mock_transport, monkeypatch):
        """Create a daemon with a mocked client (bypasses __init__'s
        BeholderClient construction to avoid live network calls)."""
        daemon = BeholderDaemon.__new__(BeholderDaemon)
        daemon._config = config
        daemon._running = False

        from datalab_beholder.state import StateStore

        daemon._state = StateStore(config.state_db)
        for wp in config.watched_paths:
            daemon._state.register_watched_path(wp.name)
        client = _make_beholder_client(mock_transport, monkeypatch)
        daemon._clients = {d.name: client for d in config.datalabs}
        daemon._clients_by_wp = {wp.name: client for wp in config.watched_paths}
        daemon._daemon_id = daemon._build_daemon_id()

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

    def test_setup_initialises_timers_without_scanning(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """setup() no longer scans — the first tick does that."""
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        daemon.setup()

        assert daemon._running is True
        assert daemon._last_push_mono is not None
        # No HTTP requests yet — scanning hasn't happened.
        assert transport.requests == []

    def test_first_tick_runs_cold_scan_when_state_empty(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """A fresh registry has NULL timestamps → cold scan fires on first
        tick (cold supersedes warm and hot)."""
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()

        daemon.tick()

        # Cold scan seeded state — there should be pending entries waiting
        # for the next push.
        pending = daemon._state.get_pending_changes("test-data")
        assert len(pending) > 0
        ts = daemon._state.get_scan_timestamps("test-data")
        assert ts.cold is not None
        assert ts.warm == ts.cold
        assert ts.hot == ts.cold

    def test_select_scan_tier_picks_cold_when_overdue(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        wp = config.watched_paths[0]
        kind = daemon._select_scan_tier(wp, time.time())
        # All timestamps NULL → cold wins.
        assert kind == "cold"

    def test_select_scan_tier_returns_none_when_nothing_due(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        wp = config.watched_paths[0]
        # Bump cold to "right now" with a long-enough interval that nothing
        # else is due.
        wp.scan.hot_interval = 10000
        wp.scan.warm_interval = 10000
        wp.scan.cold_interval = 10000
        daemon._state.update_scan_timestamp("test-data", "cold", time.time())

        assert daemon._select_scan_tier(wp, time.time()) is None

    def test_select_scan_tier_skips_cold_when_disabled(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        wp = config.watched_paths[0]
        wp.scan.cold_interval = None
        # All NULL timestamps → falls through cold-disabled, picks warm.
        kind = daemon._select_scan_tier(wp, time.time())
        assert kind == "warm"

    def test_push_pending_changes_pushes_accumulated_state(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success"},
        )
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        from datalab_beholder.scanner import FileEntry

        daemon._state.upsert_entries(
            "test-data",
            [
                FileEntry(
                    path="watcher_new.csv",
                    size=42,
                    modified=9999.0,
                    is_directory=False,
                )
            ],
        )

        daemon._push_pending_changes()

        assert len(transport.requests) == 1

    def test_push_pending_no_changes(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        daemon._push_pending_changes()
        assert transport.requests == []

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

        thread = threading.Thread(target=daemon.start)
        thread.start()
        time.sleep(0.5)
        daemon.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_tick_pushes_when_interval_elapsed(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
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
        # First tick runs the cold scan, which seeds state.
        daemon.tick()
        transport.requests.clear()

        # Force the push interval to have elapsed
        daemon._last_push_mono = time.monotonic() - config.sync.metadata_interval - 1
        daemon.tick()

        push_requests = [r for r in transport.requests if r.method == "POST"]
        assert len(push_requests) >= 1
        assert daemon.last_push_time is not None

    def test_status_properties(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        assert daemon.config is config
        assert daemon.clients is daemon._clients


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
                {
                    "name": "north",
                    "url": "https://north.example.org",
                    "api_key": "k1",
                },
                {
                    "name": "south",
                    "url": "https://south.example.org",
                    "api_key": "k2",
                },
            ],
            watched_paths=[
                {"path": str(tmp_path / "a"), "name": "wp-a", "datalab": "north"},
                {"path": str(tmp_path / "b"), "name": "wp-b", "datalab": "south"},
            ],
            state_db=tmp_path / "state.db",
        )

        from datalab_beholder import client as client_module

        constructed: list[str] = []

        class FakeClient:
            def __init__(self, datalab_api_url: str, log_level: str) -> None:
                self.datalab_api_url = datalab_api_url
                constructed.append(datalab_api_url)

        monkeypatch.setattr(client_module, "BeholderClient", FakeClient)
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
