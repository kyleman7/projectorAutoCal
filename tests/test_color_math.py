"""Unit tests for color_math.py.

Reference values validated against ICC profile specifications and known color science constants.
D65 white XYZ reference: (95.047, 100.000, 108.883)
"""

import math
import pytest

from projector_cal.color_math import (
    XYZ_to_xyY,
    xyY_to_XYZ,
    xyz_to_lab,
    delta_e_2000,
    get_target_lab,
    get_target_xyY,
    patch_rgb,
)


# ---- xyY ↔ XYZ round-trips -----------------------------------------------

class TestXyYToXYZ:
    def test_d65_white(self):
        # 4-decimal chromaticity (0.3127, 0.3290) reproduces the textbook
        # white XYZ only to ~0.03 (Z comes out 108.906 vs 108.883)
        X, Y, Z = xyY_to_XYZ(0.3127, 0.3290, 100.0)
        assert abs(X - 95.047) < 0.05
        assert Y == pytest.approx(100.0)
        assert abs(Z - 108.883) < 0.05

    def test_rec709_red(self):
        # Known: Rec.709 red at Y≈21.26 → X≈41.24, Z≈01.93
        X, Y, Z = xyY_to_XYZ(0.6400, 0.3300, 21.26)
        assert X == pytest.approx(41.24, abs=0.05)
        assert Y == pytest.approx(21.26, abs=0.01)
        assert Z == pytest.approx(1.93, abs=0.05)

    def test_zero_y_raises(self):
        with pytest.raises(ValueError, match="y chromaticity cannot be zero"):
            xyY_to_XYZ(0.3127, 0.0, 100.0)

    def test_round_trip(self):
        orig = (0.45, 0.40, 50.0)
        X, Y, Z = xyY_to_XYZ(*orig)
        x, y, Y2 = XYZ_to_xyY(X, Y, Z)
        assert x == pytest.approx(orig[0], abs=1e-6)
        assert y == pytest.approx(orig[1], abs=1e-6)
        assert Y2 == pytest.approx(orig[2], abs=1e-6)


class TestXYZToxyY:
    def test_d65_white(self):
        x, y, Y = XYZ_to_xyY(95.047, 100.0, 108.883)
        assert x == pytest.approx(0.3127, abs=0.0002)
        assert y == pytest.approx(0.3290, abs=0.0002)
        assert Y == pytest.approx(100.0)

    def test_black_returns_d65_chromaticity(self):
        x, y, Y = XYZ_to_xyY(0.0, 0.0, 0.0)
        assert x == pytest.approx(0.3127)
        assert y == pytest.approx(0.3290)
        assert Y == pytest.approx(0.0)


# ---- xyz_to_lab ---------------------------------------------------------------

class TestXyzToLab:
    def test_d65_white_is_100_0_0(self):
        """D65 white XYZ should convert to L*=100, a*≈0, b*≈0."""
        L, a, b = xyz_to_lab(95.047, 100.0, 108.883)
        assert L == pytest.approx(100.0, abs=0.01)
        assert a == pytest.approx(0.0, abs=0.1)
        assert b == pytest.approx(0.0, abs=0.1)

    def test_black_is_0_0_0(self):
        L, a, b = xyz_to_lab(0.0, 0.0, 0.0)
        assert L == pytest.approx(0.0, abs=0.01)

    def test_returns_floats(self):
        result = xyz_to_lab(50.0, 50.0, 50.0)
        for v in result:
            assert isinstance(v, float)


# ---- delta_e_2000 -------------------------------------------------------------

class TestDeltaE2000:
    def test_identical_colors_is_zero(self):
        lab = (50.0, 10.0, -10.0)
        assert delta_e_2000(lab, lab) == pytest.approx(0.0, abs=1e-6)

    def test_known_value(self):
        # Reference pair from Sharma et al. 2005 Table 1, pair 1:
        # lab1=(50.0000, 2.6772, -79.7751), lab2=(50.0000, 0.0000, -82.7485) → ΔE=2.0425
        lab1 = (50.0000, 2.6772, -79.7751)
        lab2 = (50.0000, 0.0000, -82.7485)
        de = delta_e_2000(lab1, lab2)
        assert de == pytest.approx(2.0425, abs=0.001)

    def test_large_difference(self):
        white = (100.0, 0.0, 0.0)
        black = (0.0, 0.0, 0.0)
        assert delta_e_2000(white, black) > 50

    def test_returns_float(self):
        result = delta_e_2000((50.0, 0.0, 0.0), (50.0, 1.0, 0.0))
        assert isinstance(result, float)


# ---- get_target_lab / get_target_xyY ------------------------------------------

