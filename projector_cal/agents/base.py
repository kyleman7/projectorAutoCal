"""Shared Anthropic client, request helper, and model constants for all agents."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

MODEL_OPUS = "claude-opus-4-8"       # complex reasoning: results_analyst, profile_advisor
MODEL_HAIKU = "claude-haiku-4-5-20251001"    # fast/cheap: anomaly_detector, setup_validator

_client = None
_client_key: str | None = None


def get_client():
    """Return the shared Anthropic client (lazy init; raises ImportError if not installed).

    The env var is checked on every call — the web UI can enable/disable AI or
    swap the API key at runtime, so a cached client must not outlive its key.
    """
    global _client, _client_key

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        _client = None
        _client_key = None
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it to enable AI agent features."
        )

    if _client is None or _client_key != key:
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "anthropic package is not installed. Run: pip install anthropic"
            ) from e
        _client = anthropic.Anthropic()
        _client_key = key

    return _client


def _agent_unavailable(reason: str) -> dict:
    """Return a graceful degradation dict when the agent cannot run."""
    logger.warning("Agent unavailable: %s", reason)
    return {"error": reason, "available": False}


def request_structured(
    model: str,
    prompt: str,
    schema: dict,
    max_tokens: int,
    thinking: bool = False,
) -> dict:
    """Send a one-shot prompt with structured JSON output and parse the reply.

    Shared by all agents so the request shape (output_config json_schema
    format) and the text-block extraction live in exactly one place.

    Raises:
        EnvironmentError: ANTHROPIC_API_KEY is not set.
        ImportError: the anthropic package is not installed.
        ValueError: the response contained no text block.
        Anything the SDK raises for transport/API errors.
    """
    client = get_client()
    kwargs: dict = {"thinking": {"type": "adaptive"}} if thinking else {}
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    # Find the text content block (thinking blocks come first)
    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)
    raise ValueError("No text block in model response")
