"""Pre-flight setup validation agent.

Validates that all hardware is connected, the command table is populated, the
projector has warmed up, and the colorimeter is detected — before unlocking
the "Start Calibration" button in the Setup Wizard Step 4.

Uses claude-haiku-4-5 (simple classification task, no complex reasoning needed).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ready":            {"type": "boolean"},
        "checklist": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item":   {"type": "string"},
                    "pass":   {"type": "boolean"},
                    "note":   {"type": "string"},
                },
                "required": ["item", "pass", "note"],
                "additionalProperties": False,
            },
        },
        "blocking_issues":  {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ready", "checklist", "blocking_issues"],
    "additionalProperties": False,
}


def validate_setup(
    projector_connected: bool,
    pgen_connected: bool,
    command_table: dict,
    warm_up_stable: bool,
    colorimeter_detected: bool,
    screen_info: dict,
) -> dict:
    """Run pre-flight validation and return a structured checklist.

    Args:
        projector_connected: True if ESC/VP.net handshake succeeded.
        pgen_connected: True if PGenerator TCP connection succeeded.
        command_table: Loaded command_table.json dict.
        warm_up_stable: True if Y variance < 0.5% over last 3 warm-up readings.
        colorimeter_detected: True if spotread process launched successfully.
        screen_info: Dict from Setup Wizard Step 2 (size, material, throw distance).

    Returns:
        dict with keys: ready (bool), checklist (list), blocking_issues (list).
        Falls back gracefully if agent unavailable.
    """
    from ..probe import is_command_table_complete
    from .base import MODEL_HAIKU, request_structured

    # Check command table completeness locally (no LLM needed for this)
    command_table_complete = is_command_table_complete(command_table)

    screen_desc = (
        f"Diagonal: {screen_info.get('diagonal_inches', 'unknown')} inches, "
        f"Material: {screen_info.get('material', 'unknown')}, "
        f"Throw distance: {screen_info.get('throw_distance', 'unknown')}"
    ) if screen_info else "Screen info not provided"

    prompt = f"""You are validating the hardware setup for an Epson 5040UB projector calibration session.
Evaluate each checklist item and determine if the system is ready to start calibration.

Hardware status:
  Projector connected (ESC/VP.net handshake): {projector_connected}
  PGenerator connected (TCP): {pgen_connected}
  Command table complete (all WB + CMS tokens): {command_table_complete}
  Projector warmed up and stable (Y variance < 0.5%): {warm_up_stable}
  Colorimeter (spotread) detected: {colorimeter_detected}
  Screen info: {screen_desc}

A blocking issue is anything that will prevent calibration from running:
  - projector not connected
  - PGenerator not connected
  - command table incomplete (run probe first)
  - colorimeter not detected

Non-blocking warnings (calibration can proceed but quality may suffer):
  - projector not fully warmed up (recommend 30-min warm-up for best results)
  - screen info missing

Produce a checklist with pass/fail for each item and a brief note."""

    try:
        return request_structured(MODEL_HAIKU, prompt, _OUTPUT_SCHEMA, max_tokens=512)
    except (EnvironmentError, ImportError):
        # No API key / SDK — quiet fallback, the rule-based path is fully capable
        return _rule_based_validate(
            projector_connected, pgen_connected, command_table_complete,
            warm_up_stable, colorimeter_detected, screen_info,
        )
    except Exception:
        logger.exception("setup_validator failed; falling back to rule-based")
        return _rule_based_validate(
            projector_connected, pgen_connected, command_table_complete,
            warm_up_stable, colorimeter_detected, screen_info,
        )


def _rule_based_validate(
    projector_connected: bool,
    pgen_connected: bool,
    command_table_complete: bool,
    warm_up_stable: bool,
    colorimeter_detected: bool,
    screen_info: dict,
) -> dict:
    """Rule-based fallback when the agent is unavailable."""
    checklist = [
        {
            "item": "Projector connected",
            "pass": projector_connected,
            "note": "ESC/VP.net handshake succeeded" if projector_connected
                    else "Cannot connect to projector — check IP and ensure projector is on",
        },
        {
            "item": "PGenerator connected",
            "pass": pgen_connected,
            "note": "TCP connection to Raspberry Pi succeeded" if pgen_connected
                    else "Cannot connect to PGenerator — check IP and ensure pgen_client is running",
        },
        {
            "item": "Command table complete",
            "pass": command_table_complete,
            "note": "All WB and CMS tokens present" if command_table_complete
                    else "Run probe first to auto-discover command tokens",
        },
        {
            "item": "Colorimeter detected",
            "pass": colorimeter_detected,
            "note": "spotread launched successfully" if colorimeter_detected
                    else "Check USB connection and ArgyllCMS installation",
        },
        {
            "item": "Projector warmed up",
            "pass": warm_up_stable,
            "note": "Luminance stable (< 0.5% variance)" if warm_up_stable
                    else "Wait for projector to warm up (30 min recommended for best results)",
        },
        {
            "item": "Screen info provided",
            "pass": bool(screen_info),
            "note": "Screen details entered in Setup Wizard Step 2" if screen_info
                    else "Optional: add screen info for reference",
        },
    ]
    blocking = [
        item["note"]
        for item in checklist
        if not item["pass"] and item["item"] in (
            "Projector connected", "PGenerator connected",
            "Command table complete", "Colorimeter detected",
        )
    ]
    return {
        "ready": not blocking,
        "checklist": checklist,
        "blocking_issues": blocking,
    }
