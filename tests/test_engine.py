"""Unit tests for the calibration engine's closed loop.

Uses a simulated projector + display + colorimeter so the control loop can be
exercised without hardware. The simulated device model:

- WB gains act multiplicatively on the corresponding XYZ channel:
  factor = 1 + (gain - 128) / 256  (i.e. full gain range = ±50% output)
- All readings are returned in "absolute" units (scaled by a brightness factor)
  to verify the engine's relative-colorimetry normalization.
- CMS "menu" values (set by hand on the real projector) move measured Lab
  linearly; tests change them between readings to simulate the user.
"""

from __future__ import annotations

import math
import threading

import pytest

from projector_cal.color_math import D65_WHITE_XYZ, get_target_lab, xyY_to_XYZ
from projector_cal.config import CalibrationConfig
from projector_cal.engine import (
    _MAX_WORSENING_ITERATIONS,
    _VERIFY_PATCHES,
    CalibrationEngine,
    _gain_delta,
    cms_guidance,
)
from projector_cal.pgen import RGBPatch
from projector_cal.projector import ProjectorError

from colormath.color_conversions import convert_color
from colormath.color_objects import LabColor, XYZColor


# D65 white XYZ at Y=100
_D65 = D65_WHITE_XYZ
_CMS_AXES = ["red", "green", "blue", "cyan", "magenta", "yellow"]


def lab_to_xyz(L: float, a: float, b: float) -> tuple[float, float, float]:
    lab = LabColor(float(L), float(a), float(b), illuminant="d65")
    xyz = convert_color(lab, XYZColor, target_illuminant="d65")
    return (xyz.xyz_x * 100.0, xyz.xyz_y * 100.0, xyz.xyz_z * 100.0)


class FakeProjector:
    """Records WB gains and the hand-set CMS menu; the measure functions read them."""

    def __init__(self, wb=None, cms=None, settings=None):
        self.wb = dict(wb or {"R": 128, "G": 128, "B": 128})
        self.cms = {axis: dict(cms or {"HUE": 0, "SAT": 0, "LUM": 0}) for axis in _CMS_AXES}
        # Generic settings ("BRIGHT", "OFFSETR", "GAMMALV 03", …); anything
        # absent is rejected like an unsupported token on the real projector
        self.settings = dict(settings or {})
        self.set_calls = 0

    def get_setting(self, token, param=None):
        key = token if param is None else f"{token} {param}"
        if key not in self.settings:
            raise ProjectorError(f"ERR for {key}?")
        return str(self.settings[key])

    def set_setting(self, token, value, param=None):
        key = token if param is None else f"{token} {param}"
        if key not in self.settings:
            raise ProjectorError(f"ERR for {key}")
        self.settings[key] = value if isinstance(value, str) and not value.isdigit() else int(value)
        self.set_calls += 1

    def get_wb_gain(self, ch):
        return self.wb[ch]

    def set_wb_gain(self, ch, value):
        self.wb[ch] = value
        self.set_calls += 1



class QuantizedProjector(FakeProjector):
    """Stores gains in ~2.53-unit steps like the 5040UB: ±1 writes change nothing."""

    def set_wb_gain(self, ch, value):
        super().set_wb_gain(ch, round(round(value / 2.53) * 2.53))


class FakeDisplay:
    def __init__(self):
        self.current = None

    def connect(self):
        pass

    def disconnect(self):
        pass

    def display_patch(self, patch):
        self.current = (patch.r, patch.g, patch.b)

    def display_black(self):
        self.current = (0, 0, 0)

    def display_window(self, window, background, size=0.25):
        self.current = (window.r, window.g, window.b)


def make_config(**overrides) -> CalibrationConfig:
    defaults = dict(
        delta_e_threshold=1.0,
        max_iterations_per_patch=20,
        wb_gain_center=128,
        wb_gain_range=[0, 255],
        proportional_gain_initial=0.8,
        proportional_gain_minimum=0.1,
        cms_axis_order=["red"],
    )
    defaults.update(overrides)
    return CalibrationConfig(**defaults)


def wb_measure(projector: FakeProjector, brightness: float = 0.6, gain_scale: float = 256):
    """White-patch measurement: gains scale D65 channels; absolute cd/m² scale.

    gain_scale sets how strongly one gain unit moves its channel (larger =
    finer control, so more gain steps are needed near the target).
    """
    def _measure():
        fr = 1 + (projector.wb["R"] - 128) / gain_scale
        fg = 1 + (projector.wb["G"] - 128) / gain_scale
        fb = 1 + (projector.wb["B"] - 128) / gain_scale
        return (_D65[0] * fr * brightness, _D65[1] * fg * brightness, _D65[2] * fb * brightness)
    return _measure


