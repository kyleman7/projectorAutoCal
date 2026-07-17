"""Live measurement anomaly detector agent.

Called by the engine after every 3rd measurement iteration if the readings look
unstable (Y std dev > 2% across the last 3 readings). Uses claude-haiku-4-5 for
speed and cost efficiency — this is called in real time during calibration.

Returns an action for the engine to take:
  "continue"          — readings look normal, proceed
  "retry_measurement" — re-take measurements for this patch
  "pause_and_check"   — something is wrong; notify user and pause
  "abort_patch"       — skip this patch entirely
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

_Y_VARIANCE_THRESHOLD = 0.02   # 2% — below this, don't bother calling the LLM

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "anomaly": {"type": "boolean"},
        "type":    {"type": ["string", "null"]},
        "action":  {"type": "string", "enum": ["continue", "retry_measurement", "pause_and_check", "abort_patch"]},
        "reason":  {"type": "string"},
    },
    "required": ["anomaly", "type", "action", "reason"],
    "additionalProperties": False,
}


def _y_variance(xyz_list: list[tuple[float, float, float]]) -> float:
    if len(xyz_list) < 2:
        return 0.0
    ys = [t[1] for t in xyz_list]
    mean = sum(ys) / len(ys)
    if mean < 1e-9:
        return 0.0
    variance = sum((y - mean) ** 2 for y in ys) / len(ys)
    return math.sqrt(variance) / mean


def detect_anomaly(
    patch_name: str,
    recent_xyz: list[tuple[float, float, float]],
    recent_delta_e: list[float],
) -> dict:
    """Detect measurement anomalies during live calibration.

    Only calls the LLM if Y variance is elevated (> 2%). Below that threshold,
    returns {"anomaly": False, "action": "continue"} immediately.

    Args:
        patch_name: Current patch being calibrated (e.g., "red").
        recent_xyz: Last 3–5 XYZ readings for this patch.
        recent_delta_e: Corresponding ΔE values.

    Returns:
        dict with keys: anomaly (bool), type (str|None), action (str), reason (str).
    """
    from .base import MODEL_HAIKU, request_structured

    variance = _y_variance(recent_xyz)
    if variance < _Y_VARIANCE_THRESHOLD:
        return {
            "anomaly": False,
            "type": None,
            "action": "continue",
            "reason": f"Y variance {variance:.2%} is below threshold",
        }

    xyz_str = "\n".join(
        f"  Reading {i+1}: X={x:.3f} Y={y:.3f} Z={z:.3f} ΔE={de:.3f}"
        for i, ((x, y, z), de) in enumerate(zip(recent_xyz, recent_delta_e))
    )

    prompt = f"""You are monitoring a projector calibration session. Analyze these measurements
for the '{patch_name}' patch and determine if there is an anomaly.

Recent readings:
{xyz_str}

Y luminance variance: {variance:.2%}

Common anomaly types:
- lamp_flicker: Y varies > 5% with no clear trend
- convergence_failure: ΔE is increasing across iterations instead of decreasing
- measurement_noise: single outlier reading, others consistent
- hardware_limit: ΔE is stuck > 2.0 after 5+ iterations (projector gamut limit)

Recommend an action: continue, retry_measurement, pause_and_check, or abort_patch."""

    try:
        return request_structured(MODEL_HAIKU, prompt, _OUTPUT_SCHEMA, max_tokens=256)
    except (EnvironmentError, ImportError) as e:
        # If agent unavailable, default to continue — don't block calibration
        logger.warning("anomaly_detector unavailable: %s — defaulting to continue", e)
        return {"anomaly": False, "type": None, "action": "continue",
                "reason": f"Agent unavailable: {e}"}
    except Exception as e:
        logger.exception("anomaly_detector failed")
        return {"anomaly": False, "type": None, "action": "continue",
                "reason": f"Agent error: {e}"}
