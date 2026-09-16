"""The three ``block_mode`` settings, run against an in-memory datalab.

Loads ``config.yaml`` from this directory (three watched paths over the
same ``data/`` folder, one per mode) and drives the real daemon through:

* **Stage 1** — the four sample files: three for ``bmdemo1``, one for
  ``bmdemo2``.
* **Stage 2** — a fourth ``bmdemo1`` file appears.
* **Stage 3** — the state DB is wiped and the daemon restarted, so every
  file looks new again; no duplicate blocks may appear.

The mock covers the datalab HTTP surface the attach + block paths use:
item fetch/create, file upload (with replace), block add and update.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from datalab_beholder.config import load_config
from datalab_beholder.daemon import BeholderDaemon

HERE = Path(__file__).parent


class MockDatalab:
    """In-memory datalab holding items, their files and their blocks."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self._next_file_id = 1
        self._next_block_id = 1

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _new_item(self, item_id: str) -> dict:
        return self.items.setdefault(
            item_id,
            {
                "item_id": item_id,
                "blocks_obj": {},
                "display_order": [],
                "files": [],
                "file_ObjectIds": [],
            },
        )

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
            item_id = body.get("new_sample_data", body)["item_id"]
            self._new_item(item_id)
            return httpx.Response(201, json={"sample_list_entry": {"item_id": item_id}})

        if method == "POST" and path == "/upload-file/":
            return self._handle_upload(request)

        if method == "POST" and path == "/add-data-block/":
            body = json.loads(request.content)
            item = self.items[body["item_id"]]
            block_id = f"block-{self._next_block_id}"
            self._next_block_id += 1
            block = {
                "block_id": block_id,
                "blocktype": body["block_type"],
                "item_id": body["item_id"],
            }
            item["blocks_obj"][block_id] = block
            item["display_order"].append(block_id)
            return httpx.Response(200, json={"new_block_obj": dict(block)})

        if method == "POST" and path == "/update-block/":
            data = json.loads(request.content)["block_data"]
            block = self.items[data["item_id"]]["blocks_obj"][data["block_id"]]
            block.pop("file_id", None)
            block.pop("file_ids", None)
            block.update(data)
            return httpx.Response(200, json={"new_block_data": dict(block)})

        return httpx.Response(404, json={"error": f"unmocked {method} {path}"})

    def _handle_upload(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        item_id = self._field(body, b"item_id")
        replace = self._field(body, b"replace_file")
        filename = self._filename(body)
        if not item_id or not filename:
            return httpx.Response(400, json={"error": "missing fields"})

        item = self._new_item(item_id)
        if replace:
            for f in item["files"]:
                if f["immutable_id"] == replace:
                    return httpx.Response(201, json={"file_id": replace})

        file_id = f"file-{self._next_file_id}"
        self._next_file_id += 1
        item["files"].append({"name": filename, "immutable_id": file_id})
        item["file_ObjectIds"].append(file_id)
        return httpx.Response(201, json={"file_id": file_id})

    @staticmethod
    def _field(body: bytes, name: bytes) -> str | None:
        m = re.search(b'name="' + name + b'"\\r\\n\\r\\n([^\\r]+)', body)
        return m.group(1).decode() if m else None

    @staticmethod
    def _filename(body: bytes) -> str | None:
        m = re.search(b'filename="([^"]+)"', body)
        return m.group(1).decode() if m else None

    def block_files(self, item_id: str) -> list[set[str]]:
        """Per block on ``item_id``, the names of the files wired to it."""
        item = self.items[item_id]
        names = {f["immutable_id"]: f["name"] for f in item["files"]}
        result = []
        for block in item["blocks_obj"].values():
            ids = block.get("file_ids") or [block["file_id"]]
            result.append({names[i] for i in ids})
        return result


def _patch_client_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _write_config(tmp_path: Path) -> Path:
    """Copy the example config into ``tmp_path``, pointing it at a copy of
    ``data/`` and making every scan and attach run on each tick. The mode
    settings themselves are used as-is."""
    tree = tmp_path / "data"
    shutil.copytree(HERE / "data", tree)

    src = yaml.safe_load((HERE / "config.yaml").read_text())
    for wp in src["watched_paths"]:
        wp["path"] = str(tree)
        wp["scan"] = {"hot_interval": 0, "warm_interval": 0, "cold_interval": 0}
    src["sync"]["metadata_interval"] = 0
    src["state_db"] = str(tmp_path / "state.db")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(src))
    return config_path


def _start_daemon(config_path: Path, mock: MockDatalab) -> BeholderDaemon:
    daemon = BeholderDaemon(load_config(config_path))
    for client in daemon.clients.values():
        client._session = httpx.Client(
            transport=mock.transport,
            headers=client.headers,
            timeout=client.timeout,
        )
    daemon.setup()
    return daemon


def _sorted(blocks: list[set[str]]) -> list[list[str]]:
    return sorted(sorted(b) for b in blocks)


CYCLES_1_3 = ["bmdemo1-cycle1.csv", "bmdemo1-cycle2.csv", "bmdemo1-cycle3.csv"]
CYCLES_1_4 = [*CYCLES_1_3, "bmdemo1-cycle4.csv"]


def test_block_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client_handshake(monkeypatch)
    config_path = _write_config(tmp_path)
    mock = MockDatalab()
    daemon = _start_daemon(config_path, mock)

    # ── Stage 1: the sample files ─────────────────────────────────────────
    daemon.tick()

    assert set(mock.items) == {
        f"{item}-{mode}"
        for item in ("bmdemo1", "bmdemo2")
        for mode in ("per-file", "per-item", "all-files")
    }
    # per_file: one block per file.
    assert _sorted(mock.block_files("bmdemo1-per-file")) == [[n] for n in CYCLES_1_3]
    # per_item: a single block, holding just one of the files.
    per_item = mock.block_files("bmdemo1-per-item")
    assert len(per_item) == 1 and len(per_item[0]) == 1
    # per_item_all_files: a single block holding all of them.
    assert _sorted(mock.block_files("bmdemo1-all-files")) == [CYCLES_1_3]
    # One file → one single-file block, whatever the mode.
    for mode in ("per-file", "per-item", "all-files"):
        assert mock.block_files(f"bmdemo2-{mode}") == [{"bmdemo2-cycle1.csv"}]

    # ── Stage 2: a new file for bmdemo1 ───────────────────────────────────
    (tmp_path / "data" / "bmdemo1-cycle4.csv").write_text("time_s,voltage_V\n0,3.0\n")
    daemon.tick()

    assert _sorted(mock.block_files("bmdemo1-per-file")) == [[n] for n in CYCLES_1_4]
    assert mock.block_files("bmdemo1-per-item") == per_item
    assert len(mock.items["bmdemo1-per-item"]["files"]) == 4
    assert _sorted(mock.block_files("bmdemo1-all-files")) == [CYCLES_1_4]

    # ── Stage 3: forget local state and restart ───────────────────────────
    daemon.shutdown()
    before = {item_id: mock.block_files(item_id) for item_id in mock.items}
    for db in tmp_path.glob("state.db*"):
        db.unlink()

    daemon = _start_daemon(config_path, mock)
    daemon.tick()

    # Every file is re-uploaded in place; no block is added or changed.
    assert {item_id: mock.block_files(item_id) for item_id in mock.items} == before
    daemon.shutdown()
