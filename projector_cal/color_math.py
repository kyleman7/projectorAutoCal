"""Pure color science functions: XYZ↔Lab, xyY↔XYZ, Delta-E CIE2000, calibration targets.

All Lab conversions use the D65 illuminant (not D50). Rec.709/SDR targets are D65/Rec.709;
HDR10 targets are P3-D65 (D65 white point, not DCI-native 0.314,0.351).

Critical: always cast XYZ values to float() before constructing LabColor objects to avoid
a silent numpy scalar bug in python-colormath that produces wrong Delta-E results.
"""

from __future__ import annotations

import math
from typing import Literal

from colormath.color_diff import delta_e_cie2000
from colormath.color_objects import LabColor, XYZColor
from colormath.color_conversions import convert_color

# D65 reference white (XYZ, Y=100 scale)
_D65_XYZ = (95.047, 100.000, 108.883)

# ---- SDR targets: Rec.709 primaries, D65 white (xyY) ----------------------------
# Source: ITU-R BT.709
_SDR_TARGETS_xyY: dict[str, tuple[float, float, float]] = {
    "white":   (0.3127, 0.3290, 100.0),
    "red":     (0.6400, 0.3300,  21.26),
    "green":   (0.3000, 0.6000,  71.52),
    "blue":    (0.1500, 0.0600,   7.22),
    # Secondaries derived from Rec.709 primaries
    "cyan":    (0.2254, 0.3288,  78.74),   # G+B
    "magenta": (0.3209, 0.1542,  28.48),   # R+B
    "yellow":  (0.4193, 0.5053,  92.78),   # R+G
    # Neutrals (on the achromatic axis, just luminance varies)
    "grey75":  (0.3127, 0.3290,  56.25),
    "grey50":  (0.3127, 0.3290,  25.00),
}

# ---- HDR10 targets: DCI P3 primaries, D65 white (xyY) ---------------------------
# Source: SMPTE ST 432-1 primaries + D65 white (not DCI native 0.314, 0.351)
_HDR10_TARGETS_xyY: dict[str, tuple[float, float, float]] = {
    "white":   (0.3127, 0.3290, 100.0),
    "red":     (0.6800, 0.3200,  22.90),
    "green":   (0.2650, 0.6900,  69.17),
    "blue":    (0.1500, 0.0600,   7.93),
    # Secondaries derived from P3 primaries
    "cyan":    (0.2245, 0.3275,  77.10),   # G+B
    "magenta": (0.3233, 0.1542,  30.83),   # R+B
    "yellow":  (0.4230, 0.5050,  92.07),   # R+G
    "grey75":  (0.3127, 0.3290,  56.25),
    "grey50":  (0.3127, 0.3290,  25.00),
}


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
    xyz = XYZColor(float(X), float(Y), float(Z), illuminant=illuminant.lower())
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
    x, y, Y = get_target_xyY(patch_name, mode)
    X, _, Z = xyY_to_XYZ(x, y, Y)
    # Normalize to Y=100 scale
    scale = 100.0 / Y if Y > 0 else 1.0
    return xyz_to_lab(X * scale, 100.0, Z * scale)


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
    _RGB: dict[str, tuple[int, int, int]] = {
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
    if patch_name not in _RGB:
        raise KeyError(
            f"Unknown patch '{patch_name}'. Available: {sorted(_RGB.keys())}"
        )
    return _RGB[patch_name]
