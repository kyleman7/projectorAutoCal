"""spotread command line built by Colorimeter.start() (no instrument needed)."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="colorimeter is POSIX-only")


def _argv(monkeypatch, **kwargs) -> list[str]:
    from projector_cal import colorimeter

    captured: list[list[str]] = []

    def fake_popen(cmd, **_):
        captured.append(cmd)
        raise FileNotFoundError  # stop before a real process is spawned

    monkeypatch.setattr(colorimeter.subprocess, "Popen", fake_popen)
    with pytest.raises(colorimeter.MeasurementError):
        colorimeter.Colorimeter(**kwargs).start()
    return captured[0]


def test_selects_instrument_with_c_not_d(monkeypatch):
    argv = _argv(monkeypatch, display_index=2)
    assert argv[argv.index("-c") + 1] == "2"
    assert "-d" not in argv  # -d is "display density"; its number became a log file


def test_passes_correction_file_with_X(monkeypatch, tmp_path):
    ccss = tmp_path / "pj.ccss"
    ccss.write_text("CCSS\n")
    argv = _argv(monkeypatch, ccss_path=str(ccss))
    assert argv[argv.index("-X") + 1] == str(ccss)


def test_missing_correction_file_fails_before_spawning(monkeypatch):
    from projector_cal import colorimeter

    monkeypatch.setattr(colorimeter.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(colorimeter.MeasurementError, match="not found"):
        colorimeter.Colorimeter(ccss_path="/nope/x.ccss").start()