class TestWhiteBalance:
    def test_absolute_luminance_does_not_block_convergence(self):
        """A dim-but-perfect white (60 cd/m²) must converge immediately."""
        proj = FakeProjector()
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj, brightness=0.6),
            config=make_config(),
        )
        result, _ = engine._phase1_white_balance()
        assert result.converged
        assert result.iterations == 1
        assert result.final_delta_e < 1.0
        assert proj.set_calls == 0  # nothing needed correcting

    def test_corrects_gain_toward_target(self):
        """Excess red must drive the R gain down until ΔE < threshold."""
        proj = FakeProjector(wb={"R": 150, "G": 128, "B": 118})
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj),
            config=make_config(),
        )
        result, _ = engine._phase1_white_balance()
        assert result.converged, f"ΔE stayed at {result.final_delta_e}"
        assert result.final_delta_e < 1.0
        assert proj.wb["R"] < 150
        assert proj.wb["B"] > 118
        # G is the fixed reference channel — never touched
        assert proj.wb["G"] == 128

    def test_sets_reference_white_for_later_phases(self):
        proj = FakeProjector()
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj, brightness=0.6),
            config=make_config(),
        )
        engine._phase1_white_balance()
        assert engine._ref_white_Y == pytest.approx(60.0, rel=0.01)

    def test_divergence_guard_reverts_to_best(self):
        """A measurement that ignores corrections must not run away."""
        proj = FakeProjector(wb={"R": 150, "G": 128, "B": 128})

        def stuck_measure():
            # Fixed, badly red-shifted reading regardless of gain changes
            return (_D65[0] * 1.3, _D65[1], _D65[2])

        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=stuck_measure,
            config=make_config(),
        )
        result, _ = engine._phase1_white_balance()
        assert not result.converged
        # Loop stopped early via the guard, not by exhausting max iterations
        assert result.iterations <= 2 + _MAX_WORSENING_ITERATIONS
        # Best-known gains (the initial ones) were restored
        assert proj.wb["R"] == 150

    def test_abort_before_first_measurement(self):
        proj = FakeProjector()
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj),
            config=make_config(),
        )
        engine.abort()
        report = engine.run(["wb"])
        assert report.aborted
        assert report.wb_result is None  # the phase never started
        assert not report.verify_results
        assert proj.set_calls == 0

    def test_converges_on_quantized_projector(self):
        """Near the target the proportional delta shrinks to ±1, which the
        quantized projector ignores — the loop stalls and the guard stops it
        short. The minimum step must carry it to convergence."""
        def run(min_step):
            proj = QuantizedProjector(wb={"R": 144, "G": 128, "B": 116})
            events = []
            engine = CalibrationEngine(
                projector=proj, display=FakeDisplay(),
                measure=wb_measure(proj, gain_scale=1024),
                config=make_config(wb_gain_min_step=min_step),
                on_event=events.append,
            )
            return engine._phase1_white_balance()[0], events

        stalled, _ = run(min_step=1)
        assert not stalled.converged  # the failure this guards against

        result, events = run(min_step=3)
        assert result.converged, f"ΔE stayed at {result.final_delta_e}"
        # Correction events report what the projector stored, not what was asked
        for ev in (e for e in events if e["event"] == "correction"):
            assert ev["after"] == round(round(ev["after"] / 2.53) * 2.53), ev

    def test_dry_run_never_writes_to_projector(self):
        proj = FakeProjector(wb={"R": 160, "G": 128, "B": 100})
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj),
            config=make_config(max_iterations_per_patch=5),
            dry_run=True,
        )
        engine._phase1_white_balance()
        assert proj.set_calls == 0


