"""Three-stage end-to-end attach flow against an in-memory mock datalab.

This is the CI-friendly counterpart to ``run_live.py`` (which talks to a
real datalab on localhost). Drives ``BeholderDaemon`` through the same
sequence of file-tree mutations and verifies the server-side state after
each tick:

* **Stage 1** — two new files in two new items.
* **Stage 2** — modify one existing file (replace-in-place) and add a
  third item.
* **Stage 3** — add a second attachment to the existing item from
  stage 1.

The mock implements just enough of the datalab HTTP surface (item
fetch, item create, file upload with optional replace) to let the real
daemon pipeline run unmodified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from datalab_beholder.config import load_config
from datalab_beholder.daemon import BeholderDaemon

HERE = Path(__file__).parent


class MockDatalab:
    """In-memory datalab. Stores items and synthesises file ids on upload."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self._next_file_id = 1
        self._next_collection_id = 1

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        path = request.url.path
        method = request.method

        if method == "GET" and path.startswith("/get-item-data/"):
            item_id = path.removeprefix("/get-item-data/")
            if item_id in self.items:
                return httpx.Response(200, json={"item_data": self.items[item_id]})
            return httpx.Response(404, json={"error": "not found"})

        if method == "POST" and path == "/new-sample/":
            body = json.loads(request.content)
            new_sample = body.get("new_sample_data", body)
            item_id = new_sample["item_id"]
            self.items[item_id] = {
                "item_id": item_id,
                "blocks_obj": {},
                "display_order": [],
                "files": [],
            }
            return httpx.Response(201, json={"sample_list_entry": {"item_id": item_id}})

        if method == "POST" and path == "/upload-file/":
            return self._handle_upload(request)

        # Collection auto-create flow (when `collection_id_template` is set):
        # the upstream client GETs the collection and PUTs to create it on
        # 404, then GETs again to pick up the immutable_id.
        if method == "GET" and path.startswith("/collections/"):
            cid = path.removeprefix("/collections/")
            if cid in self.collections:
                return httpx.Response(
                    200,
                    json={"data": self.collections[cid], "child_items": []},
                )
            return httpx.Response(404, json={"error": "not found"})

        if method == "PUT" and path == "/collections":
            body = json.loads(request.content)
            cid = body["data"]["collection_id"]
            collection = {
                "collection_id": cid,
                "immutable_id": f"col-{self._next_collection_id}",
                "type": "collections",
            }
            self._next_collection_id += 1
            self.collections[cid] = collection
            return httpx.Response(201, json={"data": collection})

        # Group lookup (only used when `group_id` capture is present).
        # An empty result is fine — the upstream client swallows the failure
        # and proceeds without a group binding.
        if method == "GET" and path == "/search/groups":
            return httpx.Response(200, json={"data": []})

        return httpx.Response(404, json={"error": f"unmocked {method} {path}"})

    def _handle_upload(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        item_id = self._field(body, b"item_id")
        replace = self._field(body, b"replace_file")
        filename = self._filename(body)

        if not item_id or not filename:
            return httpx.Response(400, json={"error": "missing fields"})

        item = self.items.setdefault(
            item_id,
            {
                "item_id": item_id,
                "blocks_obj": {},
                "display_order": [],
                "files": [],
            },
        )

        if replace:
            for f in item["files"]:
                if f.get("immutable_id") == replace:
                    f["name"] = filename
                    return httpx.Response(201, json={"file_id": replace})

        file_id = f"file-{self._next_file_id}"
        self._next_file_id += 1
        item["files"].append({"name": filename, "immutable_id": file_id})
        return httpx.Response(201, json={"file_id": file_id})

    @staticmethod
    def _field(body: bytes, name: bytes) -> str | None:
        m = re.search(b'name="' + name + b'"\\r\\n\\r\\n([^\\r]+)', body)
        return m.group(1).decode() if m else None

    @staticmethod
    def _filename(body: bytes) -> str | None:
        m = re.search(b'filename="([^"]+)"', body)
        return m.group(1).decode() if m else None


def _names(item: dict) -> set[str]:
    return {f["name"] for f in item["files"]}


def _write(tree: Path, rel_path: str, content: bytes) -> None:
    full = tree / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)


