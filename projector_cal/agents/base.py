"""Shared Anthropic client and model constants for all calibration agents."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MODEL_OPUS = "claude-opus-4-6"       # complex reasoning: results_analyst, profile_advisor
MODEL_HAIKU = "claude-haiku-4-5-20251001"    # fast/cheap: anomaly_detector, setup_validator

_client = None


def get_client():
    """Return the shared Anthropic client (lazy init; raises ImportError if not installed)."""
    global _client
    if _client is not None:
        return _client

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it to enable AI agent features."
        )

    try:
        import anthropic
        _client = anthropic.Anthropic()
        return _client
    except ImportError as e:
        raise ImportError(
            "anthropic package is not installed. Run: pip install anthropic"
        ) from e


def _agent_unavailable(reason: str) -> dict:
    """Return a graceful degradation dict when the agent cannot run."""
    logger.warning("Agent unavailable: %s", reason)
    return {"error": reason, "available": False}
