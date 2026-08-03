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
            sync={"metadata_interval": 1},
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
        daemon.last_attach_time = None
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
        assert daemon._last_attach_mono is not None
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
        # for the next attach pass.
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
        kind = daemon._select_scan_tier(wp, time.time())
        assert kind == "warm"

    def test_daemon_stop(self, tmp_path: Path, tmp_tree: Path, monkeypatch) -> None:
        """Daemon should stop cleanly when stop() is called."""
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        thread = threading.Thread(target=daemon.start)
        thread.start()
        time.sleep(0.5)
        daemon.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_status_properties(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = self._make_config(tmp_path, tmp_tree)
        daemon = self._make_daemon(config, transport, monkeypatch)

        assert daemon.config is config
        assert daemon.clients is daemon._clients


# --------------------------------------------------------------------------
# End-to-end attach flow (mock transport)
# --------------------------------------------------------------------------


def _attach_tree(tmp_path: Path) -> Path:
    """Build a tree where one file matches an `item_id` regex.

    Layout:
        data/
        ├── 42-cell-formation.mpr   ← matches `(?P<item_id>\\d+)-...`
        └── notes.txt               ← no match, ignored
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / "42-cell-formation.mpr").write_bytes(b"\x00" * 16)
    (data / "notes.txt").write_text("ignore me")
    return data


def _attach_config(tmp_path: Path, root: Path) -> BeholderConfig:
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
                "path": str(root),
                "name": "cells",
                "item_type": "cells",
                "include_patterns": ["*.mpr"],
                "id_patterns": [r"^(?P<item_id>[0-9]+)-.*\.mpr$"],
                "scan": {
                    "hot_interval": 0,
                    "warm_interval": 0,
                    "cold_interval": 0,
                },
            }
        ],
        sync={"metadata_interval": 0},
        state_db=tmp_path / "state.db",
    )


class TestE2EAttachFlow:
    """Drive a tick-based scan → attach pipeline with a mock transport."""

    def _make_daemon(self, config, transport, monkeypatch) -> BeholderDaemon:
        helper = TestBeholderDaemon()
        return helper._make_daemon(config, transport, monkeypatch)

    def test_new_file_creates_item_and_uploads(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """item doesn't exist yet → create_item then upload-file (no replace)."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        transport = MockTransport()

        # First get-item returns 404, then after create it returns the item.
        transport.add_response(
            "GET", "/get-item-data/42", status_code=404, json_data={}
        )
        transport.add_response(
            "POST",
            "/new-sample/",
            status_code=201,
            json_data={"sample_list_entry": {"item_id": "42"}},
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f1"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        # Cold scan + attach pass run in the same tick because both
        # intervals are 0.
        daemon.tick()

        methods = [(r.method, r.url.path) for r in transport.requests]
        # We expect a get_item probe, the create-item POST, then
        # (optionally) a re-fetch, then the upload.
        assert ("POST", "/new-sample/") in methods
        upload_calls = [m for m in methods if m == ("POST", "/upload-file/")]
        assert len(upload_calls) == 1

        # `get_pending_changes` returns only un-synced rows, so a successful
        # attach should make the matched file disappear from that view.
        pending = daemon._state.get_pending_changes("cells")
        assert not any(e.path == "42-cell-formation.mpr" for e in pending)

    def test_existing_item_with_matching_filename_replaces(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """item exists and already has a file of the same name →
        upload-file is sent with replace_file set to that file's id."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {},
                    "display_order": [],
                    "files": [
                        {"name": "42-cell-formation.mpr", "immutable_id": "old-id"},
                        {"name": "other.mpr", "immutable_id": "irrelevant"},
                    ],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f2"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        upload_reqs = [
            r
            for r in transport.requests
            if r.method == "POST" and r.url.path == "/upload-file/"
        ]
        assert len(upload_reqs) == 1
        # multipart body: replace_file appears as a form field.
        body = upload_reqs[0].content
        assert b"old-id" in body
        # No create-item happened because the item already exists.
        assert not any(
            r.method == "POST" and r.url.path == "/new-sample/"
            for r in transport.requests
        )

    def test_existing_item_no_matching_filename_uploads_as_new(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """item exists but has no file with that basename → upload as new
        (replace_file is empty)."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {},
                    "display_order": [],
                    "files": [
                        {"name": "different.mpr", "immutable_id": "other-id"},
                    ],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f3"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        upload_reqs = [
            r
            for r in transport.requests
            if r.method == "POST" and r.url.path == "/upload-file/"
        ]
        assert len(upload_reqs) == 1
        body = upload_reqs[0].content
        assert b"other-id" not in body

    def test_block_pattern_match_creates_block(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A file matching block_patterns gets a block created for it once
        it has been attached, when the item has no block of that type."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        config.watched_paths[0].block_patterns = {"*.mpr": "cycle"}
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {},
                    "display_order": [],
                    "files": [],
                    "file_ObjectIds": ["file-xyz"],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "file-xyz"},
        )
        transport.add_response(
            "POST",
            "/add-data-block/",
            status_code=200,
            json_data={"new_block_obj": {"blocktype": "cycle"}},
        )
        transport.add_response(
            "POST",
            "/update-block/",
            status_code=200,
            json_data={"new_block_data": {"blocktype": "cycle", "file_id": "file-xyz"}},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        methods = [(r.method, r.url.path) for r in transport.requests]
        assert ("POST", "/add-data-block/") in methods
        add_block_req = next(
            r
            for r in transport.requests
            if r.method == "POST" and r.url.path == "/add-data-block/"
        )
        assert b'"block_type":"cycle"' in add_block_req.content

    def test_block_pattern_skipped_when_block_already_wired_to_this_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If the item already has a block of the matched type wired to
        this exact file (e.g. a modified file re-attached with the same
        immutable id), no new block is created."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        config.watched_paths[0].block_patterns = {"*.mpr": "cycle"}
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {
                        "block-1": {"blocktype": "cycle", "file_id": "file-xyz"}
                    },
                    "display_order": ["block-1"],
                    "files": [],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "file-xyz"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        methods = [(r.method, r.url.path) for r in transport.requests]
        assert ("POST", "/add-data-block/") not in methods

    def test_block_pattern_creates_new_block_for_different_file_same_type(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A block of the matched type already exists, but wired to a
        *different* file — this file still gets its own new block."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        config.watched_paths[0].block_patterns = {"*.mpr": "cycle"}
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {
                        "block-1": {"blocktype": "cycle", "file_id": "some-other-file"}
                    },
                    "display_order": ["block-1"],
                    "files": [],
                    "file_ObjectIds": ["file-xyz"],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "file-xyz"},
        )
        transport.add_response(
            "POST",
            "/add-data-block/",
            status_code=200,
            json_data={"new_block_obj": {"blocktype": "cycle"}},
        )
        transport.add_response(
            "POST",
            "/update-block/",
            status_code=200,
            json_data={"new_block_data": {"blocktype": "cycle", "file_id": "file-xyz"}},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        methods = [(r.method, r.url.path) for r in transport.requests]
        assert ("POST", "/add-data-block/") in methods

    def test_no_block_pattern_configured_skips_block_creation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Default (empty) block_patterns never triggers block creation."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {},
                    "display_order": [],
                    "files": [],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "file-xyz"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        methods = [(r.method, r.url.path) for r in transport.requests]
        assert ("POST", "/add-data-block/") not in methods

    def test_unmatched_file_is_not_uploaded(self, tmp_path: Path, monkeypatch) -> None:
        """`notes.txt` doesn't satisfy the include_patterns nor the
        id_pattern; it must never reach the server."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {},
                    "display_order": [],
                    "files": [],
                }
            },
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f1"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        upload_bodies = [
            r.content
            for r in transport.requests
            if r.method == "POST" and r.url.path == "/upload-file/"
        ]
        assert len(upload_bodies) == 1
        assert b"notes.txt" not in upload_bodies[0]

    def test_group_id_forwarded_to_create_item(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A `group_id` capture group should be passed through to the
        item-creation payload so the upstream client (once patched) can
        apply group-level access control."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "P042" / "7-cycle.mpr").parent.mkdir(parents=True)
        (data / "P042" / "7-cycle.mpr").write_bytes(b"\x00" * 8)

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
                    "path": str(data),
                    "name": "cells",
                    "item_type": "cells",
                    "include_patterns": ["*.mpr"],
                    "id_patterns": [
                        r"^(?P<group_id>P[0-9]+)/(?P<item_id>[0-9]+)-.*\.mpr$"
                    ],
                    "scan": {
                        "hot_interval": 0,
                        "warm_interval": 0,
                        "cold_interval": 0,
                    },
                }
            ],
            sync={"metadata_interval": 0},
            state_db=tmp_path / "state.db",
        )

        transport = MockTransport()
        transport.add_response("GET", "/get-item-data/7", status_code=404, json_data={})
        transport.add_response(
            "POST",
            "/new-sample/",
            status_code=201,
            json_data={"sample_list_entry": {"item_id": "7"}},
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f1"},
        )

        # Spy: capture the kwargs the daemon hands to `create_item`.
        from datalab_api import DatalabClient

        captured: list[dict] = []

        def fake_create(self, **kwargs):
            captured.append(kwargs)
            return {"item_id": kwargs.get("item_id"), "files": []}

        monkeypatch.setattr(DatalabClient, "create_item", fake_create)

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        assert len(captured) == 1
        assert captured[0]["item_id"] == "7"
        assert captured[0]["item_type"] == "cells"
        assert captured[0]["group_ids"] == ["P042"]

    def test_item_id_template_constructs_id_from_capture_groups(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`item_id_template` joins capture groups before talking to the
        server. With ``{group_id}-{item_id}``, the regex pulls
        ``group_id=P042`` and ``item_id=7`` and the daemon must address
        the item as ``P042-7``."""
        data = tmp_path / "data"
        (data / "P042").mkdir(parents=True)
        (data / "P042" / "7-cycle.mpr").write_bytes(b"\x00" * 8)

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
                    "path": str(data),
                    "name": "cells",
                    "item_type": "cells",
                    "include_patterns": ["*.mpr"],
                    "id_patterns": [
                        r"^(?P<group_id>P[0-9]+)/(?P<item_id>[0-9]+)-.*\.mpr$"
                    ],
                    "item_id_template": "{group_id}-{item_id}",
                    "scan": {
                        "hot_interval": 0,
                        "warm_interval": 0,
                        "cold_interval": 0,
                    },
                }
            ],
            sync={"metadata_interval": 0},
            state_db=tmp_path / "state.db",
        )

        transport = MockTransport()
        transport.add_response(
            "GET", "/get-item-data/P042-7", status_code=404, json_data={}
        )
        transport.add_response(
            "POST",
            "/new-sample/",
            status_code=201,
            json_data={"sample_list_entry": {"item_id": "P042-7"}},
        )
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f1"},
        )

        from datalab_api import DatalabClient

        captured: list[dict] = []

        def fake_create(self, **kwargs):
            captured.append(kwargs)
            return {"item_id": kwargs.get("item_id"), "files": []}

        monkeypatch.setattr(DatalabClient, "create_item", fake_create)

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        # The probe must address the templated id, not the raw capture.
        assert ("GET", "/get-item-data/P042-7") in [
            (r.method, r.url.path) for r in transport.requests
        ]
        assert not any(r.url.path == "/get-item-data/7" for r in transport.requests)
        assert len(captured) == 1
        assert captured[0]["item_id"] == "P042-7"

    def test_collection_id_template_forwarded_to_create_item(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`collection_id_template` builds a collection id from capture
        groups and is forwarded into ``create_item`` as ``collection_ids``."""
        data = tmp_path / "data"
        (data / "P042").mkdir(parents=True)
        (data / "P042" / "9-cycle.mpr").write_bytes(b"\x00" * 8)

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
                    "path": str(data),
                    "name": "cells",
                    "item_type": "cells",
                    "include_patterns": ["*.mpr"],
                    "id_patterns": [
                        r"^(?P<group_id>P[0-9]+)/(?P<item_id>[0-9]+)-.*\.mpr$"
                    ],
                    "collection_id_template": "group-{group_id}",
                    "scan": {
                        "hot_interval": 0,
                        "warm_interval": 0,
                        "cold_interval": 0,
                    },
                }
            ],
            sync={"metadata_interval": 0},
            state_db=tmp_path / "state.db",
        )

        transport = MockTransport()
        transport.add_response("GET", "/get-item-data/9", status_code=404, json_data={})
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "f1"},
        )

        from datalab_api import DatalabClient

        captured: list[dict] = []

        def fake_create(self, **kwargs):
            captured.append(kwargs)
            return {"item_id": kwargs.get("item_id"), "files": []}

        monkeypatch.setattr(DatalabClient, "create_item", fake_create)

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        assert len(captured) == 1
        assert captured[0]["collection_ids"] == ["group-P042"]

    def test_failed_upload_leaves_entry_unsynced_for_retry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Server 500 on upload → state row stays `new`, next tick retries."""
        root = _attach_tree(tmp_path)
        config = _attach_config(tmp_path, root)
        transport = MockTransport()

        transport.add_response(
            "GET",
            "/get-item-data/42",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "42",
                    "blocks_obj": {},
                    "display_order": [],
                    "files": [],
                }
            },
        )
        # Single registered response: 500. Both tick() upload attempts
        # will hit it, no retry-state needed.
        transport.add_response(
            "POST",
            "/upload-file/",
            status_code=500,
            json_data={"error": "boom"},
        )

        daemon = self._make_daemon(config, transport, monkeypatch)
        daemon.setup()
        daemon.tick()

        pending_after_first = daemon._state.get_pending_changes("cells")
        assert any(
            e.path == "42-cell-formation.mpr" and e.status != "synced"
            for e in pending_after_first
        )

        # Force the attach interval to elapse again and retry.
        daemon._last_attach_mono = time.monotonic() - 10
        upload_count_before = sum(
            1
            for r in transport.requests
            if r.method == "POST" and r.url.path == "/upload-file/"
        )
        daemon.tick()
        upload_count_after = sum(
            1
            for r in transport.requests
            if r.method == "POST" and r.url.path == "/upload-file/"
        )
        # A retry happened.
        assert upload_count_after > upload_count_before


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
