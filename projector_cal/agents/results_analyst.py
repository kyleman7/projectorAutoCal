"""Post-calibration results analyst agent.

Analyzes a CalibrationReport and produces plain-language findings:
  - Overall grade (Excellent / Good / Acceptable / Poor)
  - Summary paragraph
  - List of issues (e.g., which axes didn't converge, high ΔE outliers)
  - Actionable recommendations

Uses claude-opus-4-8 with adaptive thinking and structured JSON output.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary":         {"type": "string"},
        "issues":          {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "overall_grade":   {"type": "string", "enum": ["Excellent", "Good", "Acceptable", "Poor"]},
    },
    "required": ["summary", "issues", "recommendations", "overall_grade"],
    "additionalProperties": False,
}


def analyze_results(report: "CalibrationReport") -> dict:  # type: ignore[name-defined]
    """Analyze a calibration report and return structured findings.

    Args:
        report: CalibrationReport from engine.py.

    Returns:
        dict with keys: summary, issues, recommendations, overall_grade.
        Falls back to {"error": ..., "available": False} if the agent cannot run.
    """
    from .base import MODEL_OPUS, _agent_unavailable, request_structured

    patch_rows = []
    for p in report.verify_results or report.patches:
        status = "✓" if p.converged else "✗"
        patch_rows.append(
            f"  {p.patch_name:10} ΔE={p.final_delta_e:.4f} iters={p.iterations} {status}"
        )
    patch_table = "\n".join(patch_rows) if patch_rows else "  (no data)"

    target_std = "Rec.709/D65" if report.mode == "sdr" else "P3-D65"
    prompt = f"""You are a professional display calibrator analyzing the results of a closed-loop
projector calibration run on an Epson Home Cinema 5040UB.

Calibration mode: {report.mode.upper()}
Target standard: {target_std}
Delta-E threshold: {report.delta_e_threshold}
Phase completed: {report.phase}
Total iterations used: {report.total_iterations}
Converged patches: {report.converged_patches} / {len(report.patches)}

Per-patch results (verification pass):
{patch_table}

Analyze the results. Identify which axes are problematic. Distinguish between:
- Hardware limitations (projector gamut, lamp age)
- Calibration convergence issues (didn't iterate enough, algorithm instability)
- Setup issues (colorimeter placement, warm-up not complete)

Be specific and concise. Reference patch names directly.
Grade as Excellent (all ΔE < 0.5), Good (all < 1.0), Acceptable (≤2 patches > 1.0), Poor (otherwise)."""

    try:
        return request_structured(MODEL_OPUS, prompt, _OUTPUT_SCHEMA, max_tokens=1024, thinking=True)
    except (EnvironmentError, ImportError) as e:
        return _agent_unavailable(str(e))
    except Exception as e:
        logger.exception("results_analyst failed")
        return _agent_unavailable(f"Agent error: {e}")
