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
from dataclasses import dataclass, field
from typing import Callable, Literal

from .color_math import (
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

# Consecutive non-improving iterations before a patch loop reverts to its
# best-known settings and stops (protects against a wrong control-direction
# assumption or an unstable loop walking the projector away from target).
_MAX_WORSENING_ITERATIONS = 3


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
class _LoopOutcome:
    """What one run of the shared correction loop produced."""
    final_de: float
    initial_de: float | None
    iterations: int
    converged: bool
    xyz: tuple[float, float, float]


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

    Measurements from the colorimeter are absolute (cd/m²); all targets are
    relative to white = 100. The engine measures a reference white and rescales
    every reading before comparing to targets (relative colorimetry).

    Args:
        projector: Connected ProjectorClient.
        display: PatchDisplay adapter (PGenClient or NullPatchDisplay).
        measure: Callable that returns (X, Y, Z) — typically Colorimeter.measure.
        config: CalibrationConfig with thresholds and gain ranges.
        mode: "sdr" or "hdr10".
        dry_run: If True, send corrections to projector but don't apply them (no-op).
        on_event: Optional callback for WebSocket events.
        anomaly_check: Optional callable(patch, recent_xyz, recent_de) -> dict
            (see agents.anomaly_detector.detect_anomaly). Called every 3rd
            iteration; its "action" may retry a measurement or abort the patch.
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
        anomaly_check: Callable[[str, list, list], dict] | None = None,
    ) -> None:
        self.projector = projector
        self.display = display
        self.measure = measure
        self.config = config
        self.mode = mode
        self.dry_run = dry_run
        self.on_event = on_event
        self.anomaly_check = anomaly_check
        self._abort = False
        self._ref_white_Y: float | None = None

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
    # Shared correction loop
    # ------------------------------------------------------------------
    #
    # Both correction phases (WB and CMS) run the same closed loop: measure →
    # normalize → ΔE → converge/diverge checks → anomaly check → compute and
    # apply a proportional correction → decay the step. Only the normalization
    # and the correction physics differ, so they are injected as callables.

    def _correction_loop(
        self,
        patch_name: str,
        mode: str,
        settings: dict,
        normalize: Callable[[tuple[float, float, float]], tuple[float, float, float]],
        target_lab: tuple[float, float, float],
        compute_corrections: Callable[[tuple, tuple, dict, float], dict],
        apply_settings: Callable[[dict, dict], None],
    ) -> _LoopOutcome:
        """Drive one patch's correction loop until convergence or a stop condition.

        Args:
            patch_name: Patch under correction (also the event name).
            mode: "wb" or "cms" — for patch_start events and log lines.
            settings: Live control values (mutated in place via apply_settings).
            normalize: Maps a raw absolute XYZ reading to the white=100 scale.
            target_lab: Lab target the loop converges toward.
            compute_corrections: (norm_xyz, meas_lab, settings, step_scale) →
                proposed new settings dict.
            apply_settings: Diffs, emits correction events, writes to the
                projector (unless dry-run), and mutates `settings`.
        """
        cfg = self.config
        threshold = cfg.delta_e_threshold
        step_scale = cfg.proportional_gain_initial
        initial_de: float | None = None
        final_de = float("inf")
        converged = False
        iteration = 0
        X = Y = Z = 0.0
        best_de = float("inf")
        best_settings = dict(settings)
        worse_streak = 0
        recent_xyz: list[tuple[float, float, float]] = []
        recent_de: list[float] = []

        self.display.display_patch(RGBPatch(*patch_rgb(patch_name)))

        for iteration in range(1, cfg.max_iterations_per_patch + 1):
            if self._abort:
                break

            self.on_event({"event": "patch_start", "patch": patch_name, "iteration": iteration, "mode": mode})
            X, Y, Z = self.measure()
            norm_xyz = normalize((X, Y, Z))
            meas_lab = xyz_to_lab(*norm_xyz)
            de = delta_e_2000(meas_lab, target_lab)
            self.on_event({"event": "measurement", "patch": patch_name, "xyz": [X, Y, Z], "delta_e": de})

            if initial_de is None:
                initial_de = de
            final_de = de
            recent_xyz.append((X, Y, Z))
            recent_de.append(de)
            del recent_xyz[:-5], recent_de[:-5]

            logger.info("%s %s iter %d: ΔE=%.4f (target<%.2f)",
                        mode.upper(), patch_name, iteration, de, threshold)

            if de < threshold:
                converged = True
                break

            if de < best_de:
                best_de, best_settings, worse_streak = de, dict(settings), 0
            else:
                worse_streak += 1
                if worse_streak >= _MAX_WORSENING_ITERATIONS:
                    logger.warning("%s %s diverging — reverting to best-known settings %s (ΔE=%.4f)",
                                   mode.upper(), patch_name, best_settings, best_de)
                    apply_settings(settings, best_settings)
                    break

            action = self._run_anomaly_check(patch_name, iteration, recent_xyz, recent_de)
            if action == "abort_patch":
                break
            if action == "retry_measurement":
                continue

            apply_settings(settings, compute_corrections(norm_xyz, meas_lab, settings, step_scale))
            step_scale = self._decay_step(step_scale, de)

        if not converged and not self._abort:
            # Settings changed after the last reading (final-iteration correction
            # or divergence revert) — re-measure once so the reported result
            # reflects the projector's actual final state.
            X, Y, Z = self.measure()
            final_de = delta_e_2000(xyz_to_lab(*normalize((X, Y, Z))), target_lab)

        if not converged:
            logger.warning("%s %s did not converge after %d iterations (ΔE=%.4f)",
                           mode.upper(), patch_name, iteration, final_de)

        return _LoopOutcome(
            final_de=final_de,
            initial_de=initial_de,
            iterations=iteration,
            converged=converged,
            xyz=(X, Y, Z),
        )

    def _apply_settings(
        self,
        patch: str,
        current: dict,
        new: dict,
        axis_names: dict[str, str],
        setter: Callable[[str, int], None],
    ) -> None:
        """Diff `new` against `current`, emit correction events, and write changed
        values to the projector (unless dry-run); mutates `current` in place."""
        for key in current:
            if new[key] != current[key]:
                self.on_event({
                    "event": "correction", "patch": patch, "axis": axis_names[key],
                    "before": current[key], "after": new[key],
                })
                if not self.dry_run:
                    setter(key, new[key])
                current[key] = new[key]

    def _emit_patch_result(self, outcome: _LoopOutcome, patch_name: str) -> PatchResult:
        """Build the PatchResult for a loop outcome and emit its patch_done event."""
        result = PatchResult(
            patch_name=patch_name,
            final_delta_e=outcome.final_de,
            iterations=outcome.iterations,
            converged=outcome.converged,
            final_xyz=outcome.xyz,
            initial_delta_e=outcome.initial_de,
        )
        self.on_event({"event": "patch_done", "patch": patch_name,
                       "delta_e": _de_or_none(outcome.final_de),
                       "iterations": outcome.iterations, "converged": outcome.converged})
        return result

    # ------------------------------------------------------------------
    # Phase 1 — White Balance
    # ------------------------------------------------------------------

    def _phase1_white_balance(self) -> PatchResult:
        """Adjust R/G/B gains to match D65 white point on the white patch."""
        cfg = self.config
        gain_lo, gain_hi = cfg.wb_gain_range
        gain_range = gain_hi - gain_lo
        target_lab = get_target_lab("white", self.mode)
        target_x, target_y, _ = get_target_xyY("white", self.mode)
        # D65 target at the normalized luminance — loop-invariant
        target_X, target_Y, target_Z = xyY_to_XYZ(target_x, target_y, 100.0)

        self.on_event({"event": "phase_start", "phase": "wb"})
        logger.info("Phase 1: White Balance [mode=%s]", self.mode)

        gains = {
            "R": self.projector.get_wb_gain("R"),
            "G": self.projector.get_wb_gain("G"),
            "B": self.projector.get_wb_gain("B"),
        }
        logger.debug("Initial gains: %s", gains)

        def normalize(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
            # WB corrects chromaticity only: normalize each reading to Y=100 so
            # the absolute light level (cd/m²) doesn't dominate the ΔE.
            X, Y, Z = xyz
            s = 100.0 / Y if Y > 1e-6 else 1.0
            return (X * s, Y * s, Z * s)

        def corrections(norm_xyz: tuple, meas_lab: tuple, current: dict, step_scale: float) -> dict:
            # Chromaticity correction — compare against the D65 target at the
            # same (normalized) luminance; G acts as the fixed reference channel.
            Xn, Yn, Zn = norm_xyz
            r_err = (Xn - target_X) / max(target_X, 1e-6)
            g_err = (Yn - target_Y) / max(target_Y, 1e-6)
            b_err = (Zn - target_Z) / max(target_Z, 1e-6)
            return {
                "R": _clamp(current["R"] - round(r_err * step_scale * gain_range), gain_lo, gain_hi),
                "G": _clamp(current["G"] - round(g_err * step_scale * gain_range), gain_lo, gain_hi),
                "B": _clamp(current["B"] - round(b_err * step_scale * gain_range), gain_lo, gain_hi),
            }

        def apply(current: dict, new: dict) -> None:
            self._apply_settings("white", current, new,
                                 {"R": "WB_R", "G": "WB_G", "B": "WB_B"},
                                 self.projector.set_wb_gain)

        outcome = self._correction_loop("white", "wb", gains, normalize, target_lab, corrections, apply)

        if outcome.xyz[1] > 1e-6:
            # White luminance anchors relative colorimetry for phases 2 and 3
            self._ref_white_Y = outcome.xyz[1]

        result = self._emit_patch_result(outcome, "white")
        self.on_event({"event": "phase_done", "phase": "wb"})
        return result

    # ------------------------------------------------------------------
    # Phase 2 — CMS
    # ------------------------------------------------------------------

    def _phase2_cms(self) -> list[PatchResult]:
        """Adjust CMS Hue/Sat/Lum for each color axis."""
        self.on_event({"event": "phase_start", "phase": "cms"})
        logger.info("Phase 2: CMS [mode=%s]", self.mode)
        if self._ref_white_Y is None:
            self._measure_white_reference()
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
        target_lab = get_target_lab(axis, self.mode)

        cms = {
            "HUE": self.projector.get_cms(axis, "HUE"),
            "SAT": self.projector.get_cms(axis, "SAT"),
            "LUM": self.projector.get_cms(axis, "LUM"),
        }

        def normalize(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
            return self._to_relative(*xyz)

        def corrections(norm_xyz: tuple, meas_lab: tuple, current: dict, step_scale: float) -> dict:
            # Decompose Lab error into Hue/Sat/Lum corrections
            L_meas, a_meas, b_meas = meas_lab
            L_tgt,  a_tgt,  b_tgt  = target_lab

            # Hue error: angular difference in the ab plane, normalized to (-180, 180)
            hue_err_deg = math.degrees(math.atan2(b_tgt, a_tgt) - math.atan2(b_meas, a_meas))
            hue_err_deg = (hue_err_deg + 180) % 360 - 180

            # Saturation error: chroma ratio
            chroma_meas = math.hypot(a_meas, b_meas)
            chroma_tgt  = math.hypot(a_tgt,  b_tgt)
            sat_ratio_err = (chroma_tgt - chroma_meas) / max(chroma_tgt, 1e-6)

            # Luminance error
            lum_err = (L_tgt - L_meas) / 100.0

            # Errors are (target - measured); a positive control value is assumed
            # to increase hue angle / saturation / luminance, so the correction
            # moves each property toward its target. If the projector's control
            # direction is inverted on some axis, the loop's divergence guard
            # reverts to the best-known values instead of walking away.
            return {
                "HUE": current["HUE"] + round(hue_err_deg * step_scale),
                "SAT": current["SAT"] + round(sat_ratio_err * step_scale * 10),
                "LUM": current["LUM"] + round(lum_err * step_scale * 10),
            }

        def apply(current: dict, new: dict) -> None:
            self._apply_settings(axis, current, new,
                                 {"HUE": "HUE", "SAT": "SAT", "LUM": "LUM"},
                                 lambda prop, value: self.projector.set_cms(axis, prop, value))

        outcome = self._correction_loop(axis, "cms", cms, normalize, target_lab, corrections, apply)
        return self._emit_patch_result(outcome, axis)

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
            self.on_event({"event": "patch_start", "patch": patch_name, "iteration": 1, "mode": "verify"})
            self.display.display_patch(RGBPatch(*patch_rgb(patch_name)))
            X, Y, Z = self.measure()
            if patch_name == "white" and Y > 1e-6:
                # White is measured first; it re-anchors relative colorimetry
                self._ref_white_Y = Y
            meas_lab = xyz_to_lab(*self._to_relative(X, Y, Z))
            target_lab = get_target_lab(patch_name, self.mode)
            de = delta_e_2000(meas_lab, target_lab)

            converged = de < self.config.delta_e_threshold
            self.on_event({"event": "measurement", "patch": patch_name, "xyz": [X, Y, Z], "delta_e": de})
            # patch_done drives the UI progress counter, which accounts for all
            # phases (CLAUDE.md pitfall #9) — verify must emit it too
            self.on_event({"event": "patch_done", "patch": patch_name, "delta_e": de,
                           "iterations": 1, "converged": converged})
            logger.info("Verify %s: ΔE=%.4f %s", patch_name, de, "✓" if converged else "✗")

            results.append(PatchResult(
                patch_name=patch_name,
                final_delta_e=de,
                iterations=1,
                converged=converged,
                final_xyz=(X, Y, Z),
            ))

        self.display.display_black()
        self.on_event({"event": "phase_done", "phase": "verify"})
        return results

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _measure_white_reference(self) -> None:
        """Measure the white patch to anchor relative colorimetry (white = 100)."""
        self.display.display_patch(RGBPatch(*patch_rgb("white")))
        _, Y, _ = self.measure()
        if Y > 1e-6:
            self._ref_white_Y = Y
            logger.info("Reference white measured: Y=%.3f cd/m² (normalized to 100)", Y)
        else:
            logger.warning("Reference white measurement returned Y≈0 — measurements will not be normalized")

    def _to_relative(self, X: float, Y: float, Z: float) -> tuple[float, float, float]:
        """Rescale an absolute XYZ reading so reference white lands at Y=100."""
        ref = self._ref_white_Y
        if not ref or ref <= 1e-6:
            return (X, Y, Z)
        s = 100.0 / ref
        return (X * s, Y * s, Z * s)

    def _decay_step(self, step_scale: float, de: float) -> float:
        """Decay the proportional step as ΔE approaches the threshold.

        Clamped to the initial value: the raw formula grows without bound when
        ΔE >> threshold, which destabilizes the loop.
        """
        cfg = self.config
        return min(
            cfg.proportional_gain_initial,
            max(
                cfg.proportional_gain_minimum,
                step_scale * (de / cfg.delta_e_threshold) * 0.5,
            ),
        )

    def _run_anomaly_check(
        self,
        patch: str,
        iteration: int,
        recent_xyz: list[tuple[float, float, float]],
        recent_de: list[float],
    ) -> str:
        """Consult the anomaly detector every 3rd iteration; return its action."""
        if self.anomaly_check is None or iteration % 3 != 0 or len(recent_de) < 3:
            return "continue"
        try:
            result = self.anomaly_check(patch, list(recent_xyz), list(recent_de))
        except Exception:
            logger.exception("Anomaly check failed — continuing calibration")
            return "continue"
        if result.get("anomaly"):
            self.on_event({"event": "agent_result", "agent": "anomaly_detector", "result": result})
            logger.warning("Anomaly on %s: %s → %s", patch, result.get("type"), result.get("action"))
        return result.get("action", "continue")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _de_or_none(de: float) -> float | None:
    """inf (patch never measured, e.g. aborted) is not valid JSON — use null."""
    return None if math.isinf(de) else de


def _report_to_dict(report: CalibrationReport) -> dict:
    """Serialize CalibrationReport to a JSON-safe dict for WebSocket events."""
    def _num(v: float | None) -> float | None:
        # inf (patch never measured, e.g. aborted) is not valid JSON — use null
        if v is None or math.isinf(v):
            return None
        return round(v, 4)

    def _patch_dict(p: PatchResult) -> dict:
        return {
            "patch": p.patch_name,
            "delta_e": _num(p.final_delta_e),
            "iterations": p.iterations,
            "converged": p.converged,
            "xyz": [round(v, 4) for v in p.final_xyz],
            "initial_delta_e": _num(p.initial_delta_e),
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
