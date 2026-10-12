"""
reports.py — saved calibration reports and the chart data the Results tab draws.

Every finished run's report (engine._report_to_dict) is saved to reports/ as
``<mode>_<YYYYmmdd_HHMMSS>.json`` = ``{"saved_at", "dry_run", "report"}``.
``chart_data`` turns a report's verification pass into per-patch rows (measured
and target xyY, ΔE, RGB balance, point gamma) plus a summary — pure computation,
so old reports get the same charts as new ones.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from .color_math import GREY_RAMP, XYZ_to_xyY, get_target_xyY, patch_rgb

_REPORTS_DIR = Path(__file__).parent.parent / "reports"
_FILENAME_RE = re.compile(r"^(sdr|hdr10)_\d{8}_\d{6}(_\d+)?\.json$")

# XYZ (D65, Y = 1) → linear BT.709 RGB. Neutral D65 maps to R = G = B, so a
# grey's channel balance is each channel relative to its luminance.
_XYZ_TO_709 = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)

_COLOR_MODES = {"06": "Dynamic", "07": "Natural", "0C": "Bright Cinema", "15": "Cinema",
                "22": "Digital Cinema"}
_LAMP = {"00": "High", "01": "ECO", "02": "Medium"}
_DYNRANGE = {"00": "Auto", "01": "SDR", "02": "HDR1", "03": "HDR2", "04": "HDR3",
             "05": "HDR4", "10": "Auto (Bright)"}


class ReportNotFound(KeyError):
    pass


def _path(filename: str, directory: Path) -> Path:
    # Filenames come from the URL — only names this module writes are accepted
    if not _FILENAME_RE.match(filename):
        raise ReportNotFound(filename)
    path = directory / filename
    if not path.is_file():
        raise ReportNotFound(filename)
    return path


def save_report(report: dict, directory: Path | None = None, *, dry_run: bool = False,
                now: datetime | None = None) -> Path:
    """Write a report to ``directory`` (default reports/) and return its path."""
    directory = directory or _REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    stem = f"{report.get('mode', 'sdr')}_{now:%Y%m%d_%H%M%S}"
    path = directory / f"{stem}.json"
    n = 1
    while path.exists():
        path = directory / f"{stem}_{n}.json"
        n += 1
    path.write_text(json.dumps({"saved_at": now.isoformat(timespec="seconds"),
                                "dry_run": dry_run, "report": report}, indent=1))
    return path


def list_reports(directory: Path | None = None) -> list[dict]:
    """Saved reports, newest first, with a one-line summary each."""
    directory = directory or _REPORTS_DIR
    if not directory.is_dir():
        return []
    out = []
    for path in directory.glob("*.json"):
        if not _FILENAME_RE.match(path.name):
            continue
        try:
            doc = json.loads(path.read_text())
            report = doc["report"]
        except (OSError, ValueError, KeyError):
            continue
        out.append({"filename": path.name, "saved_at": doc.get("saved_at"),
                    "dry_run": bool(doc.get("dry_run")), "mode": report.get("mode"),
                    "phase": report.get("phase"), "aborted": bool(report.get("aborted")),
                    **_summary(_rows(report))})
    return sorted(out, key=lambda r: r["saved_at"] or "", reverse=True)


def load_report(filename: str, directory: Path | None = None) -> dict:
    """A saved report with its chart data. Raises ReportNotFound."""
    path = _path(filename, directory or _REPORTS_DIR)
    doc = json.loads(path.read_text())
    return {"filename": filename, **doc, "chart": chart_data(doc["report"])}


def delete_report(filename: str, directory: Path | None = None) -> None:
    _path(filename, directory or _REPORTS_DIR).unlink()


# ---------------------------------------------------------------------------
# Chart data
# ---------------------------------------------------------------------------

def _group(patch: str) -> str:
    if patch == "white" or patch.startswith("grey"):
        return "grey"
    if "_sat" in patch:
        return "sat" + patch.split("_sat")[1]
    return "sat100"


def _rows(report: dict) -> list[dict]:
    mode = report.get("mode", "sdr")
    gamma = report.get("target_gamma") or 2.2
    results = [r for r in report.get("verify_results") or []
               if r.get("delta_e") is not None and r.get("xyz")]
    white = next((r for r in results if r["patch"] == "white"), None)
    white_Y = white["xyz"][1] if white else 0.0
    if white_Y <= 0:
        return []

    rows = []
    for r in results:
        name = r["patch"]
        try:
            tx, ty, tY = get_target_xyY(name, mode, gamma)
            code = patch_rgb(name)
        except KeyError:
            continue
        X, Y, Z = (v / white_Y for v in r["xyz"])          # relative, white = 1
        x, y, _ = XYZ_to_xyY(X, Y, Z)
        group = _group(name)
        neutral = group == "grey"
        stim = code[0] / 255 if neutral else None
        row = {
            "patch": name, "group": group,
            "color": name.split("_sat")[0] if not neutral else "grey",
            "rgb": list(code),
            "stimulus_pct": round(stim * 100, 2) if stim is not None else None,
            "x": round(x, 5), "y": round(y, 5),
            "luminance_pct": round(Y * 100, 3),
            "luminance_cdm2": round(r["xyz"][1], 4),
            "target_x": round(tx, 5), "target_y": round(ty, 5),
            "target_luminance_pct": round(tY, 3),
            "delta_e": round(r["delta_e"], 3),
            "red_pct": None, "green_pct": None, "blue_pct": None, "point_gamma": None,
        }
        if neutral and Y > 0:
            rgb = [sum(m * v for m, v in zip(line, (X, Y, Z))) for line in _XYZ_TO_709]
            row["red_pct"], row["green_pct"], row["blue_pct"] = (round(c / Y * 100, 2) for c in rgb)
            if 0 < stim < 1:
                row["point_gamma"] = round(math.log(Y) / math.log(stim), 3)
        rows.append(row)
    return rows


def _summary(rows: list[dict]) -> dict:
    if not rows:
        return {"patches": 0, "mean_de": None, "max_de": None, "worst_patch": None, "under_1": 0}
    worst = max(rows, key=lambda r: r["delta_e"])
    return {
        "patches": len(rows),
        "mean_de": round(sum(r["delta_e"] for r in rows) / len(rows), 3),
        "max_de": worst["delta_e"],
        "worst_patch": worst["patch"],
        "under_1": sum(1 for r in rows if r["delta_e"] < 1.0),
    }


def _final_settings(report: dict) -> dict:
    s = dict(report.get("baseline") or {})
    for axis, (_, after) in (report.get("setting_changes") or {}).items():
        if axis in ("BRIGHT", "CONTRAST"):
            s[axis] = str(after)
    return {
        "color_mode": _COLOR_MODES.get(s.get("CMODE", ""), s.get("CMODE")),
        "lamp": _LAMP.get(s.get("LUMINANCE", ""), s.get("LUMINANCE")),
        "dynamic_range": _DYNRANGE.get(s.get("DYNRANGE", ""), s.get("DYNRANGE")),
        "brightness": s.get("BRIGHT"), "contrast": s.get("CONTRAST"),
    }


def chart_data(report: dict) -> dict:
    """Everything the Results tab charts need, from one report."""
    rows = _rows(report)
    white = next((r for r in rows if r["patch"] == "white"), None)
    groups = {}
    for g in ("grey", "sat100", "sat75", "sat50", "sat25"):
        des = [r["delta_e"] for r in rows if r["group"] == g]
        if des:
            groups[g] = round(sum(des) / len(des), 3)
    return {
        "mode": report.get("mode"),
        "target_gamma": report.get("target_gamma") or 2.2,
        "ambient_compensation": bool(report.get("ambient_compensation")),
        "white_cdm2": white["luminance_cdm2"] if white else None,
        "settings": _final_settings(report),
        "summary": {**_summary(rows), "group_mean_de": groups},
        "grey_ramp": list(GREY_RAMP),
        "rows": rows,
    }
