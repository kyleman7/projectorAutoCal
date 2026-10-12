"""Tests for saved run reports and the chart data the Results tab draws."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from projector_cal.color_math import get_target_xyY, xyY_to_XYZ
from projector_cal.engine import _VERIFY_PATCHES
from projector_cal.reports import (
    ReportNotFound,
    chart_data,
    delete_report,
    list_reports,
    load_report,
    save_report,
)

_WHITE_CDM2 = 35.0


def _perfect_report(mode: str = "sdr", gamma: float = 2.2, de: float = 0.4) -> dict:
    """A report whose verify pass measured every target exactly (absolute cd/m²)."""
    results = []
    for name in _VERIFY_PATCHES:
        X, Y, Z = xyY_to_XYZ(*get_target_xyY(name, mode, gamma))
        results.append({"patch": name, "delta_e": de, "iterations": 1, "converged": True,
                        "xyz": [v * _WHITE_CDM2 / 100 for v in (X, Y, Z)], "initial_delta_e": None})
    return {"mode": mode, "phase": "all", "target_gamma": gamma, "ambient_compensation": False,
            "baseline": {"CMODE": "07", "LUMINANCE": "02", "DYNRANGE": "01",
                         "BRIGHT": "126", "CONTRAST": "124"},
            "setting_changes": {"CONTRAST": [124, 119], "WB_R": [103, 106]},
            "verify_results": results, "aborted": False}


def test_perfect_measurements_chart_as_neutral_and_on_target():
    chart = chart_data(_perfect_report())
    rows = {r["patch"]: r for r in chart["rows"]}
    assert len(rows) == len(_VERIFY_PATCHES)
    assert chart["white_cdm2"] == pytest.approx(_WHITE_CDM2)
    for r in rows.values():
        assert r["x"] == pytest.approx(r["target_x"], abs=1e-4)
        assert r["luminance_pct"] == pytest.approx(r["target_luminance_pct"], abs=1e-2)
    grey50 = rows["grey50"]
    assert grey50["group"] == "grey" and grey50["stimulus_pct"] == pytest.approx(50.2, abs=0.01)
    for ch in ("red_pct", "green_pct", "blue_pct"):
        assert grey50[ch] == pytest.approx(100, abs=0.1)
    assert grey50["point_gamma"] == pytest.approx(2.2, abs=1e-3)
    assert rows["white"]["point_gamma"] is None          # undefined at 100 %
    assert rows["red"]["red_pct"] is None and rows["red"]["group"] == "sat100"
    assert rows["cyan_sat50"]["group"] == "sat50" and rows["cyan_sat50"]["color"] == "cyan"


def test_point_gamma_follows_target_gamma():
    chart = chart_data(_perfect_report(gamma=2.0))
    assert chart["target_gamma"] == 2.0
    g = next(r for r in chart["rows"] if r["patch"] == "grey30")
    assert g["point_gamma"] == pytest.approx(2.0, abs=1e-3)


def test_grey_tint_shows_in_rgb_balance():
    report = _perfect_report()
    grey = next(r for r in report["verify_results"] if r["patch"] == "grey50")
    grey["xyz"][2] *= 1.1                                  # more Z → bluer
    row = next(r for r in chart_data(report)["rows"] if r["patch"] == "grey50")
    assert row["blue_pct"] > 105 and row["red_pct"] < 100


def test_summary_and_final_settings():
    report = _perfect_report()
    report["verify_results"][3]["delta_e"] = 2.5
    worst = report["verify_results"][3]["patch"]
    chart = chart_data(report)
    s = chart["summary"]
    assert s["max_de"] == 2.5 and s["worst_patch"] == worst
    assert s["under_1"] == len(_VERIFY_PATCHES) - 1
    assert set(s["group_mean_de"]) == {"grey", "sat100", "sat75", "sat50", "sat25"}
    assert chart["settings"] == {"color_mode": "Natural", "lamp": "Medium", "dynamic_range": "SDR",
                                 "brightness": "126", "contrast": "119"}


def test_unmeasured_patches_are_skipped():
    report = _perfect_report()
    report["verify_results"][5]["delta_e"] = None          # aborted before it was measured
    assert len(chart_data(report)["rows"]) == len(_VERIFY_PATCHES) - 1
    report["verify_results"] = []
    assert chart_data(report)["rows"] == []


def test_save_list_load_delete(tmp_path):
    a = save_report(_perfect_report(), tmp_path, now=datetime(2026, 10, 11, 2, 54, 23))
    b = save_report(_perfect_report("hdr10"), tmp_path, dry_run=True, now=datetime(2026, 10, 11, 4, 47, 14))
    same = save_report(_perfect_report(), tmp_path, now=datetime(2026, 10, 11, 2, 54, 23))
    assert a.name == "sdr_20261011_025423.json" and same.name == "sdr_20261011_025423_1.json"
    (tmp_path / "notes.json").write_text("{}")             # not ours — ignored

    listed = list_reports(tmp_path)
    assert [r["filename"] for r in listed][0] == b.name     # newest first
    assert len(listed) == 3
    hdr = listed[0]
    assert hdr["mode"] == "hdr10" and hdr["dry_run"] and hdr["mean_de"] == pytest.approx(0.4)

    loaded = load_report(a.name, tmp_path)
    assert loaded["saved_at"] == "2026-10-11T02:54:23"
    assert loaded["report"]["mode"] == "sdr" and loaded["chart"]["rows"]

    delete_report(a.name, tmp_path)
    assert not a.exists()
    with pytest.raises(ReportNotFound):
        load_report(a.name, tmp_path)


@pytest.mark.parametrize("name", ["../config.json", "notes.json", "sdr_20261011_025423.json/../x"])
def test_load_rejects_other_files(tmp_path, name):
    (tmp_path / "notes.json").write_text(json.dumps({"report": {}}))
    with pytest.raises(ReportNotFound):
        load_report(name, tmp_path)
