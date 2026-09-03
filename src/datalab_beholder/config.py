"""Configuration model and YAML loading for datalab-beholder."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from datalab_beholder.state import DiffResult, StateStore

# Controlled vocabulary of named capture groups allowed in id_patterns.
# These names are the only ones the rest of the daemon knows how to
# interpret when posting to the datalab API.
ALLOWED_ID_GROUPS: frozenset[str] = frozenset({"group_id", "item_id", "collection_id"})

# Latest on-disk config schema version. Configs without an explicit
# `version` field are treated as v1 for backwards compatibility with
# existing deployments.
LATEST_CONFIG_VERSION = 1

if getattr(sys, "frozen", False):
    # PyInstaller bundle: config lives next to the executable
    DEFAULT_CONFIG_DIR = Path(sys.executable).resolve().parent
else:
    # Running from source: config lives alongside the package
    DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_STATE_DB_PATH = DEFAULT_CONFIG_DIR / "state.db"

CONFIG_TEMPLATE = """\
# datalab-beholder configuration
version: 1

datalabs:
  - name: "example"
    url: "https://datalab.example.org"
    api_key: "your-api-key-here"  # or set DATALAB_API_KEY env var

watched_paths:
  - kind: "local"
    path: "/path/to/instrument/data"
    name: "Instrument-Name"
    datalab: "example"
    item_type: "cells"            # datalab item type for newly-created items
    include_patterns:
      - "*.mpr"
    exclude_patterns: []
    # Regex with named capture groups (`item_id`, `group_id`, `collection_id`).
    # Files that don't match are skipped. The `\\D*\\.mpr$` tail forces the
    # regex to land on the *last* digit run before the extension regardless
    # of subdirectory depth.
    id_patterns:
      - "^(?P<group_id>P[0-9]{3,})/.*?(?P<item_id>[0-9]+)\\\\D*\\\\.mpr$"
    # Optional templates: render the values that actually get sent to the
    # server from the capture groups. Resolved at scan time, so the
    # `state.db` and `scan` CLI output show what the daemon will create.
    item_id_template: "{group_id}-{item_id}"
    # collection_id_template: "{group_id}"
    # max_depth: null  # unlimited
    # Optional: glob pattern (matched against the basename) -> datalab block
    # type. After a matching file is attached, a block of that type is
    # created on the item if one doesn't already exist.
    # block_patterns:
    #   "*.mpr": "cycle"
    #   "*.nda": "cycle"
    # scan:
    #   hot_interval: 60        # stat recently-modified files
    #   warm_interval: 3600     # directory-mtime walk
    #   cold_interval: 86400    # full walk; null disables
    #   hot_window: 86400       # "recent" cutoff for hot scan

sync:
  metadata_interval: 1200  # seconds (20 minutes) — attach cadence

# Clear the stored per-path scan timestamps on startup, forcing a cold scan
# on the first tick rather than waiting out the configured intervals.
# reset_scan_clocks_on_startup: false

