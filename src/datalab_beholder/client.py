"""HTTP client for communicating with a datalab instance.

Inherits from BaseDatalabClient to get auth, session management, and error
handling. Adds daemon-specific methods for the planned /api/remote-files/*
endpoints and exponential backoff for resilient daemon operation.

Note: BaseDatalabClient.__init__ eagerly connects to the server (calls
_detect_api_url, get_info, get_block_info). This is accepted for now;
the base class will be refactored to make this optional.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datalab_api._base import BaseDatalabClient, DatalabAPIError

log = logging.getLogger(__name__)

INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 300.0  # 5 minutes
BACKOFF_FACTOR = 2.0


@dataclass
class FileRequest:
    """A server request for a specific file to be uploaded."""

    request_id: str
    path: str
    priority: str = "normal"


class BeholderClient(BaseDatalabClient):
    """HTTP client for the beholder daemon's communication with datalab.

    Inherits auth, session management, and error handling from
    BaseDatalabClient. Adds daemon-specific API methods with exponential
    backoff for resilient operation when the server is unreachable.
    """

    # Instance-level backoff state (shadowed from class attribute on first mutation)
    _backoff: float = INITIAL_BACKOFF
    last_request_ok: bool = False

    def check_connection(self) -> tuple[bool, bool]:
        """Check server reachability and authentication.

        Performs a lightweight GET to ``/info`` to test reachability.
        Auth is considered configured when a real API key is present
        (non-empty, non-placeholder).  A proper server-side auth check
        will be added once the ``/api/remote-files/*`` endpoints exist.

        Returns:
            ``(reachable, authenticated)`` booleans.
        """
        reachable = False
        authenticated = False
        try:
            self.get_info()
            reachable = True
        except Exception:
            pass

        if reachable:
            key = self._headers.get("DATALAB-API-KEY", "")
            authenticated = bool(key) and key != "your-api-key-here"

        self.last_request_ok = reachable
        return reachable, authenticated

    def get_info(self) -> dict[str, Any]:
        info_url = f"{self.datalab_api_url}/info"
        info_data = self._get(info_url)
        self.info = info_data["data"]
        return self.info

    def get_block_info(self) -> list[dict[str, Any]]:
        block_info_url = f"{self.datalab_api_url}/info/blocks"
        block_info_data = self._get(block_info_url)
        self.block_info = block_info_data["data"]
        return self.block_info

    def _url(self, path: str) -> str:
        return f"{self.datalab_api_url}{path}"

    def _reset_backoff(self) -> None:
        self._backoff = INITIAL_BACKOFF

    def _increase_backoff(self) -> float:
        current = self._backoff
        self._backoff = min(self._backoff * BACKOFF_FACTOR, MAX_BACKOFF)
        return current

    def _safe_request(
        self,
        method: str,
        path: str,
        expected_status: int = 200,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Make an HTTP request with exponential backoff on failure.

        Wraps the inherited _request method but returns None on failure
        (after logging the error) rather than raising, so the daemon loop
        can continue operating.
        """
        url = self._url(path)
        try:
            result = super()._request(method, url, expected_status, **kwargs)
            self._reset_backoff()
            self.last_request_ok = True
            return result
        except DatalabAPIError as e:
            wait = self._increase_backoff()
            log.error("Request failed for %s %s: %s (backing off %.1fs)", method, url, e, wait)
            self.last_request_ok = False
            return None

    def push_metadata(
        self,
        daemon_id: str,
        entries: list[dict[str, Any]],
        snapshot_type: str,
    ) -> bool:
        """Push file metadata to the server.

        Args:
            daemon_id: Unique identifier for this daemon instance.
            entries: List of file entry dicts to send.
            snapshot_type: "full" or "diff".

        Returns:
            True if the server accepted the metadata.
        """
        result = self._safe_request(
            "POST",
            "/api/remote-files/metadata",
            json={
                "source_type": "daemon",
                "daemon_id": daemon_id,
                "snapshot_type": snapshot_type,
                "entries": entries,
            },
        )
        return result is not None

    def poll_file_requests(self, daemon_id: str) -> list[FileRequest]:
        """Poll the server for pending file transfer requests.

        Args:
            daemon_id: Unique identifier for this daemon instance.

        Returns:
            List of FileRequest objects, empty on failure.
        """
        result = self._safe_request(
            "GET",
            "/api/remote-files/pending",
            params={"daemon_id": daemon_id},
        )
        if result is None:
            return []

        return [
            FileRequest(
                request_id=r["request_id"],
                path=r["path"],
                priority=r.get("priority", "normal"),
            )
            for r in result.get("requests", [])
        ]

    def upload_file(
        self,
        request_id: str,
        file_path: Path,
        metadata: dict[str, Any],
    ) -> bool:
        """Upload a file to the server in response to a file request.

        Args:
            request_id: The server's request ID for this file.
            file_path: Local path to the file to upload.
            metadata: File metadata dict (size, modified, etc.).

        Returns:
            True if the upload succeeded.
        """
        try:
            with open(file_path, "rb") as f:
                result = self._safe_request(
                    "POST",
                    "/api/remote-files/upload",
                    files={"file": (file_path.name, f)},
                    data={
                        "request_id": request_id,
                        "metadata": str(metadata),
                    },
                )
            return result is not None
        except OSError as e:
            log.error("Cannot read file %s for upload: %s", file_path, e)
            return False

    def heartbeat(self, daemon_id: str, status: dict[str, Any]) -> bool:
        """Send a heartbeat to the server.

        Args:
            daemon_id: Unique identifier for this daemon instance.
            status: Status information dict.

        Returns:
            True if the heartbeat was accepted.
        """
        result = self._safe_request(
            "POST",
            "/api/remote-files/heartbeat",
            json={"daemon_id": daemon_id, "status": status},
        )
        return result is not None

    @property
    def current_backoff(self) -> float:
        """The current backoff delay in seconds."""
        return self._backoff

    def wait_backoff(self) -> None:
        """Sleep for the current backoff duration. Used between retries."""
        if self._backoff > INITIAL_BACKOFF:
            log.info("Backing off for %.1f seconds", self._backoff)
            time.sleep(self._backoff)
