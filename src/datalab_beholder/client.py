"""HTTP client for communicating with a datalab instance.

Subclasses :class:`datalab_api.DatalabClient` to inherit auth, session
management, version negotiation, and the standard CRUD surface
(``get_item``, ``create_item``, ``upload_file``, ...). Adds only the
small surface the beholder daemon needs: a non-raising connection
probe, an "ensure item exists" route, an existing-file lookup so
modified files replace rather than duplicate, and a non-raising
attach wrapper so a single bad item can't take the loop down.

Note: ``DatalabClient.__init__`` eagerly handshakes with the server
(``_detect_api_url``, ``get_info``, ``get_block_info``). That is
accepted for now; tests monkeypatch those hooks via
``_make_beholder_client``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from datalab_api import DatalabAPIError, DatalabClient

log = logging.getLogger(__name__)


class BeholderClient(DatalabClient):
    """Datalab client with daemon-friendly, non-raising helpers."""

    last_request_ok: bool = False

    def check_connection(self) -> tuple[bool, bool]:
        """Probe the server with a lightweight ``GET /info``.

        Auth is reported as configured when a non-placeholder API key is
        present in the request headers. Once datalab grows a proper
        ``/whoami``-style endpoint, this can be tightened.

        Returns:
            ``(reachable, authenticated)``.
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

    def fetch_item(self, item_id: str) -> dict[str, Any] | None:
        """Return the item's data dict, or ``None`` if it doesn't exist
        (or any other error)."""
        try:
            return super().get_item(item_id=item_id, load_blocks=False)
        except DatalabAPIError as e:
            log.debug("get_item(%s) failed: %s", item_id, e)
            return None

    def ensure_item(
        self,
        item_id: str,
        item_type: str,
        collection_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the item's data dict; create it first if it doesn't exist.

        ``collection_id`` is forwarded to ``create_item``, which links
        (or creates) the matching collection. ``group_id`` is forwarded
        as a single-element ``group_ids`` list and grants the named
        group access control on the new item. Returns ``None`` if the
        creation attempt fails.
        """
        item = self.fetch_item(item_id)
        if item is not None:
            return item

        log.info(
            "Creating item %s (type=%s, group=%s, collection=%s)",
            item_id,
            item_type,
            group_id,
            collection_id,
        )
        try:
            created = super().create_item(
                item_id=item_id,
                item_type=item_type,
                collection_ids=[collection_id] if collection_id else None,
                group_ids=[group_id] if group_id else None,
            )
        except DatalabAPIError as e:
            log.error("Failed to create item %s: %s", item_id, e)
            return None

        # `create_item` returns a `sample_list_entry` rather than full
        # item data, but a freshly-created item has no attachments by
        # definition, so `find_existing_file_id` will correctly return
        # None against either shape.
        return created if isinstance(created, dict) else {"files": []}

    def find_existing_file_id(self, item: dict[str, Any], filename: str) -> str | None:
        """Look up the immutable file id of an attachment by basename.

        ``item`` is the dict returned by :meth:`fetch_item` /
        :meth:`ensure_item`. Returns the first match, or ``None`` if
        nothing on the item shares that filename.
        """
        for f in item.get("files", []) or []:
            if f.get("name") == filename:
                file_id = f.get("immutable_id")
                if file_id:
                    return str(file_id)
        return None

    def attach_file(
        self,
        item_id: str,
        file_path: Path,
        replace_file_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Upload ``file_path`` and attach it to ``item_id``.

        Pass ``replace_file_id`` to replace a previously-attached file
        in place (preserves the file's immutable id and any blocks
        pointing at it). Errors are logged and swallowed so the daemon
        loop survives a single bad item or transient hiccup.
        """
        try:
            result = super().upload_file(
                item_id=item_id,
                file_path=file_path,
                replace_file_id=replace_file_id,
            )
            self.last_request_ok = True
            return result
        except FileNotFoundError:
            log.warning("File vanished before upload: %s", file_path)
            self.last_request_ok = False
            return None
        except DatalabAPIError as e:
            log.error("Failed to attach %s to item %s: %s", file_path, item_id, e)
            self.last_request_ok = False
            return None
