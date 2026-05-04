"""Directory scanning using os.scandir for efficient file system traversal."""

from __future__ import annotations

import fnmatch
import re
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """Metadata for a single file or directory."""

    path: str
    size: int
    modified: float
    is_directory: bool
    ids: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanResult:
    """Result of scanning a directory tree."""

    root_path: str
    name: str
    timestamp: datetime
    entries: list[FileEntry] = field(default_factory=list)
    scan_duration_ms: int = 0
    total_files: int = 0
    total_directories: int = 0
    total_size: int = 0

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "root_path": self.root_path,
            "name": self.name,
            "timestamp": self.timestamp.isoformat(),
            "entries": [
                {
                    "path": e.path,
                    "size": e.size,
                    "modified": e.modified,
                    "is_directory": e.is_directory,
                    "ids": e.ids,
                }
                for e in self.entries
            ],
            "statistics": {
                "total_files": self.total_files,
                "total_directories": self.total_directories,
                "total_size": self.total_size,
                "scan_duration_ms": self.scan_duration_ms,
            },
        }


@dataclass
class WarmScanResult:
    """Output of a directory-mtime-aware warm scan.

    Only files under `changed_dirs` are authoritative — directories whose
    mtime didn't exceed the previous scan anchor are not re-stat'd, and
    their files are assumed unchanged. The state-store consumer scopes
    its diff to `changed_dirs`.
    """

    name: str
    entries: list[FileEntry]
    changed_dirs: list[str]  # relative paths (forward-slash)
    max_dir_mtime: float


def _matches_any(name: str, patterns: list[str]) -> bool:
    """Check if a filename matches any of the given glob patterns."""
    return any(fnmatch.fnmatch(name, p) for p in patterns if p)


def _match_id_patterns(
    path: str, patterns: list[re.Pattern[str]]
) -> dict[str, str] | None:
    """Match a path against ID-extraction regexes.

    Returns the named-group dict from the first matching pattern, an empty
    dict if patterns is empty (no constraint), or None if no pattern matched.
    """
    if not patterns:
        return {}
    for pat in patterns:
        m = pat.search(path)
        if m:
            return {k: v for k, v in m.groupdict().items() if v is not None}
    return None


def _apply_id_templates(
    ids: dict[str, str],
    item_id_template: str | None,
    collection_id_template: str | None,
    rel_path: str,
) -> dict[str, str] | None:
    """Render ``item_id`` / ``collection_id`` from capture groups.

    Mutates a copy of ``ids`` so the resolved values land in the state DB
    and can be logged at scan time. Returns None if a template references
    a capture group the regex didn't produce — the file is then skipped
    just like an unmatched ``id_pattern`` would skip it.
    """
    if not (item_id_template or collection_id_template):
        return ids
    out = dict(ids)
    try:
        if item_id_template:
            out["item_id"] = item_id_template.format(**ids)
        if collection_id_template:
            out["collection_id"] = collection_id_template.format(**ids)
    except KeyError as e:
        log.warning(
            "Skipping %s: id template references missing capture group %s",
            rel_path,
            e,
        )
        return None
    return out


