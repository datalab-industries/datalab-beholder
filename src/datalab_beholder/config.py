"""Configuration model and YAML loading for datalab-beholder."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

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
datalab:
  url: "https://datalab.example.org"
  api_key: "your-api-key-here"  # or set DATALAB_API_KEY env var

watched_paths:
  - path: "/path/to/instrument/data"
    name: "Instrument-Name"
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

    path: Path
    name: str
    include_patterns: list[str] = ["*"]
    exclude_patterns: list[str] = []
    max_depth: int | None = None

    @field_validator("path")
    @classmethod
    def expand_path(cls, v: Path) -> Path:
        return Path(os.path.expanduser(v)).resolve()


class SyncConfig(BaseModel):
    """Timing configuration for sync loops."""

    metadata_interval: int = 1200
    file_request_poll: int = 60


class DatalabConfig(BaseModel):
    """Connection details for the target datalab instance."""

    url: str
    api_key: str = ""

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

    datalab: DatalabConfig
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
