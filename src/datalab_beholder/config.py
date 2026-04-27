"""Configuration model and YAML loading for datalab-beholder."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
import yaml
from pydantic import BaseModel, field_validator, Field

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
  - path: "/path/to/instrument/data"
    name: "Instrument-Name"
    datalab: "example"
    include_patterns:
      - "*"
    exclude_patterns: []
    # max_depth: null  # unlimited

sync:
  metadata_interval: 1200  # seconds (20 minutes)
  file_request_poll: 60    # seconds (1 minute)

log_level: "info"
# state_db: "state.db"  # defaults to alongside the package/executable
"""


class WatchedPath(BaseModel):
    """A directory to monitor for file changes."""

    path: Path = Field(..., description="Path to watch for changes")
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

    @field_validator("path")
    @classmethod
    def expand_path(cls, v: Path) -> Path:
        return Path(os.path.expanduser(v)).resolve()

    @field_validator("id_patterns")
    @classmethod
    def validate_id_patterns(cls, v: list[str]) -> list[str]:
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
        return v


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
    api_key: str = Field(
        ...,
        description="API key for authenticating to the datalab instance (or set <PREFIX>_DATALAB_API_KEY env var)",
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def resolve_api_key(cls, v: str) -> str:
        if not v or v == "your-api-key-here":
            env_key = os.environ.get("DATALAB_API_KEY", "")
            if env_key:
                return env_key.strip("'").strip('"')
            return v
        return v


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

    @field_validator("state_db")
    @classmethod
    def expand_state_db(cls, v: Path) -> Path:
        return Path(os.path.expanduser(v)).resolve()


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