def scan_directory(
    root: Path,
    name: str = "",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    id_patterns: list[str] | None = None,
    item_id_template: str | None = None,
    collection_id_template: str | None = None,
    max_depth: int | None = None,
    skip_dirs: bool = True,
) -> ScanResult:
    """Scan a directory tree and return structured metadata.

    Uses os.scandir() for efficient traversal with cached stat results.

    Args:
        root: Root directory to scan.
        name: Human-readable label for this scan.
        include_patterns: Glob patterns for files to include (default: all).
        exclude_patterns: Glob patterns for files to exclude (default: none).
        id_patterns: Glob patterns to use to match file paths to item IDs (default: none).
        max_depth: Maximum recursion depth (None = unlimited).
        skip_dirs: If True, directories will not be included in the scan results.

    Returns:
        ScanResult with all discovered entries and statistics.
    """
    if include_patterns is None:
        include_patterns = ["*"]
    if exclude_patterns is None:
        exclude_patterns = []
    if id_patterns is None:
        id_patterns = []

    compiled_id_patterns = [re.compile(p) for p in id_patterns if p]

    root = Path(root).resolve()
    if not name:
        name = root.name

    start = time.monotonic()
    entries: list[FileEntry] = []
    total_files = 0
    total_dirs = 0
    total_size = 0

    def _scan(directory: Path, depth: int) -> None:
        nonlocal total_files, total_dirs, total_size

        try:
            scanner = os.scandir(directory)
        except PermissionError:
            log.warning("Permission denied: %s", directory)
            return
        except OSError as e:
            log.warning("Error scanning %s: %s", directory, e)
            return

        with scanner:
            for entry in scanner:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except (OSError, PermissionError) as e:
                    log.warning("Cannot stat %s: %s", entry.path, e)
                    continue

                entry_name = entry.name

                if _matches_any(entry_name, exclude_patterns):
                    continue

                rel_path = os.path.relpath(entry.path, root)
                # Normalize to forward slashes for cross-platform consistency
                rel_path = rel_path.replace(os.sep, "/")

                if entry.is_dir(follow_symlinks=False):
                    total_dirs += 1
                    if not skip_dirs:
                        entries.append(
                            FileEntry(
                                path=rel_path,
                                size=0,
                                modified=stat.st_mtime,
                                is_directory=True,
                            )
                        )
                    if max_depth is None or depth < max_depth:
                        _scan(Path(entry.path), depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    if not _matches_any(entry_name, include_patterns):
                        continue

                    ids = _match_id_patterns(rel_path, compiled_id_patterns)
                    if ids is None:
                        log.debug(
                            "%s does not match ID patterns, skipping",
                            rel_path,
                        )
                        continue

                    ids = _apply_id_templates(
                        ids, item_id_template, collection_id_template, rel_path
                    )
                    if ids is None:
                        continue

                    total_files += 1
                    total_size += stat.st_size
                    entries.append(
                        FileEntry(
                            path=rel_path,
                            size=stat.st_size,
                            modified=stat.st_mtime,
                            is_directory=False,
                            ids=ids,
                        )
                    )

    _scan(root, 0)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return ScanResult(
        root_path=str(root),
        name=name,
        timestamp=datetime.now(timezone.utc),
        entries=entries,
        scan_duration_ms=elapsed_ms,
        total_files=total_files,
        total_directories=total_dirs,
        total_size=total_size,
    )


def warm_scan_directory(
    root: Path,
    name: str = "",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    id_patterns: list[str] | None = None,
    item_id_template: str | None = None,
    collection_id_template: str | None = None,
    max_depth: int | None = None,
    since_mtime: float | None = None,
) -> WarmScanResult:
    """Walk the tree, but only re-stat files in directories whose mtime is
    newer than `since_mtime`.

    Discovers new files, deletions, and renames in active subtrees while
    skipping the per-file stats in cold subtrees. In-place file rewrites
    don't bump the parent dir's mtime — those are caught by the cold scan.

    Returns a `WarmScanResult` whose `changed_dirs` lists every directory
    (relative path, forward-slashed) whose contents were re-stat'd.
    """
    if include_patterns is None:
        include_patterns = ["*"]
    if exclude_patterns is None:
        exclude_patterns = []
    if id_patterns is None:
        id_patterns = []

    compiled_id_patterns = [re.compile(p) for p in id_patterns if p]

    root = Path(root).resolve()
    if not name:
        name = root.name

    entries: list[FileEntry] = []
    changed_dirs: list[str] = []
    max_dir_mtime = since_mtime or 0.0

    def _rel(p: str) -> str:
        return os.path.relpath(p, root).replace(os.sep, "/")

    def _walk(directory: Path, depth: int) -> None:
        nonlocal max_dir_mtime
        try:
            dir_mtime = directory.stat().st_mtime
        except (OSError, PermissionError) as e:
            log.warning("Cannot stat directory %s: %s", directory, e)
            return

        if dir_mtime > max_dir_mtime:
            max_dir_mtime = dir_mtime

        try:
            scanner = os.scandir(directory)
        except PermissionError:
            log.warning("Permission denied: %s", directory)
            return
        except OSError as e:
            log.warning("Error scanning %s: %s", directory, e)
            return

        # Mtime short-circuit: if this directory hasn't changed since the
        # last warm scan, its files are assumed stable. We still need to
        # recurse into subdirectories — their mtimes don't propagate up.
        scan_files_here = since_mtime is None or dir_mtime > since_mtime
        if scan_files_here:
            rel_dir = _rel(str(directory)) if directory != root else ""
            changed_dirs.append(rel_dir)

        with scanner:
            for entry in scanner:
                entry_name = entry.name
                if _matches_any(entry_name, exclude_patterns):
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as e:
                    log.warning("Cannot classify %s: %s", entry.path, e)
                    continue

                if is_dir:
                    if max_depth is None or depth < max_depth:
                        _walk(Path(entry.path), depth + 1)
                    continue

                if not scan_files_here:
                    continue

                if not entry.is_file(follow_symlinks=False):
                    continue
                if not _matches_any(entry_name, include_patterns):
                    continue

                rel_path = _rel(entry.path)
                ids = _match_id_patterns(rel_path, compiled_id_patterns)
                if ids is None:
                    continue
                ids = _apply_id_templates(
                    ids, item_id_template, collection_id_template, rel_path
                )
                if ids is None:
                    continue

                try:
                    stat = entry.stat(follow_symlinks=False)
                except (OSError, PermissionError) as e:
                    log.warning("Cannot stat %s: %s", entry.path, e)
                    continue

                entries.append(
                    FileEntry(
                        path=rel_path,
                        size=stat.st_size,
                        modified=stat.st_mtime,
                        is_directory=False,
                        ids=ids,
                    )
                )

    _walk(root, 0)

    return WarmScanResult(
        name=name,
        entries=entries,
        changed_dirs=changed_dirs,
        max_dir_mtime=max_dir_mtime,
    )


def hot_stat_paths(
    root: Path,
    paths: list[str],
    id_patterns: list[str] | None = None,
    item_id_template: str | None = None,
    collection_id_template: str | None = None,
) -> tuple[list[FileEntry], list[str]]:
    """Stat each `path` (relative to `root`) directly.

    Returns a `(survived, missing)` tuple: `survived` is the list of file
    entries that still exist with up-to-date metadata; `missing` is the
    list of paths that raised FileNotFoundError. Used by the hot scan to
    refresh a small set of recently-modified files without walking the
    tree.

    `id_patterns` is re-applied so the captured ids stay attached on the
    refreshed entries; pass the same list as the watched path's config.
    """
    if id_patterns is None:
        id_patterns = []
    compiled = [re.compile(p) for p in id_patterns if p]

    root = Path(root).resolve()
    survived: list[FileEntry] = []
    missing: list[str] = []

    for rel in paths:
        full = root / rel
        try:
            st = os.stat(full)
        except FileNotFoundError:
            missing.append(rel)
            continue
        except (OSError, PermissionError) as e:
            log.warning("Cannot stat %s: %s", full, e)
            continue

        ids = _match_id_patterns(rel, compiled)
        if ids is not None:
            ids = _apply_id_templates(
                ids, item_id_template, collection_id_template, rel
            )
        survived.append(
            FileEntry(
                path=rel,
                size=st.st_size,
                modified=st.st_mtime,
                is_directory=False,
                ids=ids or {},
            )
        )

    return survived, missing
