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
