"""RGBCMY (CMS) menu driver — remote-control key presses over ESC/VP21.

The 5040UB has no CMS commands, but it accepts remote keys (`KEY xx`). The
RGBCMY menu was mapped with a webcam (2026-10-06):

  KEY 89            open the RGBCMY grid — cursor always starts on R
                    (not remembered), so every axis starts from a known state
  Down / Up         select row R, G, B, C, M, Y
  Enter             open that colour's panel, Hue selected
  Up / Down         Hue / Saturation / Brightness
  Right / Left      ±1 menu step (0–100, default 50)
  Esc               panel → grid (same row); grid → closed
  Default (KEY 88)  resets only the selected control to 50, no confirmation

The Hue slider is labelled with the neighbouring colours (red: M◀▶Y), so Right
always moves toward the next colour on the Lab hue wheel (red → yellow →
green → cyan → blue → magenta). The open panel doesn't change readings at
screen centre, so the engine measures with it open.

Values can't be read back over the network: the driver tracks how far each
control has moved, and after reset_control() it knows the absolute value.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from .projector import ProjectorClient

logger = logging.getLogger(__name__)

KEY_OPEN_RGBCMY = "89"
KEY_ESC = "05"
KEY_ENTER = "16"
KEY_UP = "35"
KEY_DOWN = "36"
KEY_LEFT = "37"
KEY_RIGHT = "38"
KEY_DEFAULT = "88"
DEFAULT_VALUE = 50

AXES = ("red", "green", "blue", "cyan", "magenta", "yellow")
CONTROLS = ("HUE", "SAT", "LUM")   # panel order: Hue, Saturation, Brightness

Control = Literal["HUE", "SAT", "LUM"]


class RgbcmyMenu:
    """Drives the RGBCMY panel for one colour at a time.

    Args:
        projector: Connected ProjectorClient.
        key_delay: Seconds to wait after each key so the OSD keeps up.
    """

    def __init__(self, projector: ProjectorClient, key_delay: float = 0.35) -> None:
        self.projector = projector
        self.key_delay = key_delay
        self._axis: str | None = None
        self._control = 0
        # Net steps moved per (axis, control) since the run started
        self.offsets: dict[str, dict[str, int]] = {a: {c: 0 for c in CONTROLS} for a in AXES}
        # Absolute menu value per (axis, control), once known (after a reset)
        self.values: dict[str, dict[str, int]] = {a: {} for a in AXES}

    def _key(self, code: str, times: int = 1) -> None:
        for _ in range(times):
            self.projector.press_key(code)
            time.sleep(self.key_delay)

    def open_axis(self, axis: str) -> None:
        """Close any open menu, open RGBCMY at R, walk down to `axis`, enter its panel."""
        if axis not in AXES:
            raise ValueError(f"unknown axis {axis!r}")
        self._key(KEY_ESC, 3)            # from any menu depth back to no menu
        self._key(KEY_OPEN_RGBCMY)
        self._key(KEY_DOWN, AXES.index(axis))
        self._key(KEY_ENTER)
        self._axis, self._control = axis, 0
        logger.debug("RGBCMY panel open for %s", axis)

    def step(self, control: Control, steps: int) -> None:
        """Select `control` in the open panel and move it by `steps` (+ = Right)."""
        if self._axis is None:
            raise RuntimeError("open_axis() first")
        target = CONTROLS.index(control)
        delta = target - self._control
        self._key(KEY_DOWN if delta > 0 else KEY_UP, abs(delta))
        self._control = target
        if steps:
            self._key(KEY_RIGHT if steps > 0 else KEY_LEFT, abs(steps))
            self.offsets[self._axis][control] += steps
            known = self.values[self._axis]
            if control in known:
                known[control] = max(0, min(100, known[control] + steps))

    def reset_control(self, control: Control) -> None:
        """Select `control` and press Default: it returns to 50 (absolute value known)."""
        self.step(control, 0)
        self._key(KEY_DEFAULT)
        self.values[self._axis][control] = DEFAULT_VALUE

    def close(self) -> None:
        """Back out of the panel and the grid (safe to call when already closed)."""
        self._key(KEY_ESC, 3)
        self._axis = None
