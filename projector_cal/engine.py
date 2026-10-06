"""Calibration engine: the closed-loop correction algorithm.

Phases, in run order:
  Baseline      — snapshot every picture setting, then neutralize the ones that
                  would bias a calibration (color/tint/skin tone, processing).
  Levels        — guided: brightness and contrast set by eye on test patterns,
                  with the user stepping the projector from the UI.
  White Balance — two-point: R/B gains on white and R/B offsets on a dark grey,
                  alternated until both points hit D65.
  Gamma         — the 9 custom-gamma points (GAMMALV) tuned against a gamma-2.2
                  grey ramp (SDR only).
  CMS           — the 5040UB exposes no RGBCMY commands over ESC/VP21. In
                  "auto" mode the engine drives the RGBCMY menu with remote-key
                  presses (osd.RgbcmyMenu) in a closed loop; in "guided" mode
                  (or dry runs) it tells the user which control to move.
  Verification  — final measurement pass, no corrections, produces the report.

The engine is hardware-agnostic: it depends on ProjectorClient, a PatchDisplay adapter,
and a measure() callable — all injected at construction time.

Event callbacks (optional) allow the web server to broadcast progress via WebSocket.
"""

from __future__ import annotations

import logging
import math
import queue
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

from . import picture
from .color_math import (
    GREY_RAMP,
    delta_e_2000,
    hdr_patch_code10,
    pq_code10,
    get_target_lab,
    get_target_xyY,
    patch_rgb,
    xyY_to_XYZ,
    xyz_to_lab,
)
from .config import CalibrationConfig
from .osd import CONTROLS as _CMS_CONTROLS, RgbcmyMenu
from .pgen import PatchDisplay, RGBPatch
from .projector import ProjectorClient, ProjectorError

logger = logging.getLogger(__name__)

# Correction phases in run order (baseline always runs first, verify last)
PHASES = ("levels", "wb", "gamma", "cms")

_CMS_PATCHES = ["red", "green", "blue", "cyan", "magenta", "yellow"]
_VERIFY_PATCHES = ["white", *_CMS_PATCHES, *(f"grey{pct}" for pct in reversed(GREY_RAMP))]

# Consecutive non-improving iterations before a patch loop reverts to its
# best-known settings and stops (protects against a wrong control-direction
# assumption or an unstable loop walking the projector away from target).
_MAX_WORSENING_ITERATIONS = 3

# Lab hue angle increases red → yellow → green → cyan → blue → magenta → red,
# so "toward the next color" is a convention-free way to describe a hue
# correction — the projector's own Hue +/- direction per axis is unknown.
_HUE_WHEEL = ["red", "yellow", "green", "cyan", "blue", "magenta"]

# Below these, a CMS component is not worth telling the user to touch
_GUIDE_MIN_HUE_DEG = 1.0
_GUIDE_MIN_SAT_PCT = 2.0
_GUIDE_MIN_LUM = 1.0

# Automatic CMS (menu steps). Initial guess of how much one Right press moves
# each error (hue °, chroma %, L*); refined from every move's measured effect,
# including the sign, so an inverted control is learned rather than assumed.
_CMS_INITIAL_GAIN = {"HUE": 1.0, "SAT": 1.0, "LUM": 0.5}
_CMS_NOISE = {"HUE": 0.3, "SAT": 0.5, "LUM": 0.2}     # changes smaller than this are noise
_CMS_GAIN_BOUNDS = (0.05, 10.0)                       # |error units per step|
_CMS_MAX_STEP = 8                                     # presses per move
_CMS_MAX_OFFSET = 30                                  # total travel from the starting value

# Guided levels patterns: (window, background) as SDR 8-bit grey codes;
# HDR10 sends the same relative luminance PQ-encoded (see _grey_patch)
_BRIGHTNESS_PATTERN = (5, 0)      # 2% box on black
_CONTRAST_PATTERN = (250, 255)    # 98% box on white

