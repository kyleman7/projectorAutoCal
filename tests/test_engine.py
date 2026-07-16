"""Unit tests for the calibration engine's closed loop.

Uses a simulated projector + display + colorimeter so the control loop can be
exercised without hardware. The simulated device model:

- WB gains act multiplicatively on the corresponding XYZ channel:
  factor = 1 + (gain - 128) / 256  (i.e. full gain range = ±50% output)
- All readings are returned in "absolute" units (scaled by a brightness factor)
  to verify the engine's relative-colorimetry normalization.
- CMS controls move measured Lab toward/away from target linearly.
"""

from __future__ import annotations

import math

import pytest

from projector_cal.color_math import get_target_lab, xyY_to_XYZ
from projector_cal.config import CalibrationConfig
from projector_cal.engine import CalibrationEngine, _MAX_WORSENING_ITERATIONS

from colormath.color_conversions import convert_color
from colormath.color_objects import LabColor, XYZColor


# D65 white XYZ at Y=100
_D65 = (95.047, 100.000, 108.883)
_CMS_AXES = ["red", "green", "blue", "cyan", "magenta", "yellow"]


def lab_to_xyz(L: float, a: float, b: float) -> tuple[float, float, float]:
    lab = LabColor(float(L), float(a), float(b), illuminant="d65")
    xyz = convert_color(lab, XYZColor, target_illuminant="d65")
    return (xyz.xyz_x * 100.0, xyz.xyz_y * 100.0, xyz.xyz_z * 100.0)


class FakeProjector:
    """Records WB/CMS settings; the measure functions read them back."""

    def __init__(self, wb=None, cms=None):
        self.wb = dict(wb or {"R": 128, "G": 128, "B": 128})
        self.cms = {axis: dict(cms or {"HUE": 0, "SAT": 0, "LUM": 0}) for axis in _CMS_AXES}
        self.set_calls = 0

    def get_wb_gain(self, ch):
        return self.wb[ch]

    def set_wb_gain(self, ch, value):
        self.wb[ch] = value
        self.set_calls += 1

    def get_cms(self, axis, prop):
        return self.cms[axis][prop]

    def set_cms(self, axis, prop, value):
        self.cms[axis][prop] = value
        self.set_calls += 1


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


def wb_measure(projector: FakeProjector, brightness: float = 0.6):
    """White-patch measurement: gains scale D65 channels; absolute cd/m² scale."""
    def _measure():
        fr = 1 + (projector.wb["R"] - 128) / 256
        fg = 1 + (projector.wb["G"] - 128) / 256
        fb = 1 + (projector.wb["B"] - 128) / 256
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
        result = engine._phase1_white_balance()
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
        result = engine._phase1_white_balance()
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
        result = engine._phase1_white_balance()
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
        report = engine.run_wb_only()
        assert report.aborted
        assert not report.wb_result.converged

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


class TestCMS:
    def test_corrections_move_toward_target(self):
        """Undersaturated/dim/rotated red: all three controls must move positive.

        This is the regression test for the inverted CMS correction signs —
        with the old signs SAT and LUM walked negative (away from target).
        """
        proj = FakeProjector()
        engine = CalibrationEngine(
            projector=proj, display=FakeDisplay(),
            measure=cms_measure(proj, "red"),
            config=make_config(),
        )
        engine._ref_white_Y = 60.0  # phase 1 normally provides this
        result = engine._cms_axis("red")

        ctl = proj.cms["red"]
        assert ctl["HUE"] > 0, f"HUE moved the wrong way: {ctl}"
        assert ctl["SAT"] > 0, f"SAT moved the wrong way: {ctl}"
        assert ctl["LUM"] > 0, f"LUM moved the wrong way: {ctl}"
        assert result.final_delta_e < result.initial_delta_e

    def test_measures_white_reference_before_cms_only_run(self):
        """run_cms_only must anchor relative colorimetry before correcting."""
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
        result = engine._phase1_white_balance()
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
        result = engine._phase1_white_balance()
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
        assert len(results) == 9
        for r in results:
            assert r.final_delta_e < 1.0, f"{r.patch_name}: ΔE={r.final_delta_e}"
        # Verify must drive the UI progress counter: one patch_start and one
        # patch_done per patch (CLAUDE.md pitfall #9)
        assert sum(1 for e in events if e["event"] == "patch_start") == 9
        assert sum(1 for e in events if e["event"] == "patch_done") == 9

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
        report = engine.run_wb_only()
        payload = _json.dumps(_report_to_dict(report))
        assert "Infinity" not in payload