def _build_daemon(
    tmp_path: Path, mock: MockDatalab, monkeypatch: pytest.MonkeyPatch
) -> BeholderDaemon:
    """Materialise the example config into ``tmp_path`` and wire its
    client to the mock. The on-disk YAML is the source of truth — we only
    rewrite the path-shaped fields so the test runs in a clean tree."""
    from datalab_api._base import BaseDatalabClient
    from datalab_beholder.client import BeholderClient

    monkeypatch.setattr(BaseDatalabClient, "_detect_api_url", lambda self: None)
    monkeypatch.setattr(
        BeholderClient,
        "get_info",
        lambda self: setattr(  # type: ignore
            self,
            "info",
            {
                "attributes": {
                    "available_api_versions": ["0.1.0"],
                    "server_version": "0.1.0",
                }
            },
        )
        or self.info,
    )
    monkeypatch.setattr(
        BeholderClient,
        "get_block_info",
        lambda self: setattr(self, "block_info", []) or [],  # type: ignore
    )
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")

    src = yaml.safe_load((HERE / "config.yaml").read_text())
    tree = tmp_path / "instrument-pc"
    tree.mkdir()
    src["watched_paths"][0]["path"] = str(tree)
    src["state_db"] = str(tmp_path / "state.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(src))

    config = load_config(config_path)
    daemon = BeholderDaemon(config)
    for client in daemon.clients.values():
        client._session = httpx.Client(
            transport=mock.transport,
            headers=client.headers,
            timeout=client.timeout,
        )
    daemon.setup()
    return daemon


def test_three_stage_attach_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = MockDatalab()
    daemon = _build_daemon(tmp_path, mock, monkeypatch)
    tree = tmp_path / "instrument-pc"

    # ── Stage 1: two new cells, one unmatched note ────────────────────────
    _write(tree, "PROJ-A/beholder-test-001-cycle1.mpr", b"\x00" * 64)
    _write(tree, "PROJ-A/beholder-test-002-formation.mpr", b"\x00" * 128)
    _write(tree, "notes.txt", b"ignore me")

    daemon.tick()

    assert set(mock.items) == {
        "PROJ-A-beholder-test-001",
        "PROJ-A-beholder-test-002",
    }
    assert _names(mock.items["PROJ-A-beholder-test-001"]) == {
        "beholder-test-001-cycle1.mpr"
    }
    assert _names(mock.items["PROJ-A-beholder-test-002"]) == {
        "beholder-test-002-formation.mpr"
    }

    # ── Stage 2: replace cell 001's file in place, create cell 003 ────────
    original_id = mock.items["PROJ-A-beholder-test-001"]["files"][0]["immutable_id"]
    _write(tree, "PROJ-A/beholder-test-001-cycle1.mpr", b"\xff" * 96)
    _write(tree, "PROJ-A/beholder-test-003-rate.mpr", b"\x00" * 32)

    daemon.tick()

    # Same immutable_id ⇒ replaced rather than duplicated.
    cell_001_files = mock.items["PROJ-A-beholder-test-001"]["files"]
    assert len(cell_001_files) == 1
    assert cell_001_files[0]["immutable_id"] == original_id
    assert "PROJ-A-beholder-test-003" in mock.items
    assert _names(mock.items["PROJ-A-beholder-test-003"]) == {
        "beholder-test-003-rate.mpr"
    }

    # ── Stage 3: add a second attachment to cell 001 ──────────────────────
    _write(tree, "PROJ-A/beholder-test-001-cycle2.mpr", b"\x11" * 48)

    daemon.tick()

    assert _names(mock.items["PROJ-A-beholder-test-001"]) == {
        "beholder-test-001-cycle1.mpr",
        "beholder-test-001-cycle2.mpr",
    }

    daemon.shutdown()
