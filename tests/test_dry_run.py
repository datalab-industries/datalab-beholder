"""Tests for the dry-run simulation (`daemon.dry_run`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from datalab_beholder.config import BeholderConfig, LocalWatchedPath
from datalab_beholder.daemon import dry_run
from datalab_beholder.scanner import scan_directory
from datalab_beholder.state import StateStore
from tests.conftest import MockTransport, _make_beholder_client


def _make_config(tmp_path: Path, tmp_tree: Path, **wp_overrides) -> BeholderConfig:
    wp = {
        "path": str(tmp_tree),
        "name": "test-data",
        "include_patterns": ["*.csv"],
        # Matches file1.csv and subdir/file3.csv from the tmp_tree fixture.
        "id_patterns": [r"(?P<item_id>file[0-9]+)\.csv$"],
        "item_type": "samples",
        "block_patterns": {"*.csv": "tabular"},
    }
    wp.update(wp_overrides)
    return BeholderConfig(
        datalabs=[
            {
                "name": "test",
                "url": "https://test.example.org",
                "api_key": "test-key",
            }
        ],
        watched_paths=[wp],
        state_db=tmp_path / "state.db",
    )


def _clients(mock_transport, monkeypatch):
    client = _make_beholder_client(mock_transport, monkeypatch)
    return {"test": client}, client


def _actions_of(actions, kind):
    return [a for a in actions if a.action == kind]


class TestDryRun:
    def test_fresh_install_reports_creates_and_uploads(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """No state DB, no items on the server: every matched file is new,
        every item would be created, every file uploaded, every matching
        block created."""
        transport = MockTransport()  # 404 for everything → items don't exist
        config = _make_config(tmp_path, tmp_tree)
        clients, _ = _clients(transport, monkeypatch)

        actions = dry_run(config, clients=clients)

        assert {a.item_id for a in _actions_of(actions, "create_item")} == {
            "file1",
            "file3",
        }
        assert {a.path for a in _actions_of(actions, "upload")} == {
            "file1.csv",
            "subdir/file3.csv",
        }
        assert len(_actions_of(actions, "create_block")) == 2
        assert all(a.detail == "tabular" for a in _actions_of(actions, "create_block"))
        assert not _actions_of(actions, "replace")
        assert not _actions_of(actions, "skip")

    def test_dry_run_makes_no_writes(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Only GET requests hit the server, and no state DB is created."""
        transport = MockTransport()
        config = _make_config(tmp_path, tmp_tree)
        clients, _ = _clients(transport, monkeypatch)

        dry_run(config, clients=clients)

        assert not config.state_db.exists()
        assert transport.requests, "expected read-only probes to the server"
        assert all(r.method == "GET" for r in transport.requests)

    def test_existing_file_reported_as_replace(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """An item that already has a same-named attachment and a wired
        block gets a replace action and no block action."""
        transport = MockTransport()
        transport.add_response(
            "GET",
            "/get-item-data/file1",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "file1",
                    "files": [
                        {
                            "name": "file1.csv",
                            "original_name": "file1.csv",
                            "immutable_id": "abc",
                        }
                    ],
                    "blocks_obj": {"b1": {"blocktype": "tabular", "file_id": "abc"}},
                    "display_order": ["b1"],
                }
            },
        )
        config = _make_config(tmp_path, tmp_tree)
        clients, _ = _clients(transport, monkeypatch)

        actions = dry_run(config, clients=clients)

        replaces = _actions_of(actions, "replace")
        assert [(a.item_id, a.path, a.detail) for a in replaces] == [
            ("file1", "file1.csv", "file_id=abc")
        ]
        # file1 already has its block; only file3 (missing item) gets one.
        assert {a.item_id for a in _actions_of(actions, "create_block")} == {"file3"}
        assert {a.item_id for a in _actions_of(actions, "create_item")} == {"file3"}

    def test_synced_state_reports_nothing_pending(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Files already marked synced with unchanged stats produce no
        actions and no server traffic."""
        config = _make_config(tmp_path, tmp_tree)
        wp = config.watched_paths[0]
        assert isinstance(wp, LocalWatchedPath)

        state = StateStore(config.state_db)
        state.register_watched_path(wp.name)
        scan = scan_directory(
            wp.path,
            name=wp.name,
            include_patterns=wp.include_patterns,
            exclude_patterns=wp.exclude_patterns,
            id_patterns=wp.id_patterns,
            max_depth=wp.max_depth,
        )
        state.update_from_scan(scan)
        state.mark_synced(wp.name, [e.path for e in scan.entries])
        state.close()

        transport = MockTransport()
        clients, _ = _clients(transport, monkeypatch)
        actions = dry_run(config, clients=clients)

        assert actions == []
        assert transport.requests == []

    def test_pending_state_still_reported(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Files seeded in state but never synced (status 'new') are
        reported even though their on-disk stats match the DB."""
        config = _make_config(tmp_path, tmp_tree)
        wp = config.watched_paths[0]
        assert isinstance(wp, LocalWatchedPath)

        state = StateStore(config.state_db)
        state.register_watched_path(wp.name)
        scan = scan_directory(
            wp.path,
            name=wp.name,
            include_patterns=wp.include_patterns,
            id_patterns=wp.id_patterns,
            max_depth=wp.max_depth,
        )
        state.update_from_scan(scan)
        state.close()

        transport = MockTransport()
        clients, _ = _clients(transport, monkeypatch)
        actions = dry_run(config, clients=clients)

        assert {a.item_id for a in _actions_of(actions, "create_item")} == {
            "file1",
            "file3",
        }

    def test_unreachable_datalab_logs_but_does_not_raise(
        self, tmp_path: Path, tmp_tree: Path, caplog
    ) -> None:
        """A datalab that can't be reached (absent from the client map)
        degrades to attach_unknown actions plus a loud error log instead
        of crashing the dry run."""
        config = _make_config(tmp_path, tmp_tree)

        with caplog.at_level("ERROR", logger="datalab_beholder.daemon"):
            actions = dry_run(config, clients={})

        unknown = _actions_of(actions, "attach_unknown")
        assert {a.path for a in unknown} == {"file1.csv", "subdir/file3.csv"}
        assert all(a.detail == "server unreachable" for a in unknown)
        assert not _actions_of(actions, "create_item")
        assert any("unreachable" in r.getMessage() for r in caplog.records)

    def test_unscannable_watched_path_is_fatal(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        """Unlike an unreachable datalab, a watched path that can't be
        scanned aborts the dry run — otherwise a mistyped path reads as
        'nothing to do'."""
        config = _make_config(tmp_path, tmp_tree)
        wp = config.watched_paths[0]
        assert isinstance(wp, LocalWatchedPath)
        wp.path = tmp_path / "does-not-exist"

        with pytest.raises(FileNotFoundError, match="cannot be scanned"):
            dry_run(config, clients={})

    def test_missing_item_without_item_type_is_skipped(
        self, tmp_path: Path, tmp_tree: Path, monkeypatch
    ) -> None:
        transport = MockTransport()
        config = _make_config(tmp_path, tmp_tree, item_type=None)
        clients, _ = _clients(transport, monkeypatch)

        actions = dry_run(config, clients=clients)

        skips = _actions_of(actions, "skip")
        assert {a.path for a in skips} == {"file1.csv", "subdir/file3.csv"}
        assert not _actions_of(actions, "create_item")
        assert not _actions_of(actions, "upload")
