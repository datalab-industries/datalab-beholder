"""Tests for the optional notification-area icon.

The icon itself needs a desktop, so these cover the decisions around it:
what colour a status maps to, and that every path degrades to "no tray"
rather than raising.
"""

from __future__ import annotations

import logging

import pytest

from datalab_beholder import tray

# Pillow is part of the optional `gui` extra.
pil_installed = tray.tray_available() or False
try:
    import PIL  # noqa: F401

    pil_installed = True
except ImportError:  # pragma: no cover - depends on install extras
    pil_installed = False

requires_pil = pytest.mark.skipif(not pil_installed, reason="Pillow not installed")


@pytest.fixture(autouse=True)
def pystray_logger_reset():
    """Isolate the pystray logger.

    `TrayIcon.start` mutes it process-wide when it gives up on docking,
    which a GUI test earlier in the run can trigger for real — and a
    muted logger would hide the records these tests rely on.
    """
    logger = logging.getLogger(tray._PYSTRAY_LOGGER)
    level, filters = logger.level, list(logger.filters)
    logger.setLevel(logging.NOTSET)
    logger.filters.clear()
    yield
    logger.setLevel(level)
    logger.filters[:] = filters


class TestAvailability:
    @pytest.fixture(autouse=True)
    def tray_enabled(self, monkeypatch):
        """Undo the suite-wide opt-out so availability can be exercised."""
        monkeypatch.delenv(tray.NO_TRAY_ENV, raising=False)

    def test_disabled_by_env_var(self, monkeypatch) -> None:
        monkeypatch.setattr(tray.platform, "system", lambda: "Windows")
        monkeypatch.setenv(tray.NO_TRAY_ENV, "1")
        assert tray.tray_available() is False

    def test_unavailable_without_pystray(self, monkeypatch) -> None:
        monkeypatch.setattr(tray.platform, "system", lambda: "Windows")
        real_import = __import__

        def no_pystray(name, *args, **kwargs):
            if name == "pystray":
                raise ImportError("no pystray")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", no_pystray)
        assert tray.tray_available() is False

    def test_unavailable_on_macos(self, monkeypatch) -> None:
        """The macOS backend needs the main thread, which Tk owns."""
        monkeypatch.setattr(tray.platform, "system", lambda: "Darwin")
        assert tray.tray_available() is False

    @pytest.mark.parametrize(
        "system,expected",
        [("Windows", True), ("Linux", False), ("Darwin", False)],
    )
    def test_hide_on_close_default(self, monkeypatch, system, expected) -> None:
        monkeypatch.setattr(tray.platform, "system", lambda: system)
        assert tray.hide_on_close_default() is expected


@requires_pil
class TestIconImage:
    @pytest.mark.parametrize("status", sorted(tray.STATUS_COLOURS))
    def test_status_colour_is_drawn(self, status: str) -> None:
        image = tray._icon_image(status)
        assert image.size == (tray._ICON_PX, tray._ICON_PX)
        centre = image.getpixel((tray._ICON_PX // 2, tray._ICON_PX // 2))
        assert centre[:3] == tray.STATUS_COLOURS[status]

    def test_unknown_status_falls_back_to_idle(self) -> None:
        image = tray._icon_image("something-else")
        centre = image.getpixel((tray._ICON_PX // 2, tray._ICON_PX // 2))
        assert centre[:3] == tray.STATUS_COLOURS["idle"]


def _tray_icon() -> tray.TrayIcon:
    return tray.TrayIcon(
        on_show=lambda: None,
        on_toggle_sync=lambda: None,
        on_settings=lambda: None,
        on_quit=lambda: None,
        is_syncing=lambda: False,
    )


class TestTrayIcon:
    def test_start_returns_false_when_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(tray, "tray_available", lambda: False)
        icon = _tray_icon()
        assert icon.start() is False
        assert icon.active is False

    def test_status_and_stop_are_noops_when_inactive(self) -> None:
        icon = _tray_icon()
        icon.set_status("error", "whatever")  # must not raise
        icon.stop()
        assert icon.active is False

    def test_dock_failure_is_not_success(self, monkeypatch) -> None:
        """pystray sets `visible` and only then logs that docking failed."""
        icon = _tray_icon()

        class FakeIcon:
            @property
            def visible(self) -> bool:
                logging.getLogger(tray._PYSTRAY_LOGGER).warning("Failed to dock icon")
                return True

        icon._icon = FakeIcon()
        monkeypatch.setattr(tray, "_DOCK_POLL_S", 0.0)
        assert icon._wait_until_visible() is False

    def test_visible_icon_is_success(self, monkeypatch) -> None:
        icon = _tray_icon()

        class FakeIcon:
            visible = True

        icon._icon = FakeIcon()
        monkeypatch.setattr(tray, "_DOCK_POLL_S", 0.0)
        assert icon._wait_until_visible() is True