def cms_measure(projector: FakeProjector, axis: str, brightness: float = 0.6):
    """Axis-patch measurement derived from the target Lab and the CMS controls.

    Correct settings are HUE=5, SAT=5, LUM=2; each control count moves the
    measured value linearly (1°, 2% chroma, 5 L per count respectively).
    """
    L_t, a_t, b_t = get_target_lab(axis, "sdr")
    hue_t = math.degrees(math.atan2(b_t, a_t))
    chroma_t = math.hypot(a_t, b_t)

    def _measure():
        ctl = projector.cms[axis]
        hue = math.radians(hue_t + (ctl["HUE"] - 5) * 1.0)
        chroma = chroma_t * (1 + (ctl["SAT"] - 5) * 0.02)
        L = L_t + (ctl["LUM"] - 2) * 5.0
        X, Y, Z = lab_to_xyz(L, chroma * math.cos(hue), chroma * math.sin(hue))
        return (X * brightness, Y * brightness, Z * brightness)
    return _measure


class TestGainDelta:
    def test_small_nonzero_delta_widens_to_min_step(self):
        assert _gain_delta(0.005, 0.8, 255, 3) == 3     # raw delta 1
        assert _gain_delta(-0.005, 0.8, 255, 3) == -3

    def test_zero_and_large_deltas_unchanged(self):
        assert _gain_delta(0.001, 0.8, 255, 3) == 0     # raw delta 0
        assert _gain_delta(0.05, 0.8, 255, 3) == 10


class TestCmsGuidance:
    def test_directions_move_toward_target(self):
        """Red rotated toward yellow, undersaturated and dim → shift toward
        magenta, increase saturation, increase brightness."""
        L, a, b = get_target_lab("red", "sdr")
        hue = math.atan2(b, a) + math.radians(6)        # rotated toward yellow
        chroma = math.hypot(a, b) * 0.85
        meas = (L - 4, chroma * math.cos(hue), chroma * math.sin(hue))

        by_control = {g["control"]: g for g in cms_guidance("red", meas, (L, a, b))}
        assert "toward magenta" in by_control["hue"]["instruction"]
        assert by_control["saturation"]["instruction"].startswith("Increase")
        assert by_control["brightness"]["instruction"].startswith("Increase")

    def test_largest_error_listed_first(self):
        L, a, b = get_target_lab("green", "sdr")
        meas = (L - 10, a, b)                            # brightness only
        guidance = cms_guidance("green", meas, (L, a, b))
        assert guidance[0]["control"] == "brightness"
        assert guidance[0]["instruction"].startswith("Increase")

    def test_hue_wraps_from_magenta_to_red(self):
        L, a, b = get_target_lab("magenta", "sdr")
        hue = math.atan2(b, a) - math.radians(8)         # rotated toward blue
        chroma = math.hypot(a, b)
        meas = (L, chroma * math.cos(hue), chroma * math.sin(hue))
        hue_item = next(g for g in cms_guidance("magenta", meas, (L, a, b)) if g["control"] == "hue")
        assert "toward red" in hue_item["instruction"]


def _guided_engine(proj, events, **kw):
    engine = CalibrationEngine(
        projector=proj, display=FakeDisplay(),
        measure=cms_measure(proj, "red"),
        config=make_config(),
        on_event=events.append,
        **kw,
    )
    engine._ref_white_Y = 60.0  # phase 1 normally provides this
    return engine


class TestGuidedCMS:
    def test_non_interactive_measures_once_and_reports_guidance(self):
        proj = FakeProjector()
        events = []
        result = _guided_engine(proj, events)._cms_axis("red")
        assert result.iterations == 1
        assert not result.converged
        guidance = [e for e in events if e["event"] == "cms_guidance"]
        assert len(guidance) == 1 and guidance[0]["guidance"]
        assert proj.set_calls == 0  # CMS never writes to the projector

    def test_remeasure_after_manual_fix_converges(self):
        proj = FakeProjector()
        events = []
        engine = _guided_engine(proj, events, interactive=True)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", engine._cms_axis("red")))
        t.start()
        _wait_for(lambda: any(e["event"] == "cms_guidance" for e in events))
        proj.cms["red"] = {"HUE": 5, "SAT": 5, "LUM": 2}   # user fixes it in the menu
        engine.submit_action("remeasure")
        t.join(timeout=5)
        assert not t.is_alive()
        assert out["r"].converged
        assert out["r"].iterations == 2
        assert out["r"].final_delta_e < out["r"].initial_delta_e

    def test_next_accepts_without_remeasuring(self):
        proj = FakeProjector()
        events = []
        engine = _guided_engine(proj, events, interactive=True)
        engine.submit_action("remeasure")   # stale click from an earlier prompt
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", engine._cms_axis("red")))
        t.start()
        _wait_for(lambda: any(e["event"] == "cms_guidance" for e in events))
        engine.submit_action("next")
        t.join(timeout=5)
        assert out["r"].iterations == 1
        assert not out["r"].converged

    def test_abort_releases_pending_wait(self):
        proj = FakeProjector()
        events = []
        engine = _guided_engine(proj, events, interactive=True)
        t = threading.Thread(target=engine._cms_axis, args=("red",))
        t.start()
        _wait_for(lambda: any(e["event"] == "cms_guidance" for e in events))
        engine.abort()
        t.join(timeout=5)
        assert not t.is_alive()

    def test_measures_white_reference_before_cms_only_run(self):
        """A CMS-only run must anchor relative colorimetry before measuring."""
        proj = FakeProjector()
        display = FakeDisplay()

        def measure():
            if display.current == (255, 255, 255):
                return (_D65[0] * 0.6, 60.0, _D65[2] * 0.6)
            return cms_measure(proj, "red")()

        engine = CalibrationEngine(
            projector=proj, display=display,
            measure=measure,
            config=make_config(max_iterations_per_patch=3),
        )
        engine._phase2_cms()
        assert engine._ref_white_Y == pytest.approx(60.0, rel=0.01)