class TestTargets:
    def test_sdr_white_is_d65(self):
        x, y, Y = get_target_xyY("white", "sdr")
        assert x == pytest.approx(0.3127)
        assert y == pytest.approx(0.3290)

    def test_hdr10_white_is_d65_not_dci(self):
        """HDR10 white point must be D65, not DCI native (0.314, 0.351)."""
        x, y, Y = get_target_xyY("white", "hdr10")
        assert x == pytest.approx(0.3127)
        assert y == pytest.approx(0.3290)

    def test_sdr_white_lab_is_near_100(self):
        L, a, b = get_target_lab("white", "sdr")
        assert L == pytest.approx(100.0, abs=0.1)
        assert a == pytest.approx(0.0, abs=0.5)
        assert b == pytest.approx(0.0, abs=0.5)

    def test_hdr10_red_is_p3(self):
        x, y, _ = get_target_xyY("red", "hdr10")
        assert x == pytest.approx(0.680)
        assert y == pytest.approx(0.320)

    def test_sdr_red_is_rec709(self):
        x, y, _ = get_target_xyY("red", "sdr")
        assert x == pytest.approx(0.640)
        assert y == pytest.approx(0.330)

    def test_all_sdr_patches_present(self):
        patches = ["white", "red", "green", "blue", "cyan", "magenta", "yellow", "grey75", "grey50"]
        for p in patches:
            lab = get_target_lab(p, "sdr")
            assert len(lab) == 3

    def test_all_hdr10_patches_present(self):
        patches = ["white", "red", "green", "blue", "cyan", "magenta", "yellow", "grey75", "grey50"]
        for p in patches:
            lab = get_target_lab(p, "hdr10")
            assert len(lab) == 3

    def test_unknown_patch_raises(self):
        with pytest.raises(KeyError, match="Unknown patch"):
            get_target_lab("infrared", "sdr")

    def test_sdr_hdr10_red_differ(self):
        """P3-D65 red and Rec.709 red should have different Lab values."""
        sdr = get_target_lab("red", "sdr")
        hdr = get_target_lab("red", "hdr10")
        assert delta_e_2000(sdr, hdr) > 1.0

    @pytest.mark.parametrize("mode", ["sdr", "hdr10"])
    @pytest.mark.parametrize("secondary,components", [
        ("cyan", ("green", "blue")),
        ("magenta", ("red", "blue")),
        ("yellow", ("red", "green")),
    ])
    def test_secondaries_are_sum_of_primaries(self, mode, secondary, components):
        """Additive display model: secondary XYZ must equal the sum of its primaries."""
        Xs, Ys, Zs = xyY_to_XYZ(*get_target_xyY(secondary, mode))
        expected = [0.0, 0.0, 0.0]
        for prim in components:
            for i, v in enumerate(xyY_to_XYZ(*get_target_xyY(prim, mode))):
                expected[i] += v
        assert Xs == pytest.approx(expected[0], abs=0.01)
        assert Ys == pytest.approx(expected[1], abs=0.01)
        assert Zs == pytest.approx(expected[2], abs=0.01)

    def test_hdr10_yellow_matches_p3_primaries(self):
        """Regression: the old hardcoded HDR10 yellow was (0.4230, 0.5050) —
        several ΔE away from what the P3 primaries actually sum to."""
        x, y, _ = get_target_xyY("yellow", "hdr10")
        assert x == pytest.approx(0.4379, abs=0.001)
        assert y == pytest.approx(0.5359, abs=0.001)

    def test_secondary_plus_primary_is_white(self):
        """cyan + red = white for an additive display."""
        Xc, Yc, Zc = xyY_to_XYZ(*get_target_xyY("cyan", "sdr"))
        Xr, Yr, Zr = xyY_to_XYZ(*get_target_xyY("red", "sdr"))
        Xw, Yw, Zw = xyY_to_XYZ(*get_target_xyY("white", "sdr"))
        assert Xc + Xr == pytest.approx(Xw, abs=0.15)
        assert Yc + Yr == pytest.approx(Yw, abs=0.15)
        assert Zc + Zr == pytest.approx(Zw, abs=0.15)

    def test_grey_targets_use_display_gamma(self):
        """Grey luminance targets follow gamma 2.2 (not the old power-2.0 values)."""
        _, _, y75 = get_target_xyY("grey75", "sdr")
        _, _, y50 = get_target_xyY("grey50", "sdr")
        assert y75 == pytest.approx(((191 / 255) ** 2.2) * 100, abs=0.01)
        assert y50 == pytest.approx(((128 / 255) ** 2.2) * 100, abs=0.01)


# ---- patch_rgb ----------------------------------------------------------------

class TestPatchRgb:
    def test_white_is_255_255_255(self):
        assert patch_rgb("white") == (255, 255, 255)

    def test_red_is_pure_red(self):
        assert patch_rgb("red") == (255, 0, 0)

    def test_cyan_is_green_plus_blue(self):
        r, g, b = patch_rgb("cyan")
        assert r == 0 and g == 255 and b == 255

    def test_grey75_is_roughly_191(self):
        r, g, b = patch_rgb("grey75")
        assert r == g == b
        assert 185 <= r <= 195

    def test_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown patch"):
            patch_rgb("ultraviolet")
