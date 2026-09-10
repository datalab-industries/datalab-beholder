"""Tkinter GUI for the beholder daemon.

Provides a visual control panel styled after EPICS/synchrotron control
interfaces: dark background, status indicator lights, dense layout,
monospaced activity log.

The window works in two modes, and switches between them by itself:

* **Observer** — the default. The GUI opens the configured state database
  read-only and polls it, so it reports what the daemon is doing whether
  that daemon lives in this process, in a separate ``beholder run``, or in
  a service started at boot. Nothing here writes to the database, so an
  observer can never corrupt a running daemon's state.
* **Driver** — after "Start", the GUI additionally owns a `BeholderDaemon`
  and drives it via ``daemon.tick()`` scheduled through ``root.after()``.

Which mode applies is inferred from the heartbeat row the daemon writes on
every tick: a live heartbeat belonging to another process means somebody
else is doing the work, and the GUI refuses to start a competing daemon.

Connection probes run on a worker thread and report back through a queue,
so an unreachable instrument network cannot freeze the UI.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from collections.abc import Callable

import yaml

from datalab_beholder.config import BeholderConfig, load_config, write_config_template
from datalab_beholder.daemon import BeholderDaemon
from datalab_beholder.state import Heartbeat, PathSummary, StateStore

log = logging.getLogger(__name__)

# -- Colours (EPICS aesthetic) -----------------------------------------------
BG = "#2b2b2b"
BG_LIGHT = "#3c3c3c"
BG_DARK = "#1e1e1e"
FG = "#d4d4d4"
FG_DIM = "#888888"
GREEN = "#00cc00"
YELLOW = "#cccc00"
RED = "#cc0000"
BLUE = "#4a9eff"
GREY = "#666666"

# -- Fonts (derived from system defaults after Tk root exists) ---------------
FONT_TITLE: tuple[str, int, str]
FONT_HEADING: tuple[str, int, str]
FONT: tuple[str, int]
FONT_SM: tuple[str, int]
FONT_MONO: tuple[str, int]


def _init_fonts() -> None:
    """Populate font constants from the platform's default fonts."""
    global FONT_TITLE, FONT_HEADING, FONT, FONT_SM, FONT_MONO
    default = tkfont.nametofont("TkDefaultFont").actual()
    family = default["family"]
    size = abs(default["size"])  # negative means pixels on some platforms
    mono = tkfont.nametofont("TkFixedFont").actual()["family"]
    FONT_TITLE = (family, size + 8, "bold")
    FONT_HEADING = (family, size + 2, "bold")
    FONT = (family, size)
    FONT_SM = (family, max(size - 1, 8))
    FONT_MONO = (mono, size)


TICK_MS = 1000
OBSERVE_MS = 2000  # how often to re-read the state DB
CONNECTION_CHECK_TICKS = 30  # probe the server every 30 observer ticks
HEARTBEAT_TIMEOUT = 30.0  # seconds before another process's claim is stale

LOG_LEVELS = ("debug", "info", "warning", "error")
PATH_KINDS = ("local", "ssh", "cloud")
CLOUD_PROVIDERS = ("auto", "onedrive", "gdrive")


# -- Small formatting helpers -------------------------------------------------


