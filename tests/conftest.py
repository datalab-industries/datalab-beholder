"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from datalab_beholder.client import BeholderClient
from datalab_beholder.config import BeholderConfig


@pytest.fixture
def tmp_tree(tmp_path: Path) -> Path:
    """Create a sample directory tree for testing.

    Structure:
        data/
        ├── file1.csv
        ├── file2.raw
        ├── notes.txt
        ├── subdir/
        │   ├── file3.csv
        │   └── deep/
        │       └── file4.dat
        └── temp/
            └── scratch.tmp
    """
    data = tmp_path / "data"
    data.mkdir()

    (data / "file1.csv").write_text("a,b,c\n1,2,3\n")
    (data / "file2.raw").write_bytes(b"\x00" * 1024)
    (data / "notes.txt").write_text("some notes")

    subdir = data / "subdir"
    subdir.mkdir()
    (subdir / "file3.csv").write_text("x,y\n4,5\n")

    deep = subdir / "deep"
    deep.mkdir()
    (deep / "file4.dat").write_bytes(b"\xff" * 512)

    temp = data / "temp"
    temp.mkdir()
    (temp / "scratch.tmp").write_text("temporary")

    return data


@pytest.fixture
def sample_config(tmp_path: Path, tmp_tree: Path) -> BeholderConfig:
    """Create a sample BeholderConfig for testing."""
    return BeholderConfig(
        datalabs=[
            {
                "name": "test",
                "url": "https://test.example.org",
                "api_key": "test-key-123",
            }
        ],
        watched_paths=[
            {
                "path": str(tmp_tree),
                "name": "test-data",
                "include_patterns": ["*"],
                "exclude_patterns": [],
            }
        ],
        state_db=tmp_path / "state.db",
    )


class MockTransport(httpx.BaseTransport):
    """A mock httpx transport that returns canned responses."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response] = {}

    def add_response(
        self,
        method: str,
        path: str,
        status_code: int = 200,
        json_data: dict | None = None,
    ) -> None:
        key = f"{method.upper()} {path}"
        body = json.dumps(json_data or {}).encode()
        self.responses[key] = httpx.Response(
            status_code=status_code,
            content=body,
            headers={"content-type": "application/json"},
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Force the (possibly streaming) body to materialise so tests can
        # inspect `request.content` after the fact.
        request.read()
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key in self.responses:
            return self.responses[key]
        return httpx.Response(404, content=b'{"error": "not found"}')


@pytest.fixture
def mock_transport() -> MockTransport:
    return MockTransport()


def _make_beholder_client(mock_transport: MockTransport, monkeypatch) -> BeholderClient:
    """Create a BeholderClient with mocked BaseDatalabClient init handshake.

    Monkeypatches _detect_api_url, get_info, and get_block_info so that
    the parent __init__ doesn't make real network requests.
    """
    from datalab_api._base import BaseDatalabClient

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

    client = BeholderClient(datalab_api_url="https://test.example.org")
    # Replace the session with one that uses our mock transport
    client._session = httpx.Client(
        transport=mock_transport,
        headers=client.headers,
        timeout=client.timeout,
    )
    return client


@pytest.fixture
def beholder_client(mock_transport: MockTransport, monkeypatch) -> BeholderClient:
    """A BeholderClient with mocked init and mock transport for requests."""
    return _make_beholder_client(mock_transport, monkeypatch)
