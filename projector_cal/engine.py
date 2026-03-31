"""Calibration engine: the closed-loop correction algorithm.

Three phases:
  Phase 1 — White Balance: adjust R/G/B gains to hit D65 white point on the white patch.
  Phase 2 — CMS: per-axis Hue/Sat/Lum corrections for all 6 color axes.
  Phase 3 — Verification: final measurement pass, no corrections, produces the report.

The engine is hardware-agnostic: it depends on ProjectorClient, a PatchDisplay adapter,
and a measure() callable — all injected at construction time.

Event callbacks (optional) allow the web server to broadcast progress via WebSocket.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from .color_math import (
    XYZ_to_xyY,
    delta_e_2000,
    get_target_lab,
    get_target_xyY,
    patch_rgb,
    xyY_to_XYZ,
    xyz_to_lab,
)
from .config import CalibrationConfig
from .pgen import PatchDisplay, RGBPatch
from .projector import ProjectorClient, ProjectorError

logger = logging.getLogger(__name__)

# Patches measured in Phase 2 and Phase 3
_CMS_PATCHES = ["red", "green", "blue", "cyan", "magenta", "yellow"]
_VERIFY_PATCHES = ["white", "red", "green", "blue", "cyan", "magenta", "yellow", "grey75", "grey50"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PatchResult:
    """Measurement result for a single patch."""
    patch_name: str
    final_delta_e: float
    iterations: int
    converged: bool
    final_xyz: tuple[float, float, float]
    initial_delta_e: float | None = None


@dataclass
class CalibrationReport:
    """Complete result from one calibration run."""
    mode: Literal["sdr", "hdr10"]
    phase: Literal["wb", "cms", "all"]
    delta_e_threshold: float
    wb_result: PatchResult | None = None
    cms_results: list[PatchResult] = field(default_factory=list)
    verify_results: list[PatchResult] = field(default_factory=list)
    total_iterations: int = 0
    aborted: bool = False

    @property
    def patches(self) -> list[PatchResult]:
        results = []
        if self.wb_result:
            results.append(self.wb_result)
        results.extend(self.cms_results)
        results.extend(self.verify_results)
        return results

    @property
    def converged_patches(self) -> int:
        return sum(1 for p in self.patches if p.converged)

    def final_delta_e_table(self) -> dict[str, float]:
        return {p.patch_name: p.final_delta_e for p in self.verify_results or self.patches}


# ---------------------------------------------------------------------------
# Event types (for WebSocket broadcast)
# ---------------------------------------------------------------------------

EventCallback = Callable[[dict], None]


def _noop_event(_: dict) -> None:
    pass


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class CalibrationEngine:
    """Closed-loop calibration engine.

    Args:
        projector: Connected ProjectorClient.
        display: PatchDisplay adapter (PGenClient or NullPatchDisplay).
        measure: Callable that returns (X, Y, Z) — typically Colorimeter.measure.
        config: CalibrationConfig with thresholds and gain ranges.
        mode: "sdr" or "hdr10".
        dry_run: If True, send corrections to projector but don't apply them (no-op).
        on_event: Optional callback for WebSocket events.
    """

    def __init__(
        self,
        projector: ProjectorClient,
        display: PatchDisplay,
        measure: Callable[[], tuple[float, float, float]],
        config: CalibrationConfig,
        mode: Literal["sdr", "hdr10"] = "sdr",
        dry_run: bool = False,
        on_event: EventCallback = _noop_event,
    ) -> None:
        self.projector = projector
        self.display = display
        self.measure = measure
        self.config = config
        self.mode = mode
        self.dry_run = dry_run
        self.on_event = on_event
        self._abort = False

    def abort(self) -> None:
        """Signal the engine to stop after the current iteration."""
        self._abort = True

    # ------------------------------------------------------------------
    # Top-level entry points
    # ------------------------------------------------------------------

    def run_all(self) -> CalibrationReport:
        """Run Phase 1 (WB) + Phase 2 (CMS) + Phase 3 (verify)."""
        report = CalibrationReport(
            mode=self.mode,
            phase="all",
            delta_e_threshold=self.config.delta_e_threshold,
        )
        report.wb_result = self._phase1_white_balance()
        report.total_iterations += report.wb_result.iterations

        if not self._abort:
            for result in self._phase2_cms():
                report.cms_results.append(result)
                report.total_iterations += result.iterations

        if not self._abort:
            report.verify_results = self._phase3_verify()

        report.aborted = self._abort
        self.on_event({"event": "run_complete", "report": _report_to_dict(report)})
        return report

    def run_wb_only(self) -> CalibrationReport:
        """Run Phase 1 (WB) + Phase 3 (verify) only."""
        report = CalibrationReport(mode=self.mode, phase="wb", delta_e_threshold=self.config.delta_e_threshold)
        report.wb_result = self._phase1_white_balance()
        report.total_iterations = report.wb_result.iterations
        if not self._abort:
            report.verify_results = self._phase3_verify()
        report.aborted = self._abort
        self.on_event({"event": "run_complete", "report": _report_to_dict(report)})
        return report

    def run_cms_only(self) -> CalibrationReport:
        """Run Phase 2 (CMS) + Phase 3 (verify) only (assumes WB already done)."""
        report = CalibrationReport(mode=self.mode, phase="cms", delta_e_threshold=self.config.delta_e_threshold)
        for result in self._phase2_cms():
            report.cms_results.append(result)
            report.total_iterations += result.iterations
        if not self._abort:
            report.verify_results = self._phase3_verify()
        report.aborted = self._abort
        self.on_event({"event": "run_complete", "report": _report_to_dict(report)})
        return report

    # ------------------------------------------------------------------
    # Phase 1 — White Balance
    # ------------------------------------------------------------------

    def _phase1_white_balance(self) -> PatchResult:
        """Adjust R/G/B gains to match D65 white point on the white patch."""
        cfg = self.config
        threshold = cfg.delta_e_threshold
        gain_lo, gain_hi = cfg.wb_gain_range
        gain_range = gain_hi - gain_lo
        target_lab = get_target_lab("white", self.mode)
        target_x, target_y, _ = get_target_xyY("white", self.mode)

        self.on_event({"event": "phase_start", "phase": "wb"})
        logger.info("Phase 1: White Balance [mode=%s]", self.mode)

        # Read current gains
        gains = {
            "R": self.projector.get_wb_gain("R"),
            "G": self.projector.get_wb_gain("G"),
            "B": self.projector.get_wb_gain("B"),
        }
        logger.debug("Initial gains: %s", gains)

        step_scale = cfg.proportional_gain_initial
        initial_de: float | None = None
        final_de = float("inf")
        converged = False

        rgb = patch_rgb("white")
        self.display.display_patch(RGBPatch(*rgb))

        for iteration in range(1, cfg.max_iterations_per_patch + 1):
            if self._abort:
                break

            self.on_event({"event": "patch_start", "patch": "white", "iteration": iteration, "mode": "wb"})
            X, Y, Z = self.measure()
            meas_lab = xyz_to_lab(X, Y, Z)
            de = delta_e_2000(meas_lab, target_lab)
            self.on_event({"event": "measurement", "patch": "white", "xyz": [X, Y, Z], "delta_e": de})

            if initial_de is None:
                initial_de = de
            final_de = de

            logger.info("WB iter %d: ΔE=%.4f (target<%.2f)", iteration, de, threshold)

            if de < threshold:
                converged = True
                break

            # Chromaticity correction — keep Y fixed, correct xy toward D65
            # Compute how far each channel's XYZ contribution is from target
            _, meas_y, _ = XYZ_to_xyY(X, Y, Z)
            target_X, target_Y, target_Z = xyY_to_XYZ(target_x, target_y, Y)

            r_err = (X - target_X) / max(target_X, 1e-6)
            g_err = (Y - target_Y) / max(target_Y, 1e-6)
            b_err = (Z - target_Z) / max(target_Z, 1e-6)

            new_gains = {
                "R": _clamp(gains["R"] - round(r_err * step_scale * gain_range), gain_lo, gain_hi),
                "G": _clamp(gains["G"] - round(g_err * step_scale * gain_range), gain_lo, gain_hi),
                "B": _clamp(gains["B"] - round(b_err * step_scale * gain_range), gain_lo, gain_hi),
            }

            for ch in ("R", "G", "B"):
                if new_gains[ch] != gains[ch]:
                    self.on_event({
                        "event": "correction", "patch": "white", "axis": f"WB_{ch}",
                        "before": gains[ch], "after": new_gains[ch],
                    })
                    if not self.dry_run:
                        self.projector.set_wb_gain(ch, new_gains[ch])
                    gains[ch] = new_gains[ch]

            # Decay step scale toward minimum as we approach target
            step_scale = max(
                cfg.proportional_gain_minimum,
                step_scale * (de / threshold) * 0.5
            )

        if not converged:
            logger.warning("WB did not converge after %d iterations (ΔE=%.4f)", cfg.max_iterations_per_patch, final_de)

        result = PatchResult(
            patch_name="white",
            final_delta_e=final_de,
            iterations=iteration,
            converged=converged,
            final_xyz=(X, Y, Z),
            initial_delta_e=initial_de,
        )
        self.on_event({"event": "patch_done", "patch": "white", "delta_e": final_de,
                       "iterations": iteration, "converged": converged})
        self.on_event({"event": "phase_done", "phase": "wb"})
        return result

    # ------------------------------------------------------------------
    # Phase 2 — CMS
    # ------------------------------------------------------------------

    def _phase2_cms(self) -> list[PatchResult]:
        """Adjust CMS Hue/Sat/Lum for each color axis."""
        self.on_event({"event": "phase_start", "phase": "cms"})
        logger.info("Phase 2: CMS [mode=%s]", self.mode)
        results: list[PatchResult] = []

        for axis in self.config.cms_axis_order:
            if self._abort:
                break
            result = self._cms_axis(axis)
            results.append(result)

        self.on_event({"event": "phase_done", "phase": "cms"})
        return results

    def _cms_axis(self, axis: str) -> PatchResult:
        """Run the correction loop for one CMS axis."""
        cfg = self.config
        threshold = cfg.delta_e_threshold
        target_lab = get_target_lab(axis, self.mode)

        cms = {
            "HUE": self.projector.get_cms(axis, "HUE"),
            "SAT": self.projector.get_cms(axis, "SAT"),
            "LUM": self.projector.get_cms(axis, "LUM"),
        }

        step_scale = cfg.proportional_gain_initial
        initial_de: float | None = None
        final_de = float("inf")
        converged = False

        rgb = patch_rgb(axis)
        self.display.display_patch(RGBPatch(*rgb))

        X, Y, Z = 0.0, 0.0, 0.0

        for iteration in range(1, cfg.max_iterations_per_patch + 1):
            if self._abort:
                break

            self.on_event({"event": "patch_start", "patch": axis, "iteration": iteration, "mode": "cms"})
            X, Y, Z = self.measure()
            meas_lab = xyz_to_lab(X, Y, Z)
            de = delta_e_2000(meas_lab, target_lab)
            self.on_event({"event": "measurement", "patch": axis, "xyz": [X, Y, Z], "delta_e": de})

            if initial_de is None:
                initial_de = de
            final_de = de

            logger.info("CMS %s iter %d: ΔE=%.4f", axis, iteration, de)

            if de < threshold:
                converged = True
                break

            # Decompose Lab error into Hue/Sat/Lum corrections
            L_meas, a_meas, b_meas = meas_lab
            L_tgt,  a_tgt,  b_tgt  = target_lab

            # Hue error: angular difference in the ab plane
            hue_meas = math.atan2(b_meas, a_meas)
            hue_tgt  = math.atan2(b_tgt,  a_tgt)
            hue_err_deg = math.degrees(hue_tgt - hue_meas)
            # Normalize to (-180, 180)
            hue_err_deg = (hue_err_deg + 180) % 360 - 180

            # Saturation error: chroma ratio
            chroma_meas = math.sqrt(a_meas ** 2 + b_meas ** 2)
            chroma_tgt  = math.sqrt(a_tgt  ** 2 + b_tgt  ** 2)
            sat_ratio_err = (chroma_tgt - chroma_meas) / max(chroma_tgt, 1e-6)

            # Luminance error
            lum_err = (L_tgt - L_meas) / 100.0

            hue_delta = -round(hue_err_deg * step_scale)
            sat_delta = -round(sat_ratio_err * step_scale * 10)
            lum_delta = -round(lum_err * step_scale * 10)

            new_cms = {
                "HUE": cms["HUE"] + hue_delta,
                "SAT": cms["SAT"] + sat_delta,
                "LUM": cms["LUM"] + lum_delta,
            }

            for prop in ("HUE", "SAT", "LUM"):
                if new_cms[prop] != cms[prop]:
                    self.on_event({
                        "event": "correction", "patch": axis, "axis": prop,
                        "before": cms[prop], "after": new_cms[prop],
                    })
                    if not self.dry_run:
                        self.projector.set_cms(axis, prop, new_cms[prop])
                    cms[prop] = new_cms[prop]

            step_scale = max(
                cfg.proportional_gain_minimum,
                step_scale * (de / threshold) * 0.5
            )

        if not converged:
            logger.warning("CMS %s did not converge after %d iterations (ΔE=%.4f)",
                           axis, cfg.max_iterations_per_patch, final_de)

        result = PatchResult(
            patch_name=axis,
            final_delta_e=final_de,
            iterations=iteration,
            converged=converged,
            final_xyz=(X, Y, Z),
            initial_delta_e=initial_de,
        )
        self.on_event({"event": "patch_done", "patch": axis, "delta_e": final_de,
                       "iterations": iteration, "converged": converged})
        return result

    # ------------------------------------------------------------------
    # Phase 3 — Verification
    # ------------------------------------------------------------------

    def _phase3_verify(self) -> list[PatchResult]:
        """Measure all patches without applying corrections."""
        self.on_event({"event": "phase_start", "phase": "verify"})
        logger.info("Phase 3: Verification")
        results: list[PatchResult] = []

        for patch_name in _VERIFY_PATCHES:
            if self._abort:
                break
            rgb = patch_rgb(patch_name)
            self.display.display_patch(RGBPatch(*rgb))
            X, Y, Z = self.measure()
            meas_lab = xyz_to_lab(X, Y, Z)
            target_lab = get_target_lab(patch_name, self.mode)
            de = delta_e_2000(meas_lab, target_lab)

            self.on_event({"event": "measurement", "patch": patch_name, "xyz": [X, Y, Z], "delta_e": de})
            logger.info("Verify %s: ΔE=%.4f %s", patch_name, de,
                        "✓" if de < self.config.delta_e_threshold else "✗")

            results.append(PatchResult(
                patch_name=patch_name,
                final_delta_e=de,
                iterations=1,
                converged=de < self.config.delta_e_threshold,
                final_xyz=(X, Y, Z),
            ))

        self.display.display_black()
        self.on_event({"event": "phase_done", "phase": "verify"})
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _report_to_dict(report: CalibrationReport) -> dict:
    """Serialize CalibrationReport to a JSON-safe dict for WebSocket events."""
    def _patch_dict(p: PatchResult) -> dict:
        return {
            "patch": p.patch_name,
            "delta_e": round(p.final_delta_e, 4),
            "iterations": p.iterations,
            "converged": p.converged,
            "xyz": [round(v, 4) for v in p.final_xyz],
            "initial_delta_e": round(p.initial_delta_e, 4) if p.initial_delta_e is not None else None,
        }

    return {
        "mode": report.mode,
        "phase": report.phase,
        "delta_e_threshold": report.delta_e_threshold,
        "total_iterations": report.total_iterations,
        "converged_patches": report.converged_patches,
        "aborted": report.aborted,
        "wb_result": _patch_dict(report.wb_result) if report.wb_result else None,
        "cms_results": [_patch_dict(p) for p in report.cms_results],
        "verify_results": [_patch_dict(p) for p in report.verify_results],
    }
