"""Picture-setting registry for the Epson 5040UB.

Tokens come from Epson's ESC/VP21 command list (sheet "TW9300", which covers the
HC5040UB). The registry is used to snapshot the full picture state (run
baseline, profiles), restore it, neutralize settings that would bias a
calibration, and step settings by hand during guided phases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .projector import ProjectorClient, ProjectorError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    token: str
    label: str
    param: str | None = None     # e.g. GAMMALV's tone index "00"–"08"
    neutral: str | None = None   # applied by the run baseline; None = record only

    @property
    def key(self) -> str:
        """Snapshot key: the token, plus the parameter when there is one."""
        return self.token if self.param is None else f"{self.token} {self.param}"


GAMMA_POINTS = 9  # GAMMALV tones 00–08 (custom gamma, active when GAMMA = F0)
GAMMA_CUSTOM = "F0"

# Apply order matters: Epson stores most image settings per color mode, so CMODE
# goes first, and the gamma preset precedes the custom gamma points.
PICTURE_SETTINGS: tuple[Setting, ...] = (
    Setting("CMODE", "Color mode"),
    Setting("DYNRANGE", "Dynamic range"),
    Setting("LUMINANCE", "Lamp power"),
    Setting("BRIGHT", "Brightness"),
    Setting("CONTRAST", "Contrast"),
    # INIT restores the factory default, which is the neutral midpoint
    Setting("DENSITY", "Color saturation", neutral="INIT"),
    Setting("TINT", "Tint", neutral="INIT"),
    Setting("FCOLOR", "Skin tone", neutral="INIT"),
    Setting("CTEMP", "Color temperature"),
    Setting("GAINR", "Gain R"),
    Setting("GAING", "Gain G"),
    Setting("GAINB", "Gain B"),
    Setting("OFFSETR", "Offset R"),
    Setting("OFFSETG", "Offset G"),
    Setting("OFFSETB", "Offset B"),
    Setting("GAMMA", "Gamma"),
    *(Setting("GAMMALV", f"Custom gamma point {i + 1}", param=f"{i:02d}")
      for i in range(GAMMA_POINTS)),
    # Processing that can shift measured light between patches
    Setting("IMGPRESET", "Image enhancement preset", neutral="00"),
    Setting("MCFI", "Frame interpolation", neutral="00"),
    Setting("NRS", "Noise reduction"),
    Setting("MPEGNRS", "MPEG noise reduction"),
)

LABELS: dict[str, str] = {s.key: s.label for s in PICTURE_SETTINGS}


def read_settings(projector: ProjectorClient) -> dict[str, str]:
    """Snapshot every registry setting the projector answers; skips ones it rejects."""
    values: dict[str, str] = {}
    for s in PICTURE_SETTINGS:
        try:
            values[s.key] = projector.get_setting(s.token, s.param)
        except ProjectorError as e:
            logger.debug("Setting %s not readable: %s", s.key, e)
    return values


def apply_settings(projector: ProjectorClient, values: dict[str, str]) -> list[str]:
    """Write a snapshot back in dependency order; return the keys that failed."""
    failed: list[str] = []
    for s in PICTURE_SETTINGS:
        if s.key not in values:
            continue
        try:
            projector.set_setting(s.token, values[s.key], s.param)
        except ProjectorError as e:
            logger.warning("Could not restore %s=%s: %s", s.key, values[s.key], e)
            failed.append(s.key)
    return failed


def neutralize(projector: ProjectorClient) -> dict[str, str]:
    """Apply the neutral value of every setting that has one; return what was applied."""
    applied: dict[str, str] = {}
    for s in PICTURE_SETTINGS:
        if s.neutral is None:
            continue
        try:
            projector.set_setting(s.token, s.neutral, s.param)
            applied[s.key] = s.neutral
        except ProjectorError as e:
            logger.warning("Could not neutralize %s: %s", s.key, e)
    return applied


def nudge(projector: ProjectorClient, token: str, direction: int,
          param: str | None = None, max_tries: int = 6) -> int:
    """Move a quantized 0–255 setting one visible notch up (+1) or down (-1).

    The 5040UB stores values in ~2.5-unit steps and silently ignores smaller
    writes, so step the raw value until the read-back changes. Returns the
    stored value (unchanged at the end of the range).
    """
    before = int(projector.get_setting(token, param))
    for k in range(1, max_tries + 1):
        target = before + direction * k
        if not 0 <= target <= 255:
            break
        projector.set_setting(token, target, param)
        after = int(projector.get_setting(token, param))
        if after != before:
            return after
    return before
