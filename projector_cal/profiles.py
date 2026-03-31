"""Calibration profile save/load/apply/list/delete.

A profile captures all WB gains and CMS values for one mode at one point in time,
plus the final verification ΔE values. Applying a profile re-sends every command
to the projector without re-running calibration.

Profiles are stored as plain JSON files in the `profiles/` directory.
File names: `{safe_name}_{mode}_{timestamp}.json`
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .projector import ProjectorClient

logger = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).parent.parent / "profiles"


@dataclass
class CalibrationProfile:
    """Complete projector state snapshot for one calibration mode."""
    name: str
    mode: Literal["sdr", "hdr10"]
    created_at: str                  # ISO 8601 UTC timestamp
    wb_gains: dict[str, int]         # {"R": 128, "G": 131, "B": 124}
    cms_values: dict[str, dict]      # {"red": {"HUE": 0, "SAT": -3, "LUM": 2}, ...}
    final_delta_e: dict[str, float]  # {"white": 0.42, "red": 0.71, ...}
    screen_info: dict = field(default_factory=dict)  # size, gain, throw distance from wizard


def save_profile(profile: CalibrationProfile, directory: Path | None = None) -> Path:
    """Persist a CalibrationProfile to disk as JSON.

    File name: `{safe_name}_{mode}_{timestamp}.json`

    Args:
        profile: Profile to save.
        directory: Directory to save into. Defaults to `profiles/`.

    Returns:
        Path of the written file.
    """
    directory = directory or _PROFILES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(profile.name)
    timestamp = profile.created_at.replace(":", "").replace("-", "").replace("T", "_")[:15]
    filename = f"{safe_name}_{profile.mode}_{timestamp}.json"
    path = directory / filename
    with open(path, "w") as f:
        json.dump(asdict(profile), f, indent=2)
    logger.info("Profile saved: %s", path)
    return path


def load_profile(path: Path) -> CalibrationProfile:
    """Load a CalibrationProfile from a JSON file.

    Args:
        path: Path to the profile JSON file.

    Returns:
        CalibrationProfile dataclass.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the JSON is missing required fields.
    """
    with open(path) as f:
        data = json.load(f)
    required = ("name", "mode", "created_at", "wb_gains", "cms_values", "final_delta_e")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Profile {path} is missing required fields: {missing}")
    return CalibrationProfile(
        name=data["name"],
        mode=data["mode"],
        created_at=data["created_at"],
        wb_gains=data["wb_gains"],
        cms_values=data["cms_values"],
        final_delta_e=data["final_delta_e"],
        screen_info=data.get("screen_info", {}),
    )


def list_profiles(directory: Path | None = None) -> list[CalibrationProfile]:
    """Return all saved profiles sorted by creation date (newest first).

    Args:
        directory: Directory to scan. Defaults to `profiles/`.

    Returns:
        List of CalibrationProfile objects. Returns empty list if directory doesn't exist.
    """
    directory = directory or _PROFILES_DIR
    if not directory.exists():
        return []
    profiles = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            profiles.append(load_profile(path))
        except Exception as e:
            logger.warning("Skipping invalid profile %s: %s", path.name, e)
    return profiles


def delete_profile(path: Path) -> None:
    """Delete a profile JSON file.

    Args:
        path: Path to the profile JSON file.

    Raises:
        FileNotFoundError: if the file does not exist.
    """
    path.unlink()
    logger.info("Profile deleted: %s", path)


def apply_profile(profile: CalibrationProfile, projector: ProjectorClient) -> None:
    """Send all WB gain and CMS commands from a profile to the projector.

    Does not re-run calibration — just re-applies the stored correction values.
    The projector must already be connected and in the correct picture mode.

    Args:
        profile: Profile whose values to apply.
        projector: Connected ProjectorClient.
    """
    logger.info(
        "Applying profile '%s' (mode=%s, created=%s)",
        profile.name, profile.mode, profile.created_at,
    )

    # White balance gains
    for ch in ("R", "G", "B"):
        value = profile.wb_gains.get(ch)
        if value is not None:
            projector.set_wb_gain(ch, value)
            logger.debug("Applied WB.%s = %d", ch, value)

    # CMS values
    for axis, props in profile.cms_values.items():
        for prop in ("HUE", "SAT", "LUM"):
            value = props.get(prop)
            if value is not None:
                projector.set_cms(axis, prop, value)
                logger.debug("Applied CMS.%s.%s = %d", axis, prop, value)

    logger.info("Profile '%s' applied successfully.", profile.name)


def profile_from_projector(
    name: str,
    mode: Literal["sdr", "hdr10"],
    projector: ProjectorClient,
    final_delta_e: dict[str, float] | None = None,
    screen_info: dict | None = None,
) -> CalibrationProfile:
    """Read the current WB gains and CMS values from the projector and build a profile.

    Args:
        name: Human-readable profile name.
        mode: "sdr" or "hdr10".
        projector: Connected ProjectorClient.
        final_delta_e: Optional ΔE table from the last verification pass.
        screen_info: Optional dict with screen size, gain, throw distance.

    Returns:
        CalibrationProfile populated with current projector state.
    """
    wb_gains = {
        "R": projector.get_wb_gain("R"),
        "G": projector.get_wb_gain("G"),
        "B": projector.get_wb_gain("B"),
    }

    cms_axes = ["red", "green", "blue", "cyan", "magenta", "yellow"]
    cms_values: dict[str, dict] = {}
    for axis in cms_axes:
        cms_values[axis] = {
            "HUE": projector.get_cms(axis, "HUE"),
            "SAT": projector.get_cms(axis, "SAT"),
            "LUM": projector.get_cms(axis, "LUM"),
        }

    return CalibrationProfile(
        name=name,
        mode=mode,
        created_at=datetime.now(timezone.utc).isoformat(),
        wb_gains=wb_gains,
        cms_values=cms_values,
        final_delta_e=final_delta_e or {},
        screen_info=screen_info or {},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Convert an arbitrary string to a safe filename component."""
    safe = re.sub(r"[^\w\-]", "_", name.strip())
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:64] or "profile"
