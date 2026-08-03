"""Tests for the API client."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import _make_beholder_client


class TestBeholderClient:
    def test_check_connection_reachable_and_authed(
        self, mock_transport, monkeypatch
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        # _make_beholder_client monkeypatches get_info to succeed and sets a
        # real-looking key, so both flags should come back True.
        reachable, authed = client.check_connection()
        assert reachable is True
        assert authed is True

    def test_check_connection_unreachable(self, mock_transport, monkeypatch) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)

        def boom(self):
            raise RuntimeError("server down")

        from datalab_beholder.client import BeholderClient

        monkeypatch.setattr(BeholderClient, "get_info", boom)
        reachable, authed = client.check_connection()
        assert reachable is False
        assert authed is False

    def test_attach_file_success(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        # Parent's upload_file POSTs to /upload-file/ with status 201.
        mock_transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "file-xyz"},
        )

        test_file = tmp_path / "test.csv"
        test_file.write_text("a,b,c\n1,2,3\n")

        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.attach_file(item_id="item-1", file_path=test_file)

        assert result is not None
        assert result.get("file_id") == "file-xyz"
        assert client.last_request_ok is True

    def test_attach_file_missing_returns_none(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.attach_file(
            item_id="item-1", file_path=tmp_path / "nonexistent.csv"
        )
        assert result is None
        assert client.last_request_ok is False

    def test_attach_file_server_error_returns_none(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        mock_transport.add_response(
            "POST",
            "/upload-file/",
            status_code=500,
        )

        test_file = tmp_path / "test.csv"
        test_file.write_text("data")

        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.attach_file(item_id="item-1", file_path=test_file)
        assert result is None
        assert client.last_request_ok is False

    def test_per_client_api_keys_dont_leak_across_instances(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Regression: ``BaseDatalabClient`` declares ``_headers = {}`` at
        class level, so without ``BeholderClient.__init__`` shadowing it
        per-instance, the second client's API key would clobber the
        first one's. Construct two clients with different keys and
        verify each sends its own."""
        import httpx

        from tests.conftest import MockTransport

        from datalab_api._base import BaseDatalabClient
        from datalab_beholder.client import BeholderClient

        monkeypatch.setattr(BaseDatalabClient, "_detect_api_url", lambda self: None)

        def mock_get_info(self):
            self.info = {
                "attributes": {
                    "available_api_versions": ["0.1.0"],
                    "server_version": "0.1.0",
                }
            }
            return self.info

        monkeypatch.setattr(BeholderClient, "get_info", mock_get_info)
        monkeypatch.setattr(
            BeholderClient,
            "get_block_info",
            lambda self: setattr(self, "block_info", []) or [],  # type: ignore
        )

        # Set the key, build client A, then change the key and build B —
        # this is exactly what `_build_clients` does for multi-datalab.
        monkeypatch.setenv("DATALAB_API_KEY", "key-A")
        client_a = BeholderClient(datalab_api_url="https://a.example.org")
        monkeypatch.setenv("DATALAB_API_KEY", "key-B")
        client_b = BeholderClient(datalab_api_url="https://b.example.org")

        transport_a = MockTransport()
        transport_a.add_response("POST", "/upload-file/", status_code=201, json_data={})
        transport_b = MockTransport()
        transport_b.add_response("POST", "/upload-file/", status_code=201, json_data={})

        client_a._session = httpx.Client(
            transport=transport_a, headers=client_a.headers, timeout=client_a.timeout
        )
        client_b._session = httpx.Client(
            transport=transport_b, headers=client_b.headers, timeout=client_b.timeout
        )

        f = tmp_path / "x.csv"
        f.write_text("data")
        client_a.attach_file(item_id="i", file_path=f)
        client_b.attach_file(item_id="i", file_path=f)

        assert transport_a.requests[0].headers["DATALAB-API-KEY"] == "key-A"
        assert transport_b.requests[0].headers["DATALAB-API-KEY"] == "key-B"

    def test_find_existing_file_id_matches_secured_name_via_original(
        self, mock_transport, monkeypatch
    ) -> None:
        """Regression: server stores ``name = secure_filename(...)`` which
        replaces spaces with underscores, while the on-disk basename keeps
        them. The unmangled value lives in ``original_name`` — match
        against that first so files with spaces don't duplicate on every
        attach pass."""
        client = _make_beholder_client(mock_transport, monkeypatch)
        item = {
            "files": [
                {
                    "name": "P036-CEL-017-PACC_1_Na-ion_half_cell_100_cycles.ndax",
                    "original_name": "P036-CEL-017-PACC_1_Na-ion half cell_100 cycles.ndax",
                    "immutable_id": "abc123",
                },
            ],
        }
        match = client.find_existing_file_id(
            item, "P036-CEL-017-PACC_1_Na-ion half cell_100 cycles.ndax"
        )
        assert match == "abc123"

    def test_find_existing_file_id_case_insensitive(
        self, mock_transport, monkeypatch
    ) -> None:
        """Windows filesystems are case-preserving but case-insensitive,
        so the same file can surface as ``Foo.mpr`` or ``foo.mpr``
        across runs. Match should ignore case."""
        client = _make_beholder_client(mock_transport, monkeypatch)
        item = {"files": [{"name": "Foo.MPR", "immutable_id": "id-1"}]}
        assert client.find_existing_file_id(item, "foo.mpr") == "id-1"

    def test_find_existing_file_id_no_match_returns_none(
        self, mock_transport, monkeypatch
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        item = {
            "files": [
                {
                    "name": "other.mpr",
                    "original_name": "other.mpr",
                    "immutable_id": "id-1",
                },
            ],
        }
        assert client.find_existing_file_id(item, "wanted.mpr") is None

    def test_auth_header_sent(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        mock_transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "ok"},
        )
        test_file = tmp_path / "test.csv"
        test_file.write_text("data")

        client = _make_beholder_client(mock_transport, monkeypatch)
        client.attach_file(item_id="item-1", file_path=test_file)

        req = mock_transport.requests[0]
        assert req.headers["DATALAB-API-KEY"] == "test-key"

    def test_find_block_for_file_empty_when_no_blocks_obj(
        self, mock_transport, monkeypatch
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        assert client.find_block_for_file({}, "cycle", "file-xyz") is None

    def test_find_block_for_file_matches_type_and_file_id(
        self, mock_transport, monkeypatch
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        item = {
            "blocks_obj": {
                "block-1": {"blocktype": "cycle", "file_id": "file-xyz"},
                "block-2": {"blocktype": "raman", "file_id": "file-abc"},
            }
        }
        assert client.find_block_for_file(item, "cycle", "file-xyz") == "block-1"

    def test_find_block_for_file_same_type_different_file_is_no_match(
        self, mock_transport, monkeypatch
    ) -> None:
        """A block of the right type but wired to a different file must
        not be treated as a match — each file gets its own block."""
        client = _make_beholder_client(mock_transport, monkeypatch)
        item = {
            "blocks_obj": {
                "block-1": {"blocktype": "cycle", "file_id": "file-other"},
            }
        }
        assert client.find_block_for_file(item, "cycle", "file-xyz") is None

    def test_create_block_posts_to_add_data_block(
        self, mock_transport, monkeypatch
    ) -> None:
        mock_transport.add_response(
            "POST",
            "/add-data-block/",
            status_code=200,
            json_data={"new_block_obj": {"blocktype": "cycle"}},
        )
        mock_transport.add_response(
            "GET",
            "/get-item-data/item-1",
            status_code=200,
            json_data={
                "item_data": {
                    "item_id": "item-1",
                    "blocks_obj": {},
                    "display_order": [],
                    "file_ObjectIds": ["file-xyz"],
                }
            },
        )
        mock_transport.add_response(
            "POST",
            "/update-block/",
            status_code=200,
            json_data={"new_block_data": {"blocktype": "cycle", "file_id": "file-xyz"}},
        )

        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.create_block(
            item_id="item-1", block_type="cycle", file_id="file-xyz"
        )

        assert result is not None
        assert result.get("blocktype") == "cycle"
        methods = [(r.method, r.url.path) for r in mock_transport.requests]
        assert ("POST", "/add-data-block/") in methods

    def test_create_block_error_returns_none(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/add-data-block/",
            status_code=500,
            json_data={"error": "boom"},
        )

        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.create_block(
            item_id="item-1", block_type="cycle", file_id="file-xyz"
        )
        assert result is None
