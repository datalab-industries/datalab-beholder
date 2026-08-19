"""HTTP client for communicating with a datalab instance.

Subclasses ``datalab_api.DatalabClient`` to inherit auth, session
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

        ``item`` is the dict returned by ``fetch_item`` /
        ``ensure_item``. Returns the first match, or ``None`` if
        nothing on the item shares that filename.

        Comparison prefers ``original_name`` (the unaltered filename the
        client sent) and falls back to ``name``. The server stores
        ``name = secure_filename(original_name)``, which mangles spaces
        and other characters into underscores — so a basename like
        ``"foo bar.mpr"`` on disk would never compare equal to the
        ``name`` field, causing a duplicate upload on every tick. Match
        is also case-insensitive to cope with Windows filesystems being
        case-preserving but case-insensitive.
        """
        target = filename.casefold()
        for f in item.get("files", []) or []:
            for key in ("original_name", "name"):
                candidate = f.get(key)
                if candidate and candidate.casefold() == target:
                    file_id = f.get("immutable_id")
                    if file_id:
                        return str(file_id)
                    break
        return None

    def find_block_for_file(
        self, item: dict[str, Any], block_type: str, file_id: str
    ) -> str | None:
        """Return the id of an existing ``block_type`` block wired to
        ``file_id``, or ``None`` if no such block exists.

        ``item`` is the dict returned by ``fetch_item`` /
        ``ensure_item``. Deliberately checks each block's own
        ``file_id`` field (set when we create/update a block) rather
        than the file's own record on the item — the latter is not
        reliably kept in sync by the server. A freshly-created item
        (or one fetched without full item data) simply has no
        ``blocks_obj``, so this returns ``None`` rather than raising.
        """
        for block_id, block in (item.get("blocks_obj") or {}).items():
            if block.get("blocktype") == block_type and block.get("file_id") == file_id:
                return block_id
        return None

    def create_block(
        self, item_id: str, block_type: str, file_id: str
    ) -> dict[str, Any] | None:
        """Create a ``block_type`` block on ``item_id``, wired to ``file_id``.

        ``file_id`` must already be uploaded and attached to the item.
        Errors are logged and swallowed so the daemon loop survives a
        single bad item or transient hiccup.
        """
        try:
            return super().create_data_block(
                item_id=item_id, block_type=block_type, file_ids=file_id
            )
        except DatalabAPIError as e:
            log.error(
                "Failed to create %s block on item %s: %s", block_type, item_id, e
            )
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

        A replace upload whose content the server already holds comes
        back with ``not_modified=True`` and the existing ``file_id``
        (datalab compares hashes and replies ``304``; datalab-api turns
        that into a normal result). That is a successful no-op, not an
        error — the caller marks the file synced and stops re-uploading
        it on every tick.
        """
        try:
            result = super().upload_file(
                item_id=item_id,
                file_path=file_path,
                replace_file_id=replace_file_id,
            )
        except FileNotFoundError:
            log.warning("File vanished before upload: %s", file_path)
            self.last_request_ok = False
            return None
        except OSError as e:
            # Locked by the acquisition software, or the share dropped
            # out mid-read — both routine on an instrument PC watching a
            # network mount.
            log.warning("Could not read %s for upload: %s", file_path, e)
            self.last_request_ok = False
            return None
        except DatalabAPIError as e:
            log.error("Failed to attach %s to item %s: %s", file_path, item_id, e)
            self.last_request_ok = False
            return None

        if result.get("not_modified"):
            log.debug(
                "%s already up to date on item %s, no upload needed",
                file_path,
                item_id,
            )

        self.last_request_ok = True
        return result
