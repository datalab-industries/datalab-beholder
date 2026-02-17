"""Tests for the API client."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import _make_beholder_client


class TestBeholderClient:
    def test_push_metadata_success(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            json_data={"status": "success", "received": 2},
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        result = client.push_metadata(
            daemon_id="test-daemon",
            entries=[
                {"path": "file1.csv", "size": 100, "status": "new"},
                {"path": "file2.raw", "size": 200, "status": "new"},
            ],
            snapshot_type="full",
        )

        assert result is True
        assert len(mock_transport.requests) == 1

    def test_push_metadata_failure(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            status_code=500,
            json_data={"error": "internal error"},
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        result = client.push_metadata(
            daemon_id="test-daemon",
            entries=[],
            snapshot_type="full",
        )

        assert result is False

    def test_poll_file_requests(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "GET",
            "/api/remote-files/pending",
            json_data={
                "requests": [
                    {
                        "request_id": "req-1",
                        "path": "data/file.csv",
                        "priority": "high",
                    },
                    {
                        "request_id": "req-2",
                        "path": "data/file2.raw",
                    },
                ]
            },
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        requests = client.poll_file_requests("test-daemon")
        assert len(requests) == 2
        assert requests[0].request_id == "req-1"
        assert requests[0].path == "data/file.csv"
        assert requests[0].priority == "high"
        assert requests[1].priority == "normal"

    def test_poll_file_requests_empty(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "GET",
            "/api/remote-files/pending",
            json_data={"requests": []},
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        requests = client.poll_file_requests("test-daemon")
        assert requests == []

    def test_poll_file_requests_server_down(self, mock_transport, monkeypatch) -> None:
        """Returns empty list on failure instead of raising."""
        client = _make_beholder_client(mock_transport, monkeypatch)

        # No response configured, will 404
        requests = client.poll_file_requests("test-daemon")
        assert requests == []

    def test_upload_file(self, mock_transport, monkeypatch, tmp_path: Path) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/upload",
            json_data={"status": "success", "file_id": "file-xyz"},
        )

        test_file = tmp_path / "test.csv"
        test_file.write_text("a,b,c\n1,2,3\n")

        client = _make_beholder_client(mock_transport, monkeypatch)

        result = client.upload_file(
            request_id="req-1",
            file_path=test_file,
            metadata={"size": 14, "modified": 1234567890.0},
        )

        assert result is True

    def test_upload_file_missing(
        self, mock_transport, monkeypatch, tmp_path: Path
    ) -> None:
        client = _make_beholder_client(mock_transport, monkeypatch)

        result = client.upload_file(
            request_id="req-1",
            file_path=tmp_path / "nonexistent.csv",
            metadata={},
        )

        assert result is False

    def test_heartbeat(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/heartbeat",
            json_data={"status": "ok"},
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        result = client.heartbeat("test-daemon", {"uptime": 3600})
        assert result is True

    def test_backoff_increases_on_failure(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            status_code=500,
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        initial_backoff = client.current_backoff
        client.push_metadata("test", [], "full")
        assert client.current_backoff > initial_backoff

    def test_backoff_resets_on_success(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            status_code=500,
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        # Fail to increase backoff
        client.push_metadata("test", [], "full")
        assert client.current_backoff > 1.0

        # Now succeed
        mock_transport.add_response(
            "POST",
            "/api/remote-files/metadata",
            status_code=200,
            json_data={"status": "ok"},
        )
        client.push_metadata("test", [], "full")
        assert client.current_backoff == 1.0

    def test_auth_header_sent(self, mock_transport, monkeypatch) -> None:
        mock_transport.add_response(
            "POST",
            "/api/remote-files/heartbeat",
            json_data={"status": "ok"},
        )
        client = _make_beholder_client(mock_transport, monkeypatch)

        client.heartbeat("test", {})

        req = mock_transport.requests[0]
        assert req.headers["DATALAB-API-KEY"] == "test-key"
