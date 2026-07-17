"""Profile comparison agent.

Compares two CalibrationProfiles and explains what changed in plain language.
Used in the Profiles tab of the web UI when the user selects two profiles to compare.

Uses claude-opus-4-8 with adaptive thinking.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "narrative":       {"type": "string"},
        "delta_e_delta":   {"type": "object"},
        "verdict":         {"type": "string", "enum": ["Better", "Worse", "Similar"]},
        "notable_changes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["narrative", "delta_e_delta", "verdict", "notable_changes"],
    "additionalProperties": False,
}


def compare_profiles(profile_a: "CalibrationProfile", profile_b: "CalibrationProfile") -> dict:  # type: ignore[name-defined]
    """Compare two calibration profiles and explain the differences.

    Args:
        profile_a: Reference profile ("before").
        profile_b: Comparison profile ("after").

    Returns:
        dict with keys: narrative, delta_e_delta, verdict, notable_changes.
        Falls back to {"error": ..., "available": False} on failure.
    """
    from .base import MODEL_OPUS, _agent_unavailable, request_structured

    def _fmt_wb(gains: dict) -> str:
        return "  " + "  ".join(f"{ch}={v}" for ch, v in sorted(gains.items()))

    def _fmt_cms(cms: dict) -> str:
        lines = []
        for axis in ["red", "green", "blue", "cyan", "magenta", "yellow"]:
            props = cms.get(axis, {})
            lines.append(f"  {axis:8} HUE={props.get('HUE', '?'):4} SAT={props.get('SAT', '?'):4} LUM={props.get('LUM', '?'):4}")
        return "\n".join(lines)

    def _fmt_de(de: dict) -> str:
        return "  " + "  ".join(f"{k}={v:.3f}" for k, v in sorted(de.items()))

    # Compute ΔE delta table
    de_delta: dict[str, float] = {}
    for patch in set(list(profile_a.final_delta_e.keys()) + list(profile_b.final_delta_e.keys())):
        a_val = profile_a.final_delta_e.get(patch)
        b_val = profile_b.final_delta_e.get(patch)
        if a_val is not None and b_val is not None:
            de_delta[patch] = round(b_val - a_val, 4)

    prompt = f"""You are a professional display calibrator comparing two projector calibration profiles
for an Epson Home Cinema 5040UB.

Profile A (reference): "{profile_a.name}"
  Mode: {profile_a.mode.upper()}
  Created: {profile_a.created_at}
  White Balance:
{_fmt_wb(profile_a.wb_gains)}
  CMS:
{_fmt_cms(profile_a.cms_values)}
  Final ΔE:
{_fmt_de(profile_a.final_delta_e)}

Profile B (comparison): "{profile_b.name}"
  Mode: {profile_b.mode.upper()}
  Created: {profile_b.created_at}
  White Balance:
{_fmt_wb(profile_b.wb_gains)}
  CMS:
{_fmt_cms(profile_b.cms_values)}
  Final ΔE:
{_fmt_de(profile_b.final_delta_e)}

ΔE change (B minus A, negative = improvement):
  {de_delta}

Explain in plain language what changed between these calibrations and whether
Profile B is better, worse, or similar to Profile A. Be specific about which
axes improved or degraded. Note any large WB or CMS changes that suggest
hardware drift or a re-calibration was needed."""

    try:
        result = request_structured(MODEL_OPUS, prompt, _OUTPUT_SCHEMA, max_tokens=1024, thinking=True)
        result["delta_e_delta"] = de_delta  # inject computed table
        return result
    except (EnvironmentError, ImportError) as e:
        return _agent_unavailable(str(e))
    except Exception as e:
        logger.exception("profile_advisor failed")
        return _agent_unavailable(f"Agent error: {e}")
