"""CLI interface for datalab-beholder."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

import click

from datalab_beholder import __version__


def _setup_logging(level: str, logfile: Path | None = None) -> None:
    """Configure root logging: coloured columns on stderr (and optionally
    a plain-text rotating file).

    The console handler is a Rich handler, giving time | level | logger |
    message columns with level colouring (colour auto-disables when
    stderr is not a terminal). The file handler keeps the flat
    ANSI-free format.

    httpx/httpcore log every request and connection event at INFO/DEBUG,
    which drowns the beholder's own output when the root logger runs at
    debug — cap them at WARNING.
    """
    import shutil

    from rich.console import Console
    from rich.logging import RichHandler

    # Rich falls back to 80 columns when it can't detect a terminal,
    # which wraps almost every message; give it a generous floor.
    # omit_repeated_times would leave blank space in the time column for
    # messages logged within the same second — always print it instead.
    console_width = max(shutil.get_terminal_size().columns, 160)
    console_handler = RichHandler(
        console=Console(stderr=True, width=console_width),
        show_path=False,
        omit_repeated_times=False,
        log_time_format="%Y-%m-%d %H:%M:%S",
        rich_tracebacks=True,
    )
    console_handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))

    handlers: list[logging.Handler] = [console_handler]
    if logfile is not None:
        file_handler = logging.handlers.RotatingFileHandler(
            logfile,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            # Pin UTF-8 so non-ASCII characters in messages don't blow
            # up on Windows installs (default `cp1252` can't encode
            # things like en-dashes or arrows).
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        handlers.append(file_handler)

    logging.basicConfig(level=level.upper(), handlers=handlers)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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

    _setup_logging(config.log_level, logfile=config.state_db.parent / "beholder.log")

    daemon = BeholderDaemon(config)
    daemon.start()


@main.command(name="dry-run")
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
    help="Override log level from config. Use debug to see every skipped file and why.",
)
def dry_run(config_path: Path | None, log_level: str | None) -> None:
    """Simulate one full scan + attach pass without making any changes.

    Scans every watched path exactly like `start` would, reports how
    each file fares against the configured include/exclude/id patterns
    (at debug level, every skipped file is logged with the reason),
    then queries the datalab server read-only to report what the
    daemon would do: items it would create, files it would upload or
    replace, and blocks it would create. Neither the local state DB
    nor the server is modified.
    """
    from datalab_beholder.config import load_config
    from datalab_beholder.daemon import dry_run as run_dry_run

    config = load_config(config_path)
    if log_level:
        config.log_level = log_level

    # Log to stderr only — a dry run shouldn't touch beholder.log either.
    _setup_logging(config.log_level)

    try:
        actions = run_dry_run(config)
    except FileNotFoundError as e:
        # An unscannable watched path is fatal — unlike an unreachable
        # datalab, which only degrades the report.
        raise click.ClickException(str(e))

    if not actions:
        click.echo("Dry run complete — no changes were made, and nothing is pending.")
        return

    labels = {
        "create_item": "create {n} item(s): {items}",
        "upload": "upload {n} new file(s)",
        "replace": "replace {n} existing file(s)",
        "create_block": "create {n} data block(s)",
        "skip": "skip {n} file(s) whose item is missing and not creatable",
        "attach_unknown": (
            "attach {n} file(s) — datalab unreachable, cannot tell create from replace"
        ),
    }
    click.echo("Dry run complete — no changes were made. A real run would:")
    for wp in config.watched_paths:
        wp_actions = [a for a in actions if a.watched_path == wp.name]
        if not wp_actions:
            continue
        click.echo(f"\n  {wp.name}:")
        for key, template in labels.items():
            matched = [a for a in wp_actions if a.action == key]
            if not matched:
                continue
            item_ids = sorted({a.item_id for a in matched})
            shown = ", ".join(item_ids[:10])
            if len(item_ids) > 10:
                shown += f", … ({len(item_ids) - 10} more)"
            click.echo("    - " + template.format(n=len(matched), items=shown))


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

    _setup_logging(log_level or "INFO")

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
def status(config_path: Path | None) -> None:
    """Show daemon state and sync history."""
    from datalab_beholder.config import load_config
    from datalab_beholder.state import StateStore

    config = load_config(config_path)

    if not config.state_db.exists():
        click.echo("No state database found. Has the daemon been run?")
        return

    from datalab_beholder.config import LocalWatchedPath

    state = StateStore(config.state_db)
    try:
        for wp in config.watched_paths:
            location = str(getattr(wp, "path", wp.kind))
            click.echo(f"\n{wp.name} ({location})")
            if isinstance(wp, LocalWatchedPath):
                click.echo(f"  Path exists: {wp.path.exists()}")
            else:
                click.echo(f"  Kind: {wp.kind} (presence not checked)")

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
