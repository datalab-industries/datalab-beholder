"""Optional notification-area ("system tray") icon for the GUI.

``pystray`` and ``Pillow`` are an optional extra (``pip install
datalab-beholder[gui]``), so everything here degrades to "no tray" when
they are missing: ``tray_available()`` returns False and the GUI keeps
its ordinary window-only behaviour.

The icon is drawn at runtime rather than shipped as an asset, so there is
no binary blob in the repo and no PyInstaller ``datas`` entry to keep in
step — and the colour can follow the daemon's status for free.

Threading: ``pystray`` needs an event loop of its own, which runs here
on a daemon thread, so every callback handed to ``TrayIcon`` is invoked
from that thread. The GUI wraps each one in ``root.after`` before
touching Tk or the state DB, which keeps SQLite and Tk on the main
thread as the rest of the design assumes.

Two deliberate choices, both about not hanging or trapping the window:

* We start the loop ourselves rather than using ``run_detached``, whose
  thread is *not* a daemon — on a desktop with no notification area the
  icon never docks and that thread keeps the process alive after the
  window closes.
* ``start()`` waits for the icon to actually appear before reporting
  success. An icon the user cannot see must not be the only way to get
  a hidden window back. The X11 backend reports a docking failure by
  logging from its own thread *after* setting ``visible``, so both
  signals are checked: the flag, and a dock-failure log record.

macOS is skipped: its backend has to own the main thread, which Tk
already does here. The window behaves as it always has there.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# Status key → RGB. Keys match the GUI's own notion of overall health.
STATUS_COLOURS: dict[str, tuple[int, int, int]] = {
    "ok": (0, 204, 0),
    "warn": (204, 204, 0),
    "error": (204, 0, 0),
    "idle": (120, 120, 120),
}

_RING = (30, 30, 30)
_ICON_PX = 64

# How long to wait for the icon to dock before giving up on it.
DOCK_TIMEOUT_S = 2.0
_DOCK_POLL_S = 0.05

# pystray logs this (from its own loop thread) when there is no
# notification area to dock into; it sets `visible` anyway.
_DOCK_FAILURE_TEXT = "Failed to dock"
_PYSTRAY_LOGGER = "pystray"

# Set to 1/true/yes to force window-only behaviour: headless machines,
# users who would rather not have the icon, and the test suite, which
# must not spawn a backend loop per window it builds.
NO_TRAY_ENV = "BEHOLDER_NO_TRAY"


class _DockFailureWatcher(logging.Handler):
    """Catches pystray's own "failed to dock" report."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _DOCK_FAILURE_TEXT in record.getMessage():
                self.failed = True
        except Exception:  # pragma: no cover - defensive
            pass


def hide_on_close_default() -> bool:
    """Whether closing the window should hide it to the tray.

    True on Windows, where an instrument PC is expected to keep syncing
    with the window out of the way and the notification area is a
    reliable place to find it again. Elsewhere, closing still quits:
    Linux desktops vary too much in whether an icon really appears for
    it to be the only way back to a hidden window.
    """
    return platform.system() == "Windows"


def tray_available() -> bool:
    """Whether a tray icon can be created in this environment."""
    if os.environ.get(NO_TRAY_ENV, "").strip().lower() in ("1", "true", "yes"):
        log.debug("Tray icon disabled by %s", NO_TRAY_ENV)
        return False
    if platform.system() == "Darwin":
        log.debug("Tray icon unsupported on macOS (backend needs the main thread)")
        return False
    try:
        import PIL.Image  # noqa: F401
        import pystray  # noqa: F401
    except ImportError as e:
        log.debug("Tray icon unavailable: %s", e)
        return False
    return True


def _icon_image(status: str) -> Any:
    """Draw the tray glyph: a dark ring around a status-coloured disc."""
    from PIL import Image, ImageDraw

    colour = STATUS_COLOURS.get(status, STATUS_COLOURS["idle"])
    image = Image.new("RGBA", (_ICON_PX, _ICON_PX), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, _ICON_PX - 3, _ICON_PX - 3), fill=_RING)
    draw.ellipse((10, 10, _ICON_PX - 11, _ICON_PX - 11), fill=colour)
    return image


