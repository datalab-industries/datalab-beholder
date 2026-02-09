"""CLI interface for datalab-beholder."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

import click

from datalab_beholder import __version__


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """Filesystem watcher daemon for datalab instances."""


@main.command()
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write the config file. Default: ~/.datalab-beholder/config.yaml",
)
def init(path: Path | None) -> None:
    """Create a configuration template."""
    from datalab_beholder.config import write_config_template

    out = write_config_template(path)
    click.echo(f"Config template written to {out}")
    click.echo("Edit this file with your datalab URL, API key, and watched paths.")


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--name", default="", help="Label for this scan.")
@click.option(
    "--include",
    multiple=True,
    default=["*"],
    help="Glob pattern(s) to include (default: *).",
)
@click.option(
    "--exclude",
    multiple=True,
    default=[],
    help="Glob pattern(s) to exclude.",
)
@click.option("--max-depth", type=int, default=None, help="Maximum recursion depth.")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
def scan(
    path: Path,
    name: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    max_depth: int | None,
    pretty: bool,
) -> None:
    """Scan a directory and output structured JSON."""
    from datalab_beholder.scanner import scan_directory

    result = scan_directory(
        root=path,
        name=name,
        include_patterns=list(include),
        exclude_patterns=list(exclude),
        max_depth=max_depth,
    )

    indent = 2 if pretty else None
    click.echo(json.dumps(result.to_dict(), indent=indent))


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to config file. Default: ~/.datalab-beholder/config.yaml",
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default=None,
    help="Override log level from config.",
)
def start(config_path: Path | None, log_level: str | None) -> None:
    """Start the beholder daemon."""
    from datalab_beholder.config import load_config
    from datalab_beholder.daemon import BeholderDaemon

    config = load_config(config_path)
    if log_level:
        config.log_level = log_level

    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.handlers.RotatingFileHandler(
                config.state_db.parent / "beholder.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )

    daemon = BeholderDaemon(config)
    daemon.start()


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to config file. Default: ~/.datalab-beholder/config.yaml",
)
def status(config_path: Path | None) -> None:
    """Show daemon state and sync history."""
    from datalab_beholder.config import load_config
    from datalab_beholder.state import StateStore

    config = load_config(config_path)

    if not config.state_db.exists():
        click.echo("No state database found. Has the daemon been run?")
        return

    state = StateStore(config.state_db)
    try:
        for wp in config.watched_paths:
            click.echo(f"\n{wp.name} ({wp.path})")
            click.echo(f"  Path exists: {wp.path.exists()}")

            last_sync = state.get_last_sync(wp.name)
            if last_sync:
                from datetime import datetime, timezone

                dt = datetime.fromtimestamp(last_sync, tz=timezone.utc)
                click.echo(f"  Last sync: {dt.isoformat()}")
            else:
                click.echo("  Last sync: never")

            pending = state.get_pending_changes(wp.name)
            click.echo(f"  Pending changes: {len(pending)}")
    finally:
        state.close()