def _wait_for(cond, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out waiting for condition"
        time.sleep(0.01)


class TestStepDecay:
    def test_step_scale_never_exceeds_initial(self):
        engine = CalibrationEngine(
            projector=FakeProjector(), display=FakeDisplay(),
            measure=lambda: _D65,
            config=make_config(),
        )
        # Huge ΔE used to *grow* the step (0.8 * 20 * 0.5 = 8.0)
        assert engine._decay_step(0.8, de=20.0) == pytest.approx(0.8)

    def test_step_scale_decays_near_threshold(self):
        engine = CalibrationEngine(
            projector=FakeProjector(), display=FakeDisplay(),
            measure=lambda: _D65,
            config=make_config(),
        )
        assert engine._decay_step(0.8, de=1.0) == pytest.approx(0.4)

    def test_step_scale_floors_at_minimum(self):
        engine = CalibrationEngine(
            projector=FakeProjector(), display=FakeDisplay(),
            measure=lambda: _D65,
            config=make_config(),
        )
        assert engine._decay_step(0.11, de=1.0) == pytest.approx(0.1)


class TestAnomalyHook:
    def test_abort_patch_action_stops_the_loop(self):
        proj = FakeProjector()
        calls = []
        readings = iter(range(100))

        def slowly_improving_measure():
            # Error shrinks every reading (never trips the divergence guard)
            # but stays far from converged for many iterations.
            n = next(readings)
            err = 30.0 * (0.95 ** n)
            return (_D65[0] + err, _D65[1], _D65[2] - err)

        def anomaly_check(patch, xyz, de):
            calls.append(patch)
            return {"anomaly": True, "type": "lamp_flicker", "action": "abort_patch", "reason": "test"}

        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=slowly_improving_measure,
            config=make_config(),
            anomaly_check=anomaly_check,
        )
        result, _ = engine._phase1_white_balance()
        assert calls, "anomaly hook was never invoked"
        assert result.iterations == 3  # fires on the 3rd iteration

    def test_hook_errors_never_break_calibration(self):
        proj = FakeProjector(wb={"R": 140, "G": 128, "B": 128})

        def broken_hook(patch, xyz, de):
            raise RuntimeError("agent exploded")

        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj),
            config=make_config(),
            anomaly_check=broken_hook,
        )
        result, _ = engine._phase1_white_balance()
        assert result.converged


