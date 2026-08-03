"""CLI interface for datalab-beholder."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import click

from datalab_beholder import __version__


@click.group(invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Filesystem watcher daemon for datalab instances."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(start)


@main.command()
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write the config file. Default: config.yaml alongside the package/executable",
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
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to config file. Default: config.yaml alongside the package/executable",
)
def scan(
    path: Path,
    name: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    max_depth: int | None,
    config_path: Path | None,
    pretty: bool,
) -> None:
    """Scan a directory and output structured JSON."""
    from datalab_beholder.scanner import scan_directory
    from datalab_beholder.config import load_config

    config = None

    if config_path:
        config = load_config(config_path)
        config.log_level = "DEBUG"

        logging.basicConfig(
            level=config.log_level.upper(),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            handlers=[
                logging.StreamHandler(sys.stderr),
            ],
        )

    indent = 2 if pretty else None

    if config:
        from datalab_beholder.config import LocalWatchedPath

        for d in config.watched_paths:
            if not isinstance(d, LocalWatchedPath):
                click.echo(
                    f"Skipping non-local watched path {d.name!r} (kind={d.kind!r}); "
                    "only local paths are scannable from the CLI today.",
                    err=True,
                )
                continue
            result = scan_directory(
                root=d.path,
                name=d.name,
                include_patterns=d.include_patterns,
                exclude_patterns=d.exclude_patterns,
                id_patterns=d.id_patterns,
                item_id_template=d.item_id_template,
                collection_id_template=d.collection_id_template,
                max_depth=d.max_depth,
            )
            click.echo(json.dumps(result.to_dict(), indent=indent))
    else:
        result = scan_directory(
            root=path,
            name=name,
            include_patterns=list(include),
            exclude_patterns=list(exclude),
            max_depth=max_depth,
        )
        click.echo(json.dumps(result.to_dict(), indent=indent))


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to config file. Default: config.yaml alongside the package/executable",
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
                # Pin UTF-8 so non-ASCII characters in messages don't blow
                # up on Windows installs (default `cp1252` can't encode
                # things like en-dashes or arrows).
                encoding="utf-8",
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
    help="Path to config file. Default: config.yaml alongside the package/executable",
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default=None,
    help="Override log level from config.",
)
def gui(config_path: Path | None, log_level: str | None) -> None:
    """Launch the beholder GUI."""
    try:
        from datalab_beholder.gui import BeholderGUI
    except ImportError:
        raise click.ClickException(
            "tkinter is required for the GUI but is not installed.\n"
            "  Ubuntu/Debian: sudo apt install python3-tk\n"
            "  Fedora:        sudo dnf install python3-tkinter\n"
            "  macOS:         brew install python-tk"
        )

    if log_level:
        logging.basicConfig(
            level=log_level.upper(),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level="INFO",
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )

    app = BeholderGUI(config_path)
    app.mainloop()


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to config file. Default: config.yaml alongside the package/executable",
)
@click.option(
    "--match-limit",
    type=int,
    default=3,
    show_default=True,
    help="How many sample id_pattern matches to show per watched path.",
)
def status(config_path: Path | None, match_limit: int) -> None:
    """Show daemon state, per-path routing, and sync history.

    Connects to each configured (datalab, user) pair to verify the
    resolved API key authenticates, then prints one block per watched
    path showing where its files will be sent and a sample of files
    that matched the configured `id_patterns`.
    """
    from datalab_beholder.config import LocalWatchedPath, load_config
    from datalab_beholder.daemon import _client_key
    from datalab_beholder.state import StateStore

    # Suppress the noisy datalab_api INFO logs that fire on client
    # construction — `status` should be quiet by default.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    config = load_config(config_path)

    click.echo(f"Loaded config: {config_path or '(default)'}")
    click.echo(f"  state_db: {config.state_db}")
    click.echo(f"  schema version: {config.version}\n")

    # Build clients up front so each watched-path block can probe its
    # resolved client. _build_clients does the env-var dance and the
    # eager handshake; failures show up as exceptions we catch below.
    from datalab_beholder.daemon import BeholderDaemon

    click.echo("Datalabs:")
    for d in config.datalabs:
        users = ", ".join(u.name for u in d.users) or "(none)"
        key_state = "set" if d.api_key else "missing"
        click.echo(f"  {d.name} → {d.url}")
        click.echo(f"    default api_key: {key_state}")
        click.echo(f"    users: {users}")
    click.echo()

    # Client construction handshakes with each datalab. If a server is
    # unreachable we don't want `status` to die — flag it on the offending
    # watched paths and keep printing the rest of the report.
    try:
        clients = BeholderDaemon._build_clients(config)
        client_error: str | None = None
    except Exception as exc:
        clients = {}
        client_error = str(exc)
        click.echo(f"Warning: could not connect to all datalabs: {exc}\n", err=True)

    state: StateStore | None = None
    if config.state_db.exists():
        state = StateStore(config.state_db)

    try:
        for wp in config.watched_paths:
            location = str(getattr(wp, "path", wp.kind))
            route_label = _client_key(wp.datalab or "?", wp.user)
            click.echo(f"{wp.name}")
            click.echo(f"  path: {location}")
            click.echo(f"  → datalab/user: {route_label}")
            if isinstance(wp, LocalWatchedPath):
                click.echo(f"  path exists: {wp.path.exists()}")
            else:
                click.echo(f"  kind: {wp.kind} (presence not checked)")

            client = (
                clients.get(_client_key(wp.datalab, wp.user)) if wp.datalab else None
            )
            if client is None:
                detail = f" ({client_error})" if client_error else ""
                click.echo(f"  connection: skipped{detail}")
            else:
                try:
                    reachable, authed = client.check_connection()
                except Exception as exc:
                    click.echo(f"  connection: error ({exc})")
                else:
                    click.echo(
                        f"  connection: reachable={reachable}, authenticated={authed}"
                    )

            if state is not None:
                last_sync = state.get_last_sync(wp.name)
                if last_sync:
                    from datetime import datetime, timezone

                    dt = datetime.fromtimestamp(last_sync, tz=timezone.utc)
                    click.echo(f"  last sync: {dt.isoformat()}")
                else:
                    click.echo("  last sync: never")

                pending = state.get_pending_changes(wp.name)
                click.echo(f"  pending changes: {len(pending)}")

            if isinstance(wp, LocalWatchedPath) and wp.path.exists():
                _echo_sample_matches(wp, limit=match_limit)
            click.echo()
    finally:
        if state is not None:
            state.close()


def _echo_sample_matches(wp: Any, *, limit: int) -> None:
    """Print up to ``limit`` files under ``wp.path`` that match its
    ``id_patterns``, with the resolved item_id/collection_id shown."""
    from datalab_beholder.scanner import scan_directory

    if not wp.id_patterns:
        click.echo("  id_patterns: (none configured — all files match)")
        return

    try:
        result = scan_directory(
            root=wp.path,
            name=wp.name,
            include_patterns=wp.include_patterns,
            exclude_patterns=wp.exclude_patterns,
            id_patterns=wp.id_patterns,
            item_id_template=wp.item_id_template,
            collection_id_template=wp.collection_id_template,
            max_depth=wp.max_depth,
        )
    except Exception as exc:
        click.echo(f"  id_patterns: scan failed ({exc})")
        return

    matched = [e for e in result.entries if not e.is_directory and e.ids.get("item_id")]
    click.echo(f"  id_patterns: {len(matched)}/{result.total_files} files match")
    for entry in matched[:limit]:
        ids = entry.ids
        bits = [f"item_id={ids.get('item_id')!r}"]
        if "collection_id" in ids:
            bits.append(f"collection_id={ids['collection_id']!r}")
        if "group_id" in ids:
            bits.append(f"group_id={ids['group_id']!r}")
        click.echo(f"    {entry.path} → {', '.join(bits)}")
    if len(matched) > limit:
        click.echo(f"    ... and {len(matched) - limit} more")
