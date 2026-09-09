"""Tkinter GUI for the beholder daemon.

Provides a visual control panel styled after EPICS/synchrotron control
interfaces: dark background, status indicator lights, dense layout,
monospaced activity log.

The GUI drives the daemon via ``daemon.tick()`` scheduled through
``root.after()`` — no extra threads beyond the watchdog Observer.
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Any

import yaml

from datalab_beholder.config import BeholderConfig, load_config, write_config_template
from datalab_beholder.daemon import BeholderDaemon

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# -- Colours (EPICS aesthetic) -----------------------------------------------
BG = "#2b2b2b"
BG_LIGHT = "#3c3c3c"
FG = "#d4d4d4"
FG_DIM = "#888888"
GREEN = "#00cc00"
YELLOW = "#cccc00"
RED = "#cc0000"
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
CONNECTION_CHECK_TICKS = 30  # check connection every 30s


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


# -- Logging handler that writes to a Tk Text widget -------------------------


class TextWidgetHandler(logging.Handler):
    """Logging handler that appends formatted records to a Tk Text widget."""

    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self._text = text_widget

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = f"{ts}  {record.getMessage()}\n"
            self._text.configure(state="normal")
            self._text.insert("end", msg)
            self._text.see("end")
            self._text.configure(state="disabled")
        except tk.TclError:
            pass  # widget destroyed


# -- Main GUI ----------------------------------------------------------------


class BeholderGUI(tk.Tk):
    """Main beholder GUI window."""

    def __init__(self, config_path: Path | None = None):
        super().__init__()
        _init_fonts()
        self.title("BEHOLDER")
        self.configure(bg=BG)
        self.geometry("620x520")
        self.minsize(500, 400)

        # Indicator widgets (populated by _build_status_panel)
        self._server_ind: tuple[tk.Canvas, int]
        self._auth_ind: tuple[tk.Canvas, int]
        self._sync_ind: tuple[tk.Canvas, int]
        self._server_ind_label: tk.Label
        self._auth_ind_label: tk.Label
        self._sync_ind_label: tk.Label
        self._last_scan_val: tk.Label
        self._last_push_val: tk.Label
        self._pending_val: tk.Label

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
        self._tick_counter = 0

        # Connection state — aggregated across all configured datalabs.
        self._n_total = 0
        self._n_reachable = 0
        self._n_authed = 0

        self._build_ui()
        self._install_log_handler()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        # Configure grid weights for resizing
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)  # activity log gets the stretch

        self._build_header()
        self._build_status_panel()
        self._build_activity_log()

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        header.columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="BEHOLDER",
            font=FONT_TITLE,
            bg=BG,
            fg=FG,
        ).grid(row=0, column=0, sticky="w")

        btn_frame = tk.Frame(header, bg=BG)
        btn_frame.grid(row=0, column=1, sticky="e")

        self._settings_btn = tk.Button(
            btn_frame,
            text="Settings",
            command=self._open_settings,
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=8,
            pady=2,
        )
        self._settings_btn.pack(side="left", padx=(0, 4))

        self._start_stop_btn = tk.Button(
            btn_frame,
            text="Start",
            command=self._toggle_daemon,
            bg=BG_LIGHT,
            fg=GREEN,
            activebackground=BG,
            activeforeground=GREEN,
            relief="flat",
            padx=8,
            pady=2,
        )
        self._start_stop_btn.pack(side="left")

    def _build_status_panel(self) -> None:
        panel = tk.Frame(self, bg=BG)
        panel.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        panel.columnconfigure(1, weight=1)

        tk.Label(
            panel,
            text="STATUS",
            font=FONT_HEADING,
            bg=BG,
            fg=FG_DIM,
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        # Indicator rows: (label, attr_name)
        indicators = [
            ("Server", "_server_ind"),
            ("Auth", "_auth_ind"),
            ("Sync", "_sync_ind"),
        ]
        for i, (label, attr) in enumerate(indicators, start=1):
            canvas = tk.Canvas(
                panel,
                width=12,
                height=12,
                bg=BG,
                highlightthickness=0,
            )
            canvas.grid(row=i, column=0, sticky="w", padx=(4, 6))
            oval = canvas.create_oval(1, 1, 11, 11, fill=GREY, outline="")
            setattr(self, attr, (canvas, oval))

            tk.Label(
                panel,
                text=label,
                font=FONT,
                bg=BG,
                fg=FG,
                anchor="w",
                width=8,
            ).grid(row=i, column=1, sticky="w")

            status_label = tk.Label(
                panel,
                text="—",
                font=FONT,
                bg=BG,
                fg=FG_DIM,
                anchor="w",
            )
            status_label.grid(row=i, column=2, sticky="w")
            setattr(self, f"{attr}_label", status_label)

        sep = tk.Frame(panel, bg=BG_LIGHT, height=1)
        sep.grid(row=len(indicators) + 1, column=0, columnspan=3, sticky="ew", pady=6)

        # Stats row
        stats = tk.Frame(panel, bg=BG)
        stats.grid(row=len(indicators) + 2, column=0, columnspan=3, sticky="ew")

        for col, (label, attr) in enumerate(
            [
                ("Last scan:", "_last_scan_val"),
                ("Last push:", "_last_push_val"),
                ("Pending:", "_pending_val"),
            ]
        ):
            tk.Label(
                stats,
                text=label,
                font=FONT_SM,
                bg=BG,
                fg=FG_DIM,
            ).grid(row=0, column=col * 2, sticky="w", padx=(0 if col == 0 else 12, 4))

            val = tk.Label(
                stats,
                text="—",
                font=FONT_SM,
                bg=BG,
                fg=FG,
            )
            val.grid(row=0, column=col * 2 + 1, sticky="w")
            setattr(self, attr, val)

    def _build_activity_log(self) -> None:
        log_frame = tk.Frame(self, bg=BG)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        header = tk.Frame(log_frame, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="ACTIVITY LOG",
            font=FONT_HEADING,
            bg=BG,
            fg=FG_DIM,
        ).grid(row=0, column=0, sticky="w")

        tk.Button(
            header,
            text="Clear Log",
            command=self._clear_log,
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=6,
            pady=1,
        ).grid(row=0, column=1, sticky="e")

        # Text widget with scrollbar
        text_frame = tk.Frame(log_frame, bg=BG)
        text_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self._log_text = tk.Text(
            text_frame,
            font=FONT_MONO,
            bg="#1e1e1e",
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

        scrollbar = tk.Scrollbar(
            text_frame,
            command=self._log_text.yview,
            bg=BG_LIGHT,
            troughcolor=BG,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._log_text.configure(yscrollcommand=scrollbar.set)

    # -- Logging handler ------------------------------------------------------

    def _install_log_handler(self) -> None:
        handler = TextWidgetHandler(self._log_text)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.setLevel(logging.INFO)
        # Attach to root logger so all beholder modules are captured
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

    def _toggle_daemon(self) -> None:
        if self._running:
            self._stop_daemon()
        else:
            self._start_daemon()

    def _start_daemon(self) -> None:
        try:
            self._config = load_config(self._config_path)
        except Exception as e:
            messagebox.showerror("Config Error", str(e))
            return

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
        self._tick_counter = 0
        self._check_connection()
        self._tick()

    def _stop_daemon(self) -> None:
        self._running = False
        if self._daemon is not None:
            self._daemon.stop()
            self._daemon.shutdown()
            self._daemon = None
        self._start_stop_btn.configure(text="Start", fg=GREEN, activeforeground=GREEN)
        self._settings_btn.configure(state="normal")
        self._set_indicator(self._server_ind, GREY, self._server_ind_label, "—")
        self._set_indicator(self._auth_ind, GREY, self._auth_ind_label, "—")
        self._set_indicator(self._sync_ind, GREY, self._sync_ind_label, "—")

    # -- Connection check -----------------------------------------------------

    def _check_connection(self) -> None:
        """Aggregate connection/auth status across every configured datalab."""
        if self._daemon is None:
            return
        clients = self._daemon.clients
        self._n_total = len(clients)
        self._n_reachable = 0
        self._n_authed = 0
        for name, client in clients.items():
            try:
                reachable, authed = client.check_connection()
            except Exception:
                log.exception("Connection check failed for %s", name)
                continue
            if reachable:
                self._n_reachable += 1
            if authed:
                self._n_authed += 1
        self._update_status()

    # -- Tick loop (driven by Tk.after) ---------------------------------------

    def _tick(self) -> None:
        if not self._running or self._daemon is None:
            return

        try:
            self._daemon.tick()
        except Exception:
            log.exception("Tick error")

        self._tick_counter += 1

        if self._tick_counter % CONNECTION_CHECK_TICKS == 0:
            self._check_connection()

        self._update_status()
        self.after(TICK_MS, self._tick)

    def _update_status(self) -> None:
        if self._daemon is None:
            return

        # Server / Auth indicators show aggregate "N/M" with worst-case colour.
        total = self._n_total
        if total == 0:
            self._set_indicator(self._server_ind, GREY, self._server_ind_label, "—")
            self._set_indicator(self._auth_ind, GREY, self._auth_ind_label, "—")
        else:
            reachable = self._n_reachable
            if reachable == total:
                server_colour = GREEN
            elif reachable == 0:
                server_colour = RED
            else:
                server_colour = YELLOW
            self._set_indicator(
                self._server_ind,
                server_colour,
                self._server_ind_label,
                f"{reachable}/{total} connected",
            )

            authed = self._n_authed
            if authed == total:
                auth_colour = GREEN
            elif authed == 0:
                auth_colour = RED
            else:
                auth_colour = YELLOW
            self._set_indicator(
                self._auth_ind,
                auth_colour,
                self._auth_ind_label,
                f"{authed}/{total} authenticated",
            )

        # Sync indicator
        status = self._daemon.sync_status
        if status == "pushing":
            self._set_indicator(
                self._sync_ind,
                YELLOW,
                self._sync_ind_label,
                "Pushing",
            )
        elif status == "error":
            self._set_indicator(
                self._sync_ind,
                RED,
                self._sync_ind_label,
                "Error",
            )
        elif self._daemon.last_attach_time is not None:
            self._set_indicator(
                self._sync_ind,
                GREEN,
                self._sync_ind_label,
                "Idle",
            )
        else:
            self._set_indicator(
                self._sync_ind,
                GREY,
                self._sync_ind_label,
                "Waiting",
            )

        # Stats
        if self._daemon.last_scan_time is not None:
            dt = datetime.fromtimestamp(self._daemon.last_scan_time)
            self._last_scan_val.configure(text=dt.strftime("%Y-%m-%d %H:%M:%S"))
        if self._daemon.last_attach_time is not None:
            dt = datetime.fromtimestamp(self._daemon.last_attach_time)
            self._last_push_val.configure(text=dt.strftime("%Y-%m-%d %H:%M:%S"))
        self._pending_val.configure(text=f"{self._daemon.pending_count} files")

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
        if self._running:
            log.warning(
                "Configuration changed — restart the daemon for it to take effect."
            )

    # -- Cleanup --------------------------------------------------------------

    def _on_close(self) -> None:
        if self._running:
            self._stop_daemon()
        self.destroy()


# -- Settings Dialog ----------------------------------------------------------


class SettingsDialog(tk.Toplevel):
    """Modal settings window for editing configuration."""

    def __init__(
        self,
        parent: BeholderGUI,
        config_path: Path | None,
        config: BeholderConfig,
    ):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG)
        self.geometry("620x560")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._parent = parent
        self._config_path = config_path
        self._config = config

        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)

        # -- Datalabs section -------------------------------------------------
        tk.Label(
            self,
            text="datalab instances:",
            font=FONT_HEADING,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        self._datalabs_frame = tk.Frame(self, bg=BG)
        self._datalabs_frame.grid(row=1, column=0, sticky="ew", padx=12)
        self._datalabs_frame.columnconfigure(2, weight=1)

        # Both row lists must exist before any row is built: adding a datalab
        # row refreshes the path rows' datalab dropdowns, which reads
        # `_path_rows`.
        self._datalab_rows: list[dict] = []
        self._path_rows: list[dict] = []

        for d in self._config.datalabs:
            self._add_datalab_row(
                d.name,
                d.url,
                d.api_key or "",
                elevate_permissions=d.elevate_permissions,
                original=d.model_dump(mode="json", exclude_defaults=True),
            )

        tk.Button(
            self,
            text="+ Add Datalab",
            command=lambda: self._add_datalab_row("", "", ""),
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=6,
        ).grid(row=2, column=0, sticky="w", padx=12, pady=4)

        # -- Watched paths section --------------------------------------------
        tk.Label(
            self,
            text="Watched Paths:",
            font=FONT_HEADING,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=4, column=0, sticky="w", padx=12, pady=(12, 4))

        self._paths_frame = tk.Frame(self, bg=BG)
        self._paths_frame.grid(row=5, column=0, sticky="ew", padx=12)
        self._paths_frame.columnconfigure(1, weight=1)

        for wp in self._config.watched_paths:
            original = wp.model_dump(mode="json", exclude_defaults=True)
            # `kind` is each subclass's own default, so `exclude_defaults`
            # drops it — but a watched path with no `kind` is read back as
            # local, which would silently rewrite an ssh/cloud path.
            original["kind"] = wp.kind
            self._add_path_row(
                wp.name, str(wp.path), wp.datalab or "", original=original
            )

        tk.Button(
            self,
            text="+ Add Path",
            command=self._add_path_dialog,
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=6,
        ).grid(row=6, column=0, sticky="w", padx=12, pady=4)

        # -- Sync intervals ---------------------------------------------------
        tk.Label(
            self,
            text="Sync intervals:",
            font=FONT_HEADING,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=7, column=0, sticky="w", padx=12, pady=(12, 4))

        intervals_frame = tk.Frame(self, bg=BG)
        intervals_frame.grid(row=8, column=0, sticky="ew", padx=12)

        tk.Label(
            intervals_frame,
            text="Metadata push:",
            font=FONT_SM,
            bg=BG,
            fg=FG,
        ).grid(row=0, column=0, sticky="w")

        self._metadata_var = tk.StringVar(
            value=str(self._config.sync.metadata_interval),
        )
        tk.Entry(
            intervals_frame,
            textvariable=self._metadata_var,
            width=8,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=1, padx=4)

        tk.Label(
            intervals_frame,
            text="s",
            font=FONT_SM,
            bg=BG,
            fg=FG_DIM,
        ).grid(row=0, column=2, sticky="w")

        # -- Daemon options ---------------------------------------------------
        tk.Label(
            self,
            text="Daemon:",
            font=FONT_HEADING,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=9, column=0, sticky="w", padx=12, pady=(12, 4))

        options_frame = tk.Frame(self, bg=BG)
        options_frame.grid(row=10, column=0, sticky="ew", padx=12)

        tk.Label(
            options_frame,
            text="Log level:",
            font=FONT_SM,
            bg=BG,
            fg=FG,
        ).grid(row=0, column=0, sticky="w")

        self._log_level_var = tk.StringVar(value=self._config.log_level)
        log_level_menu = tk.OptionMenu(
            options_frame,
            self._log_level_var,
            *("debug", "info", "warning", "error"),
        )
        log_level_menu.configure(
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            highlightthickness=0,
            relief="flat",
            font=FONT_SM,
        )
        log_level_menu["menu"].configure(bg=BG_LIGHT, fg=FG)
        log_level_menu.grid(row=0, column=1, sticky="w", padx=4)

        self._reset_clocks_var = tk.BooleanVar(
            value=self._config.reset_scan_clocks_on_startup,
        )
        reset_check = tk.Checkbutton(
            options_frame,
            text="Full rescan on startup",
            variable=self._reset_clocks_var,
            font=FONT_SM,
            bg=BG,
            fg=FG,
            selectcolor=BG_LIGHT,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
        )
        reset_check.grid(row=0, column=2, sticky="w", padx=(16, 0))
        _tooltip(
            reset_check,
            "Clear the stored scan timestamps on startup so a full cold scan\n"
            "runs on the first tick. Already-synced files are not re-uploaded.",
        )

        # -- Buttons ----------------------------------------------------------
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.grid(row=11, column=0, sticky="e", padx=12, pady=(16, 12))

        tk.Button(
            btn_frame,
            text="Cancel",
            command=self.destroy,
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            btn_frame,
            text="Save",
            command=self._save,
            bg=BG_LIGHT,
            fg=GREEN,
            activebackground=BG,
            activeforeground=GREEN,
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="right")

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
        tk.Entry(
            frame,
            textvariable=name_var,
            width=12,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=0, padx=4, pady=2)

        url_var = tk.StringVar(value=url)
        tk.Entry(
            frame,
            textvariable=url_var,
            width=24,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=1, padx=4, pady=2)

        api_key_var = tk.StringVar(value=api_key)
        tk.Entry(
            frame,
            textvariable=api_key_var,
            show="*",
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=2, sticky="ew", padx=4, pady=2)

        # Admin super-user mode: let an admin key attach data to items owned
        # by other users. No-op server-side for a non-admin key.
        elevate_var = tk.BooleanVar(value=elevate_permissions)
        elevate_check = tk.Checkbutton(
            frame,
            text="sudo",
            variable=elevate_var,
            font=FONT_SM,
            bg=BG_LIGHT,
            fg=FG,
            selectcolor=BG,
            activebackground=BG_LIGHT,
            activeforeground=FG,
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
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

        tk.Button(
            frame,
            text="x",
            command=remove,
            bg=BG_LIGHT,
            fg=RED,
            activebackground=BG,
            activeforeground=RED,
            relief="flat",
            padx=4,
        ).grid(row=0, column=4, padx=(0, 4), pady=2)

        # Re-render to keep the value commitments live in the entry; for the
        # name field, propagate live changes to the path-row dropdowns so the
        # user sees the new option as soon as they type it.
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
        frame.columnconfigure(1, weight=1)

        name_var = tk.StringVar(value=name)
        tk.Entry(
            frame,
            textvariable=name_var,
            width=12,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=0, padx=4, pady=2)

        path_var = tk.StringVar(value=path)
        tk.Entry(
            frame,
            textvariable=path_var,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        datalab_var = tk.StringVar(value=datalab)
        names = self._datalab_names() or [""]
        datalab_menu = tk.OptionMenu(frame, datalab_var, *names)
        datalab_menu.configure(
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            highlightthickness=0,
            width=10,
        )
        datalab_menu["menu"].configure(bg=BG_LIGHT, fg=FG)
        datalab_menu.grid(row=0, column=2, padx=4, pady=2)

        def remove() -> None:
            frame.destroy()
            self._path_rows = [r for r in self._path_rows if r["frame"] is not frame]

        tk.Button(
            frame,
            text="x",
            command=remove,
            bg=BG_LIGHT,
            fg=RED,
            activebackground=BG,
            activeforeground=RED,
            relief="flat",
            padx=4,
        ).grid(row=0, column=3, padx=(0, 4), pady=2)

        self._path_rows.append(
            {
                "frame": frame,
                "name": name_var,
                "path": path_var,
                "datalab": datalab_var,
                "datalab_menu": datalab_menu,
                "original": original or {},
            }
        )

    def _add_path_dialog(self) -> None:
        AddPathDialog(self)

    def add_path(
        self,
        name: str,
        path: str,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        """Called by AddPathDialog on confirmation."""
        # Datalab can be picked from the dropdown after the row is added; if
        # there's only one configured datalab, pre-select it.
        names = self._datalab_names()
        default = names[0] if len(names) == 1 else ""
        original: dict = {"kind": "local"}
        if include_patterns:
            original["include_patterns"] = include_patterns
        if exclude_patterns:
            original["exclude_patterns"] = exclude_patterns
        self._add_path_row(name, path, default, original=original)

    def _save(self) -> None:
        try:
            metadata_interval = int(self._metadata_var.get())
        except ValueError:
            messagebox.showerror(
                "Validation",
                "Sync intervals must be integers.",
                parent=self,
            )
            return

        datalabs: list[dict] = []
        seen_names: set[str] = set()
        for row in self._datalab_rows:
            name = row["name"].get().strip()
            url = row["url"].get().strip()
            api_key = row["api_key"].get().strip()
            if not (name or url or api_key):
                continue
            if not name or not url:
                messagebox.showerror(
                    "Validation",
                    "Each datalab needs a name and URL.",
                    parent=self,
                )
                return
            if name in seen_names:
                messagebox.showerror(
                    "Validation",
                    f"Duplicate datalab name: {name!r}",
                    parent=self,
                )
                return
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
            messagebox.showerror(
                "Validation",
                "At least one datalab is required.",
                parent=self,
            )
            return

        watched_paths: list[dict] = []
        for row in self._path_rows:
            name = row["name"].get().strip()
            path = row["path"].get().strip()
            datalab = row["datalab"].get().strip()
            if not (name or path):
                continue
            if not name or not path:
                messagebox.showerror(
                    "Validation",
                    "Each watched path needs a name and path.",
                    parent=self,
                )
                return
            entry = dict(row["original"])
            entry.update({"path": path, "name": name})
            if datalab:
                entry["datalab"] = datalab
            else:
                entry.pop("datalab", None)
            watched_paths.append(entry)

        if not watched_paths:
            messagebox.showerror(
                "Validation",
                "At least one watched path is required.",
                parent=self,
            )
            return

        # Everything the dialog does not edit (state_db, per-path patterns,
        # scan cadences, ...) is preserved by starting from the loaded config
        # rather than rebuilding it from the widgets alone. Writing a settings
        # dialog that only knows about a subset of the schema must not be a
        # way to silently drop the rest of the user's YAML.
        config_dict = self._config.model_dump(mode="json", exclude_defaults=True)
        config_dict.update(
            {
                # `version` is written explicitly even when it matches the
                # current default: a config that loses it is re-read as v1 and
                # would be migrated a second time.
                "version": self._config.version,
                "datalabs": datalabs,
                "watched_paths": watched_paths,
                "sync": {
                    "metadata_interval": metadata_interval,
                },
                "log_level": self._log_level_var.get(),
                "reset_scan_clocks_on_startup": self._reset_clocks_var.get(),
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

        # Write to file
        config_path = self._config_path
        if config_path is None:
            from datalab_beholder.config import DEFAULT_CONFIG_PATH

            config_path = DEFAULT_CONFIG_PATH

        config_path = Path(config_path).expanduser().resolve()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.safe_dump(config_dict, f, default_flow_style=False)

        log.info("Configuration saved to %s", config_path)
        self._parent.reload_config(config_path)
        self.destroy()


# -- Add Path Sub-Dialog -----------------------------------------------------


class AddPathDialog(tk.Toplevel):
    """Dialog for adding a new watched path with all options."""

    def __init__(self, parent: SettingsDialog):
        super().__init__(parent)
        self.title("Add Watched Path")
        self.configure(bg=BG)
        self.geometry("420x280")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._parent = parent
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        pad: dict[str, Any] = {"padx": 12, "pady": 4}

        tk.Label(
            self,
            text="Name:",
            font=FONT,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", **pad)

        self._name_var = tk.StringVar()
        tk.Entry(
            self,
            textvariable=self._name_var,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=1, column=0, sticky="ew", **pad)

        tk.Label(
            self,
            text="Path:",
            font=FONT,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=2, column=0, sticky="w", **pad)

        path_frame = tk.Frame(self, bg=BG)
        path_frame.grid(row=3, column=0, sticky="ew", **pad)
        path_frame.columnconfigure(0, weight=1)

        self._path_var = tk.StringVar()
        tk.Entry(
            path_frame,
            textvariable=self._path_var,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=0, column=0, sticky="ew")

        tk.Button(
            path_frame,
            text="Browse",
            command=self._browse,
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=6,
        ).grid(row=0, column=1, padx=(4, 0))

        tk.Label(
            self,
            text="Include patterns (comma-separated):",
            font=FONT_SM,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=4, column=0, sticky="w", **pad)

        self._include_var = tk.StringVar(value="*")
        tk.Entry(
            self,
            textvariable=self._include_var,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=5, column=0, sticky="ew", **pad)

        tk.Label(
            self,
            text="Exclude patterns (comma-separated):",
            font=FONT_SM,
            bg=BG,
            fg=FG,
            anchor="w",
        ).grid(row=6, column=0, sticky="w", **pad)

        self._exclude_var = tk.StringVar()
        tk.Entry(
            self,
            textvariable=self._exclude_var,
            bg=BG_LIGHT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).grid(row=7, column=0, sticky="ew", **pad)

        # Buttons
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.grid(row=8, column=0, sticky="e", padx=12, pady=(12, 12))

        tk.Button(
            btn_frame,
            text="Cancel",
            command=self.destroy,
            bg=BG_LIGHT,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            btn_frame,
            text="Add",
            command=self._add,
            bg=BG_LIGHT,
            fg=GREEN,
            activebackground=BG,
            activeforeground=GREEN,
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="right")

    def _browse(self) -> None:
        directory = filedialog.askdirectory(parent=self)
        if directory:
            self._path_var.set(directory)
            if not self._name_var.get():
                self._name_var.set(Path(directory).name)

    def _add(self) -> None:
        name = self._name_var.get().strip()
        path = self._path_var.get().strip()

        if not name or not path:
            messagebox.showerror(
                "Validation",
                "Name and path are required.",
                parent=self,
            )
            return

        def split(raw: str) -> list[str]:
            return [part.strip() for part in raw.split(",") if part.strip()]

        self._parent.add_path(
            name,
            path,
            include_patterns=split(self._include_var.get()),
            exclude_patterns=split(self._exclude_var.get()),
        )
        self.destroy()