class TestVerify:
    def test_verify_normalizes_against_measured_white(self):
        """A perfectly-calibrated-but-dim projector must verify with low ΔE."""
        proj = FakeProjector()
        display = FakeDisplay()
        brightness = 0.55

        def measure():
            r, g, b = display.current
            # Ideal additive display: channel contributions per sRGB primary
            lin = [(c / 255.0) ** 2.2 for c in (r, g, b)]
            prim = {
                "red":   xyY_to_XYZ(0.64, 0.33, 21.26),
                "green": xyY_to_XYZ(0.30, 0.60, 71.52),
                "blue":  xyY_to_XYZ(0.15, 0.06, 7.22),
            }
            X = sum(f * p[0] for f, p in zip(lin, prim.values()))
            Y = sum(f * p[1] for f, p in zip(lin, prim.values()))
            Z = sum(f * p[2] for f, p in zip(lin, prim.values()))
            return (X * brightness, Y * brightness, Z * brightness)

        events = []
        engine = CalibrationEngine(
            projector=proj, display=display,
            measure=measure,
            config=make_config(),
            on_event=events.append,
        )
        results = engine._phase3_verify()
        assert len(results) == len(_VERIFY_PATCHES) == 16
        for r in results:
            assert r.final_delta_e < 1.0, f"{r.patch_name}: ΔE={r.final_delta_e}"
        # Verify must drive the UI progress counter: one patch_start and one
        # patch_done per patch (CLAUDE.md pitfall #9)
        assert sum(1 for e in events if e["event"] == "patch_start") == 16
        assert sum(1 for e in events if e["event"] == "patch_done") == 16

    def test_report_serialization_has_no_infinity(self):
        """Aborted runs must not leak Infinity into the JSON report."""
        import json as _json
        from projector_cal.engine import _report_to_dict

        proj = FakeProjector()
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=wb_measure(proj),
            config=make_config(),
        )
        engine.abort()
        report = engine.run(["wb"])
        payload = _json.dumps(_report_to_dict(report))
        assert "Infinity" not in payload


# ---- Baseline / levels / two-point WB / gamma ---------------------------------

def _grey_level(display: FakeDisplay) -> float:
    """Linear light (0–1) of the equal-RGB patch currently shown."""
    return (display.current[1] / 255.0) ** 2.2


class TestBaseline:
    def test_records_settings_and_neutralizes(self):
        proj = FakeProjector(settings={"CMODE": "07", "DENSITY": 140, "IMGPRESET": "03", "MCFI": "02"})
        events = []
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(), measure=wb_measure(proj),
            config=make_config(), on_event=events.append,
        )
        report = engine.run([])
        assert report.baseline == {"CMODE": "07", "DENSITY": "140", "IMGPRESET": "03", "MCFI": "02"}
        assert proj.settings["DENSITY"] == "INIT"
        assert proj.settings["IMGPRESET"] == 0 and proj.settings["MCFI"] == 0
        assert proj.settings["CMODE"] == "07"  # record-only settings are untouched
        baseline = next(e for e in events if e["event"] == "baseline")
        assert set(baseline["neutralized"]) == {"DENSITY", "IMGPRESET", "MCFI"}

    def test_dry_run_records_but_never_writes(self):
        proj = FakeProjector(settings={"DENSITY": 140, "IMGPRESET": "03"})
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(), measure=wb_measure(proj),
            config=make_config(), dry_run=True,
        )
        report = engine.run([])
        assert report.baseline["DENSITY"] == "140"
        assert proj.set_calls == 0


class TestLevels:
    def test_adjust_steps_the_projector_then_done_moves_on(self):
        proj = FakeProjector(settings={"BRIGHT": 128, "CONTRAST": 128})
        events = []
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(), measure=wb_measure(proj),
            config=make_config(), on_event=events.append, interactive=True,
        )
        t = threading.Thread(target=engine._phase_levels)
        t.start()

        def steps(name):
            return [e for e in events if e["event"] == "manual_step" and e["step"] == name]

        _wait_for(lambda: steps("brightness"))
        engine.submit_action("adjust", "BRIGHT", 1)
        _wait_for(lambda: len(steps("brightness")) == 2)
        assert steps("brightness")[-1]["controls"][0]["value"] == "129"
        engine.submit_action("next")
        _wait_for(lambda: steps("contrast"))
        engine.submit_action("adjust", "BRIGHT", 1)   # not a control of this step: ignored
        engine.submit_action("next")
        t.join(timeout=5)
        assert not t.is_alive()
        assert proj.settings["BRIGHT"] == 129 and proj.settings["CONTRAST"] == 128
        assert [e["step"] for e in events if e["event"] == "manual_step_done"] == ["brightness", "contrast"]


def two_point_measure(proj: FakeProjector, display: FakeDisplay, brightness: float = 0.6):
    """Gains scale each channel; offsets add a constant that dominates dark greys."""
    def _measure():
        v = _grey_level(display)
        out = []
        for ch, ref in zip("RGB", _D65):
            gain = 1 + (proj.wb[ch] - 128) / 1024
            offset = (proj.settings[f"OFFSET{ch}"] - 128) / 1024 * 0.2
            out.append(ref * max(v * gain + offset, 1e-4) * brightness)
        return tuple(out)
    return _measure


