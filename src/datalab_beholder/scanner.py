"""Directory scanning using os.scandir for efficient file system traversal."""

from __future__ import annotations

import fnmatch
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


def _matches_any(name: str, patterns: list[str]) -> bool:
    """Check if a filename matches any of the given glob patterns."""
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def scan_directory(
    root: Path,
    name: str = "",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_depth: int | None = None,
) -> ScanResult:
    """Scan a directory tree and return structured metadata.

    Uses os.scandir() for efficient traversal with cached stat results.

    Args:
        root: Root directory to scan.
        name: Human-readable label for this scan.
        include_patterns: Glob patterns for files to include (default: all).
        exclude_patterns: Glob patterns for files to exclude (default: none).
        max_depth: Maximum recursion depth (None = unlimited).

    Returns:
        ScanResult with all discovered entries and statistics.
    """
    if include_patterns is None:
        include_patterns = ["*"]
    if exclude_patterns is None:
        exclude_patterns = []

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
                    total_files += 1
                    total_size += stat.st_size
                    entries.append(
                        FileEntry(
                            path=rel_path,
                            size=stat.st_size,
                            modified=stat.st_mtime,
                            is_directory=False,
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
