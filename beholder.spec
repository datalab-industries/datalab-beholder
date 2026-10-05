# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for building a standalone datalab-beholder binary."""

a = Analysis(
    ["src/datalab_beholder/__main__.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pydantic",
        "pydantic.deprecated",
        "pydantic.deprecated.decorator",
        "pydantic._internal",
        "pydantic._internal._config",
        "pydantic._internal._decorators",
        "pydantic._internal._fields",
        "pydantic._internal._generate_schema",
        "pydantic._internal._generics",
        "pydantic._internal._model_construction",
        "pydantic._internal._validators",
        "yaml",
        "httpx",
        "httpx._transports",
        "httpcore",
        "h11",
        "certifi",
        "anyio",
        "anyio._backends",
        "anyio._backends._asyncio",
        "watchdog",
        "watchdog.observers",
        "watchdog.events",
        "click",
        "sqlite3",
        # Tray icon (optional extra, bundled in the binary when present).
        "pystray",
        "pystray._win32",
        "pystray._xorg",
        "pystray._darwin",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "bokeh",
        "tornado",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="datalab-beholder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    # Console app, so `start`/`status`/`dry-run` still print into a
    # terminal that launched them — but when the exe creates a console of
    # its own (double-click, or launch at login) it is hidden straight
    # away, so `gui` comes up as a windowed app with no console flash.
    hide_console="hide-early",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