class TestTwoPointWhiteBalance:
    def test_gains_fix_white_and_offsets_fix_dark_grey(self):
        proj = FakeProjector(wb={"R": 140, "G": 128, "B": 120},
                             settings={"OFFSETR": 110, "OFFSETG": 128, "OFFSETB": 140})
        display = FakeDisplay()
        engine = CalibrationEngine(
            projector=proj, display=display, measure=two_point_measure(proj, display),
            config=make_config(),
        )
        white, low = engine._phase1_white_balance()
        assert white.converged, f"white ΔE {white.final_delta_e}"
        assert low is not None and low.patch_name == "grey30"
        assert low.converged, f"grey30 ΔE {low.final_delta_e}"
        assert low.final_delta_e < low.initial_delta_e
        assert proj.settings["OFFSETR"] > 110 and proj.settings["OFFSETB"] < 140

    def test_without_offsets_falls_back_to_gain_only(self):
        proj = FakeProjector(wb={"R": 150, "G": 128, "B": 118})
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(), measure=wb_measure(proj),
            config=make_config(),
        )
        white, low = engine._phase1_white_balance()
        assert white.converged and low is None


def gamma_measure(proj: FakeProjector, display: FakeDisplay, points, brightness: float = 0.6):
    """Neutral greys; each tuned grey's GAMMALV tone scales its luminance (128 = 22% dark)."""
    tone_for = {grey: tone for grey, tone in points}
    from projector_cal.color_math import get_target_xyY

    def _measure():
        r = display.current[1]
        if r == 255:
            Y = 100.0
        else:
            pct = round(r * 100 / 255)
            target_Y = get_target_xyY(f"grey{pct}", "sdr")[2]
            tone = tone_for.get(pct)
            factor = 1 + (proj.settings[f"GAMMALV {tone:02d}"] - 150) / 100 if tone is not None else 1
            Y = target_Y * factor
        return (_D65[0] / 100 * Y * brightness, Y * brightness, _D65[2] / 100 * Y * brightness)
    return _measure


class TestGamma:
    def _projector(self, gamma="22"):
        return FakeProjector(settings={"GAMMA": gamma, **{f"GAMMALV {i:02d}": 128 for i in range(9)}})

    def test_points_converge_and_custom_gamma_is_selected(self):
        proj = self._projector()
        display = FakeDisplay()
        cfg = make_config()
        engine = CalibrationEngine(
            projector=proj, display=display,
            measure=gamma_measure(proj, display, cfg.gamma_points), config=cfg,
        )
        results = engine._phase_gamma()
        assert proj.settings["GAMMA"] == "F0"
        assert [r.patch_name for r in results] == [f"grey{g}" for g, _ in cfg.gamma_points]
        moved = 0
        for (grey, tone), r in zip(cfg.gamma_points, results):
            assert r.converged, f"{r.patch_name}: ΔE {r.final_delta_e}"
            if r.initial_delta_e > 1.0:  # near black, 22% is still under 1 L*
                assert proj.settings[f"GAMMALV {tone:02d}"] > 128, r.patch_name
                moved += 1
        assert moved >= len(cfg.gamma_points) - 1
        # Untuned tones (curve ends) are never written
        assert proj.settings["GAMMALV 00"] == 128 and proj.settings["GAMMALV 08"] == 128

    def test_second_pass_rechecks_points_disturbed_by_neighbours(self):
        """Tone 03 also drags 10% grey down (as on the real projector), after
        10% was already tuned; only a second pass brings 10% back."""
        from projector_cal.color_math import delta_e_2000, get_target_lab, xyz_to_lab

        def final_grey10_de(passes):
            proj = self._projector()
            display = FakeDisplay()
            cfg = make_config(gamma_points=[[10, 1], [20, 3]], gamma_passes=passes)
            base = gamma_measure(proj, display, cfg.gamma_points)

            def measure():
                X, Y, Z = base()
                if display.current[1] == round(10 * 255 / 100):
                    k = 1 - (proj.settings["GAMMALV 03"] - 128) / 60   # neighbour interaction
                    return (X * k, Y * k, Z * k)
                return (X, Y, Z)

            engine = CalibrationEngine(projector=proj, display=display, measure=measure, config=cfg)
            engine._phase_gamma()
            # Re-measure the projector's actual end state — not the loop's last reading
            display.display_patch(engine._patch("grey10"))
            Y = engine._to_relative(*measure())[1]
            return delta_e_2000(xyz_to_lab(0.95047 * Y, Y, 1.08883 * Y), get_target_lab("grey10", "sdr"))

        assert final_grey10_de(passes=1) > 1.0   # the failure this guards against
        assert final_grey10_de(passes=2) < 1.0

    def test_skipped_in_hdr10(self):
        proj = self._projector()
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(), measure=wb_measure(proj),
            config=make_config(), mode="hdr10",
        )
        assert engine._phase_gamma() == []
        assert proj.set_calls == 0


