"""Pure color science functions: XYZ↔Lab, xyY↔XYZ, Delta-E CIE2000, calibration targets.

All Lab conversions use the D65 illuminant (not D50). Rec.709/SDR targets are D65/Rec.709;
HDR10 targets are P3-D65 (D65 white point, not DCI-native 0.314,0.351).

Critical: always cast XYZ values to float() before constructing LabColor objects to avoid
a silent numpy scalar bug in python-colormath that produces wrong Delta-E results.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as _np

# python-colormath 3.0 (unmaintained) still calls numpy.asscalar(), which was
# removed in numpy 1.23 — without this shim every delta_e call raises
# AttributeError under the project's pinned numpy>=1.24.
if not hasattr(_np, "asscalar"):
    _np.asscalar = lambda a: a.item()

from colormath.color_diff import delta_e_cie2000
from colormath.color_objects import LabColor, XYZColor
from colormath.color_conversions import convert_color

# D65 reference white (XYZ, Y=100 scale) — public so tests and simulations
# share one definition of the white point
D65_WHITE_XYZ = (95.047, 100.000, 108.883)
_D65_WHITE_xy = (0.3127, 0.3290)

# ---- Primary targets (xyY) ------------------------------------------------------
# SDR: ITU-R BT.709 primaries. HDR10: SMPTE ST 432-1 (P3) primaries, D65 white
# (not DCI native 0.314, 0.351).
_SDR_PRIMARIES_xyY: dict[str, tuple[float, float, float]] = {
    "red":   (0.6400, 0.3300, 21.26),
    "green": (0.3000, 0.6000, 71.52),
    "blue":  (0.1500, 0.0600,  7.22),
}

_HDR10_PRIMARIES_xyY: dict[str, tuple[float, float, float]] = {
    "red":   (0.6800, 0.3200, 22.90),
    "green": (0.2650, 0.6900, 69.17),
    "blue":  (0.1500, 0.0600,  7.93),
}

# Display gamma assumed when deriving neutral (grey) luminance targets from
# 8-bit patch codes. 2.2 matches the SDR calibration target for this projector.
_GAMMA = 2.2


def xyY_to_XYZ(x: float, y: float, Y: float) -> tuple[float, float, float]:
    """Convert xyY chromaticity + luminance to XYZ tristimulus values.

    Args:
        x: CIE x chromaticity
        y: CIE y chromaticity (must be > 0)
        Y: Luminance (any scale; returned XYZ uses the same scale)

    Returns:
        (X, Y, Z) tristimulus values

    Raises:
        ValueError: if y is zero (degenerate chromaticity)
    """
    if y == 0.0:
        raise ValueError("xyY_to_XYZ: y chromaticity cannot be zero")
    X = (x / y) * Y
    Z = ((1.0 - x - y) / y) * Y
    return (X, Y, Z)


def XYZ_to_xyY(X: float, Y: float, Z: float) -> tuple[float, float, float]:
    """Convert XYZ tristimulus values to xyY chromaticity + luminance.

    Returns D65 white chromaticity (0.3127, 0.3290) when X+Y+Z ≈ 0.

    Args:
        X, Y, Z: Tristimulus values (any consistent scale)

    Returns:
        (x, y, Y) where x,y are chromaticity coordinates and Y is luminance
    """
    total = X + Y + Z
    if total < 1e-10:
        # Degenerate (black) — return D65 white chromaticity with Y=0
        return (0.3127, 0.3290, Y)
    x = X / total
    y = Y / total
    return (x, y, Y)


def _grey_target_Y(code: int) -> float:
    """Luminance target (white=100) for an equal-RGB 8-bit patch code, gamma 2.2."""
    return ((code / 255.0) ** _GAMMA) * 100.0


def _build_targets(primaries: dict[str, tuple[float, float, float]]) -> dict[str, tuple[float, float, float]]:
    """Build the full 9-patch target table from a set of primaries.

    Secondaries are derived by summing the XYZ of their two constituent primaries
    (additive display model) rather than hardcoded, so they are always consistent
    with the primaries. Neutrals sit on the D65 axis with gamma-2.2 luminance.
    """
    targets: dict[str, tuple[float, float, float]] = {
        "white": (*_D65_WHITE_xy, 100.0),
        **primaries,
    }
    for name, (a, b) in (
        ("cyan",    ("green", "blue")),
        ("magenta", ("red",   "blue")),
        ("yellow",  ("red",   "green")),
    ):
        Xa, Ya, Za = xyY_to_XYZ(*primaries[a])
        Xb, Yb, Zb = xyY_to_XYZ(*primaries[b])
        targets[name] = XYZ_to_xyY(Xa + Xb, Ya + Yb, Za + Zb)
    targets["grey75"] = (*_D65_WHITE_xy, _grey_target_Y(191))
    targets["grey50"] = (*_D65_WHITE_xy, _grey_target_Y(128))
    return targets


_SDR_TARGETS_xyY = _build_targets(_SDR_PRIMARIES_xyY)
_HDR10_TARGETS_xyY = _build_targets(_HDR10_PRIMARIES_xyY)


def xyz_to_lab(X: float, Y: float, Z: float, illuminant: str = "D65") -> tuple[float, float, float]:
    """Convert XYZ (Y=100 scale) to CIE L*a*b* under the given illuminant.

    Uses python-colormath. Values are always cast to float() to avoid the numpy
    scalar bug that causes silent wrong Delta-E results.

    Args:
        X, Y, Z: Tristimulus values on Y=100 scale
        illuminant: Illuminant name (default "D65"). Only "D65" is tested.

    Returns:
        (L, a, b) as plain Python floats
    """
    # colormath's XYZColor is on the 0–1 nominal scale (reference white
    # ≈ (0.95047, 1.0, 1.08883)); passing Y=100-scale values yields L*≈522
    # for white — divide by 100 first.
    xyz = XYZColor(
        float(X) / 100.0, float(Y) / 100.0, float(Z) / 100.0,
        illuminant=illuminant.lower(),
    )
    lab: LabColor = convert_color(xyz, LabColor)
    return (float(lab.lab_l), float(lab.lab_a), float(lab.lab_b))


def delta_e_2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """Calculate CIE Delta-E 2000 between two Lab colors.

    Args:
        lab1: (L, a, b) reference color
        lab2: (L, a, b) sample color

    Returns:
        Delta-E 2000 value as a plain Python float
    """
    c1 = LabColor(float(lab1[0]), float(lab1[1]), float(lab1[2]))
    c2 = LabColor(float(lab2[0]), float(lab2[1]), float(lab2[2]))
    return float(delta_e_cie2000(c1, c2))


def get_target_xyY(patch_name: str, mode: Literal["sdr", "hdr10"]) -> tuple[float, float, float]:
    """Return the xyY calibration target for a patch in the given mode.

    Args:
        patch_name: One of: white, red, green, blue, cyan, magenta, yellow, grey75, grey50
        mode: "sdr" or "hdr10"

    Returns:
        (x, y, Y) target chromaticity and luminance

    Raises:
        KeyError: if patch_name is not in the target table for the given mode
    """
    table = _SDR_TARGETS_xyY if mode == "sdr" else _HDR10_TARGETS_xyY
    if patch_name not in table:
        raise KeyError(
            f"Unknown patch '{patch_name}' for mode '{mode}'. "
            f"Available: {sorted(table.keys())}"
        )
    return table[patch_name]


def get_target_lab(patch_name: str, mode: Literal["sdr", "hdr10"]) -> tuple[float, float, float]:
    """Return the Lab calibration target for a patch in the given mode (D65 illuminant).

    Args:
        patch_name: One of: white, red, green, blue, cyan, magenta, yellow, grey75, grey50
        mode: "sdr" or "hdr10"

    Returns:
        (L, a, b) target under D65 illuminant

    Raises:
        KeyError: if patch_name is not in the target table
    """
    # Target xyY is already relative to white = 100 — convert directly.
    # (Do NOT rescale each patch to Y=100: that would make every target as
    # bright as white, e.g. red would get L*=100 instead of ~53.)
    x, y, Y = get_target_xyY(patch_name, mode)
    X, Y, Z = xyY_to_XYZ(x, y, Y)
    return xyz_to_lab(X, Y, Z)


def patch_rgb(patch_name: str) -> tuple[int, int, int]:
    """Return the full-saturation 8-bit RGB values for a test patch.

    These are the RGB values sent to PGenerator to display the patch on screen.
    Primaries and secondaries are at maximum saturation; neutrals use equal R=G=B.

    Args:
        patch_name: Patch name (white, red, green, blue, cyan, magenta, yellow, grey75, grey50)

    Returns:
        (R, G, B) as integers in [0, 255]

    Raises:
        KeyError: if patch_name is unknown
    """
    if patch_name not in _PATCH_RGB:
        raise KeyError(
            f"Unknown patch '{patch_name}'. Available: {sorted(_PATCH_RGB.keys())}"
        )
    return _PATCH_RGB[patch_name]


_PATCH_RGB: dict[str, tuple[int, int, int]] = {
    "white":   (255, 255, 255),
    "red":     (255,   0,   0),
    "green":   (  0, 255,   0),
    "blue":    (  0,   0, 255),
    "cyan":    (  0, 255, 255),
    "magenta": (255,   0, 255),
    "yellow":  (255, 255,   0),
    "grey75":  (191, 191, 191),
    "grey50":  (128, 128, 128),
}