def _fmt_ts(ts: float | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a unix timestamp, or an em dash when it is unset."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime(fmt)


def _fmt_ago(ts: float | None) -> str:
    """Render a timestamp as a compact relative age (e.g. ``4m ago``)."""
    if not ts:
        return "never"
    delta = max(0.0, datetime.now().timestamp() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _split_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _parse_mapping(raw: str) -> dict[str, str]:
    """Parse ``glob = value`` lines into a dict.

    Used for `block_patterns`, where both halves matter and a comma-joined
    single line would be unreadable for more than about two entries.
    """
    out: dict[str, str] = {}
    for line in _split_lines(raw):
        if "=" not in line:
            raise ValueError(f"expected 'pattern = block_type', got {line!r}")
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ValueError(f"expected 'pattern = block_type', got {line!r}")
        out[key] = value
    return out


def _format_mapping(mapping: dict[str, str]) -> str:
    return "\n".join(f"{k} = {v}" for k, v in mapping.items())


def _parse_optional_int(raw: str, label: str) -> int | None:
    """Parse an int entry where empty means "unset/disabled"."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{label} must be a whole number of seconds (or blank)")


# -- Themed widget factories --------------------------------------------------
#
# Tk has no stylesheet, so every widget otherwise repeats the same dozen
# colour keywords. These wrappers keep the layout code readable and the
# palette consistent.


def _label(parent: tk.Misc, text: str, **kw: Any) -> tk.Label:
    opts: dict[str, Any] = {"font": FONT, "bg": BG, "fg": FG, "anchor": "w"}
    opts.update(kw)
    return tk.Label(parent, text=text, **opts)


def _entry(parent: tk.Misc, var: tk.Variable, **kw: Any) -> tk.Entry:
    opts: dict[str, Any] = {
        "bg": BG_LIGHT,
        "fg": FG,
        "insertbackground": FG,
        "relief": "flat",
        "font": FONT,
    }
    opts.update(kw)
    return tk.Entry(parent, textvariable=var, **opts)


def _button(
    parent: tk.Misc, text: str, command: Callable[[], None], **kw: Any
) -> tk.Button:
    opts: dict[str, Any] = {
        "bg": BG_LIGHT,
        "fg": FG,
        "activebackground": BG,
        "activeforeground": kw.get("fg", FG),
        "relief": "flat",
        "padx": 8,
        "pady": 2,
        "font": FONT,
        "disabledforeground": GREY,
    }
    opts.update(kw)
    opts["activeforeground"] = opts["fg"]
    return tk.Button(parent, text=text, command=command, **opts)


def _check(parent: tk.Misc, text: str, var: tk.BooleanVar, **kw: Any) -> tk.Checkbutton:
    opts: dict[str, Any] = {
        "font": FONT_SM,
        "bg": BG,
        "fg": FG,
        "selectcolor": BG_LIGHT,
        "activebackground": BG,
        "activeforeground": FG,
        "relief": "flat",
        "highlightthickness": 0,
        "borderwidth": 0,
        "anchor": "w",
    }
    opts.update(kw)
    return tk.Checkbutton(parent, text=text, variable=var, **opts)


def _option_menu(
    parent: tk.Misc, var: tk.StringVar, values: tuple[str, ...] | list[str], **kw: Any
) -> tk.OptionMenu:
    menu = tk.OptionMenu(parent, var, *(values or [""]))
    opts: dict[str, Any] = {
        "bg": BG_LIGHT,
        "fg": FG,
        "activebackground": BG,
        "activeforeground": FG,
        "highlightthickness": 0,
        "relief": "flat",
        "font": FONT_SM,
    }
    opts.update(kw)
    menu.configure(**opts)
    menu["menu"].configure(bg=BG_LIGHT, fg=FG)
    return menu


def _text(parent: tk.Misc, height: int = 4, **kw: Any) -> tk.Text:
    opts: dict[str, Any] = {
        "bg": BG_LIGHT,
        "fg": FG,
        "insertbackground": FG,
        "relief": "flat",
        "font": FONT_MONO,
        "height": height,
        "wrap": "none",
        "highlightthickness": 0,
    }
    opts.update(kw)
    return tk.Text(parent, **opts)


def _tooltip(widget: tk.Widget, text: str) -> None:
    """Show ``text`` in a small borderless window while the cursor is over
    ``widget``.

    Several settings (notably the per-datalab ``sudo`` toggle) need more
    explanation than fits in a label, and Tk has no built-in tooltip.
    """
    state: dict[str, tk.Toplevel | None] = {"window": None}

    def show(_event: object = None) -> None:
        if state["window"] is not None:
            return
        x = widget.winfo_rootx() + 20
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(
            win,
            text=text,
            font=FONT_SM,
            bg=BG_LIGHT,
            fg=FG,
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
        ).pack()
        state["window"] = win

    def hide(_event: object = None) -> None:
        win = state["window"]
        if win is not None:
            win.destroy()
            state["window"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<Destroy>", hide)


class _ScrollFrame(tk.Frame):
    """A vertically scrollable container.

    The settings tabs hold more rows than fit on an instrument PC's screen,
    and Tk gives us no scrolling container out of the box — this is the
    standard Canvas-plus-inner-Frame construction. Add children to
    ``.body``.
    """

    def __init__(self, parent: tk.Misc, **kw: Any):
        super().__init__(parent, bg=BG, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")

        bar = tk.Scrollbar(
            self,
            orient="vertical",
            command=self._canvas.yview,
            bg=BG_LIGHT,
            troughcolor=BG,
        )
        bar.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=bar.set)

        self.body = tk.Frame(self._canvas, bg=BG)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_body_configure(self, _event: object) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event: Any) -> None:
        # Keep the inner frame as wide as the viewport so children can
        # stretch horizontally instead of collapsing to their requested size.
        self._canvas.itemconfigure(self._window, width=event.width)


# -- Logging handler that writes to a Tk Text widget -------------------------


class TextWidgetHandler(logging.Handler):
    """Logging handler that appends formatted records to a Tk Text widget."""

    def __init__(self, text_widget: tk.Text, max_lines: int = 2000):
        super().__init__()
        self._text = text_widget
        self._max_lines = max_lines

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = f"{ts}  {record.getMessage()}\n"
            self._text.configure(state="normal")
            self._text.insert("end", msg, record.levelname)
            # A GUI left running for weeks would otherwise grow the Text
            # widget without bound.
            excess = int(self._text.index("end-1c").split(".")[0]) - self._max_lines
            if excess > 0:
                self._text.delete("1.0", f"{excess + 1}.0")
            self._text.see("end")
            self._text.configure(state="disabled")
        except tk.TclError:
            pass  # widget destroyed


# -- Observer: read-only view of whatever daemon owns the state DB -----------


@dataclass
class ObserverSnapshot:
    """One read of the state database."""

    available: bool = False
    summaries: list[PathSummary] = field(default_factory=list)
    heartbeat: Heartbeat | None = None
    error: str | None = None

    @property
    def total_pending(self) -> int:
        return sum(s.pending for s in self.summaries)

    @property
    def last_scan(self) -> float | None:
        stamps = [
            ts
            for s in self.summaries
            for ts in (s.scans.hot, s.scans.warm, s.scans.cold)
            if ts
        ]
        return max(stamps) if stamps else None

    @property
    def last_sync(self) -> float | None:
        stamps = [s.last_synced for s in self.summaries if s.last_synced]
        return max(stamps) if stamps else None


class StateObserver:
    """Polls a beholder state database read-only.

    Deliberately opens its own connection in SQLite's ``mode=ro`` rather
    than sharing the daemon's: that makes it impossible for the GUI to
    mutate sync state, and lets it observe a daemon in a completely
    separate process. The database may not exist yet (no daemon has ever
    run), so every read tolerates failure and simply reports unavailable —
    the GUI keeps polling and picks the daemon up when it appears.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._store: StateStore | None = None

    def close(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass
            self._store = None

    def set_path(self, db_path: Path) -> None:
        """Point the observer at a different database (config reloaded)."""
        db_path = Path(db_path)
        if db_path != self.db_path:
            self.close()
            self.db_path = db_path

    def _connect(self) -> StateStore | None:
        if self._store is not None:
            return self._store
        if not self.db_path.exists():
            return None
        try:
            self._store = StateStore(self.db_path, read_only=True)
        except Exception as e:
            log.debug("Could not open state DB %s read-only: %s", self.db_path, e)
            return None
        return self._store

    def poll(self) -> ObserverSnapshot:
        store = self._connect()
        if store is None:
            return ObserverSnapshot(available=False)
        try:
            return ObserverSnapshot(
                available=True,
                summaries=store.summarise(),
                heartbeat=store.get_heartbeat(),
            )
        except Exception as e:
            # The daemon may have recreated the DB underneath us (e.g. the
            # file was deleted and a fresh one written). Drop the handle so
            # the next poll reconnects rather than erroring forever.
            log.debug("State DB poll failed: %s", e)
            self.close()
            return ObserverSnapshot(available=False, error=str(e))


class DaemonPresence:
    """Interprets a heartbeat into something the UI can act on."""

    NONE = "none"
    LOCAL = "local"
    EXTERNAL = "external"
    STALE = "stale"

    def __init__(self, heartbeat: Heartbeat | None, driving_locally: bool):
        self.heartbeat = heartbeat
        if heartbeat is None:
            self.kind = self.LOCAL if driving_locally else self.NONE
        elif heartbeat.is_stale(timeout=HEARTBEAT_TIMEOUT):
            self.kind = self.LOCAL if driving_locally else self.STALE
        elif heartbeat.is_this_process() or driving_locally:
            self.kind = self.LOCAL
        else:
            self.kind = self.EXTERNAL

    @property
    def blocks_start(self) -> bool:
        """Whether starting a local daemon would fight an existing one."""
        return self.kind == self.EXTERNAL

    def describe(self) -> tuple[str, str]:
        """Return ``(text, colour)`` for the daemon indicator."""
        hb = self.heartbeat
        if self.kind == self.LOCAL:
            return "This window", GREEN
        if self.kind == self.EXTERNAL and hb is not None:
            return f"External — pid {hb.pid} on {hb.hostname}", BLUE
        if self.kind == self.STALE and hb is not None:
            return f"Stale — pid {hb.pid}, last beat {_fmt_ago(hb.last_tick)}", YELLOW
        return "Not running", GREY


# -- Connection probing (off the UI thread) ----------------------------------


class ConnectionProbe:
    """Checks datalab reachability on a worker thread.

    The probe does real network I/O with multi-second timeouts. Running it
    inline would freeze the window every time it fires — unacceptable for a
    GUI that is meant to sit in the background on an instrument PC with a
    flaky network — so results come back through a queue that the Tk loop
    drains.
    """

    def __init__(self) -> None:
        self._results: queue.Queue[tuple[int, int, int]] = queue.Queue()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request(self, clients: dict[str, Any]) -> None:
        """Start a probe, unless one is already in flight."""
        if self.busy or not clients:
            return

        def run() -> None:
            total = len(clients)
            reachable = authed = 0
            for name, client in clients.items():
                try:
                    ok, auth = client.check_connection()
                except Exception:
                    log.debug("Connection check failed for %s", name, exc_info=True)
                    continue
                reachable += bool(ok)
                authed += bool(auth)
            self._results.put((total, reachable, authed))

        self._thread = threading.Thread(
            target=run, name="beholder-connection-probe", daemon=True
        )
        self._thread.start()

    def poll(self) -> tuple[int, int, int] | None:
        """Return the newest result, or None if nothing has landed."""
        latest = None
        while True:
            try:
                latest = self._results.get_nowait()
            except queue.Empty:
                return latest


# -- Main GUI ----------------------------------------------------------------


class BeholderGUI(tk.Tk):
    """Main beholder GUI window."""

    def __init__(
        self,
        config_path: Path | None = None,
        autostart: bool = False,
        minimized: bool = False,
    ):
        super().__init__()
        _init_fonts()
        self.title("BEHOLDER")
        self.configure(bg=BG)
        self.geometry("820x640")
        self.minsize(640, 480)

        # Indicator widgets (populated by _build_status_panel)
        self._server_ind: tuple[tk.Canvas, int]
        self._auth_ind: tuple[tk.Canvas, int]
        self._sync_ind: tuple[tk.Canvas, int]
        self._daemon_ind: tuple[tk.Canvas, int]
        self._server_ind_label: tk.Label
        self._auth_ind_label: tk.Label
        self._sync_ind_label: tk.Label
        self._daemon_ind_label: tk.Label
        self._last_scan_val: tk.Label
        self._last_push_val: tk.Label
        self._pending_val: tk.Label
        self._tracked_val: tk.Label

        self._config_path = config_path
        try:
            self._config = load_config(config_path)
        except FileNotFoundError:
            log.info("No config file found — creating template")
            written = write_config_template(config_path)
            self._config_path = written
            self._config = load_config(written)

        self._daemon: BeholderDaemon | None = None
        self._running = False
        self._observe_counter = 0

        # Connection state — aggregated across all configured datalabs.
        self._n_total = 0
        self._n_reachable = 0
        self._n_authed = 0
        self._probe = ConnectionProbe()
        self._probe_clients: dict[str, Any] | None = None

        self._observer = StateObserver(self._config.state_db)
        self._snapshot = ObserverSnapshot()
        self._presence = DaemonPresence(None, driving_locally=False)

        self._build_ui()
        self._install_log_handler()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Observing starts immediately and never stops: the window is useful
        # (and is the point of `--autostart`) even with no local daemon.
        self._observe()

        if minimized:
            self.iconify()
        if autostart:
            # Defer past the first observe so an already-running external
            # daemon is detected before we try to start a competing one.
            self.after(100, self._autostart)

    # -- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)  # paths table
        self.rowconfigure(3, weight=2)  # activity log gets the stretch

        self._init_ttk_style()
        self._build_header()
        self._build_status_panel()
        self._build_paths_table()
        self._build_activity_log()

    def _init_ttk_style(self) -> None:
        """Theme the ttk widgets (Treeview, Notebook) to match.

        Only the 'clam' theme honours background/foreground overrides on
        every platform; the native themes ignore them, which would leave a
        white table in the middle of a dark window.
        """
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover - platform dependent
            pass
        style.configure(
            "Beholder.Treeview",
            background=BG_DARK,
            fieldbackground=BG_DARK,
            foreground=FG,
            borderwidth=0,
            rowheight=20,
        )
        style.configure(
            "Beholder.Treeview.Heading",
            background=BG_LIGHT,
            foreground=FG_DIM,
            relief="flat",
        )
        style.map(
            "Beholder.Treeview",
            background=[("selected", "#264f78")],
            foreground=[("selected", FG)],
        )
        style.configure("Beholder.TNotebook", background=BG, borderwidth=0)
        style.configure(
            "Beholder.TNotebook.Tab",
            background=BG_LIGHT,
            foreground=FG,
            padding=(12, 6),
            borderwidth=0,
        )
        style.map(
            "Beholder.TNotebook.Tab",
            background=[("selected", BG)],
            foreground=[("selected", GREEN)],
        )

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        header.columnconfigure(0, weight=1)

        title = tk.Frame(header, bg=BG)
        title.grid(row=0, column=0, sticky="w")
        tk.Label(title, text="BEHOLDER", font=FONT_TITLE, bg=BG, fg=FG).pack(
            side="left"
        )
        self._config_label = tk.Label(
            title,
            text=str(self._config_path or ""),
            font=FONT_SM,
            bg=BG,
            fg=FG_DIM,
        )
        self._config_label.pack(side="left", padx=(10, 0), pady=(8, 0))

        btn_frame = tk.Frame(header, bg=BG)
        btn_frame.grid(row=0, column=1, sticky="e")

        self._settings_btn = _button(btn_frame, "Settings", self._open_settings)
        self._settings_btn.pack(side="left", padx=(0, 4))

        self._start_stop_btn = _button(
            btn_frame, "Start", self._toggle_daemon, fg=GREEN
        )
        self._start_stop_btn.pack(side="left")

    def _build_status_panel(self) -> None:
        panel = tk.Frame(self, bg=BG)
        panel.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        # The value column absorbs the slack; giving it to the name column
        # instead would strand each status text against the right edge.
        panel.columnconfigure(2, weight=1)

        tk.Label(
            panel, text="STATUS", font=FONT_HEADING, bg=BG, fg=FG_DIM, anchor="w"
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        indicators = [
            ("Daemon", "_daemon_ind"),
            ("Server", "_server_ind"),
            ("Auth", "_auth_ind"),
            ("Sync", "_sync_ind"),
        ]
        for i, (label, attr) in enumerate(indicators, start=1):
            canvas = tk.Canvas(panel, width=12, height=12, bg=BG, highlightthickness=0)
            canvas.grid(row=i, column=0, sticky="w", padx=(4, 6))
            oval = canvas.create_oval(1, 1, 11, 11, fill=GREY, outline="")
            setattr(self, attr, (canvas, oval))

            tk.Label(
                panel, text=label, font=FONT, bg=BG, fg=FG, anchor="w", width=8
            ).grid(row=i, column=1, sticky="w")

            status_label = tk.Label(
                panel, text="—", font=FONT, bg=BG, fg=FG_DIM, anchor="w"
            )
            status_label.grid(row=i, column=2, sticky="w")
            setattr(self, f"{attr}_label", status_label)

        sep = tk.Frame(panel, bg=BG_LIGHT, height=1)
        sep.grid(row=len(indicators) + 1, column=0, columnspan=3, sticky="ew", pady=6)

        stats = tk.Frame(panel, bg=BG)
        stats.grid(row=len(indicators) + 2, column=0, columnspan=3, sticky="ew")

        for col, (label, attr) in enumerate(
            [
                ("Last scan:", "_last_scan_val"),
                ("Last push:", "_last_push_val"),
                ("Pending:", "_pending_val"),
                ("Tracked:", "_tracked_val"),
            ]
        ):
            tk.Label(stats, text=label, font=FONT_SM, bg=BG, fg=FG_DIM).grid(
                row=0, column=col * 2, sticky="w", padx=(0 if col == 0 else 12, 4)
            )
            val = tk.Label(stats, text="—", font=FONT_SM, bg=BG, fg=FG)
            val.grid(row=0, column=col * 2 + 1, sticky="w")
            setattr(self, attr, val)

    def _build_paths_table(self) -> None:
        frame = tk.Frame(self, bg=BG)
        frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        tk.Label(frame, text="WATCHED PATHS", font=FONT_HEADING, bg=BG, fg=FG_DIM).grid(
            row=0, column=0, sticky="w"
        )

        columns = ("path", "datalab", "pending", "synced", "total", "scan", "sync")
        headings = {
            "path": "Name",
            "datalab": "datalab",
            "pending": "Pending",
            "synced": "Synced",
            "total": "Tracked",
            "scan": "Last scan",
            "sync": "Last sync",
        }
        widths = {
            "path": 180,
            "datalab": 90,
            "pending": 70,
            "synced": 70,
            "total": 70,
            "scan": 110,
            "sync": 110,
        }

        self._paths_tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            style="Beholder.Treeview",
            height=5,
        )
        for col in columns:
            self._paths_tree.heading(col, text=headings[col])
            self._paths_tree.column(
                col,
                width=widths[col],
                anchor="w" if col in ("path", "datalab") else "center",
                stretch=col == "path",
            )
        self._paths_tree.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self._paths_tree.tag_configure("pending", foreground=YELLOW)
        self._paths_tree.tag_configure("idle", foreground=FG)
        self._paths_tree.tag_configure("unseen", foreground=FG_DIM)

        bar = tk.Scrollbar(
            frame, command=self._paths_tree.yview, bg=BG_LIGHT, troughcolor=BG
        )
        bar.grid(row=1, column=1, sticky="ns", pady=(4, 0))
        self._paths_tree.configure(yscrollcommand=bar.set)

    def _build_activity_log(self) -> None:
        log_frame = tk.Frame(self, bg=BG)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        header = tk.Frame(log_frame, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        tk.Label(header, text="ACTIVITY LOG", font=FONT_HEADING, bg=BG, fg=FG_DIM).grid(
            row=0, column=0, sticky="w"
        )

        _button(header, "Clear Log", self._clear_log, padx=6, pady=1).grid(
            row=0, column=1, sticky="e"
        )

        text_frame = tk.Frame(log_frame, bg=BG)
        text_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self._log_text = tk.Text(
            text_frame,
            font=FONT_MONO,
            bg=BG_DARK,
            fg=FG,
            insertbackground=FG,
            selectbackground="#264f78",
            state="disabled",
            wrap="word",
            height=10,
            borderwidth=0,
            highlightthickness=0,
        )
        self._log_text.grid(row=0, column=0, sticky="nsew")
        # Colour by severity so a warning is findable in a long log.
        self._log_text.tag_configure("WARNING", foreground=YELLOW)
        self._log_text.tag_configure("ERROR", foreground=RED)
        self._log_text.tag_configure("CRITICAL", foreground=RED)
        self._log_text.tag_configure("DEBUG", foreground=FG_DIM)

        scrollbar = tk.Scrollbar(
            text_frame, command=self._log_text.yview, bg=BG_LIGHT, troughcolor=BG
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._log_text.configure(yscrollcommand=scrollbar.set)

    # -- Logging handler ------------------------------------------------------

    def _install_log_handler(self) -> None:
        handler = TextWidgetHandler(self._log_text)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.setLevel(logging.INFO)
        logging.getLogger("datalab_beholder").addHandler(handler)
        self._log_handler = handler

    # -- Indicator helpers ----------------------------------------------------

    def _set_indicator(
        self,
        indicator: tuple[tk.Canvas, int],
        colour: str,
        label: tk.Label,
        text: str,
    ) -> None:
        canvas, oval = indicator
        canvas.itemconfig(oval, fill=colour)
        label.configure(text=text)

    # -- Daemon control -------------------------------------------------------

    def _autostart(self) -> None:
        """Start a daemon on launch, unless one is already running."""
        if self._presence.blocks_start:
            log.info(
                "Not autostarting: an external daemon already owns %s",
                self._config.state_db,
            )
            return
        self._start_daemon()

    def _toggle_daemon(self) -> None:
        if self._running:
            self._stop_daemon()
        else:
            self._start_daemon()

    def _start_daemon(self) -> None:
        if self._presence.blocks_start:
            hb = self._presence.heartbeat
            where = f"pid {hb.pid} on {hb.hostname}" if hb else "another process"
            messagebox.showwarning(
                "Daemon already running",
                "Another beholder daemon is already syncing this state "
                f"database:\n\n  {where}\n\n"
                "This window will keep showing its progress. Stop that "
                "daemon first if you want to run the sync from here.",
                parent=self,
            )
            return

        try:
            self._config = load_config(self._config_path)
        except Exception as e:
            messagebox.showerror("Config Error", str(e))
            return

        self._observer.set_path(self._config.state_db)
        self._daemon = BeholderDaemon(self._config)
        try:
            self._daemon.setup()
        except Exception as e:
            log.error("Failed to start daemon: %s", e)
            messagebox.showerror("Start Error", str(e))
            self._daemon = None
            return

        self._running = True
        self._start_stop_btn.configure(text="Stop", fg=RED, activeforeground=RED)
        self._settings_btn.configure(state="disabled")
        self._probe_clients = self._daemon.clients
        self._probe.request(self._probe_clients)
        self._tick()
        self._refresh_now()

    def _stop_daemon(self) -> None:
        self._running = False
        if self._daemon is not None:
            self._daemon.stop()
            self._daemon.shutdown()
            self._daemon = None
        self._probe_clients = None
        self._n_total = self._n_reachable = self._n_authed = 0
        self._start_stop_btn.configure(text="Start", fg=GREEN, activeforeground=GREEN)
        self._settings_btn.configure(state="normal")
        self._set_indicator(self._server_ind, GREY, self._server_ind_label, "—")
        self._set_indicator(self._auth_ind, GREY, self._auth_ind_label, "—")
        self._refresh_now()

    # -- Tick loop (driven by Tk.after) ---------------------------------------

    def _refresh_now(self) -> None:
        """Recompute presence and repaint without waiting for the timer.

        Start/stop change what the window should say immediately; leaving
        that to the next poll makes the button feel unresponsive.
        """
        self._snapshot = self._observer.poll()
        self._presence = DaemonPresence(
            self._snapshot.heartbeat, driving_locally=self._running
        )
        self._update_status()
        self._update_paths_table()

    def _tick(self) -> None:
        """Drive the locally-owned daemon. Only runs while `_running`."""
        if not self._running or self._daemon is None:
            return
        try:
            self._daemon.tick()
        except Exception:
            log.exception("Tick error")
        self.after(TICK_MS, self._tick)

    def _observe(self) -> None:
        """Re-read the state DB and repaint. Runs forever, daemon or not."""
        try:
            self._snapshot = self._observer.poll()
            previous = self._presence.kind
            self._presence = DaemonPresence(
                self._snapshot.heartbeat, driving_locally=self._running
            )
            if self._presence.kind != previous:
                self._log_presence_change()

            result = self._probe.poll()
            if result is not None:
                self._n_total, self._n_reachable, self._n_authed = result

            self._observe_counter += 1
            if self._observe_counter % CONNECTION_CHECK_TICKS == 0:
                self._probe.request(self._probe_clients or {})

            self._update_status()
            self._update_paths_table()
        except Exception:
            log.exception("Observer error")
        finally:
            # Rescheduling in `finally` means a transient error can never
            # silently kill the refresh loop and leave a frozen display.
            self.after(OBSERVE_MS, self._observe)

    def _log_presence_change(self) -> None:
        """Narrate who owns the state DB, so the log explains the display."""
        hb = self._presence.heartbeat
        kind = self._presence.kind
        if kind == DaemonPresence.EXTERNAL and hb is not None:
            log.info(
                "Observing external daemon (pid %s on %s), state db %s",
                hb.pid,
                hb.hostname,
                self._observer.db_path,
            )
        elif kind == DaemonPresence.STALE and hb is not None:
            log.warning(
                "Daemon pid %s stopped beating %s — it may have crashed",
                hb.pid,
                _fmt_ago(hb.last_tick),
            )
        elif kind == DaemonPresence.NONE:
            log.info("No daemon is running; press Start to sync from this window.")

    def _update_status(self) -> None:
        presence = self._presence
        text, colour = presence.describe()
        self._set_indicator(self._daemon_ind, colour, self._daemon_ind_label, text)

        # The Start button is meaningless while somebody else owns the DB.
        if presence.blocks_start and not self._running:
            self._start_stop_btn.configure(state="disabled")
        else:
            self._start_stop_btn.configure(state="normal")

        total = self._n_total
        if total == 0:
            hint = "—" if self._running else "no local daemon"
            self._set_indicator(self._server_ind, GREY, self._server_ind_label, hint)
            self._set_indicator(self._auth_ind, GREY, self._auth_ind_label, hint)
        else:
            reachable = self._n_reachable
            colour = (
                GREEN if reachable == total else (RED if reachable == 0 else YELLOW)
            )
            self._set_indicator(
                self._server_ind,
                colour,
                self._server_ind_label,
                f"{reachable}/{total} connected",
            )
            authed = self._n_authed
            colour = GREEN if authed == total else (RED if authed == 0 else YELLOW)
            self._set_indicator(
                self._auth_ind,
                colour,
                self._auth_ind_label,
                f"{authed}/{total} authenticated",
            )

        self._update_sync_indicator()

        snap = self._snapshot
        # Prefer the live daemon's own counters when we own it; otherwise
        # everything comes from the database, which is what makes the window
        # useful next to an external daemon.
        if self._daemon is not None:
            last_scan = self._daemon.last_scan_time or snap.last_scan
            last_push = self._daemon.last_attach_time or snap.last_sync
            pending = self._daemon.pending_count
        else:
            last_scan, last_push = snap.last_scan, snap.last_sync
            pending = snap.total_pending

        self._last_scan_val.configure(text=_fmt_ts(last_scan))
        self._last_push_val.configure(text=_fmt_ts(last_push))
        self._pending_val.configure(text=f"{pending} files")
        self._tracked_val.configure(
            text=f"{sum(s.total for s in snap.summaries)} files"
        )

    def _update_sync_indicator(self) -> None:
        snap = self._snapshot
        status = ""
        if self._daemon is not None:
            status = self._daemon.sync_status
        elif snap.heartbeat is not None and not snap.heartbeat.is_stale(
            timeout=HEARTBEAT_TIMEOUT
        ):
            status = snap.heartbeat.status

        if not snap.available:
            self._set_indicator(
                self._sync_ind, GREY, self._sync_ind_label, "no state database yet"
            )
            return
        if status == "attaching":
            self._set_indicator(self._sync_ind, YELLOW, self._sync_ind_label, "Pushing")
        elif status == "error":
            self._set_indicator(self._sync_ind, RED, self._sync_ind_label, "Error")
        elif snap.total_pending:
            self._set_indicator(
                self._sync_ind,
                YELLOW,
                self._sync_ind_label,
                f"{snap.total_pending} pending",
            )
        elif snap.last_sync:
            self._set_indicator(self._sync_ind, GREEN, self._sync_ind_label, "Idle")
        else:
            self._set_indicator(self._sync_ind, GREY, self._sync_ind_label, "Waiting")

    def _update_paths_table(self) -> None:
        """Repaint the per-path table from the latest snapshot.

        Rows are keyed by watched-path name and updated in place rather than
        cleared and rebuilt, so the user's selection and scroll position
        survive the 2-second refresh.
        """
        tree = self._paths_tree
        by_name = {s.name: s for s in self._snapshot.summaries}

        # Paths in the config but not yet in the DB still deserve a row —
        # otherwise a freshly-configured path is invisible until first scan.
        configured = {wp.name: wp for wp in self._config.watched_paths}
        for name in configured:
            by_name.setdefault(name, PathSummary(name=name))

        for name in list(tree.get_children("")):
            if name not in by_name:
                tree.delete(name)

        for name, summary in sorted(by_name.items()):
            wp = configured.get(name)
            scans = summary.scans
            last_scan = max(
                (ts for ts in (scans.hot, scans.warm, scans.cold) if ts), default=None
            )
            values = (
                name,
                (getattr(wp, "datalab", None) or "—") if wp else "(not configured)",
                summary.pending,
                summary.synced,
                summary.total,
                _fmt_ago(last_scan),
                _fmt_ago(summary.last_synced),
            )
            if summary.total == 0:
                tag = "unseen"
            elif summary.pending:
                tag = "pending"
            else:
                tag = "idle"
            if tree.exists(name):
                tree.item(name, values=values, tags=(tag,))
            else:
                tree.insert("", "end", iid=name, values=values, tags=(tag,))

    # -- Activity log ---------------------------------------------------------

    def _clear_log(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # -- Settings dialog ------------------------------------------------------

    def _open_settings(self) -> None:
        SettingsDialog(self, self._config_path, self._config)

    def reload_config(self, config_path: Path) -> None:
        """Re-read the config from disk after the settings dialog writes it.

        Without this the window keeps the copy it loaded at startup, so
        reopening Settings would show the pre-save values and saving again
        would write them back — silently reverting the edit just made.
        """
        try:
            self._config = load_config(config_path)
        except Exception as e:  # pragma: no cover - defensive
            log.error("Could not reload configuration from %s: %s", config_path, e)
            return

        self._config_path = config_path
        self._config_label.configure(text=str(config_path))
        # The state DB may have moved, so the observer has to follow it.
        self._observer.set_path(self._config.state_db)
        if self._running:
            log.warning(
                "Configuration changed — restart the daemon for it to take effect."
            )

    # -- Cleanup --------------------------------------------------------------

    def _on_close(self) -> None:
        if self._running:
            self._stop_daemon()
        self._observer.close()
        self.destroy()


# -- Settings Dialog ----------------------------------------------------------


class SettingsDialog(tk.Toplevel):
    """Modal settings window for editing the whole configuration.

    Every field in `BeholderConfig` is reachable from here: connection
    details on the first tab, watched paths (each with its own full editor)
    on the second, and daemon-wide options on the third.

    Values the dialog does not model are still round-tripped. Each row keeps
    the dict it was loaded from in ``original`` and updates it in place, so
    a config written by a newer beholder — or hand-edited with keys this
    build has never heard of — survives a save untouched.
    """

    def __init__(
        self,
        parent: BeholderGUI,
        config_path: Path | None,
        config: BeholderConfig,
    ):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG)
        self.geometry("760x620")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._parent = parent
        self._config_path = config_path
        self._config = config

        # Both row lists must exist before any row is built: adding a datalab
        # row refreshes the path rows' datalab dropdowns, which reads
        # `_path_rows`.
        self._datalab_rows: list[dict] = []
        self._path_rows: list[dict] = []

        self._build_ui()

    # -- Construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(self, style="Beholder.TNotebook")
        notebook.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        datalabs_tab = _ScrollFrame(notebook)
        paths_tab = _ScrollFrame(notebook)
        daemon_tab = _ScrollFrame(notebook)
        notebook.add(datalabs_tab, text="datalab instances")
        notebook.add(paths_tab, text="Watched paths")
        notebook.add(daemon_tab, text="Daemon")

        self._build_datalabs_tab(datalabs_tab.body)
        self._build_paths_tab(paths_tab.body)
        self._build_daemon_tab(daemon_tab.body)
        self._build_buttons()

    def _build_datalabs_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        _label(
            parent,
            "Each entry is a datalab deployment this daemon can push to.",
            font=FONT_SM,
            fg=FG_DIM,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        headers = tk.Frame(parent, bg=BG)
        headers.grid(row=1, column=0, sticky="ew", padx=12)
        for col, (text, width) in enumerate(
            [("Name", 12), ("URL", 26), ("API key", 22), ("", 6), ("", 3)]
        ):
            tk.Label(
                headers,
                text=text,
                font=FONT_SM,
                bg=BG,
                fg=FG_DIM,
                width=width,
                anchor="w",
            ).grid(row=0, column=col, sticky="w", padx=4)

        self._datalabs_frame = tk.Frame(parent, bg=BG)
        self._datalabs_frame.grid(row=2, column=0, sticky="ew", padx=12)
        self._datalabs_frame.columnconfigure(0, weight=1)

        for d in self._config.datalabs:
            self._add_datalab_row(
                d.name,
                d.url,
                d.api_key or "",
                elevate_permissions=d.elevate_permissions,
                original=d.model_dump(mode="json", exclude_defaults=True),
            )

        _button(
            parent, "+ Add datalab", lambda: self._add_datalab_row("", "", ""), padx=6
        ).grid(row=3, column=0, sticky="w", padx=12, pady=8)

    def _build_paths_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        _label(
            parent,
            'Directories to watch. "Edit…" exposes patterns, id regexes, '
            "templates and scan cadence.",
            font=FONT_SM,
            fg=FG_DIM,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        self._paths_frame = tk.Frame(parent, bg=BG)
        self._paths_frame.grid(row=1, column=0, sticky="ew", padx=12)
        self._paths_frame.columnconfigure(0, weight=1)

        for wp in self._config.watched_paths:
            original = wp.model_dump(mode="json", exclude_defaults=True)
            # `kind` is each subclass's own default, so `exclude_defaults`
            # drops it — but a watched path with no `kind` is read back as
            # local, which would silently rewrite an ssh/cloud path.
            original["kind"] = wp.kind
            self._add_path_row(
                wp.name,
                str(getattr(wp, "path", "")),
                wp.datalab or "",
                original=original,
            )

        _button(parent, "+ Add path", self._add_path_dialog, padx=6).grid(
            row=2, column=0, sticky="w", padx=12, pady=8
        )

    def _build_daemon_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        row = 0

        # -- Sync ------------------------------------------------------------
        _label(parent, "Sync", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(12, 4)
        )
        row += 1

        sync_frame = tk.Frame(parent, bg=BG)
        sync_frame.grid(row=row, column=0, sticky="ew", padx=12)
        row += 1

        _label(sync_frame, "Attach interval:", font=FONT_SM).grid(
            row=0, column=0, sticky="w"
        )
        self._metadata_var = tk.StringVar(
            value=str(self._config.sync.metadata_interval)
        )
        _entry(sync_frame, self._metadata_var, width=10).grid(row=0, column=1, padx=4)
        _label(sync_frame, "seconds", font=FONT_SM, fg=FG_DIM).grid(
            row=0, column=2, sticky="w"
        )
        _tooltip(
            sync_frame,
            "How often the daemon pushes pending files to datalab.\n"
            "Scanning happens on its own per-path cadence.",
        )

        # -- Storage ----------------------------------------------------------
        _label(parent, "Storage", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(16, 4)
        )
        row += 1

        db_frame = tk.Frame(parent, bg=BG)
        db_frame.grid(row=row, column=0, sticky="ew", padx=12)
        db_frame.columnconfigure(1, weight=1)
        row += 1

        _label(db_frame, "State database:", font=FONT_SM).grid(
            row=0, column=0, sticky="w"
        )
        self._state_db_var = tk.StringVar(value=str(self._config.state_db))
        _entry(db_frame, self._state_db_var).grid(row=0, column=1, sticky="ew", padx=4)

        def browse_db() -> None:
            chosen = filedialog.asksaveasfilename(
                parent=self,
                title="State database",
                initialfile=Path(self._state_db_var.get()).name or "state.db",
                confirmoverwrite=False,
            )
            if chosen:
                self._state_db_var.set(chosen)

        _button(db_frame, "Browse", browse_db, padx=6).grid(row=0, column=2)
        _tooltip(
            db_frame,
            "Where sync state is recorded. The GUI also reads this file to\n"
            "monitor a daemon running in another process.",
        )

        # -- Logging / behaviour ---------------------------------------------
        _label(parent, "Behaviour", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(16, 4)
        )
        row += 1

        opts = tk.Frame(parent, bg=BG)
        opts.grid(row=row, column=0, sticky="ew", padx=12)
        row += 1

        _label(opts, "Log level:", font=FONT_SM).grid(row=0, column=0, sticky="w")
        self._log_level_var = tk.StringVar(value=self._config.log_level)
        _option_menu(opts, self._log_level_var, LOG_LEVELS).grid(
            row=0, column=1, sticky="w", padx=4
        )

        self._reset_clocks_var = tk.BooleanVar(
            value=self._config.reset_scan_clocks_on_startup
        )
        reset_check = _check(opts, "Full rescan on startup", self._reset_clocks_var)
        reset_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        _tooltip(
            reset_check,
            "Clear the stored scan timestamps on startup so a full cold scan\n"
            "runs on the first tick. Already-synced files are not re-uploaded.",
        )

    def _build_buttons(self) -> None:
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))

        _button(btn_frame, "Cancel", self.destroy, padx=12, pady=4).pack(
            side="right", padx=(4, 0)
        )
        _button(btn_frame, "Save", self._save, fg=GREEN, padx=12, pady=4).pack(
            side="right"
        )

    # -- Datalab rows ---------------------------------------------------------

    def _add_datalab_row(
        self,
        name: str,
        url: str,
        api_key: str,
        elevate_permissions: bool = False,
        original: dict | None = None,
    ) -> None:
        row_idx = len(self._datalab_rows)
        frame = tk.Frame(self._datalabs_frame, bg=BG_LIGHT)
        frame.grid(row=row_idx, column=0, sticky="ew", pady=1)
        frame.columnconfigure(2, weight=1)

        name_var = tk.StringVar(value=name)
        _entry(frame, name_var, width=12).grid(row=0, column=0, padx=4, pady=2)

        url_var = tk.StringVar(value=url)
        _entry(frame, url_var, width=26).grid(row=0, column=1, padx=4, pady=2)

        api_key_var = tk.StringVar(value=api_key)
        key_entry = _entry(frame, api_key_var, show="*")
        key_entry.grid(row=0, column=2, sticky="ew", padx=4, pady=2)

        # Admin super-user mode: let an admin key attach data to items owned
        # by other users. No-op server-side for a non-admin key.
        elevate_var = tk.BooleanVar(value=elevate_permissions)
        elevate_check = _check(
            frame,
            "sudo",
            elevate_var,
            bg=BG_LIGHT,
            selectcolor=BG,
            activebackground=BG_LIGHT,
        )
        elevate_check.grid(row=0, column=3, padx=4, pady=2)
        _tooltip(
            elevate_check,
            "Elevate permissions (admin keys only): read items belonging to\n"
            "other users so files can be attached to samples that aren't\n"
            "shared with this account. Ignored for non-admin keys.",
        )

        def remove() -> None:
            frame.destroy()
            self._datalab_rows = [
                r for r in self._datalab_rows if r["frame"] is not frame
            ]
            self._refresh_path_dropdowns()

        _button(frame, "x", remove, fg=RED, padx=4).grid(
            row=0, column=4, padx=(0, 4), pady=2
        )

        # Propagate live name changes to the path-row dropdowns so the user
        # sees the new option as soon as they type it.
        name_var.trace_add("write", lambda *_: self._refresh_path_dropdowns())

        self._datalab_rows.append(
            {
                "frame": frame,
                "name": name_var,
                "url": url_var,
                "api_key": api_key_var,
                "elevate_permissions": elevate_var,
                "original": original or {},
            }
        )
        self._refresh_path_dropdowns()

    def _datalab_names(self) -> list[str]:
        return [
            r["name"].get().strip()
            for r in self._datalab_rows
            if r["name"].get().strip()
        ]

    def _refresh_path_dropdowns(self) -> None:
        names = self._datalab_names()
        for row in self._path_rows:
            menu = row["datalab_menu"]["menu"]
            menu.delete(0, "end")
            for n in names:
                menu.add_command(
                    label=n, command=lambda v=n, var=row["datalab"]: var.set(v)
                )
            # If the row's current value isn't in the list any more, blank it.
            if row["datalab"].get() not in names:
                row["datalab"].set("")

    # -- Watched-path rows ----------------------------------------------------

    def _add_path_row(
        self,
        name: str,
        path: str,
        datalab: str = "",
        original: dict | None = None,
    ) -> None:
        row_idx = len(self._path_rows)
        frame = tk.Frame(self._paths_frame, bg=BG_LIGHT)
        frame.grid(row=row_idx, column=0, sticky="ew", pady=1)
        frame.columnconfigure(2, weight=1)

        original = dict(original or {})
        original.setdefault("kind", "local")

        name_var = tk.StringVar(value=name)
        _entry(frame, name_var, width=14).grid(row=0, column=0, padx=4, pady=2)

        kind_label = tk.Label(
            frame,
            text=original.get("kind", "local"),
            font=FONT_SM,
            bg=BG_LIGHT,
            fg=FG_DIM,
            width=6,
        )
        kind_label.grid(row=0, column=1, padx=2, pady=2)

        path_var = tk.StringVar(value=path)
        _entry(frame, path_var).grid(row=0, column=2, sticky="ew", padx=4, pady=2)

        datalab_var = tk.StringVar(value=datalab)
        datalab_menu = _option_menu(
            frame, datalab_var, self._datalab_names() or [""], width=10
        )
        datalab_menu.grid(row=0, column=3, padx=4, pady=2)

        row: dict[str, Any] = {
            "frame": frame,
            "name": name_var,
            "path": path_var,
            "datalab": datalab_var,
            "datalab_menu": datalab_menu,
            "kind_label": kind_label,
            "original": original,
        }

        _button(frame, "Edit…", lambda: self._edit_path_row(row), padx=6).grid(
            row=0, column=4, padx=2, pady=2
        )

        def remove() -> None:
            frame.destroy()
            self._path_rows = [r for r in self._path_rows if r["frame"] is not frame]

        _button(frame, "x", remove, fg=RED, padx=4).grid(
            row=0, column=5, padx=(0, 4), pady=2
        )

        self._path_rows.append(row)

    def _edit_path_row(self, row: dict) -> None:
        """Open the full editor for one watched path."""
        # The inline entries are the authority for the fields they show, so
        # fold them in before handing the dict to the editor.
        entry = dict(row["original"])
        entry["name"] = row["name"].get().strip()
        if row["path"].get().strip():
            entry["path"] = row["path"].get().strip()
        if row["datalab"].get().strip():
            entry["datalab"] = row["datalab"].get().strip()
        WatchedPathDialog(
            self, entry, lambda updated: self._apply_path_edit(row, updated)
        )

    def _apply_path_edit(self, row: dict, updated: dict) -> None:
        """Write an edited watched path back into its row."""
        row["original"] = updated
        row["name"].set(updated.get("name", ""))
        row["path"].set(str(updated.get("path", "")))
        row["kind_label"].configure(text=updated.get("kind", "local"))
        names = self._datalab_names()
        wanted = updated.get("datalab", "")
        # Only adopt a datalab that still exists, or the dropdown would show
        # a value the user cannot re-select.
        row["datalab"].set(wanted if wanted in names else "")

    def _add_path_dialog(self) -> None:
        names = self._datalab_names()
        seed: dict[str, Any] = {"kind": "local", "include_patterns": ["*"]}
        if len(names) == 1:
            seed["datalab"] = names[0]
        WatchedPathDialog(self, seed, self._add_path_from_dialog, is_new=True)

    def _add_path_from_dialog(self, entry: dict) -> None:
        self._add_path_row(
            entry.get("name", ""),
            str(entry.get("path", "")),
            entry.get("datalab", ""),
            original=entry,
        )

    def add_path(
        self,
        name: str,
        path: str,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        **extra: Any,
    ) -> None:
        """Add a watched-path row programmatically.

        Kept as a stable entry point for the add-path flow (and for tests)
        now that the editor dialog handles the full field set.
        """
        names = self._datalab_names()
        default = extra.pop("datalab", "") or (names[0] if len(names) == 1 else "")
        original: dict = {"kind": "local", "name": name, "path": path}
        if include_patterns:
            original["include_patterns"] = include_patterns
        if exclude_patterns:
            original["exclude_patterns"] = exclude_patterns
        original.update(extra)
        if default:
            original["datalab"] = default
        self._add_path_row(name, path, default, original=original)

    # -- Save -----------------------------------------------------------------

    def _collect_datalabs(self) -> list[dict]:
        datalabs: list[dict] = []
        seen_names: set[str] = set()
        for row in self._datalab_rows:
            name = row["name"].get().strip()
            url = row["url"].get().strip()
            api_key = row["api_key"].get().strip()
            if not (name or url or api_key):
                continue
            if not name or not url:
                raise ValueError("Each datalab needs a name and URL.")
            if name in seen_names:
                raise ValueError(f"Duplicate datalab name: {name!r}")
            seen_names.add(name)
            # Start from the entry as loaded so fields this dialog doesn't
            # model are carried through untouched.
            entry = dict(row["original"])
            entry.update(
                {
                    "name": name,
                    "url": url,
                    "api_key": api_key,
                    "elevate_permissions": row["elevate_permissions"].get(),
                }
            )
            datalabs.append(entry)

        if not datalabs:
            raise ValueError("At least one datalab is required.")
        return datalabs

    def _collect_watched_paths(self) -> list[dict]:
        watched_paths: list[dict] = []
        seen: set[str] = set()
        for row in self._path_rows:
            name = row["name"].get().strip()
            path = row["path"].get().strip()
            datalab = row["datalab"].get().strip()
            if not (name or path):
                continue
            if not name or not path:
                raise ValueError("Each watched path needs a name and path.")
            if name in seen:
                raise ValueError(f"Duplicate watched path name: {name!r}")
            seen.add(name)
            entry = dict(row["original"])
            entry.update({"path": path, "name": name})
            if datalab:
                entry["datalab"] = datalab
            else:
                entry.pop("datalab", None)
            watched_paths.append(entry)

        if not watched_paths:
            raise ValueError("At least one watched path is required.")
        return watched_paths

    def _save(self) -> None:
        try:
            metadata_interval = int(self._metadata_var.get())
        except ValueError:
            messagebox.showerror(
                "Validation", "Sync intervals must be integers.", parent=self
            )
            return

        state_db = self._state_db_var.get().strip()
        if not state_db:
            messagebox.showerror(
                "Validation", "A state database path is required.", parent=self
            )
            return

        try:
            datalabs = self._collect_datalabs()
            watched_paths = self._collect_watched_paths()
        except ValueError as e:
            messagebox.showerror("Validation", str(e), parent=self)
            return

        # Everything the dialog does not edit is preserved by starting from
        # the loaded config rather than rebuilding it from the widgets alone.
        # A settings dialog that only knows about a subset of the schema must
        # not be a way to silently drop the rest of the user's YAML.
        config_dict = self._config.model_dump(mode="json", exclude_defaults=True)
        config_dict.update(
            {
                # `version` is written explicitly even when it matches the
                # current default: a config that loses it is re-read as v1 and
                # would be migrated a second time.
                "version": self._config.version,
                "datalabs": datalabs,
                "watched_paths": watched_paths,
                "sync": {"metadata_interval": metadata_interval},
                "log_level": self._log_level_var.get(),
                "reset_scan_clocks_on_startup": self._reset_clocks_var.get(),
                "state_db": state_db,
            }
        )

        # Run the full pydantic validation chain so cross-field issues
        # (unknown datalab refs, ambiguous defaults, etc.) surface in the
        # dialog instead of being written to disk and then exploding on load.
        try:
            BeholderConfig(**config_dict)
        except Exception as e:
            messagebox.showerror("Validation", str(e), parent=self)
            return

        config_path = self._config_path
        if config_path is None:
            from datalab_beholder.config import DEFAULT_CONFIG_PATH

            config_path = DEFAULT_CONFIG_PATH

        config_path = Path(config_path).expanduser().resolve()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.safe_dump(config_dict, f, default_flow_style=False, sort_keys=False)

        log.info("Configuration saved to %s", config_path)
        self._parent.reload_config(config_path)
        self.destroy()


# -- Watched-path editor ------------------------------------------------------


class WatchedPathDialog(tk.Toplevel):
    """Full editor for a single watched path.

    Covers every field on the watched-path models, including the ones that
    previously had no UI at all: id regexes, id/collection templates, block
    patterns, item type, max depth and the three-tier scan cadence.

    On save the assembled dict is validated against the same discriminated
    union the config loader uses, so a bad regex or an unknown capture group
    is reported here rather than at next startup.
    """

    def __init__(
        self,
        parent: SettingsDialog,
        entry: dict,
        on_commit: Callable[[dict], None],
        is_new: bool = False,
    ):
        super().__init__(parent)
        self.title("Add watched path" if is_new else "Edit watched path")
        self.configure(bg=BG)
        self.geometry("620x700")
        self.transient(parent)
        self.grab_set()

        self._parent = parent
        self._entry = dict(entry)
        self._on_commit = on_commit
        self._build_ui()

    # -- Construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        scroll = _ScrollFrame(self)
        scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        body = scroll.body
        body.columnconfigure(1, weight=1)

        e = self._entry
        row = 0

        def section(text: str) -> None:
            nonlocal row
            _label(body, text, font=FONT_HEADING).grid(
                row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(12, 4)
            )
            row += 1

        def field(
            label: str, var: tk.Variable, tip: str = "", widget: tk.Widget | None = None
        ) -> tk.Widget:
            nonlocal row
            lbl = _label(body, label, font=FONT_SM)
            lbl.grid(row=row, column=0, sticky="w", padx=8, pady=3)
            w = widget or _entry(body, var)
            w.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=3)
            if tip:
                _tooltip(lbl, tip)
            row += 1
            return w

        def text_field(label: str, value: str, height: int, tip: str = "") -> tk.Text:
            nonlocal row
            lbl = _label(body, label, font=FONT_SM)
            lbl.grid(row=row, column=0, sticky="nw", padx=8, pady=3)
            widget = _text(body, height=height)
            widget.insert("1.0", value)
            widget.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=3)
            if tip:
                _tooltip(lbl, tip)
            row += 1
            return widget

        # -- Source -----------------------------------------------------------
        section("Source")

        self._kind_var = tk.StringVar(value=e.get("kind", "local"))
        field(
            "Kind:",
            self._kind_var,
            "local: a mounted directory.\n"
            "ssh / cloud: scaffolded in the config schema but not yet\n"
            "implemented by the scanner.",
            widget=_option_menu(body, self._kind_var, PATH_KINDS),
        )

        self._name_var = tk.StringVar(value=e.get("name", ""))
        field(
            "Name:", self._name_var, "Label for this path; also names its state table."
        )

        path_frame = tk.Frame(body, bg=BG)
        path_frame.columnconfigure(0, weight=1)
        self._path_var = tk.StringVar(value=str(e.get("path", "")))
        _entry(path_frame, self._path_var).grid(row=0, column=0, sticky="ew")
        _button(path_frame, "Browse", self._browse, padx=6).grid(
            row=0, column=1, padx=(4, 0)
        )
        field("Path:", self._path_var, widget=path_frame)

        self._host_var = tk.StringVar(value=e.get("host", ""))
        self._host_widget = field(
            "SSH host:",
            self._host_var,
            "SSH-config alias or user@host (ssh paths only).",
        )

        self._provider_var = tk.StringVar(value=e.get("provider", "auto"))
        self._provider_widget = field(
            "Cloud provider:",
            self._provider_var,
            widget=_option_menu(body, self._provider_var, CLOUD_PROVIDERS),
        )

        self._datalab_var = tk.StringVar(value=e.get("datalab", ""))
        field(
            "datalab:",
            self._datalab_var,
            "Which configured datalab instance to push this path to.",
            widget=_option_menu(
                body, self._datalab_var, self._parent._datalab_names() or [""]
            ),
        )

        # -- Matching ---------------------------------------------------------
        section("File matching")

        self._include_var = tk.StringVar(
            value=", ".join(e.get("include_patterns", ["*"]))
        )
        field(
            "Include globs:",
            self._include_var,
            "Comma-separated globs. Only matching files are considered.",
        )

        self._exclude_var = tk.StringVar(value=", ".join(e.get("exclude_patterns", [])))
        field("Exclude globs:", self._exclude_var, "Comma-separated globs to skip.")

        self._max_depth_var = tk.StringVar(
            value="" if e.get("max_depth") is None else str(e.get("max_depth"))
        )
        field(
            "Max depth:",
            self._max_depth_var,
            "How deep to walk below the watched directory. Blank = unlimited.",
        )

        self._id_patterns_text = text_field(
            "ID patterns:",
            "\n".join(e.get("id_patterns", [])),
            4,
            "One regex per line, with named groups. Must include (?P<item_id>...);\n"
            "may also use (?P<group_id>...) and (?P<collection_id>...).\n"
            "Files matching no pattern are skipped.",
        )

        # -- Destination ------------------------------------------------------
        section("datalab destination")

        self._item_type_var = tk.StringVar(value=e.get("item_type", "") or "")
        field(
            "Item type:",
            self._item_type_var,
            "Type used for items this path creates (e.g. samples, cells).",
        )

        self._item_tpl_var = tk.StringVar(value=e.get("item_id_template", "") or "")
        field(
            "Item ID template:",
            self._item_tpl_var,
            "str.format template over the capture groups, e.g. {group_id}-{item_id}.\n"
            "Blank uses the raw item_id group.",
        )

        self._collection_tpl_var = tk.StringVar(
            value=e.get("collection_id_template", "") or ""
        )
        field(
            "Collection template:",
            self._collection_tpl_var,
            "Optional collection_id template. Blank sets no collection.",
        )

        self._block_patterns_text = text_field(
            "Block patterns:",
            _format_mapping(e.get("block_patterns", {})),
            3,
            "One 'glob = block_type' per line. After a matching file is\n"
            "attached, a block of that type is created if absent.",
        )

        # -- Cadence ----------------------------------------------------------
        section("Scan cadence")

        scan = e.get("scan", {}) or {}
        self._hot_var = tk.StringVar(value=str(scan.get("hot_interval", 60)))
        field(
            "Hot interval:",
            self._hot_var,
            "Seconds between stat-only scans of recently-modified files.",
        )

        self._warm_var = tk.StringVar(value=str(scan.get("warm_interval", 3600)))
        field(
            "Warm interval:", self._warm_var, "Seconds between directory-mtime walks."
        )

        cold = scan.get("cold_interval", 86400)
        self._cold_var = tk.StringVar(value="" if cold is None else str(cold))
        field(
            "Cold interval:",
            self._cold_var,
            "Seconds between full walks. Blank disables cold scans entirely —\n"
            "useful for write-once network archives.",
        )

        self._hot_window_var = tk.StringVar(value=str(scan.get("hot_window", 86400)))
        field(
            "Hot window:",
            self._hot_window_var,
            "How recently a file must have changed to be eligible for hot scans.",
        )

        # Show only the location fields that apply to the selected kind.
        self._kind_var.trace_add("write", lambda *_: self._sync_kind_fields())
        self._sync_kind_fields()

        btns = tk.Frame(self, bg=BG)
        btns.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))
        _button(btns, "Cancel", self.destroy, padx=12, pady=4).pack(
            side="right", padx=(4, 0)
        )
        _button(btns, "OK", self._commit, fg=GREEN, padx=12, pady=4).pack(side="right")

    def _sync_kind_fields(self) -> None:
        """Grey out location fields that don't apply to the selected kind."""
        kind = self._kind_var.get()
        self._host_widget.configure(  # type: ignore[call-arg]
            state="normal" if kind == "ssh" else "disabled"
        )
        self._provider_widget.configure(  # type: ignore[call-arg]
            state="normal" if kind == "cloud" else "disabled"
        )

    def _browse(self) -> None:
        directory = filedialog.askdirectory(parent=self)
        if directory:
            self._path_var.set(directory)
            if not self._name_var.get():
                self._name_var.set(Path(directory).name)

    # -- Commit ---------------------------------------------------------------

    def _commit(self) -> None:
        try:
            entry = self._collect()
        except ValueError as e:
            messagebox.showerror("Validation", str(e), parent=self)
            return

        # Validate against the real schema — the discriminated union picks
        # the right subclass off `kind`, so this catches bad regexes, unknown
        # capture groups and missing ssh hosts alike.
        try:
            from pydantic import TypeAdapter

            from datalab_beholder.config import WatchedPath

            TypeAdapter(WatchedPath).validate_python(entry)
        except Exception as e:
            messagebox.showerror("Validation", str(e), parent=self)
            return

        self._on_commit(entry)
        self.destroy()

    def _collect(self) -> dict:
        """Assemble the edited dict, preserving unknown keys."""
        # Start from what we were given so keys this dialog doesn't model
        # (e.g. added by a newer beholder) survive the round trip.
        entry = dict(self._entry)
        kind = self._kind_var.get()
        entry["kind"] = kind
        entry["name"] = self._name_var.get().strip()
        entry["path"] = self._path_var.get().strip()

        if not entry["name"] or not entry["path"]:
            raise ValueError("Name and path are required.")

        # Location fields only belong on the kind that defines them; leaving
        # a stale `host` on a local path would fail schema validation.
        if kind == "ssh":
            host = self._host_var.get().strip()
            if not host:
                raise ValueError("An SSH host is required for ssh paths.")
            entry["host"] = host
        else:
            entry.pop("host", None)

        if kind == "cloud":
            entry["provider"] = self._provider_var.get()
        else:
            entry.pop("provider", None)

        datalab = self._datalab_var.get().strip()
        if datalab:
            entry["datalab"] = datalab
        else:
            entry.pop("datalab", None)

        entry["include_patterns"] = _split_csv(self._include_var.get()) or ["*"]
        entry["exclude_patterns"] = _split_csv(self._exclude_var.get())
        entry["id_patterns"] = _split_lines(self._id_patterns_text.get("1.0", "end"))

        max_depth = _parse_optional_int(self._max_depth_var.get(), "Max depth")
        entry["max_depth"] = max_depth

        try:
            entry["block_patterns"] = _parse_mapping(
                self._block_patterns_text.get("1.0", "end")
            )
        except ValueError as e:
            raise ValueError(f"Block patterns: {e}")

        for key, var in (
            ("item_type", self._item_type_var),
            ("item_id_template", self._item_tpl_var),
            ("collection_id_template", self._collection_tpl_var),
        ):
            value = var.get().strip()
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)

        scan: dict[str, Any] = {}
        for key, var, label in (
            ("hot_interval", self._hot_var, "Hot interval"),
            ("warm_interval", self._warm_var, "Warm interval"),
            ("hot_window", self._hot_window_var, "Hot window"),
        ):
            interval = _parse_optional_int(var.get(), label)
            if interval is None:
                raise ValueError(f"{label} is required.")
            scan[key] = interval
        # Cold alone is nullable: blank means "never run a full walk".
        scan["cold_interval"] = _parse_optional_int(
            self._cold_var.get(), "Cold interval"
        )
        entry["scan"] = scan

        return entry


# Backwards-compatible alias: the add-path flow is now the full editor.
AddPathDialog = WatchedPathDialog
