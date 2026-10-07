"""Calibration engine: the closed-loop correction algorithm.

Phases, in run order:
  Baseline      — snapshot every picture setting, then neutralize the ones that
                  would bias a calibration (color/tint/skin tone, processing).
  Levels        — measured: Brightness at the lowest setting before near-black
                  detail crushes, Contrast at the highest before white clips
                  or tints (colorimeter). "guided" mode: by eye on patterns.
  White Balance — Color Temp preset nearest D65 first, then two-point: gains
                  on white (strongest channel kept at its ceiling for maximum
                  light) and offsets on a dark grey, alternated until settled.
  Gamma         — all 9 custom-gamma points solved together against the
                  5–95 % grey ramp (measured Jacobian + Broyden updates; SDR).
  Levels, White Balance and Gamma repeat (calibration_passes) until a pass
  makes no change, since each moves the others.
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

import numpy as np
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

from . import picture
from .color_math import (
    GREY_RAMP,
    SAT_SWEEP,
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
_VERIFY_PATCHES = [
    "white", *_CMS_PATCHES, *(f"grey{pct}" for pct in reversed(GREY_RAMP)),
    *(f"{c}_sat{s}" for c in _CMS_PATCHES for s in SAT_SWEEP),
]

# Measured levels: near-black step used to detect crushing, and the near-white
# step used to detect clipping (8-bit full-range codes)
_NEAR_BLACK_CODE = 10          # ~4 %: clearly above the black floor yet near it
_NEAR_WHITE_CODE = 245         # ~96 %
_CRUSH_KEEP = 0.8              # near-black step must keep 80 % of its gamma-target size
_BRIGHTNESS_GAMMA_PRESET = "22"  # gamma "0" preset used while judging black
_CLIP_RATIO_TOL = 0.01         # near-white/white ratio may exceed gamma by 1 %
_CLIP_CHROMA_DE = 1.0          # near-white vs white chromaticity difference allowed

# Two-point WB offsets on a dark grey (learned step sizes)
_OFFSET_INITIAL_GAIN = 0.02    # normalized chromaticity error per notch (first guess)
_OFFSET_NOISE = 0.002          # smaller changes are measurement noise
_OFFSET_MAX_NOTCHES = 4

# HDR10 levels: the projector tone-maps PQ, so crush/clip are judged against
# the factory-default Brightness/Contrast (126 = menu 50) instead of a gamma curve
_HDR_LEVEL_REFERENCE = 126
_HDR_NEAR_BLACK_CODE = 26      # 10 % grey → ~0.6-nit PQ stimulus (100-nit white)
_HDR_CLIP_HIGH_NITS = 1000     # highlight pair whose ratio reveals clipping
_HDR_CLIP_LOW_NITS = 600
_HDR_CLIP_RATIO_TOL = 0.01

# Gamma solver
_GAMMA_BUMP = 20               # units each tone is moved to measure its effect
_GAMMA_MAX_STEP = 15           # largest change to one tone per iteration
_GAMMA_RIDGE = 0.02            # damping, relative to the mean squared sensitivity

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
    # Absolute RGBCMY menu values, where known (controls reset to 50 this run)
    cms_values: dict[str, dict[str, int]] = field(default_factory=dict)
    # Every projector setting the run changed: {axis: [first before, last after]}
    setting_changes: dict[str, list] = field(default_factory=dict)
    passes: int = 0
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
        self._user_on_event = on_event
        self.anomaly_check = anomaly_check
        self.interactive = interactive
        self.cms_menu = cms_menu
        self._actions: queue.Queue[tuple[str, str | None, int]] = queue.Queue()
        self._abort = False
        self._ref_white_Y: float | None = None
        self._cms_at_limit: dict[str, set[str]] = {}
        self._pass = 1
        self._corrections = 0
        self._setting_changes: dict[str, list] = {}
        self._gamma_J: np.ndarray | None = None
        # HDR10: measured tone curve, (target relative Y, measured relative Y)
        # pairs, white = 100 — colour targets follow it instead of SDR gamma
        self._tone: list[tuple[float, float]] | None = None
        self._hdr_dark_ref: float | None = None
        self._hdr_clip_ref: float | None = None

    def on_event(self, event: dict) -> None:
        """Forward an event, tracking projector changes for passes and the report."""
        if event.get("event") == "correction":
            self._corrections += 1
            entry = self._setting_changes.setdefault(event["axis"], [event["before"], event["after"]])
            entry[1] = event["after"]
        self._user_on_event(event)

    @property
    def _target(self) -> float:
        """ΔE the correction loops aim for (finer than the pass/fail threshold)."""
        return self.config.correction_target or self.config.delta_e_threshold

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

        # Levels, WB and gamma each move the others: repeat them until a pass
        # makes no change. Guided levels only prompt the user on the first pass.
        tuning = [p for p in ("levels", "wb", "gamma") if p in phases]
        for self._pass in range(1, (self.config.calibration_passes if tuning else 0) + 1):
            if self._abort:
                break
            before = self._corrections
            for phase in tuning:
                if self._abort:
                    break
                if phase == "levels":
                    self._phase_levels()
                elif phase == "wb":
                    report.wb_result, report.wb_offset_result = self._phase1_white_balance()
                elif phase == "gamma":
                    report.gamma_results = self._phase_gamma()
            report.passes = self._pass
            changes = self._corrections - before
            logger.info("Pass %d made %d corrections", self._pass, changes)
            self.on_event({"event": "pass_done", "pass": self._pass, "corrections": changes})
            if changes == 0:
                break

        for phase in ("cms",):
            if self._abort or phase not in phases:
                continue
            if phase == "cms":
                report.cms_results = self._phase2_cms()
                report.cms_at_limit = {a: sorted(c) for a, c in self._cms_at_limit.items() if c}
                if self.cms_menu is not None:
                    report.cms_values = {a: dict(v) for a, v in self.cms_menu.values.items() if v}
                    report.cms_adjustments = {
                        axis: {c: n for c, n in moved.items() if n}
                        for axis, moved in self.cms_menu.offsets.items() if any(moved.values())
                    }

        report.total_iterations = sum(p.iterations for p in report.correction_results)
        report.setting_changes = {k: v for k, v in self._setting_changes.items() if v[0] != v[1]}
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
        measure: Callable[[], tuple[float, float, float]] | None = None,
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
        measure = measure or self.measure
        threshold = self._target
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
            X, Y, Z = measure()
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
            X, Y, Z = measure()
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
        measured = self.config.levels_mode == "measured" and not self.dry_run
        if not measured and self._pass > 1:
            return  # guided levels only ask the user once
        self.on_event({"event": "phase_start", "phase": "levels", "patch_count": 0})
        logger.info("Levels (%s) [mode=%s, pass %d]", "measured" if measured else "guided",
                    self.mode, self._pass)
        if measured:
            # First pass searches widely; later passes only re-check nearby
            span = 6 if self._pass == 1 else 2
            self._tune_brightness(span)
            if not self._abort:
                self._tune_contrast(span)
            self.on_event({"event": "phase_done", "phase": "levels"})
            return
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

    def _measure_patch(self, patch: RGBPatch) -> tuple[float, float, float]:
        self.display.display_patch(patch)
        return self.measure()

    def _step_setting(self, token: str, direction: int) -> int:
        """Move a 0–255 setting one visible notch; emit the change; return the stored value."""
        before = int(self.projector.get_setting(token))
        after = picture.nudge(self.projector, token, direction)
        if after != before:
            self.on_event({"event": "correction", "patch": "levels", "axis": token,
                           "before": before, "after": after})
        return after

    def _walk_to(self, token: str, value: int) -> None:
        current = int(self.projector.get_setting(token))
        while current != value and not self._abort:
            moved = self._step_setting(token, 1 if value > current else -1)
            if moved == current:
                break
            current = moved

    def _tune_brightness(self, span: int) -> None:
        """Lowest Brightness at which the near-black step is still fully there.

        Brightness offsets the signal before the gamma curve, so the step
        between black and near-black (code 10, ~4 %) shrinks as Brightness
        drops — it is not constant — and only collapses below what gamma 2.2
        asks for once near-black detail starts crushing. So the reference is
        the target step itself, (10/255)^2.2 × white: walk down while the
        measured step keeps ≥ _CRUSH_KEEP of it (or up, if already crushed).
        """
        # Judge black against a standard gamma preset, not the custom curve:
        # once gamma lifts the shadows, the step looks bigger and Brightness
        # would keep dropping pass after pass while gamma compensates (seen on
        # the first live multi-pass run: 136 → 114 → 103).
        if self.mode == "hdr10":
            self._tune_brightness_hdr(span)
            return
        try:
            gamma: str | None = self.projector.get_setting("GAMMA")
        except ProjectorError:
            gamma = None                       # no gamma control: judge as-is
        swap = gamma not in (None, _BRIGHTNESS_GAMMA_PRESET)
        if swap:
            self.projector.set_setting("GAMMA", _BRIGHTNESS_GAMMA_PRESET)
        try:
            self._tune_brightness_on_preset(span)
        finally:
            if swap:
                self.projector.set_setting("GAMMA", gamma)

    def _tune_brightness_on_preset(self, span: int) -> None:
        def step() -> float:
            y0 = self._measure_patch(self._grey_patch(0))[1]
            yn = self._measure_patch(self._grey_patch(_NEAR_BLACK_CODE))[1]
            return yn - y0

        white = self._measure_patch(self._grey_patch(255))[1]
        ideal = (_NEAR_BLACK_CODE / 255) ** 2.2 * white
        start = current = int(self.projector.get_setting("BRIGHT"))

        def ok(d: float) -> bool:
            logger.info("Brightness %d: near-black step %.4f cd/m² (%.0f%% of the %.4f gamma target)",
                        current, d, 100 * d / ideal if ideal > 0 else 0, ideal)
            return ideal > 0 and d >= _CRUSH_KEEP * ideal

        if not ok(step()):
            for _ in range(3 * span):          # crushing already: raise until the step returns
                moved = self._step_setting("BRIGHT", +1)
                if moved == current or self._abort:
                    break
                current = moved
                if ok(step()):
                    break
        else:
            best = current
            for _ in range(3 * span):          # lower while the step survives
                moved = self._step_setting("BRIGHT", -1)
                if moved == current or self._abort:
                    break
                current = moved
                if not ok(step()):
                    break
                best = current
            self._walk_to("BRIGHT", best)
        logger.info("Brightness %d → %s", start, self.projector.get_setting("BRIGHT"))

    def _tune_brightness_hdr(self, span: int) -> None:
        """HDR10: lowest Brightness keeping a dark grey as distinct from black
        as at the factory default — the tone curve sets the shadows, so there
        is no gamma target to compare with."""
        def step() -> float:
            y0 = self._measure_patch(self._grey_patch(0))[1]
            return self._measure_patch(self._grey_patch(_HDR_NEAR_BLACK_CODE))[1] - y0

        start = int(self.projector.get_setting("BRIGHT"))
        if self._pass == 1:
            self._walk_to("BRIGHT", _HDR_LEVEL_REFERENCE)
        if self._hdr_dark_ref is None:
            self._hdr_dark_ref = step()
        ref = self._hdr_dark_ref
        current = best = int(self.projector.get_setting("BRIGHT"))
        logger.info("HDR Brightness reference step %.4f cd/m² (factory %d)", ref, _HDR_LEVEL_REFERENCE)
        for _ in range(3 * span):
            moved = self._step_setting("BRIGHT", -1)
            if moved == current or self._abort:
                break
            current = moved
            d = step()
            logger.info("HDR Brightness %d: dark step %.4f (%.0f%% of factory)", current, d, 100 * d / ref if ref > 0 else 0)
            if ref <= 0 or d < _CRUSH_KEEP * ref:
                break
            best = current
        self._walk_to("BRIGHT", best)
        logger.info("HDR Brightness %d → %d", start, best)

    def _tune_contrast_hdr(self, span: int) -> None:
        """HDR10: highest Contrast before highlights clip — Y(600 nit)/Y(1000 nit)
        stays at its factory-default ratio and the highlight doesn't tint."""
        hi = RGBPatch(*(pq_code10(_HDR_CLIP_HIGH_NITS),) * 3, bits=10)
        lo = RGBPatch(*(pq_code10(_HDR_CLIP_LOW_NITS),) * 3, bits=10)

        def sample() -> tuple[float, float]:
            w = self._measure_patch(hi)
            n = self._measure_patch(lo)
            norm = lambda xyz: tuple(v * 100.0 / xyz[1] for v in xyz)  # noqa: E731
            return (n[1] / w[1] if w[1] > 1e-6 else 1.0,
                    delta_e_2000(xyz_to_lab(*norm(n)), xyz_to_lab(*norm(w))))

        start = int(self.projector.get_setting("CONTRAST"))
        if self._pass == 1:
            self._walk_to("CONTRAST", _HDR_LEVEL_REFERENCE)
        if self._hdr_clip_ref is None:
            self._hdr_clip_ref = sample()[0]
        ref = self._hdr_clip_ref
        current = best = int(self.projector.get_setting("CONTRAST"))
        for _ in range(4 * span):
            moved = self._step_setting("CONTRAST", +1)
            if moved == current or self._abort:
                break
            current = moved
            ratio, shift = sample()
            clean = ratio <= ref + _HDR_CLIP_RATIO_TOL and shift <= _CLIP_CHROMA_DE
            logger.info("HDR Contrast %d: 600/1000-nit %.4f (factory %.4f), tint %.2f → %s",
                        current, ratio, ref, shift, "clean" if clean else "clipping")
            if not clean:
                break
            best = current
        self._walk_to("CONTRAST", best)
        logger.info("HDR Contrast %d → %d", start, best)

    def _tune_contrast(self, span: int) -> None:
        """Highest Contrast at which near-white stays below white (no clip/tint).

        Unclipped, Y(96 %)/Y(100 %) follows the gamma curve; as white clips the
        ratio rises toward 1, and a channel clipping first shows up as a
        chromaticity difference between the two.
        """
        if self.mode == "hdr10":
            self._tune_contrast_hdr(span)
            return
        expected = (_NEAR_WHITE_CODE / 255) ** 2.2

        def clean() -> bool:
            w = self._measure_patch(self._grey_patch(255))
            n = self._measure_patch(self._grey_patch(_NEAR_WHITE_CODE))
            ratio = n[1] / w[1] if w[1] > 1e-6 else 1.0
            norm = lambda xyz: tuple(v * 100.0 / xyz[1] for v in xyz)  # noqa: E731
            shift = delta_e_2000(xyz_to_lab(*norm(n)), xyz_to_lab(*norm(w)))
            ok = ratio <= expected + _CLIP_RATIO_TOL and shift <= _CLIP_CHROMA_DE
            logger.info("Contrast %s: white %.2f cd/m², 96%%/100%% %.4f (gamma %.4f), tint shift %.2f → %s",
                        self.projector.get_setting("CONTRAST"), w[1], ratio, expected, shift,
                        "clean" if ok else "clipping")
            return ok

        start = int(self.projector.get_setting("CONTRAST"))
        current = start
        if not clean():
            for _ in range(3 * span):        # already clipping: back off
                moved = self._step_setting("CONTRAST", -1)
                if moved == current or self._abort:
                    break
                current = moved
                if clean():
                    break
        else:
            best = current
            for _ in range(2 * span):         # find the top clean setting
                moved = self._step_setting("CONTRAST", +1)
                if moved == current or self._abort:
                    break
                current = moved
                if not clean():
                    break
                best = current
            self._walk_to("CONTRAST", best)
        logger.info("Contrast %d → %s", start, self.projector.get_setting("CONTRAST"))

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

        low_patch = cfg.hdr_wb_offset_patch if self.mode == "hdr10" else cfg.wb_offset_patch
        self.on_event({"event": "phase_start", "phase": "wb", "patch_count": 2 if has_offsets else 1})
        if cfg.ctemp_select and self._pass == 1 and not self.dry_run:
            self._select_ctemp()
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
            lo = self._wb_offset_point(low_patch)
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

    def _white_chroma_de(self) -> float:
        X, Y, Z = self._measure_patch(self._patch("white"))
        s = 100.0 / Y if Y > 1e-6 else 1.0
        return delta_e_2000(xyz_to_lab(X * s, Y * s, Z * s), get_target_lab("white", self.mode))

    def _select_ctemp(self) -> None:
        """Pick the Color Temp preset whose white is nearest D65 with neutral gains.

        Starting the gains from a preset that is already close leaves them near
        their ceiling, so less light is thrown away correcting white.
        """
        ceiling = self.config.wb_gain_ceiling
        for ch in ("R", "G", "B"):
            before = self.projector.get_wb_gain(ch)
            if before != ceiling:
                self.projector.set_wb_gain(ch, ceiling)
                self.on_event({"event": "correction", "patch": "white", "axis": f"WB_{ch}",
                               "before": before, "after": self.projector.get_wb_gain(ch)})
        try:
            start = int(self.projector.get_setting("CTEMP"))
        except ProjectorError:
            return
        scores = {start: self._white_chroma_de()}
        logger.info("Color Temp %d: white ΔE %.2f", start, scores[start])
        for direction in (+1, -1):
            if direction == -1:
                self._walk_to("CTEMP", start)
            current, worse = start, 0
            while worse < 2 and not self._abort:
                before = current
                current = picture.nudge(self.projector, "CTEMP", direction)
                if current == before:
                    break
                best_so_far = min(scores.values())
                scores[current] = self._white_chroma_de()
                logger.info("Color Temp %d: white ΔE %.2f", current, scores[current])
                worse = worse + 1 if scores[current] >= best_so_far else 0
        best = min(scores, key=scores.get)
        self._walk_to_quiet("CTEMP", best)
        if best != start:
            self.on_event({"event": "correction", "patch": "white", "axis": "CTEMP",
                           "before": start, "after": best})
        logger.info("Color Temp %d → %d (white ΔE %.2f with neutral gains)", start, best, scores[best])

    def _walk_to_quiet(self, token: str, value: int) -> None:
        current = int(self.projector.get_setting(token))
        while current != value:
            moved = picture.nudge(self.projector, token, 1 if value > current else -1)
            if moved == current:
                break
            current = moved

    def _wb_offset_point(self, patch: str) -> _LoopOutcome:
        """Offsets on a dark grey, with step sizes learned from measured effect.

        On a dark grey one offset notch moves chromaticity a lot (one blue
        notch swung ΔE 3.6 → 13.7 at 30 % on the 5040UB), so the gains'
        proportional formula overshoots. Each move is one channel by a few
        notches; the per-notch effect (signed) is re-estimated from every
        move, like the automatic CMS. G stays the reference.
        """
        cfg = self.config
        target_lab = get_target_lab("white", self.mode)
        tx, ty, _ = get_target_xyY("white", self.mode)
        TX, _, TZ = xyY_to_XYZ(tx, ty, 100.0)
        tokens = {"R": "OFFSETR", "B": "OFFSETB"}
        gains = {"R": _OFFSET_INITIAL_GAIN, "B": _OFFSET_INITIAL_GAIN}  # error per notch
        best_de, best_vals, worse = float("inf"), None, 0
        initial_de: float | None = None
        de, converged, iteration = float("inf"), False, 0
        X = Y = Z = 0.0
        last: tuple[str, int, float] | None = None

        def read() -> dict[str, int]:
            return {ch: int(self.projector.get_setting(t)) for ch, t in tokens.items()}

        def measure() -> tuple[float, dict[str, float], tuple[float, float, float]]:
            xyz = self.measure()
            s = 100.0 / xyz[1] if xyz[1] > 1e-6 else 1.0
            Xn, Yn, Zn = (v * s for v in xyz)
            return (delta_e_2000(xyz_to_lab(Xn, Yn, Zn), target_lab),
                    {"R": (Xn - TX) / TX, "B": (Zn - TZ) / TZ}, xyz)

        self.display.display_patch(self._patch(patch))
        for iteration in range(1, cfg.max_iterations_per_patch + 1):
            if self._abort:
                break
            self.on_event({"event": "patch_start", "patch": patch, "iteration": iteration, "mode": "wb"})
            de, errs, (X, Y, Z) = measure()
            self.on_event({"event": "measurement", "patch": patch, "xyz": [X, Y, Z], "delta_e": de})
            logger.info("WB offsets %s iter %d: ΔE=%.3f R%+.4f B%+.4f", patch, iteration, de, errs["R"], errs["B"])
            if initial_de is None:
                initial_de = de
            if last is not None:
                ch, notches, before = last
                change = errs[ch] - before
                if abs(change) >= _OFFSET_NOISE:
                    g = change / notches
                    gains[ch] = math.copysign(min(0.2, max(0.002, abs(g))), g)
            if de < self._target or self.dry_run:
                converged = de < self._target
                break
            if de < best_de:
                best_de, best_vals, worse = de, read(), 0
            else:
                worse += 1
                if worse >= _MAX_WORSENING_ITERATIONS:
                    logger.warning("WB offsets %s not improving — reverting to %s (ΔE=%.3f)", patch, best_vals, best_de)
                    for ch, v in best_vals.items():
                        self._walk_offset(tokens[ch], v)
                    break
            ch = max(tokens, key=lambda c: abs(errs[c]))
            notches = round(-errs[ch] / gains[ch])
            if notches == 0:
                notches = -1 if errs[ch] * gains[ch] > 0 else 1
            notches = max(-_OFFSET_MAX_NOTCHES, min(_OFFSET_MAX_NOTCHES, notches))
            moved = self._move_offset(tokens[ch], notches)
            if moved == 0:
                logger.warning("WB offset %s at its limit", tokens[ch])
                break
            last = (ch, moved, errs[ch])

        if not converged and not self._abort and not self.dry_run:
            de, _, (X, Y, Z) = measure()
        return _LoopOutcome(final_de=de, initial_de=initial_de, iterations=iteration,
                            converged=converged, xyz=(X, Y, Z))

    def _move_offset(self, token: str, notches: int) -> int:
        """Move an offset by whole visible notches; return the notches actually moved."""
        moved = 0
        for _ in range(abs(notches)):
            before = int(self.projector.get_setting(token))
            after = picture.nudge(self.projector, token, 1 if notches > 0 else -1)
            if after == before:
                break
            self.on_event({"event": "correction", "patch": "grey", "axis": f"WB_OFFSET_{token[-1]}",
                           "before": before, "after": after})
            moved += 1 if notches > 0 else -1
        return moved

    def _walk_offset(self, token: str, value: int) -> None:
        current = int(self.projector.get_setting(token))
        while current != value:
            if self._move_offset(token, 1 if value > current else -1) == 0:
                break
            current = int(self.projector.get_setting(token))

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

        ceiling = cfg.wb_gain_ceiling

        def corrections(norm_xyz: tuple, meas_lab: tuple, current: dict, step_scale: float) -> dict:
            # Chromaticity correction — compare against the D65 target at the
            # same (normalized) luminance; G acts as the reference channel.
            Xn, Yn, Zn = norm_xyz
            r_err = (Xn - target_X) / max(target_X, 1e-6)
            g_err = (Yn - target_Y) / max(target_Y, 1e-6)
            b_err = (Zn - target_Z) / max(target_Z, 1e-6)
            new = {
                ch: current[ch] - _gain_delta(err, step_scale, gain_range, min_step)
                for ch, err in (("R", r_err), ("G", g_err), ("B", b_err))
            }
            if kind == "gain" and cfg.wb_maximize_light:
                # Shift all three so the strongest channel sits at the ceiling:
                # white comes from lowering the others, never from dimming all
                shift = ceiling - max(new.values())
                new = {ch: v + shift for ch, v in new.items()}
            return {ch: _clamp(v, gain_lo, gain_hi) for ch, v in new.items()}

        def apply(current: dict, new: dict) -> None:
            self._apply_settings(patch, current, new, names, put, read_back=get)

        measure = None
        spread = [p for p in (cfg.wb_gain_patches if kind == "gain" else [patch])]
        if len(spread) > 1:
            # Balance white over several greys: the 5040UB's channels don't
            # track each other between the two WB points (magenta tint peaking
            # at 70–80 % with white and 30 % neutral), so judging gains on the
            # mean chromaticity of e.g. white + 80 % splits that error instead
            # of putting all of it mid-scale.
            def measure() -> tuple[float, float, float]:
                acc = [0.0, 0.0, 0.0]
                for q in spread:
                    X, Y, Z = self._measure_patch(self._patch(q))
                    k = 100.0 / Y if Y > 1e-6 else 1.0
                    acc = [a + v * k / len(spread) for a, v in zip(acc, (X, Y, Z))]
                return tuple(acc)  # type: ignore[return-value]

        outcome = self._correction_loop(patch, "wb", values, normalize, target_lab, corrections, apply, measure)
        if measure is not None:
            self._measure_white_reference()   # the loop's readings are normalized, not absolute
            outcome.xyz = (outcome.xyz[0], self._ref_white_Y or outcome.xyz[1], outcome.xyz[2])
        return outcome

    # ------------------------------------------------------------------
    # Gamma — custom gamma points
    # ------------------------------------------------------------------

    def _phase_gamma(self) -> list[PatchResult]:
        """Solve all 9 custom-gamma points together against the grey ramp.

        Every tone moves several greys and neighbours interact (some pull the
        other way), so the points are solved as one system: the Jacobian of
        grey L* against each GAMMALV tone is measured once per run (each tone
        bumped by _GAMMA_BUMP), then each iteration measures the ramp, solves
        a damped least-squares step for all tones, applies it, and refines the
        Jacobian with a Broyden update from what actually happened. Readings
        are reduced to lightness — chromaticity is the WB phase's job.
        """
        greys = list(GREY_RAMP)
        self.on_event({"event": "phase_start", "phase": "gamma", "patch_count": len(greys)})
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

        if current_gamma != picture.GAMMA_CUSTOM:
            self.on_event({"event": "correction", "patch": "gamma", "axis": "GAMMA",
                           "before": current_gamma, "after": picture.GAMMA_CUSTOM})
            if not self.dry_run:
                self.projector.set_setting("GAMMA", picture.GAMMA_CUSTOM)

        tones = list(range(picture.GAMMA_POINTS))
        target_L = np.array([get_target_lab(f"grey{g}", self.mode)[0] for g in greys])

        def read_tones() -> np.ndarray:
            return np.array([int(self.projector.get_setting("GAMMALV", f"{t:02d}")) for t in tones], float)

        def write_tone(t: int, value: int) -> int:
            self.projector.set_setting("GAMMALV", value, f"{t:02d}")
            return int(self.projector.get_setting("GAMMALV", f"{t:02d}"))

        def ramp(iteration: int) -> tuple[np.ndarray, list]:
            self._measure_white_reference()       # white drifts; re-anchor every ramp
            Ls, xyzs = [], []
            for g in greys:
                if self._abort:
                    break
                patch = f"grey{g}"
                self.on_event({"event": "patch_start", "patch": patch, "iteration": iteration, "mode": "gamma"})
                xyz = self._measure_patch(self._patch(patch))
                Y = self._to_relative(*xyz)[1]
                L = _lightness(Y)
                self.on_event({"event": "measurement", "patch": patch, "xyz": list(xyz),
                               "delta_e": _l_de(L, target_L[len(Ls)])})
                Ls.append(L)
                xyzs.append(xyz)
            return np.array(Ls), xyzs

        values = read_tones()
        L, xyzs = ramp(1)
        if self._abort:
            self.on_event({"event": "phase_done", "phase": "gamma"})
            return results
        initial_de = [_l_de(l, t) for l, t in zip(L, target_L)]

        if self._gamma_J is None and not self.dry_run:
            self._gamma_J = self._measure_gamma_jacobian(tones, values, L, ramp)
        J = self._gamma_J if self._gamma_J is not None else np.zeros((len(greys), len(tones)))

        best = (max(initial_de), values.copy(), L.copy(), list(xyzs))
        worse = 0
        iteration = 1
        for iteration in range(2, self.config.max_iterations_per_patch + 2):
            de = [_l_de(l, t) for l, t in zip(L, target_L)]
            logger.info("Gamma iter %d: worst ΔL %.2f, tones %s", iteration - 1, max(de), values.astype(int).tolist())
            if max(de) < self._target or self.dry_run or self._abort:
                break
            e = target_L - L
            # Damped least squares for all tones at once
            JtJ = J.T @ J
            A = JtJ + _GAMMA_RIDGE * max(float(np.mean(np.diag(JtJ))), 1e-9) * np.eye(len(tones))
            step = np.linalg.solve(A, J.T @ e)
            step = np.clip(step, -_GAMMA_MAX_STEP, _GAMMA_MAX_STEP)
            step[np.abs(step) < 1.5] = 0.0     # below the ~2.5-unit quantization
            if not step.any():
                logger.info("Gamma: remaining error is below one step of any point")
                break
            new_values = values.copy()
            for t in tones:
                if step[t]:
                    target_v = int(np.clip(round(values[t] + step[t]), 0, 255))
                    new_values[t] = write_tone(t, target_v)
                    if new_values[t] != values[t]:
                        self.on_event({"event": "correction", "patch": "gamma", "axis": f"GAMMA_{t + 1}",
                                       "before": int(values[t]), "after": int(new_values[t])})
            dv = new_values - values
            values = new_values
            L_new, xyzs = ramp(iteration)
            if self._abort or not dv.any():
                L = L_new if len(L_new) == len(L) else L
                break
            # Broyden: correct J along the move with what actually happened
            J = J + np.outer((L_new - L) - J @ dv, dv) / float(dv @ dv)
            L = L_new
            worst = max(_l_de(l, t) for l, t in zip(L, target_L))
            if worst < best[0]:
                best, worse = (worst, values.copy(), L.copy(), list(xyzs)), 0
            else:
                worse += 1
                if worse >= _MAX_WORSENING_ITERATIONS:
                    logger.warning("Gamma not improving — reverting to best tones (worst ΔL %.2f)", best[0])
                    for t in tones:
                        if values[t] != best[1][t]:
                            got = write_tone(t, int(best[1][t]))
                            self.on_event({"event": "correction", "patch": "gamma", "axis": f"GAMMA_{t + 1}",
                                           "before": int(values[t]), "after": got})
                    values, L, xyzs = best[1].copy(), best[2], best[3]
                    break
        self._gamma_J = J

        for g, l, t, xyz, de0 in zip(greys, L, target_L, xyzs, initial_de):
            de = _l_de(l, t)
            outcome = _LoopOutcome(final_de=de, initial_de=de0, iterations=iteration,
                                   converged=de < self._target, xyz=tuple(xyz))
            results.append(self._emit_patch_result(outcome, f"grey{g}"))
        self.on_event({"event": "phase_done", "phase": "gamma"})
        return results

    def _measure_gamma_jacobian(self, tones, values, base_L, ramp) -> np.ndarray:
        """dL*/dunit for every grey × tone: bump each tone, re-measure, restore."""
        J = np.zeros((len(base_L), len(tones)))
        logger.info("Gamma: measuring how each of the %d points moves the ramp", len(tones))
        for t in tones:
            if self._abort:
                break
            v = int(values[t])
            bump = _GAMMA_BUMP if v + _GAMMA_BUMP <= 255 else -_GAMMA_BUMP
            self.projector.set_setting("GAMMALV", v + bump, f"{t:02d}")
            actual = int(self.projector.get_setting("GAMMALV", f"{t:02d}")) - v
            L_b, _ = ramp(0)
            self.projector.set_setting("GAMMALV", v, f"{t:02d}")
            if actual and len(L_b) == len(base_L):
                J[:, t] = (L_b - base_L) / actual
            logger.info("Gamma point %d: %s", t + 1, np.round(J[:, t], 3).tolist())
        return J

    # ------------------------------------------------------------------
    # Phase 2 — CMS
    # ------------------------------------------------------------------

    def _phase2_cms(self) -> list[PatchResult]:
        """CMS for each color axis: automatic through the RGBCMY menu, or guided."""
        auto = self.config.cms_mode == "auto" and self.cms_menu is not None and not self.dry_run
        self.on_event({"event": "phase_start", "phase": "cms", "patch_count": len(self.config.cms_axis_order)})
        logger.info("Phase 2: CMS (%s) [mode=%s]", "automatic" if auto else "guided", self.mode)
        if self.mode == "hdr10":
            self._measure_tone_curve()      # colour luminance targets follow it
        elif self._ref_white_Y is None:
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

        levels = cfg.cms_sat_levels
        patches = [axis if lvl == 100 else f"{axis}_sat{lvl}" for lvl in levels]
        targets = {p: self._target_lab(p) for p in patches}
        main = axis if axis in patches else patches[-1]

        menu.open_axis(axis)
        if cfg.cms_reset:
            # Start from the factory 50s: known absolute values, and no
            # leftover extreme settings distorting the mid-saturations
            for c in _CMS_CONTROLS:
                menu.reset_control(c)
                offsets[c] = 0
            self.on_event({"event": "correction", "patch": axis, "axis": f"CMS_{axis}_RESET",
                           "before": "custom", "after": "50/50/50"})

        def reading() -> tuple[float, float, dict[str, float], tuple[float, float, float]]:
            """Mean ΔE and per-control errors over the saturation levels."""
            des, errs_all, xyz_main = [], [], (0.0, 0.0, 0.0)
            for p in patches:
                xyz = self._measure_patch(self._patch(p))
                lab = xyz_to_lab(*self._to_relative(*xyz))
                d = delta_e_2000(lab, targets[p])
                self.on_event({"event": "measurement", "patch": p, "xyz": list(xyz), "delta_e": d})
                des.append(d)
                errs_all.append(cms_errors(lab, targets[p]))
                if p == main:
                    xyz_main = xyz
            errs = {c: sum(e[c] for e in errs_all) / len(errs_all) for c in _CMS_CONTROLS}
            return sum(des) / len(des), max(des), errs, xyz_main

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
            de, worst, errs, (X, Y, Z) = reading()
            logger.info("CMS %s iter %d: mean ΔE=%.3f (worst %.3f over %s) hue=%+.1f° sat=%+.1f%% lum=%+.1f",
                        axis, iteration, de, worst, levels, errs["HUE"], errs["SAT"], errs["LUM"])
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

            if worst < self._target:
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
            de, _, _, (X, Y, Z) = reading()   # report the projector's actual final state

        outcome = _LoopOutcome(final_de=de, initial_de=initial_de, iterations=iteration,
                               converged=converged, xyz=(X, Y, Z))
        return self._emit_patch_result(outcome, axis)

    def _cms_axis(self, axis: str) -> PatchResult:
        """Measure one axis; until it converges, emit guidance and wait for the user.

        The user adjusts the projector's RGBCMY menu by hand between readings,
        so there is no correction step, step decay, or divergence guard here.
        """
        cfg = self.config
        target_lab = self._target_lab(axis)
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
        hdr = self.mode == "hdr10"
        greys = [p for p in _VERIFY_PATCHES if p.startswith("grey")]
        order = (["white", *greys, *(p for p in _VERIFY_PATCHES if p != "white" and p not in greys)]
                 if hdr else _VERIFY_PATCHES)
        if hdr:
            self._tone = []             # rebuilt from this pass's own grey readings

        for patch_name in order:
            if self._abort:
                break
            self.on_event({"event": "patch_start", "patch": patch_name, "iteration": 1, "mode": "verify"})
            self.display.display_patch(self._patch(patch_name))
            X, Y, Z = self.measure()
            if patch_name == "white" and Y > 1e-6:
                # White is measured first; it re-anchors relative colorimetry
                self._ref_white_Y = Y
            meas_lab = xyz_to_lab(*self._to_relative(X, Y, Z))
            if hdr and patch_name.startswith("grey"):
                self._tone.append((get_target_xyY(patch_name, self.mode)[2], self._to_relative(X, Y, Z)[1]))
            target_lab = self._target_lab(patch_name) if not (hdr and patch_name.startswith("grey")) \
                else _neutral_at(self._to_relative(X, Y, Z)[1], self.mode)
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

    def _target_lab(self, name: str) -> tuple[float, float, float]:
        """Lab target for a patch. In HDR10 the projector tone-maps PQ, so the
        target keeps its chromaticity but takes the luminance the measured tone
        curve gives its stimulus (white = 100) — not the SDR gamma ratio."""
        lab = get_target_lab(name, self.mode)
        if self.mode != "hdr10" or not self._tone or name == "white":
            return lab
        x, y, Y = get_target_xyY(name, self.mode)
        return xyz_to_lab(*xyY_to_XYZ(x, y, _tone_map(self._tone, Y)))

    def _measure_tone_curve(self) -> None:
        """HDR10: measure white and the grey ramp; store the tone curve."""
        self._measure_white_reference()
        points = []
        for g in GREY_RAMP:
            if self._abort:
                return
            Y = self._to_relative(*self._measure_patch(self._patch(f"grey{g}")))[1]
            points.append((get_target_xyY(f"grey{g}", self.mode)[2], Y))
        self._tone = points
        logger.info("HDR tone curve (target → measured, white=100): %s",
                    [(round(a, 2), round(b, 2)) for a, b in points])
        self.on_event({"event": "tone_curve", "points": [[round(a, 4), round(b, 4)] for a, b in points]})

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
                step_scale * (de / self._target) * 0.5,
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