_AUTO_IRIS_NOTE = ("Turn Auto Iris off in the projector's Image menu first — it changes "
                   "light output between patches and can't be set over the network.")


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
    phase: str                     # "all" or the comma-joined phases that ran
    delta_e_threshold: float
    baseline: dict[str, str] = field(default_factory=dict)   # settings before the run
    wb_result: PatchResult | None = None                      # gains, on white
    wb_offset_result: PatchResult | None = None               # offsets, on a dark grey
    gamma_results: list[PatchResult] = field(default_factory=list)
    cms_results: list[PatchResult] = field(default_factory=list)
    # Automatic CMS: net menu steps moved per axis/control (start values unknown)
    cms_adjustments: dict[str, dict[str, int]] = field(default_factory=dict)
    # Controls that hit a menu end stop (0/100) — their step counts overstate the travel
    cms_at_limit: dict[str, list[str]] = field(default_factory=dict)
    verify_results: list[PatchResult] = field(default_factory=list)
    total_iterations: int = 0
    aborted: bool = False

    @property
    def correction_results(self) -> list[PatchResult]:
        results = [p for p in (self.wb_result, self.wb_offset_result) if p]
        return results + self.gamma_results + self.cms_results

    @property
    def patches(self) -> list[PatchResult]:
        return self.correction_results + self.verify_results

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
        dry_run: If True, never write to the projector (measure and report only).
        on_event: Optional callback for WebSocket events.
        anomaly_check: Optional callable(patch, recent_xyz, recent_de) -> dict
            (see agents.anomaly_detector.detect_anomaly). Called every 3rd
            iteration; its "action" may retry a measurement or abort the patch.
        interactive: If True, guided steps (levels, CMS) wait for the user via
            submit_action(). If False, each guided step is shown/measured once.
        cms_menu: RGBCMY menu driver. With config.cms_mode == "auto" (and not a
            dry run) the CMS phase adjusts the projector through it; without
            one, CMS is guided.
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
        interactive: bool = False,
        cms_menu: RgbcmyMenu | None = None,
    ) -> None:
        self.projector = projector
        self.display = display
        self.measure = measure
        self.config = config
        self.mode = mode
        self.dry_run = dry_run
        self.on_event = on_event
        self.anomaly_check = anomaly_check
        self.interactive = interactive
        self.cms_menu = cms_menu
        self._actions: queue.Queue[tuple[str, str | None, int]] = queue.Queue()
        self._abort = False
        self._ref_white_Y: float | None = None
        self._cms_at_limit: dict[str, set[str]] = {}

    def abort(self) -> None:
        """Signal the engine to stop after the current iteration."""
        self._abort = True
        self._actions.put(("abort", None, 0))  # release a pending guided-step wait

    def submit_action(self, action: str, token: str | None = None, direction: int = 0) -> None:
        """User response to a guided step.

        "remeasure" / "next" answer a cms_guidance prompt; "adjust" (with a
        setting token and ±1 direction) steps a control in a manual_step;
        "next" also finishes a manual_step.
        """
        self._actions.put((action, token, direction))

    # ------------------------------------------------------------------
    # Top-level entry point
    # ------------------------------------------------------------------

    def run(self, phases: Sequence[str] = PHASES) -> CalibrationReport:
        """Baseline, then the requested phases in canonical order, then verify."""
        unknown = set(phases) - set(PHASES)
        if unknown:
            raise ValueError(f"Unknown phases: {sorted(unknown)}")
        report = CalibrationReport(
            mode=self.mode,
            phase="all" if set(phases) == set(PHASES) else ",".join(p for p in PHASES if p in phases),
            delta_e_threshold=self.config.delta_e_threshold,
        )
        report.baseline = self._phase0_baseline()

        for phase in PHASES:
            if self._abort:
                break
            if phase not in phases:
                continue
            if phase == "levels":
                self._phase_levels()
            elif phase == "wb":
                report.wb_result, report.wb_offset_result = self._phase1_white_balance()
            elif phase == "gamma":
                report.gamma_results = self._phase_gamma()
            elif phase == "cms":
                report.cms_results = self._phase2_cms()
                report.cms_at_limit = {a: sorted(c) for a, c in self._cms_at_limit.items() if c}
                if self.cms_menu is not None:
                    report.cms_adjustments = {
                        axis: {c: n for c, n in moved.items() if n}
                        for axis, moved in self.cms_menu.offsets.items() if any(moved.values())
                    }

        report.total_iterations = sum(p.iterations for p in report.correction_results)
        if not self._abort:
            report.verify_results = self._phase3_verify()

        report.aborted = self._abort
        self.on_event({"event": "run_complete", "report": _report_to_dict(report)})
        return report

    # ------------------------------------------------------------------
    # Shared correction loop
    # ------------------------------------------------------------------
    #
    # The automatic phases (WB, gamma) run a closed loop: measure → normalize →
    # ΔE → converge/diverge checks → anomaly check → compute and apply a
    # proportional correction → decay the step. The normalization and the
    # correction physics are injected as callables.

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
            mode: "wb" or "gamma" — for patch_start events and log lines.
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

        self.display.display_patch(self._patch(patch_name))

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
        read_back: Callable[[str], int] | None = None,
    ) -> None:
        """Diff `new` against `current`, write changed values to the projector
        (unless dry-run), and emit correction events; mutates `current` in place.

        With `read_back`, `current` records the value the projector actually
        stored — the 5040UB quantizes values, so the requested value can differ.
        """
        for key in current:
            if new[key] != current[key]:
                actual = new[key]
                if not self.dry_run:
                    setter(key, new[key])
                    if read_back is not None:
                        actual = read_back(key)
                self.on_event({
                    "event": "correction", "patch": patch, "axis": axis_names[key],
                    "before": current[key], "after": actual,
                })
                current[key] = actual

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
    # Phase 0 — Baseline
    # ------------------------------------------------------------------

    def _phase0_baseline(self) -> dict[str, str]:
        """Record every picture setting, then neutralize the biasing ones."""
        snapshot = picture.read_settings(self.projector)
        applied: dict[str, str] = {}
        if self.config.apply_baseline_neutral and not self.dry_run:
            applied = picture.neutralize(self.projector)
        logger.info("Baseline: %d settings recorded, neutralized %s", len(snapshot), applied)
        self.on_event({"event": "baseline", "settings": snapshot, "neutralized": applied})
        return snapshot

    # ------------------------------------------------------------------
    # Levels — guided brightness / contrast
    # ------------------------------------------------------------------

    def _phase_levels(self) -> None:
        self.on_event({"event": "phase_start", "phase": "levels", "patch_count": 0})
        logger.info("Levels (guided) [mode=%s]", self.mode)
        self._manual_step(
            "brightness", "Set Brightness (black level)",
            [_AUTO_IRIS_NOTE,
             "The screen shows a near-black square (2% grey) on black.",
             "Lower Brightness until the square disappears, then raise it until it is just visible again."],
            [("BRIGHT", "Brightness")],
            _BRIGHTNESS_PATTERN,
        )
        if not self._abort:
            self._manual_step(
                "contrast", "Set Contrast (white level)",
                ["The screen shows a slightly darker square (98% white) on full white.",
                 "Raise Contrast until the square blends into the background, then lower it until "
                 "the square is just visible again.",
                 "If the white turns tinted before the square disappears, stop there."],
                [("CONTRAST", "Contrast")],
                _CONTRAST_PATTERN,
            )
        self.on_event({"event": "phase_done", "phase": "levels"})

    def _manual_step(
        self,
        step: str,
        title: str,
        instructions: list[str],
        controls: list[tuple[str, str]],
        pattern: tuple[int, int],
    ) -> None:
        """Show a pattern and let the user step projector controls until done."""
        window, background = pattern
        self.display.display_window(self._grey_patch(window), self._grey_patch(background))
        tokens = dict(controls)

        while not self._abort:
            values: dict[str, str | None] = {}
            for token in tokens:
                try:
                    values[token] = self.projector.get_setting(token)
                except ProjectorError:
                    values[token] = None
            self._drain_actions()
            self.on_event({
                "event": "manual_step", "step": step, "title": title, "instructions": instructions,
                "controls": [{"token": t, "label": label, "value": values[t]} for t, label in controls],
            })
            if not self.interactive:
                break
            action, token, direction = self._wait_for_action()
            if action == "adjust" and token in tokens and direction in (-1, 1):
                if self.dry_run:
                    logger.info("Dry run — not adjusting %s", token)
                else:
                    try:
                        picture.nudge(self.projector, token, direction)
                    except (ProjectorError, ValueError) as e:
                        logger.warning("Adjusting %s failed: %s", token, e)
                continue
            if action == "remeasure":
                continue
            break

        self.on_event({"event": "manual_step_done", "step": step})

    # ------------------------------------------------------------------
    # Phase 1 — White Balance (two-point)
    # ------------------------------------------------------------------

    def _phase1_white_balance(self) -> tuple[PatchResult, PatchResult | None]:
        """Gains on white and offsets on a dark grey, alternated until both settle.

        Gains move the top of the grey scale and offsets the bottom, but each
        also shifts the other point, so the pair is repeated (up to
        wb_two_point_rounds) until a round needs no correction at either point.
        """
        cfg = self.config
        try:
            self.projector.get_setting("OFFSETR")
            has_offsets = True
        except ProjectorError:
            has_offsets = False
            logger.warning("Projector rejects OFFSETR — single-point (gain-only) white balance")

        low_patch = cfg.wb_offset_patch
        self.on_event({"event": "phase_start", "phase": "wb", "patch_count": 2 if has_offsets else 1})
        logger.info("Phase 1: White Balance [mode=%s, %s]", self.mode,
                    f"two-point, offsets on {low_patch}" if has_offsets else "gain only")

        hi = lo = None
        hi_first_de = lo_first_de = None
        hi_iters = lo_iters = 0
        for round_no in range(1, (cfg.wb_two_point_rounds if has_offsets else 1) + 1):
            hi = self._wb_point("white", "gain")
            hi_iters += hi.iterations
            hi_first_de = hi.initial_de if hi_first_de is None else hi_first_de
            if self._abort or not has_offsets:
                break
            lo = self._wb_point(low_patch, "offset")
            lo_iters += lo.iterations
            lo_first_de = lo.initial_de if lo_first_de is None else lo_first_de
            settled = all(o.converged and o.iterations == 1 for o in (hi, lo))
            logger.info("WB round %d: white ΔE=%.3f, %s ΔE=%.3f", round_no, hi.final_de, low_patch, lo.final_de)
            if settled or self._abort:
                break

        if hi.xyz[1] > 1e-6:
            # White luminance anchors relative colorimetry for later phases
            self._ref_white_Y = hi.xyz[1]

        hi.initial_de, hi.iterations = hi_first_de, hi_iters
        white_result = self._emit_patch_result(hi, "white")
        low_result = None
        if lo is not None:
            lo.initial_de, lo.iterations = lo_first_de, lo_iters
            low_result = self._emit_patch_result(lo, low_patch)
        self.on_event({"event": "phase_done", "phase": "wb"})
        return white_result, low_result

    def _wb_point(self, patch: str, kind: Literal["gain", "offset"]) -> _LoopOutcome:
        """Correct one grey-scale point's chromaticity with gains or offsets.

        G is the fixed reference channel (its error is 0 by construction after
        normalizing to Y=100); R and B move.
        """
        cfg = self.config
        gain_lo, gain_hi = cfg.wb_gain_range
        gain_range = gain_hi - gain_lo
        min_step = cfg.wb_gain_min_step
        target_lab = get_target_lab("white", self.mode)
        target_x, target_y, _ = get_target_xyY("white", self.mode)
        # D65 target at the normalized luminance — loop-invariant
        target_X, target_Y, target_Z = xyY_to_XYZ(target_x, target_y, 100.0)

        if kind == "gain":
            get, put = self.projector.get_wb_gain, self.projector.set_wb_gain
            names = {"R": "WB_R", "G": "WB_G", "B": "WB_B"}
        else:
            def get(ch: str) -> int:
                return int(self.projector.get_setting(f"OFFSET{ch}"))

            def put(ch: str, value: int) -> None:
                self.projector.set_setting(f"OFFSET{ch}", value)
            names = {"R": "WB_OFFSET_R", "G": "WB_OFFSET_G", "B": "WB_OFFSET_B"}

        values = {ch: get(ch) for ch in ("R", "G", "B")}
        logger.debug("Initial %s values: %s", kind, values)

        def normalize(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
            # WB corrects chromaticity only: normalize each reading to Y=100 so
            # the light level (absolute, or the grey's own level) doesn't
            # dominate the ΔE.
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
                ch: _clamp(current[ch] - _gain_delta(err, step_scale, gain_range, min_step), gain_lo, gain_hi)
                for ch, err in (("R", r_err), ("G", g_err), ("B", b_err))
            }

        def apply(current: dict, new: dict) -> None:
            self._apply_settings(patch, current, new, names, put, read_back=get)

        return self._correction_loop(patch, "wb", values, normalize, target_lab, corrections, apply)

    # ------------------------------------------------------------------
    # Gamma — custom gamma points
    # ------------------------------------------------------------------

    def _phase_gamma(self) -> list[PatchResult]:
        """Tune the custom-gamma points so the grey ramp tracks gamma 2.2.

        Each tuned grey is paired with the GAMMALV tone that moves it most
        cleanly (cfg.gamma_points, measured on a 5040UB). Neighbouring tones
        interact, so the set is repeated (cfg.gamma_passes) until a pass needs
        no correction. Readings are reduced to lightness only — chromaticity
        is the WB phase's job — so ΔE here is the L* error against the target.
        """
        cfg = self.config
        points = cfg.gamma_points
        self.on_event({"event": "phase_start", "phase": "gamma", "patch_count": len(points)})
        results: list[PatchResult] = []

        if self.mode != "sdr":
            # HDR10 is PQ tone-mapped by the projector; a power-law target is wrong
            logger.warning("Gamma phase skipped in %s mode — targets are SDR gamma 2.2", self.mode)
            self.on_event({"event": "warning", "message": "Gamma calibration is SDR-only; skipped for HDR10."})
            self.on_event({"event": "phase_done", "phase": "gamma"})
            return results
        try:
            current_gamma = self.projector.get_setting("GAMMA")
        except ProjectorError as e:
            logger.warning("Gamma phase skipped — projector rejects GAMMA?: %s", e)
            self.on_event({"event": "phase_done", "phase": "gamma"})
            return results

        logger.info("Gamma: tuning %d custom points [gamma preset was %s]", len(points), current_gamma)
        if current_gamma != picture.GAMMA_CUSTOM:
            self.on_event({"event": "correction", "patch": "gamma", "axis": "GAMMA",
                           "before": current_gamma, "after": picture.GAMMA_CUSTOM})
            if not self.dry_run:
                self.projector.set_setting("GAMMA", picture.GAMMA_CUSTOM)

        self._measure_white_reference()  # WB moved white since phase 1's reading
        Xw, Yw, Zw = xyY_to_XYZ(*get_target_xyY("white", self.mode))

        outcomes: dict[str, _LoopOutcome] = {}
        first_de: dict[str, float | None] = {}
        iterations: dict[str, int] = {}
        for pass_no in range(1, cfg.gamma_passes + 1):
            for pct, tone in points:
                if self._abort:
                    break
                patch = f"grey{pct}"
                o = self._gamma_point(tone, patch, (Xw / Yw, Zw / Yw))
                outcomes[patch] = o
                first_de.setdefault(patch, o.initial_de)
                iterations[patch] = iterations.get(patch, 0) + o.iterations
            settled = all(o.converged and o.iterations == 1 for o in outcomes.values())
            logger.info("Gamma pass %d: %s", pass_no,
                        {p: round(o.final_de, 2) for p, o in outcomes.items()})
            if settled or self._abort:
                break

        for patch, o in outcomes.items():
            o.initial_de, o.iterations = first_de[patch], iterations[patch]
            results.append(self._emit_patch_result(o, patch))
        self.on_event({"event": "phase_done", "phase": "gamma"})
        return results

    def _gamma_point(self, tone: int, patch: str, white_ratios: tuple[float, float]) -> _LoopOutcome:
        cfg = self.config
        param = f"{tone:02d}"
        target_lab = get_target_lab(patch, self.mode)
        xw, zw = white_ratios

        def get(_key: str) -> int:
            return int(self.projector.get_setting("GAMMALV", param))

        def put(_key: str, value: int) -> None:
            self.projector.set_setting("GAMMALV", value, param)

        values = {"V": get("V")}

        def normalize(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
            # Relative luminance on the white=100 scale, placed on the neutral
            # axis so the Lab comparison sees lightness error only
            Y = self._to_relative(*xyz)[1]
            return (xw * Y, Y, zw * Y)

        def corrections(norm_xyz: tuple, meas_lab: tuple, current: dict, step_scale: float) -> dict:
            # Assumes a higher GAMMALV value brightens its tone; if inverted,
            # the divergence guard reverts instead of walking away
            err = (target_lab[0] - meas_lab[0]) / 100.0
            return {"V": _clamp(current["V"] + _gain_delta(err, step_scale, 255, cfg.wb_gain_min_step), 0, 255)}

        def apply(current: dict, new: dict) -> None:
            self._apply_settings(patch, current, new, {"V": f"GAMMA_{tone + 1}"}, put, read_back=get)

        return self._correction_loop(patch, "gamma", values, normalize, target_lab, corrections, apply)

    # ------------------------------------------------------------------
    # Phase 2 — CMS
    # ------------------------------------------------------------------

    def _phase2_cms(self) -> list[PatchResult]:
        """CMS for each color axis: automatic through the RGBCMY menu, or guided."""
        auto = self.config.cms_mode == "auto" and self.cms_menu is not None and not self.dry_run
        self.on_event({"event": "phase_start", "phase": "cms", "patch_count": len(self.config.cms_axis_order)})
        logger.info("Phase 2: CMS (%s) [mode=%s]", "automatic" if auto else "guided", self.mode)
        if self._ref_white_Y is None:
            self._measure_white_reference()
        results: list[PatchResult] = []

        try:
            for axis in self.config.cms_axis_order:
                if self._abort:
                    break
                results.append(self._cms_axis_auto(axis) if auto else self._cms_axis(axis))
        finally:
            if auto:
                self.cms_menu.close()   # never leave the OSD over the picture

        self.on_event({"event": "phase_done", "phase": "cms"})
        return results

    def _cms_axis_auto(self, axis: str) -> PatchResult:
        """Closed-loop Hue/Saturation/Brightness correction for one axis via menu keys.

        Each iteration moves the control with the largest (scaled) error by an
        estimated number of steps. The per-step effect is re-estimated from
        every move (secant), so unknown gains and inverted controls are learned.
        The divergence guard reverts to the best-measured offsets.
        """
        cfg = self.config
        menu = self.cms_menu
        target_lab = get_target_lab(axis, self.mode)
        gains = dict(_CMS_INITIAL_GAIN)
        offsets = {c: 0 for c in _CMS_CONTROLS}
        best_de, best, worse_streak = float("inf"), dict(offsets), 0
        initial_de: float | None = None
        de = float("inf")
        converged = False
        iteration = 0
        X = Y = Z = 0.0
        last_move: tuple[str, int, float] | None = None
        # A control whose moves stop having any effect has hit a menu end stop
        # (0/100); two such moves in one direction block that direction.
        stalls = {c: 0 for c in _CMS_CONTROLS}
        blocked: dict[str, int] = {}
        at_limit = self._cms_at_limit.setdefault(axis, set())

        self.display.display_patch(self._patch(axis))
        menu.open_axis(axis)

        def move(control: str, steps: int) -> None:
            before = offsets[control]
            menu.step(control, steps)
            offsets[control] += steps
            self.on_event({"event": "correction", "patch": axis, "axis": f"CMS_{axis}_{control}",
                           "before": f"{before:+d}", "after": f"{offsets[control]:+d}"})

        for iteration in range(1, cfg.max_iterations_per_patch + 1):
            if self._abort:
                break
            self.on_event({"event": "patch_start", "patch": axis, "iteration": iteration, "mode": "cms"})
            X, Y, Z = self.measure()
            meas_lab = xyz_to_lab(*self._to_relative(X, Y, Z))
            de = delta_e_2000(meas_lab, target_lab)
            self.on_event({"event": "measurement", "patch": axis, "xyz": [X, Y, Z], "delta_e": de})
            errs = cms_errors(meas_lab, target_lab)
            logger.info("CMS %s iter %d: ΔE=%.3f hue=%+.1f° sat=%+.1f%% lum=%+.1f",
                        axis, iteration, de, errs["HUE"], errs["SAT"], errs["LUM"])
            if initial_de is None:
                initial_de = de

            if last_move is not None:
                control, steps, err_before = last_move
                observed = err_before - errs[control]
                if abs(observed) >= _CMS_NOISE[control]:
                    lo, hi = _CMS_GAIN_BOUNDS
                    g = observed / steps
                    gains[control] = math.copysign(min(hi, max(lo, abs(g))), g)
                    stalls[control] = 0
                else:
                    stalls[control] += 1
                    if stalls[control] >= 2:
                        blocked[control] = 1 if steps > 0 else -1
                        at_limit.add(control)
                        logger.warning("CMS %s.%s stopped responding — menu limit reached", axis, control)
                        self.on_event({"event": "warning", "message":
                            f"CMS {axis} {control.lower()} reached its menu limit; leaving it there."})

            if de < cfg.delta_e_threshold:
                converged = True
                break

            if de < best_de:
                best_de, best, worse_streak = de, dict(offsets), 0
            else:
                worse_streak += 1
                if worse_streak >= _MAX_WORSENING_ITERATIONS:
                    logger.warning("CMS %s diverging — reverting to best offsets %s (ΔE=%.3f)",
                                   axis, best, best_de)
                    for c in _CMS_CONTROLS:
                        if best[c] != offsets[c]:
                            move(c, best[c] - offsets[c])
                    break

            def wanted(c: str) -> int:
                return 1 if errs[c] * gains[c] > 0 else -1

            usable = [c for c in _CMS_CONTROLS if blocked.get(c) != wanted(c)]
            if not usable:
                logger.warning("CMS %s: every useful control is at a menu limit", axis)
                break
            control = max(usable, key=lambda c: abs(errs[c]) / _CMS_NOISE[c])
            steps = round(errs[control] / gains[control])
            if steps == 0:
                steps = wanted(control)
            steps = max(-_CMS_MAX_STEP, min(_CMS_MAX_STEP, steps))
            # Keep each control within its travel limit from the starting value
            steps = max(-_CMS_MAX_OFFSET - offsets[control], min(_CMS_MAX_OFFSET - offsets[control], steps))
            if steps == 0:
                logger.warning("CMS %s.%s at its travel limit", axis, control)
                break
            move(control, steps)
            last_move = (control, steps, errs[control])

        if not converged and not self._abort:
            X, Y, Z = self.measure()   # report the projector's actual final state
            de = delta_e_2000(xyz_to_lab(*self._to_relative(X, Y, Z)), target_lab)

        outcome = _LoopOutcome(final_de=de, initial_de=initial_de, iterations=iteration,
                               converged=converged, xyz=(X, Y, Z))
        return self._emit_patch_result(outcome, axis)

    def _cms_axis(self, axis: str) -> PatchResult:
        """Measure one axis; until it converges, emit guidance and wait for the user.

        The user adjusts the projector's RGBCMY menu by hand between readings,
        so there is no correction step, step decay, or divergence guard here.
        """
        cfg = self.config
        target_lab = get_target_lab(axis, self.mode)
        initial_de: float | None = None
        de = float("inf")
        converged = False
        iteration = 0
        X = Y = Z = 0.0

        self.display.display_patch(self._patch(axis))

        for iteration in range(1, cfg.max_iterations_per_patch + 1):
            if self._abort:
                break

            self.on_event({"event": "patch_start", "patch": axis, "iteration": iteration, "mode": "cms"})
            X, Y, Z = self.measure()
            meas_lab = xyz_to_lab(*self._to_relative(X, Y, Z))
            de = delta_e_2000(meas_lab, target_lab)
            self.on_event({"event": "measurement", "patch": axis, "xyz": [X, Y, Z], "delta_e": de})
            if initial_de is None:
                initial_de = de
            logger.info("CMS %s reading %d: ΔE=%.4f (target<%.2f)",
                        axis, iteration, de, cfg.delta_e_threshold)

            if de < cfg.delta_e_threshold:
                converged = True
                break

            self._drain_actions()
            self.on_event({
                "event": "cms_guidance", "patch": axis, "iteration": iteration,
                "delta_e": de, "guidance": cms_guidance(axis, meas_lab, target_lab),
            })
            if not self.interactive or iteration == cfg.max_iterations_per_patch:
                break
            if self._wait_for_action()[0] != "remeasure":
                break

        outcome = _LoopOutcome(final_de=de, initial_de=initial_de, iterations=iteration,
                               converged=converged, xyz=(X, Y, Z))
        return self._emit_patch_result(outcome, axis)

    def _drain_actions(self) -> None:
        """Discard clicks left over from an earlier prompt."""
        while True:
            try:
                self._actions.get_nowait()
            except queue.Empty:
                return

    def _wait_for_action(self) -> tuple[str, str | None, int]:
        """Block until the user answers the current prompt (or the run aborts)."""
        if self._abort:
            return ("abort", None, 0)
        action = self._actions.get()
        return ("abort", None, 0) if self._abort else action

    # ------------------------------------------------------------------
    # Phase 3 — Verification
    # ------------------------------------------------------------------

    def _phase3_verify(self) -> list[PatchResult]:
        """Measure white, the colors and the grey ramp without applying corrections."""
        self.on_event({"event": "phase_start", "phase": "verify", "patch_count": len(_VERIFY_PATCHES)})
        logger.info("Phase 3: Verification")
        results: list[PatchResult] = []

        for patch_name in _VERIFY_PATCHES:
            if self._abort:
                break
            self.on_event({"event": "patch_start", "patch": patch_name, "iteration": 1, "mode": "verify"})
            self.display.display_patch(self._patch(patch_name))
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

    def _patch(self, patch_name: str) -> RGBPatch:
        """The signal for a named patch: 8-bit video codes for SDR, 10-bit
        BT.2020 PQ for HDR10 (white at hdr_white_nits)."""
        if self.mode == "hdr10":
            return RGBPatch(*hdr_patch_code10(patch_name, self.config.hdr_white_nits), bits=10)
        return RGBPatch(*patch_rgb(patch_name))

    def _grey_patch(self, code8: int) -> RGBPatch:
        """An SDR 8-bit grey code, or the same relative luminance in HDR10."""
        if self.mode == "hdr10":
            level = pq_code10((code8 / 255) ** 2.2 * self.config.hdr_white_nits)
            return RGBPatch(level, level, level, bits=10)
        return RGBPatch(code8, code8, code8)

    def _measure_white_reference(self) -> None:
        """Measure the white patch to anchor relative colorimetry (white = 100)."""
        self.display.display_patch(self._patch("white"))
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


def _gain_delta(err: float, step_scale: float, gain_range: int, min_step: int) -> int:
    """Proportional gain change, widened to at least `min_step` when nonzero —
    smaller writes fall inside the projector's quantization and change nothing."""
    delta = round(err * step_scale * gain_range)
    if delta and abs(delta) < min_step:
        return min_step if delta > 0 else -min_step
    return delta


def cms_errors(
    meas_lab: tuple[float, float, float],
    target_lab: tuple[float, float, float],
) -> dict[str, float]:
    """Decompose a colour error into the RGBCMY menu's three controls.

    All signed (target − measured), so positive means "press Right":
    HUE in degrees of Lab hue angle (Right moves toward the next colour on
    the hue wheel), SAT in % of target chroma, LUM in L*.
    """
    L_meas, a_meas, b_meas = meas_lab
    L_tgt, a_tgt, b_tgt = target_lab
    hue_err = math.degrees(math.atan2(b_tgt, a_tgt) - math.atan2(b_meas, a_meas))
    chroma_tgt = math.hypot(a_tgt, b_tgt)
    return {
        "HUE": (hue_err + 180) % 360 - 180,
        "SAT": (chroma_tgt - math.hypot(a_meas, b_meas)) / max(chroma_tgt, 1e-6) * 100,
        "LUM": L_tgt - L_meas,
    }


def cms_guidance(
    axis: str,
    meas_lab: tuple[float, float, float],
    target_lab: tuple[float, float, float],
) -> list[dict]:
    """Describe what to change in the projector's RGBCMY menu for one axis.

    Returns items ordered largest error first, each with the menu control
    ("hue", "saturation", "brightness"), a signed error, and an instruction.
    Hue is described as a shift toward a neighboring color rather than +/-,
    because the projector's hue direction per axis is not known.
    """
    errs = cms_errors(meas_lab, target_lab)
    hue_err, sat_err_pct, lum_err = errs["HUE"], errs["SAT"], errs["LUM"]

    items: list[tuple[float, dict]] = []
    if abs(hue_err) >= _GUIDE_MIN_HUE_DEG:
        i = _HUE_WHEEL.index(axis)
        toward = _HUE_WHEEL[(i + 1) % 6] if hue_err > 0 else _HUE_WHEEL[i - 1]
        items.append((abs(hue_err) / _GUIDE_MIN_HUE_DEG, {
            "control": "hue", "error": round(hue_err, 1),
            "instruction": f"Shift Hue toward {toward} ({abs(hue_err):.1f}° off)",
        }))
    if abs(sat_err_pct) >= _GUIDE_MIN_SAT_PCT:
        items.append((abs(sat_err_pct) / _GUIDE_MIN_SAT_PCT, {
            "control": "saturation", "error": round(sat_err_pct, 1),
            "instruction": f"{'Increase' if sat_err_pct > 0 else 'Decrease'} Saturation "
                           f"({abs(sat_err_pct):.0f}% {'low' if sat_err_pct > 0 else 'high'})",
        }))
    if abs(lum_err) >= _GUIDE_MIN_LUM:
        items.append((abs(lum_err) / _GUIDE_MIN_LUM, {
            "control": "brightness", "error": round(lum_err, 1),
            "instruction": f"{'Increase' if lum_err > 0 else 'Decrease'} Brightness "
                           f"(L* {abs(lum_err):.1f} {'low' if lum_err > 0 else 'high'})",
        }))
    if not items:
        return [{"control": "any", "error": 0.0,
                 "instruction": "Small combined error — nudge one control a step at a time"}]
    return [item for _, item in sorted(items, key=lambda t: t[0], reverse=True)]


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

    def _patch_dict(p: PatchResult | None) -> dict | None:
        if p is None:
            return None
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
        "baseline": report.baseline,
        "wb_result": _patch_dict(report.wb_result),
        "wb_offset_result": _patch_dict(report.wb_offset_result),
        "gamma_results": [_patch_dict(p) for p in report.gamma_results],
        "cms_results": [_patch_dict(p) for p in report.cms_results],
        "cms_adjustments": report.cms_adjustments,
        "cms_at_limit": report.cms_at_limit,
        "verify_results": [_patch_dict(p) for p in report.verify_results],
    }