log_level: "info"
# state_db: "state.db"  # defaults to alongside the package/executable
"""


class ScanCadence(BaseModel):
    """How often each scan tier runs for a given watched path."""

    hot_interval: int = Field(
        60,
        description="Seconds between hot scans (stat recently-modified files only).",
    )
    warm_interval: int = Field(
        3600,
        description="Seconds between warm scans (directory-mtime walk).",
    )
    cold_interval: int | None = Field(
        86400,
        description=(
            "Seconds between cold scans (full walk). Set to null to disable; "
            "useful for SSH/network archives where files are write-once."
        ),
    )
    hot_window: int = Field(
        86400,
        description=(
            "How recently a file must have been modified to be eligible for "
            "the hot scan, in seconds."
        ),
    )


def _validate_id_patterns(v: list[str]) -> list[str]:
    for pattern in v:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex {pattern!r}: {e}") from e

        group_names = set(compiled.groupindex)
        if not group_names:
            raise ValueError(
                f"id_pattern {pattern!r} has no named capture groups; "
                f"expected at least one of {sorted(ALLOWED_ID_GROUPS)}"
            )
        unknown = group_names - ALLOWED_ID_GROUPS
        if unknown:
            raise ValueError(
                f"id_pattern {pattern!r} contains unsupported capture "
                f"groups {sorted(unknown)}; allowed: {sorted(ALLOWED_ID_GROUPS)}"
            )
        if "item_id" not in group_names:
            raise ValueError(
                f"id_pattern {pattern!r} must include a named capture "
                f"group called 'item_id' (the daemon uses it to attach "
                f"files to datalab items)"
            )
    return v


class WatchedPathBase(BaseModel):
    """Common fields for any watched-path source.

    Concrete subclasses (`LocalWatchedPath`, `SSHWatchedPath`,
    `CloudWatchedPath`) carry their own location fields and implement the
    three scan methods. The daemon never branches on `kind` — it just calls
    the scan methods polymorphically.
    """

    name: str = Field(..., description="Human-friendly name for this path")
    include_patterns: list[str] = Field(
        default_factory=lambda: ["*"],
        description="List of unix glob patterns to include (default: ['*'])",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        description="List of unix glob patterns to exclude (default: [])",
    )
    id_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "List of regex patterns with named capture groups to extract IDs from file paths. "
            f"Allowed group names: {sorted(ALLOWED_ID_GROUPS)}. These will be validated at "
            "startup and used to populate metadata fields when posting to the datalab API."
        ),
    )

    block_patterns: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps unix glob patterns (matched against the file's basename, same syntax as "
            "include_patterns) to a datalab block type. After a matching file is attached, "
            "the daemon checks whether the item already has a block of that type; if not, it "
            "creates one and wires it to the newly-attached file. Patterns are tried in order "
            "and the first match wins."
        ),
        examples=[{"*.mpr": "cycle", "*.nda": "cycle"}],
    )

    item_id_template: str | None = Field(
        None,
        description=(
            "Optional template string to construct an item_id from the capture groups in "
            "id_patterns. Uses Python's str.format syntax; capture group names are available "
            "as variables. If unset, the raw item_id capture group value is used as the item_id."
        ),
        examples=["{item_id}", "{group_id}-{item_id}"],
    )

    collection_id_template: str | None = Field(
        None,
        description=(
            "Optional template string to construct a collection_id from the capture groups in "
            "id_patterns. Uses Python's str.format syntax; capture group names are available "
            "as variables. If unset, no collection_id is set on the datalab item."
        ),
        examples=["{collection_id}", "group-{group_id}"],
    )
    item_type: str | None = Field(
        None, description="Optional item type to set when posting to the datalab API"
    )
    max_depth: int | None = Field(
        10, description="Maximum directory depth to watch (default: 10)"
    )
    datalab: str | None = Field(
        None,
        description="Name of the datalab instance to post to (must match a datalab in the config)",
    )
    scan: ScanCadence = Field(
        default_factory=ScanCadence,
        description="Per-tier scan cadence for this path.",
    )

    @field_validator("id_patterns")
    @classmethod
    def validate_id_patterns(cls, v: list[str]) -> list[str]:
        return _validate_id_patterns(v)

    @field_validator("block_patterns")
    @classmethod
    def validate_block_patterns(cls, v: dict[str, str]) -> dict[str, str]:
        for pattern, block_type in v.items():
            if not pattern:
                raise ValueError("block_patterns keys must be non-empty glob patterns")
            if not block_type:
                raise ValueError(
                    f"block_patterns[{pattern!r}] must map to a non-empty block type"
                )
        return v

    def hot_scan(self, state: StateStore) -> DiffResult:
        """Stat recently-modified files; cheap, runs frequently."""
        raise NotImplementedError

    def warm_scan(self, state: StateStore) -> DiffResult:
        """Directory-mtime walk; discovers new files in active subtrees."""
        raise NotImplementedError

    def cold_scan(self, state: StateStore) -> DiffResult:
        """Full walk; ground-truth reconciliation."""
        raise NotImplementedError


class LocalWatchedPath(WatchedPathBase):
    """A directory on the local filesystem (or any locally-mounted FS)."""

    kind: Literal["local"] = "local"
    path: Path = Field(..., description="Path to watch for changes")

    @field_validator("path")
    @classmethod
    def expand_path(cls, v: Path) -> Path:
        # Only handle ``~`` here — ``load_config`` resolves any remaining
        # relative paths against the YAML file's directory so a relative
        # path in the config doesn't depend on the shell's CWD.
        return Path(os.path.expanduser(v))

    def hot_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        from time import time as _now

        from datalab_beholder.scanner import hot_stat_paths

        cutoff = _now() - self.scan.hot_window
        candidate_paths = state.recently_modified_paths(self.name, since=cutoff)
        survived, missing = hot_stat_paths(
            self.path,
            candidate_paths,
            id_patterns=self.id_patterns,
            item_id_template=self.item_id_template,
            collection_id_template=self.collection_id_template,
        )
        diff = state.update_from_targeted_stats(self.name, survived, missing)
        state.update_scan_timestamp(self.name, "hot", _now())
        return diff

    def warm_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        from time import time as _now

        from datalab_beholder.scanner import warm_scan_directory

        ts = state.get_scan_timestamps(self.name)
        warm = warm_scan_directory(
            self.path,
            name=self.name,
            include_patterns=self.include_patterns,
            exclude_patterns=self.exclude_patterns,
            id_patterns=self.id_patterns,
            item_id_template=self.item_id_template,
            collection_id_template=self.collection_id_template,
            max_depth=self.max_depth,
            since_mtime=ts.max_dir_mtime,
        )
        diff = state.update_from_warm_scan(warm)
        state.update_scan_timestamp(self.name, "warm", _now())
        state.update_max_dir_mtime(self.name, warm.max_dir_mtime)
        return diff

    def cold_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        from dataclasses import replace
        from time import time as _now

        from datalab_beholder.scanner import scan_directory

        # Include dir entries so we can compute max_dir_mtime — the warm
        # scan's short-circuit anchor — but strip them before diffing
        # state, which only stores file rows.
        scan = scan_directory(
            self.path,
            name=self.name,
            include_patterns=self.include_patterns,
            exclude_patterns=self.exclude_patterns,
            id_patterns=self.id_patterns,
            item_id_template=self.item_id_template,
            collection_id_template=self.collection_id_template,
            max_depth=self.max_depth,
            skip_dirs=False,
        )
        file_entries = [e for e in scan.entries if not e.is_directory]
        dir_entries = [e for e in scan.entries if e.is_directory]

        file_scan = replace(scan, entries=file_entries)
        diff = state.update_from_scan(file_scan)

        max_dir_mtime = max((e.modified for e in dir_entries), default=0.0)
        if max_dir_mtime:
            state.update_max_dir_mtime(self.name, max_dir_mtime)
        state.update_scan_timestamp(self.name, "cold", _now())
        return diff


class SSHWatchedPath(WatchedPathBase):
    """A directory on a remote host accessed via SSH.

    Scaffolded for an upcoming implementation — the scan methods raise
    `NotImplementedError` until the SSH backend lands.
    """

    kind: Literal["ssh"] = "ssh"
    host: str = Field(
        ...,
        description="SSH-config alias or user@host string for the remote host.",
    )
    path: PurePosixPath = Field(
        ...,
        description="Absolute POSIX path on the remote host.",
    )

    def _not_implemented(self) -> None:
        raise NotImplementedError(
            "SSH watched paths are scaffolded but not yet implemented; "
            "planned for a follow-up."
        )

    def hot_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        self._not_implemented()
        raise AssertionError  # unreachable

    def warm_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        self._not_implemented()
        raise AssertionError

    def cold_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        self._not_implemented()
        raise AssertionError


class CloudWatchedPath(WatchedPathBase):
    """A locally-mounted cloud-sync folder (OneDrive, Google Drive, etc.).

    Scaffolded for an upcoming implementation. Cloud-sync providers can
    update files out-of-band; this subclass will eventually surface
    placeholder/cloud-only state and skip aggressive hydration.
    """

    kind: Literal["cloud"] = "cloud"
    path: Path = Field(
        ...,
        description="Local mount path of the cloud-sync folder.",
    )
    provider: Literal["onedrive", "gdrive", "auto"] = Field(
        "auto",
        description="Which cloud provider this folder belongs to.",
    )

    @field_validator("path")
    @classmethod
    def expand_path(cls, v: Path) -> Path:
        return Path(os.path.expanduser(v))

    def _not_implemented(self) -> None:
        raise NotImplementedError(
            "Cloud watched paths are scaffolded but not yet implemented; "
            "planned for a follow-up."
        )

    def hot_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        self._not_implemented()
        raise AssertionError

    def warm_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        self._not_implemented()
        raise AssertionError

    def cold_scan(self, state: StateStore) -> DiffResult:  # type: ignore[override]
        self._not_implemented()
        raise AssertionError


WatchedPath = Annotated[
    Union[LocalWatchedPath, SSHWatchedPath, CloudWatchedPath],
    Field(discriminator="kind"),
]


class SyncConfig(BaseModel):
    """Timing configuration for sync loops."""

    metadata_interval: int = 1200


class DatalabConfig(BaseModel):
    """Connection details for the target datalab instances."""

    name: str = Field(
        ...,
        description="Unique name for this datalab instance (used to reference from WatchedPath)",
    )
    url: str = Field(
        ...,
        description="Base URL of the datalab instance, e.g. https://datalab.example.org",
    )
    api_key: str | None = Field(
        None,
        description=(
            "API key for authenticating to the datalab instance. If unset, "
            "the underlying datalab client looks it up from the appropriate "
            "<PREFIX>_DATALAB_API_KEY env var, where <PREFIX> matches the "
            "deployment's identifier prefix."
        ),
    )


class BeholderConfig(BaseModel):
    """Top-level configuration for the beholder daemon."""

    version: int = Field(
        LATEST_CONFIG_VERSION,
        description=(
            "On-disk config schema version. Absent in older configs (treated "
            "as v1). `load_config` migrates older versions forward before "
            "validation."
        ),
    )
    datalabs: list[DatalabConfig] = Field(
        ...,
        description="List of datalab instances to post to; multiple versions of the same datalab can be included with different names for different paths or users",
    )
    watched_paths: list[WatchedPath]
    sync: SyncConfig = SyncConfig()
    reset_scan_clocks_on_startup: bool = Field(
        False,
        description=(
            "If true, clear every watched path's stored hot/warm/cold scan "
            "timestamps when the daemon starts, so a full cold scan runs on "
            "the first tick instead of waiting for the configured interval. "
            "File sync state is untouched — already-synced files are not "
            "re-uploaded."
        ),
    )
    log_level: str = "info"
    state_db: Path = DEFAULT_STATE_DB_PATH

    @model_validator(mode="before")
    @classmethod
    def default_watched_path_kind(cls, data: Any) -> Any:
        """Default missing `kind` on watched_paths entries to 'local'.

        Pydantic discriminated unions require the discriminator field to be
        present on input. This shim lets older YAMLs (which never had a
        `kind` field) keep working: anything without `kind` is treated as a
        local path.
        """
        if isinstance(data, dict):
            wps = data.get("watched_paths")
            if isinstance(wps, list):
                for entry in wps:
                    if isinstance(entry, dict) and "kind" not in entry:
                        entry["kind"] = "local"
        return data

    @field_validator("state_db")
    @classmethod
    def expand_state_db(cls, v: Path) -> Path:
        # See LocalWatchedPath.expand_path — resolution happens in
        # ``load_config`` so relative paths track the YAML location.
        return Path(os.path.expanduser(v))

    @model_validator(mode="after")
    def validate_datalab_refs(self) -> BeholderConfig:
        if not self.datalabs:
            raise ValueError("at least one datalab instance must be configured")

        names = [d.name for d in self.datalabs]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"datalab names must be unique; duplicates: {sorted(duplicates)}"
            )

        valid = set(names)
        for wp in self.watched_paths:
            if wp.datalab is None:
                if len(self.datalabs) == 1:
                    wp.datalab = self.datalabs[0].name
                else:
                    raise ValueError(
                        f"watched_path {wp.name!r} must specify a datalab "
                        f"(multiple configured: {sorted(valid)})"
                    )
            elif wp.datalab not in valid:
                raise ValueError(
                    f"watched_path {wp.name!r} references unknown datalab "
                    f"{wp.datalab!r}; configured: {sorted(valid)}"
                )
        return self


def _migrate_raw_config(raw: dict, *, source: str) -> dict:
    """Walk the migrator chain from ``raw['version']`` up to the latest.

    Mutates and returns ``raw``. A missing ``version`` field is treated as
    v1 — older configs predate the field.
    """
    from datalab_beholder.config_migrations import MIGRATORS

    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a mapping: {source}")

    raw_version = raw.get("version", 1)
    if not isinstance(raw_version, int):
        raise ValueError(
            f"Config `version` must be an integer, got {raw_version!r}: {source}"
        )

    if raw_version > LATEST_CONFIG_VERSION:
        raise ValueError(
            f"Config {source} is version {raw_version}, but this beholder "
            f"only understands up to v{LATEST_CONFIG_VERSION}. Upgrade the "
            "daemon."
        )

    while raw_version < LATEST_CONFIG_VERSION:
        migrator = MIGRATORS.get(raw_version)
        if migrator is None:
            raise ValueError(
                f"No migrator registered for config v{raw_version} → "
                f"v{raw_version + 1} (source: {source})"
            )
        raw = migrator(raw)
        raw_version = raw.get("version", raw_version + 1)

    return raw


def load_config(path: Path | None = None) -> BeholderConfig:
    """Load and validate configuration from a YAML file.

    Args:
        path: Path to config file. Defaults to config.yaml alongside the package.

    Returns:
        Validated BeholderConfig instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config file is invalid.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH

    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Run 'datalab-beholder init' to create a template."
        )

    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Config file is empty: {path}")

    raw = _migrate_raw_config(raw, source=str(path))

    config = BeholderConfig(**raw)

    # Resolve relative `path` fields against the config file's directory
    # so a YAML written with `./data` works regardless of the shell's CWD.
    config_dir = path.parent
    for wp in config.watched_paths:
        wp_path = getattr(wp, "path", None)
        if isinstance(wp_path, Path) and not wp_path.is_absolute():
            wp.path = (config_dir / wp_path).resolve()
        elif isinstance(wp_path, Path):
            wp.path = wp_path.resolve()

    if not config.state_db.is_absolute():
        config.state_db = (config_dir / config.state_db).resolve()

    return config


def write_config_template(path: Path | None = None) -> Path:
    """Write a config template YAML file.

    Args:
        path: Where to write the template. Defaults to config.yaml alongside the package.

    Returns:
        The path where the template was written.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH

    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE)
    return path
