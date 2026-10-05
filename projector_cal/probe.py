"""ESC/VP21 command token auto-discovery for the Epson 5040UB.

Run once before first calibration to populate command_table.json with the actual
command tokens that this specific projector firmware accepts.

Algorithm:
  For each required command slot, try each candidate token via a query (CANDIDATE?\r).
  If the projector returns CANDIDATE=VALUE:, the token is valid and recorded.
  If it returns ERR, the token is rejected.
  Writes a verified command_table.json when done.

The 5040UB has no RGBCMY (CMS) commands over ESC/VP21 (Epson's command list,
"TW9300" sheet), so CMS is not probed — phase 2 of the engine guides the user
through the projector menu instead.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .projector import ProjectorClient, ProjectorError

logger = logging.getLogger(__name__)

_INTER_PROBE_SLEEP = 0.1  # seconds between probe queries to avoid flooding projector

# ---------------------------------------------------------------------------
# Candidate tables
# GAINx is Epson's documented token; the rest are guesses kept for other
# firmware versions.
# ---------------------------------------------------------------------------

WB_CANDIDATES: dict[str, list[str]] = {
    "R": ["GAINR", "WBALGAINR", "WBGAINR", "CRED", "WBRED", "BCRED", "RGAIN"],
    "G": ["GAING", "WBALGAING", "WBGAING", "CGRN", "WBGRN", "BCGRN", "GGAIN"],
    "B": ["GAINB", "WBALGAINB", "WBGAINB", "CBLU", "WBBLU", "BCBLU", "BGAIN"],
}

# Picture-mode switching on the 5040UB: CMODE selects the color mode, DYNRANGE
# the HDR mode (00 Auto, 01 SDR, 02–05 HDR Mode 1–4). Each mode maps to the
# [command, value] pairs sent in order by ProjectorClient.set_picture_mode.
# SDR deliberately leaves DYNRANGE alone: forcing it would also force every
# later HDR source to SDR.
PICTURE_MODE_PRESETS: dict[str, list[list[str]]] = {
    "sdr":   [["CMODE", "07"]],      # Natural — closest to Rec.709
    "hdr10": [["DYNRANGE", "05"]],   # HDR Mode 4 — darkest / most accurate
}


def is_command_table_complete(table: dict) -> bool:
    """True when every required WB slot has a verified token.

    Shared by the setup validator and the /api/setup/status endpoint so
    "ready to calibrate" means the same thing everywhere.
    """
    # The shipped placeholder file carries a "_note" marker; its tokens are
    # unverified against this projector — treat as not probed.
    if table.get("_note"):
        return False
    return all(table.get("white_balance", {}).get(ch) for ch in ("R", "G", "B"))


@dataclass
class SlotResult:
    """Result for one required command slot."""
    slot_id: str              # e.g., "wb.R"
    accepted_token: str | None = None
    current_value: str | None = None
    candidates_tried: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.accepted_token is not None


@dataclass
class ProbeResult:
    """Full result from a probe run."""
    wb_slots: dict[str, SlotResult] = field(default_factory=dict)      # "R", "G", "B"
    picture_mode: dict[str, list[list[str]]] = field(default_factory=dict)

    def missing_slots(self) -> list[str]:
        """Return list of required slot IDs that were not resolved."""
        return [f"white_balance.{ch}" for ch, slot in self.wb_slots.items() if not slot.found]

    def to_command_table(self) -> dict:
        """Serialize to command_table.json schema.

        Only includes slots where a token was found; unresolved slots are left
        out so the user can fill them in manually if needed.
        """
        table: dict = {"white_balance": {}, "picture_mode": dict(self.picture_mode)}
        for ch in ("R", "G", "B"):
            slot = self.wb_slots.get(ch)
            if slot and slot.accepted_token:
                table["white_balance"][ch] = slot.accepted_token
        return table

    def summary_lines(self) -> list[str]:
        """Human-readable probe summary for logging / web UI display."""
        lines: list[str] = []
        lines.append("=== Probe Results ===")

        lines.append("\nWhite Balance:")
        for ch in ("R", "G", "B"):
            slot = self.wb_slots.get(ch)
            if slot and slot.found:
                lines.append(f"  WB.{ch}  ✓  token={slot.accepted_token!r}  current={slot.current_value}")
            else:
                tried = slot.candidates_tried if slot else []
                lines.append(f"  WB.{ch}  ✗  (tried: {tried})")

        lines.append("\nPicture Mode:")
        if self.picture_mode:
            for mode, steps in self.picture_mode.items():
                lines.append(f"  {mode}: " + ", ".join(f"{c} {v}" for c, v in steps))
        else:
            lines.append("  ✗ CMODE/DYNRANGE not supported — switch modes manually")

        lines.append("\nCMS: not available over ESC/VP21 — guided manual adjustment in phase 2")

        missing = self.missing_slots()
        if missing:
            lines.append(f"\n⚠ Missing slots ({len(missing)}): {missing}")
        else:
            lines.append("\n✓ All required slots resolved.")
        return lines


def run_probe(
    projector: ProjectorClient,
    candidates: dict | None = None,
    progress_cb: "((str) -> None) | None" = None,
) -> ProbeResult:
    """Auto-discover ESC/VP21 command tokens for the connected projector.

    Args:
        projector: Connected ProjectorClient instance.
        candidates: Override the built-in candidate tables (for testing).
                    If None, uses WB_CANDIDATES.
        progress_cb: Optional callback called with status strings as the probe runs.
                     Used by the web server to stream progress via WebSocket.

    Returns:
        ProbeResult with all discovered token mappings.
    """
    def _report(msg: str) -> None:
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    # probe_command() treats a timeout like ERR, so over a dead link every
    # candidate reads as "rejected" and an empty table gets saved. Fail fast.
    _report("Checking projector responds…")
    try:
        power = projector.get_power_status()
    except ProjectorError as e:
        raise ProjectorError(
            f"Projector not responding to PWR? — check cabling (RS-232 may need a "
            f"null-modem cable) and that the projector is powered: {e}"
        ) from e
    _report(f"  Projector responded (PWR={power})")

    wb_cands = (candidates or {}).get("white_balance") or WB_CANDIDATES

    result = ProbeResult()

    # ---- White Balance -------------------------------------------------------
    _report("Probing white balance channels…")
    for ch in ("R", "G", "B"):
        slot = SlotResult(slot_id=f"wb.{ch}")
        for token in wb_cands.get(ch, []):
            slot.candidates_tried.append(token)
            accepted, value = projector.probe_command(token)
            time.sleep(_INTER_PROBE_SLEEP)
            if accepted:
                slot.accepted_token = token
                slot.current_value = value
                _report(f"  WB.{ch}: found '{token}' = {value}")
                break
            else:
                _report(f"  WB.{ch}: '{token}' rejected")
        if not slot.found:
            _report(f"  WB.{ch}: no token found (tried {slot.candidates_tried})")
        result.wb_slots[ch] = slot

    if not any(slot.found for slot in result.wb_slots.values()):
        _report("  ⚠ The projector answers PWR? but rejects settings queries — "
                "enable Control4/AMX in the projector's Network menu, then probe again.")

    # ---- Picture Mode --------------------------------------------------------
    _report("Probing picture mode commands…")
    supported = {}
    for token in ("CMODE", "DYNRANGE"):
        accepted, value = projector.probe_command(token)
        time.sleep(_INTER_PROBE_SLEEP)
        supported[token] = accepted
        _report(f"  {token}: " + (f"current={value}" if accepted else "rejected"))
    for mode, steps in PICTURE_MODE_PRESETS.items():
        if all(supported.get(cmd) for cmd, _ in steps):
            result.picture_mode[mode] = [list(step) for step in steps]

    return result


def save_command_table(result: ProbeResult, output_path: str | Path) -> None:
    """Write a verified command_table.json from ProbeResult.

    Args:
        result: ProbeResult from run_probe().
        output_path: Destination JSON file path.
    """
    table = result.to_command_table()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(table, f, indent=2)
    logger.info("Command table written to %s", output_path)

    missing = result.missing_slots()
    if missing:
        logger.warning(
            "⚠ %d slot(s) could not be resolved — fill them manually in %s: %s",
            len(missing),
            output_path,
            missing,
        )