def _tone_map(points: list[tuple[float, float]], Y: float) -> float:
    """Relative output luminance for target relative luminance Y, interpolated
    in log–log over the measured (target, measured) grey points + white."""
    pts = sorted([(100.0, 100.0), *[(a, b) for a, b in points if a > 0 and b > 0]])
    if Y <= pts[0][0]:
        return Y * pts[0][1] / pts[0][0]
    for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
        if a0 <= Y <= a1:
            t = (math.log(Y) - math.log(a0)) / (math.log(a1) - math.log(a0)) if a1 > a0 else 0.0
            return math.exp(math.log(b0) + t * (math.log(b1) - math.log(b0)))
    return Y * pts[-1][1] / pts[-1][0]


def _neutral_at(Y: float, mode: str) -> tuple[float, float, float]:
    """Lab of the white point at relative luminance Y (grey judged on tint only)."""
    x, y, _ = get_target_xyY("white", mode)
    return xyz_to_lab(*xyY_to_XYZ(x, y, Y))


def _lightness(Y: float) -> float:
    """CIE L* of a relative luminance (white = 100)."""
    y = max(Y, 0.0) / 100.0
    f = y ** (1 / 3) if y > (6 / 29) ** 3 else y / (3 * (6 / 29) ** 2) + 4 / 29
    return 116 * f - 16


def _l_de(L: float, L_target: float) -> float:
    """CIEDE2000 between two neutrals that differ only in lightness."""
    return delta_e_2000((L, 0.0, 0.0), (L_target, 0.0, 0.0))


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
        "cms_values": report.cms_values,
        "setting_changes": report.setting_changes,
        "passes": report.passes,
        "verify_results": [_patch_dict(p) for p in report.verify_results],
    }