class TestHdrPatches:
    def test_hdr10_runs_send_10_bit_pq_patches(self):
        from projector_cal.color_math import hdr_patch_code10
        shown = []

        class RecordingDisplay(FakeDisplay):
            def display_patch(self, patch):
                shown.append(patch)
                super().display_patch(patch)

        proj = FakeProjector()
        engine = CalibrationEngine(
            projector=proj, display=RecordingDisplay(), measure=wb_measure(proj),
            config=make_config(), mode="hdr10",
        )
        engine._phase3_verify()
        white = shown[0]
        assert white.bits == 10 and (white.r, white.g, white.b) == hdr_patch_code10("white", 100)

    def test_sdr_runs_send_8_bit_codes(self):
        proj = FakeProjector()
        display = FakeDisplay()
        engine = CalibrationEngine(
            projector=proj, display=display, measure=wb_measure(proj), config=make_config(),
        )
        assert engine._patch("grey50") == RGBPatch(128, 128, 128)
        assert engine._grey_patch(5) == RGBPatch(5, 5, 5)


class FakeMenu:
    """RgbcmyMenu stand-in that edits FakeProjector.cms like the real panel.

    `invert` lists controls whose Right press moves the value the other way.
    """

    def __init__(self, proj: FakeProjector, invert=()):
        self.proj = proj
        self.invert = set(invert)
        self.axis = None
        self.open_count = 0
        self.closed = True
        self.offsets = {a: {"HUE": 0, "SAT": 0, "LUM": 0} for a in _CMS_AXES}

    def open_axis(self, axis):
        self.axis, self.closed = axis, False
        self.open_count += 1

    def step(self, control, steps):
        sign = -1 if control in self.invert else 1
        self.proj.cms[self.axis][control] += sign * steps
        self.offsets[self.axis][control] += steps

    def close(self):
        self.closed = True


class TestAutoCMS:
    def _engine(self, proj, menu, **kw):
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(), measure=cms_measure(proj, "red"),
            config=make_config(), cms_menu=menu, **kw,
        )
        engine._ref_white_Y = 60.0
        return engine

    def test_converges_and_closes_the_menu(self):
        proj = FakeProjector()
        menu = FakeMenu(proj)
        report = self._engine(proj, menu).run(["cms"])
        red = report.cms_results[0]
        assert red.converged, f"ΔE {red.final_delta_e}"
        assert red.final_delta_e < red.initial_delta_e
        assert menu.closed
        assert report.cms_adjustments["red"]   # net steps recorded for the profile

    def test_learns_an_inverted_control(self):
        proj = FakeProjector()
        menu = FakeMenu(proj, invert={"HUE"})
        result = self._engine(proj, menu)._phase2_cms()[0]
        assert result.converged, f"ΔE {result.final_delta_e}"
        assert proj.cms["red"]["HUE"] == 5   # reached the true hue despite the inversion

    def test_dry_run_is_guided_and_presses_nothing(self):
        proj = FakeProjector()
        menu = FakeMenu(proj)
        self._engine(proj, menu, dry_run=True)._phase2_cms()
        assert menu.open_count == 0
        assert proj.cms["red"] == {"HUE": 0, "SAT": 0, "LUM": 0}

    def test_guided_mode_ignores_the_menu(self):
        proj = FakeProjector()
        menu = FakeMenu(proj)
        engine = self._engine(proj, menu)
        engine.config.cms_mode = "guided"
        engine._phase2_cms()
        assert menu.open_count == 0

    def test_abort_still_closes_the_menu(self):
        proj = FakeProjector()
        menu = FakeMenu(proj)
        engine = self._engine(proj, menu)
        calls = {"n": 0}
        base = engine.measure

        def measure_then_abort():
            calls["n"] += 1
            if calls["n"] == 2:
                engine.abort()
            return base()

        engine.measure = measure_then_abort
        engine._phase2_cms()
        assert menu.closed
