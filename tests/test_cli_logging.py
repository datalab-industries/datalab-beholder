"""Tests for the GUI's stdout/stderr handling.

A windowed build has no console, so writing to `sys.stdout` raises (or
the stream is None outright). The GUI points those at its log file —
but must leave working streams alone, or every record would land in the
file twice.
"""

from __future__ import annotations

import io
from pathlib import Path

from datalab_beholder.cli import _redirect_std_streams, _stream_usable


class _Broken(io.StringIO):
    def write(self, _s: str) -> int:
        raise OSError("no console")


class TestStreamUsable:
    def test_none_is_unusable(self) -> None:
        assert _stream_usable(None) is False

    def test_raising_stream_is_unusable(self) -> None:
        assert _stream_usable(_Broken()) is False

    def test_normal_stream_is_usable(self) -> None:
        assert _stream_usable(io.StringIO()) is True


class TestRedirect:
    def test_working_streams_are_left_alone(self, tmp_path: Path, monkeypatch) -> None:
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        monkeypatch.setattr("sys.stderr", err)

        assert _redirect_std_streams(tmp_path / "beholder.log") is False

        import sys

        assert sys.stdout is out
        assert sys.stderr is err
        assert not (tmp_path / "beholder.log").exists()

    def test_unusable_streams_go_to_the_logfile(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        logfile = tmp_path / "beholder.log"
        monkeypatch.setattr("sys.stdout", None)
        monkeypatch.setattr("sys.stderr", _Broken())

        assert _redirect_std_streams(logfile) is True

        import sys

        print("printed with no console")
        sys.stdout.flush()
        assert "printed with no console" in logfile.read_text()

    def test_unwritable_logfile_is_survivable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("sys.stdout", None)
        monkeypatch.setattr("sys.stderr", None)
        # A directory where the file should be: open() fails.
        logfile = tmp_path / "beholder.log"
        logfile.mkdir()

        assert _redirect_std_streams(logfile) is False
