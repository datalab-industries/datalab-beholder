"""Tests for the API client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from datalab_beholder.client import ItemLookupError
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
        # A successful upload POSTs to /upload-file/ and returns 201.
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

    def test_attach_file_not_modified_is_success(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        """A replace upload the server already holds comes back 304 with
        an empty body, which datalab-api >= 0.6 surfaces as a normal
        result carrying ``not_modified``. That is a successful no-op,
        not an error: the existing file id is returned so the caller
        marks it synced."""
        mock_transport.add_response("POST", "/upload-file/", status_code=304)

        test_file = tmp_path / "test.csv"
        test_file.write_text("a,b,c\n1,2,3\n")

        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.attach_file(
            item_id="item-1", file_path=test_file, replace_file_id="old-id"
        )

        assert result is not None
        assert result["not_modified"] is True
        assert result["file_id"] == "old-id"
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

    def test_find_block_of_type_ignores_file_id(
        self, mock_transport, monkeypatch
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        item = {
            "blocks_obj": {
                "b1": {"blocktype": "tabular", "file_id": "other"},
                "b2": {"blocktype": "cycle", "file_id": "other"},
            }
        }
        assert client.find_block_of_type(item, "cycle") == "b2"
        assert client.find_block_of_type(item, "nmr") is None
        assert client.find_block_of_type({}, "cycle") is None

    def test_block_file_ids_covers_both_shapes(
        self, mock_transport, monkeypatch
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)
        assert client.block_file_ids({"file_id": "a"}) == ["a"]
        assert client.block_file_ids({"file_ids": ["a", "b"]}) == ["a", "b"]
        # A block that carries both keys shouldn't report a duplicate.
        assert client.block_file_ids({"file_ids": ["a", "b"], "file_id": "a"}) == [
            "a",
            "b",
        ]
        assert client.block_file_ids({}) == []

    def test_update_block_files_sends_file_ids(
        self, mock_transport, monkeypatch
    ) -> None:
        mock_transport.add_response(
            "POST",
            "/update-block/",
            status_code=200,
            json_data={
                "new_block_data": {"blocktype": "cycle", "file_ids": ["a", "b"]}
            },
        )

        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.update_block_files(
            item_id="item-1",
            block_id="block-1",
            block_type="cycle",
            block={"blocktype": "cycle", "file_id": "a", "title": "keep me"},
            file_ids=["a", "b"],
        )

        assert result is not None
        req = next(r for r in mock_transport.requests if r.url.path == "/update-block/")
        sent = json.loads(req.content)["block_data"]
        assert sent["file_ids"] == ["a", "b"]
        assert "file_id" not in sent
        assert sent["title"] == "keep me"
        assert sent["block_id"] == "block-1"

    def test_update_block_files_error_returns_none(
        self, mock_transport, monkeypatch
    ) -> None:
        mock_transport.add_response(
            "POST", "/update-block/", status_code=500, json_data={"error": "boom"}
        )
        client = _make_beholder_client(mock_transport, monkeypatch)
        assert (
            client.update_block_files(
                item_id="item-1",
                block_id="block-1",
                block_type="cycle",
                block={"blocktype": "cycle", "file_id": "a"},
                file_ids=["a", "b"],
            )
            is None
        )

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

    def test_create_block_unlisted_file_returns_none(
        self, mock_transport, monkeypatch
    ) -> None:
        """datalab-api raises a bare RuntimeError when the server doesn't
        list the just-uploaded file on the item yet (#49). A block is a
        convenience on top of a successful attach, so swallow it."""
        mock_transport.add_response(
            "GET",
            "/get-item-data/item-1",
            json_data={
                "item_data": {
                    "item_id": "item-1",
                    "blocks_obj": {},
                    "display_order": [],
                    "file_ObjectIds": [],
                }
            },
        )
        client = _make_beholder_client(mock_transport, monkeypatch)
        result = client.create_block(
            item_id="item-1", block_type="cycle", file_id="file-xyz"
        )
        assert result is None


class TestItemLookup:
    """Only a 404 means "item doesn't exist" (#53)."""

    def test_fetch_item_404_returns_none(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response("GET", "/get-item-data/42", status_code=404)
        client = _make_beholder_client(mock_transport, monkeypatch)
        assert client.fetch_item("42") is None

    @pytest.mark.parametrize("status", [401, 500, 502, 503, 504])
    def test_fetch_item_server_error_raises(
        self, mock_transport, monkeypatch, status: int
    ) -> None:
        mock_transport.add_response("GET", "/get-item-data/42", status_code=status)
        client = _make_beholder_client(mock_transport, monkeypatch)
        with pytest.raises(ItemLookupError):
            client.fetch_item("42")

    def test_fetch_item_transport_error_raises(
        self, mock_transport, monkeypatch
    ) -> None:
        def dropped(request):
            raise httpx.ConnectError("connection dropped", request=request)

        monkeypatch.setattr(mock_transport, "handle_request", dropped)
        client = _make_beholder_client(mock_transport, monkeypatch)
        with pytest.raises(ItemLookupError):
            client.fetch_item("42")

    def test_fetch_item_malformed_response_raises(
        self, mock_transport, monkeypatch
    ) -> None:
        mock_transport.add_response(
            "GET", "/get-item-data/42", json_data={"item_data": {"item_id": "42"}}
        )
        client = _make_beholder_client(mock_transport, monkeypatch)
        with pytest.raises(ItemLookupError):
            client.fetch_item("42")

    def test_ensure_item_does_not_create_on_server_error(
        self, mock_transport, monkeypatch
    ) -> None:
        mock_transport.add_response("GET", "/get-item-data/42", status_code=503)
        client = _make_beholder_client(mock_transport, monkeypatch)
        with pytest.raises(ItemLookupError):
            client.ensure_item("42", item_type="cells")
        assert not any(r.url.path == "/new-sample/" for r in mock_transport.requests)

    def test_ensure_item_duplicate_refetches(self, mock_transport, monkeypatch) -> None:
        """Item created by someone else between lookup and create."""
        mock_transport.add_response(
            "POST", "/new-sample/", status_code=409, json_data={"message": "dup"}
        )
        client = _make_beholder_client(mock_transport, monkeypatch)
        existing = {"item_id": "42", "files": []}
        lookups = iter([None, existing])
        monkeypatch.setattr(client, "fetch_item", lambda item_id: next(lookups))

        assert client.ensure_item("42", item_type="cells") == existing


class TestElevatedPermissions:
    """Elevation only touches reads.

    datalab gives an active admin unrestricted access on writes with no
    opt-in, but treats them as an ordinary user on GETs unless the
    request carries ``sudo=1``. So the daemon only needs to elevate its
    reads, and must not send the parameter on writes.
    """

    ITEM_RESPONSE = {
        "status": "success",
        "item_data": {
            "item_id": "item-1",
            "files": [],
            "blocks_obj": {},
            "display_order": [],
        },
    }

    def test_get_appends_sudo_when_elevated(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "GET", "/get-item-data/item-1", json_data=self.ITEM_RESPONSE
        )
        client = _make_beholder_client(
            mock_transport, monkeypatch, elevate_permissions=True
        )

        assert client.fetch_item("item-1") is not None
        assert mock_transport.requests[-1].url.params.get("sudo") == "1"

    def test_get_omits_sudo_by_default(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "GET", "/get-item-data/item-1", json_data=self.ITEM_RESPONSE
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        assert client.fetch_item("item-1") is not None
        assert "sudo" not in mock_transport.requests[-1].url.params

    def test_writes_never_carry_sudo(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        mock_transport.add_response(
            "POST",
            "/upload-file/",
            status_code=201,
            json_data={"status": "success", "file_id": "file-xyz"},
        )
        test_file = tmp_path / "test.csv"
        test_file.write_text("a,b,c\n1,2,3\n")

        client = _make_beholder_client(
            mock_transport, monkeypatch, elevate_permissions=True
        )
        assert client.attach_file(item_id="item-1", file_path=test_file) is not None

        upload = mock_transport.requests[-1]
        assert upload.method == "POST"
        assert "sudo" not in upload.url.params

    def test_existing_query_params_are_preserved(
        self, mock_transport, monkeypatch
    ) -> None:
        """A caller-supplied `params` dict must survive elevation."""
        mock_transport.add_response("GET", "/search-items/", json_data={"items": []})
        client = _make_beholder_client(
            mock_transport, monkeypatch, elevate_permissions=True
        )

        client._get(f"{client.datalab_api_url}/search-items/", params={"query": "abc"})

        params = mock_transport.requests[-1].url.params
        assert params.get("query") == "abc"
        assert params.get("sudo") == "1"
