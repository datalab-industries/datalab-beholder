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
datalabs:
  - name: "example"
    url: "https://datalab.example.org"
    api_key: "your-api-key-here"  # or set DATALAB_API_KEY env var

watched_paths:
  - kind: "local"
    path: "/path/to/instrument/data"
    name: "Instrument-Name"
    datalab: "example"
    include_patterns:
      - "*"
    exclude_patterns: []
    # max_depth: null  # unlimited
    # scan:
    #   hot_interval: 60        # stat recently-modified files
    #   warm_interval: 3600     # directory-mtime walk
    #   cold_interval: 86400    # full walk; null disables
    #   hot_window: 86400       # "recent" cutoff for hot scan

sync:
  metadata_interval: 1200  # seconds (20 minutes)
  file_request_poll: 60    # seconds (1 minute)

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
        return Path(os.path.expanduser(v)).resolve()

    # Scan methods land in the next commit; for now inherit the
    # NotImplementedError stubs from WatchedPathBase.


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
        return Path(os.path.expanduser(v)).resolve()

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
    file_request_poll: int = 60


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

    datalabs: list[DatalabConfig] = Field(
        ...,
        description="List of datalab instances to post to; multiple versions of the same datalab can be included with different names for different paths or users",
    )
    watched_paths: list[WatchedPath]
    sync: SyncConfig = SyncConfig()
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
        return Path(os.path.expanduser(v)).resolve()

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

    return BeholderConfig(**raw)


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