class TrayIcon:
    """A notification-area icon driving the GUI window.

    All callbacks are called from pystray's own thread; the caller is
    responsible for marshalling them onto the Tk thread.
    """

    def __init__(
        self,
        *,
        on_show: Callable[[], None],
        on_toggle_sync: Callable[[], None],
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        is_syncing: Callable[[], bool],
        title: str = "beholder",
    ):
        self._on_show = on_show
        self._on_toggle_sync = on_toggle_sync
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._is_syncing = is_syncing
        self._title = title
        self._icon: Any | None = None
        self._thread: threading.Thread | None = None
        self._status = "idle"

    @property
    def active(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        """Create and show the icon, returning whether it is really there.

        A missing extra, or a desktop with no notification area, is not
        an error worth taking the GUI down for — the window still works
        on its own, and the caller falls back to window-only behaviour.
        """
        if not tray_available():
            return False
        try:
            import pystray

            self._icon = pystray.Icon(
                "datalab-beholder",
                icon=_icon_image(self._status),
                title=self._title,
                menu=pystray.Menu(
                    pystray.MenuItem(
                        "Show window", lambda: self._on_show(), default=True
                    ),
                    pystray.MenuItem(
                        lambda _item: (
                            "Stop syncing" if self._is_syncing() else "Start syncing"
                        ),
                        lambda: self._on_toggle_sync(),
                    ),
                    pystray.MenuItem("Settings…", lambda: self._on_settings()),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Quit", lambda: self._on_quit()),
                ),
            )
            # Daemon thread: a backend that wedges can never hold the
            # process open once the window has gone.
            self._thread = threading.Thread(
                target=self._icon.run, name="beholder-tray", daemon=True
            )
            self._thread.start()
        except Exception:
            # Backends raise all sorts of things when no tray host exists.
            log.warning("Could not create a tray icon", exc_info=True)
            self._icon = None
            return False

        if not self._wait_until_visible():
            log.warning(
                "No notification area is available — continuing without a "
                "tray icon; the window will behave normally"
            )
            # The backend retries docking on every systray event and logs
            # a traceback each time; that noise would fill the GUI's
            # activity log for a feature we have just given up on.
            logging.getLogger(_PYSTRAY_LOGGER).setLevel(logging.CRITICAL)
            self.stop()
            return False
        return True

    def _wait_until_visible(self) -> bool:
        """Wait for the backend to report the icon as shown.

        ``pystray`` sets ``visible`` only once the platform call
        succeeded, and reports a docking failure by logging on its own
        thread, so this is the one honest signal available.
        """
        icon = self._icon
        if icon is None:
            return False

        watcher = _DockFailureWatcher()
        pystray_log = logging.getLogger(_PYSTRAY_LOGGER)
        pystray_log.addHandler(watcher)
        try:
            deadline = time.monotonic() + DOCK_TIMEOUT_S
            visible = False
            while time.monotonic() < deadline:
                if watcher.failed:
                    return False
                try:
                    visible = bool(icon.visible)
                except Exception:  # pragma: no cover - backend dependent
                    return False
                if visible:
                    # Give the backend a moment to report a failure it
                    # only notices after flipping the flag.
                    time.sleep(_DOCK_POLL_S)
                    return not watcher.failed
                time.sleep(_DOCK_POLL_S)
            return False
        finally:
            pystray_log.removeHandler(watcher)

    def set_status(self, status: str, tooltip: str) -> None:
        """Recolour the icon and replace its hover text."""
        if self._icon is None:
            return
        try:
            if status != self._status:
                self._status = status
                self._icon.icon = _icon_image(status)
            self._icon.title = tooltip
        except Exception:
            log.debug("Could not update tray icon", exc_info=True)

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            log.debug("Could not stop tray icon", exc_info=True)
        finally:
            self._icon = None
