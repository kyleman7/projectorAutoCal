"""ESC/VP21 command token auto-discovery for the Epson 5040UB.

Run once before first calibration to populate command_table.json with the actual
command tokens that this specific projector firmware accepts.

Algorithm:
  For each required command slot, try each candidate token via a query (CANDIDATE?\r).
  If the projector returns CANDIDATE=VALUE:, the token is valid and recorded.
  If it returns ERR, the token is rejected.
  Writes a verified command_table.json when done.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .projector import ProjectorClient, ProjectorError

logger = logging.getLogger(__name__)

_INTER_PROBE_SLEEP = 0.1  # seconds between probe queries to avoid flooding projector

# ---------------------------------------------------------------------------
# Candidate tables
# All known ESC/VP21 token names that may correspond to each required slot
# across different Epson firmware versions.
# ---------------------------------------------------------------------------

WB_CANDIDATES: dict[str, list[str]] = {
    "R": ["WBALGAINR", "WBGAINR", "CRED", "WBRED", "BCRED", "RGAIN"],
    "G": ["WBALGAING", "WBGAING", "CGRN", "WBGRN", "BCGRN", "GGAIN"],
    "B": ["WBALGAINB", "WBGAINB", "CBLU", "WBBLU", "BCBLU", "BGAIN"],
}

CMS_CANDIDATES: dict[tuple[str, str], list[str]] = {
    ("red",     "HUE"): ["CMSRHUE", "CMSR1", "CMSREDHUE",   "CMSHUE1"],
    ("red",     "SAT"): ["CMSRSAT", "CMSR2", "CMSREDSAT",   "CMSSAT1"],
    ("red",     "LUM"): ["CMSRLUM", "CMSR3", "CMSREDLUM",   "CMSLUM1"],
    ("green",   "HUE"): ["CMSGHUE", "CMSG1", "CMSGRENHUE",  "CMSHUE2"],
    ("green",   "SAT"): ["CMSGSAT", "CMSG2", "CMSGRENSAT",  "CMSSAT2"],
    ("green",   "LUM"): ["CMSGLUM", "CMSG3", "CMSGRENSAT",  "CMSLUM2"],
    ("blue",    "HUE"): ["CMSBHUE", "CMSB1", "CMSBLUEHUE",  "CMSHUE3"],
    ("blue",    "SAT"): ["CMSBSAT", "CMSB2", "CMSBLUESAT",  "CMSSAT3"],
    ("blue",    "LUM"): ["CMSBLUM", "CMSB3", "CMSBLUELUM",  "CMSLUM3"],
    ("cyan",    "HUE"): ["CMSCHUE", "CMSC1", "CMSCYANHUE",  "CMSHUE4"],
    ("cyan",    "SAT"): ["CMSCSAT", "CMSC2", "CMSCYANSAT",  "CMSSAT4"],
    ("cyan",    "LUM"): ["CMSCLUM", "CMSC3", "CMSCYANLUM",  "CMSLUM4"],
    ("magenta", "HUE"): ["CMSMHUE", "CMSM1", "CMSMAGHUE",   "CMSHUE5"],
    ("magenta", "SAT"): ["CMSMSAT", "CMSM2", "CMSMAGSAT",   "CMSSAT5"],
    ("magenta", "LUM"): ["CMSMLUM", "CMSM3", "CMSMAGLUM",   "CMSLUM5"],
    ("yellow",  "HUE"): ["CMSYHUE", "CMSY1", "CMSYELHUE",   "CMSHUE6"],
    ("yellow",  "SAT"): ["CMSYSAT", "CMSY2", "CMSYELSAT",   "CMSSAT6"],
    ("yellow",  "LUM"): ["CMSYLUM", "CMSY3", "CMSYELLUM",   "CMSLUM6"],
}

# Picture mode switch candidates
PICTURE_MODE_CANDIDATES: dict[str, list[str]] = {
    "command": ["PMOD", "MMOD", "CMOD", "PCTMOD", "SIGNAL"],
    "sdr_value":   ["CINEMA", "FILM", "NATURAL", "CINE", "SDR"],
    "hdr10_value": ["HDR4", "HDR3", "HDR2", "HDR1", "HDR"],
}


_CMS_AXES = ("red", "green", "blue", "cyan", "magenta", "yellow")


def is_command_table_complete(table: dict) -> bool:
    """True when every required WB and CMS slot has a verified token.

    Shared by the setup validator and the /api/setup/status endpoint so
    "ready to calibrate" means the same thing everywhere.
    """
    # The shipped placeholder file carries a "_note" marker; its tokens fill
    # every slot but are unverified guesses — treat as not probed.
    if table.get("_note"):
        return False
    wb_ok = all(table.get("white_balance", {}).get(ch) for ch in ("R", "G", "B"))
    cms_ok = all(
        table.get("cms", {}).get(axis, {}).get(prop)
        for axis in _CMS_AXES
        for prop in ("HUE", "SAT", "LUM")
    )
    return wb_ok and cms_ok


@dataclass
class SlotResult:
    """Result for one required command slot."""
    slot_id: str              # e.g., "wb.R" or "cms.red.HUE"
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
    cms_slots: dict[str, SlotResult] = field(default_factory=dict)     # "red.HUE", etc.
    picture_mode_command: str | None = None
    picture_mode_sdr: str | None = None
    picture_mode_hdr10: str | None = None

    def missing_slots(self) -> list[str]:
        """Return list of required slot IDs that were not resolved."""
        missing = []
        for ch, slot in self.wb_slots.items():
            if not slot.found:
                missing.append(f"white_balance.{ch}")
        for key, slot in self.cms_slots.items():
            if not slot.found:
                missing.append(f"cms.{key}")
        return missing

    def to_command_table(self) -> dict:
        """Serialize to command_table.json schema.

        Only includes slots where a token was found; unresolved slots are left
        out so the user can fill them in manually if needed.
        """
        table: dict = {"white_balance": {}, "picture_mode": {}, "cms": {}}

        for ch in ("R", "G", "B"):
            slot = self.wb_slots.get(ch)
            if slot and slot.accepted_token:
                table["white_balance"][ch] = slot.accepted_token

        if self.picture_mode_command:
            table["picture_mode"]["command"] = self.picture_mode_command
            if self.picture_mode_sdr:
                table["picture_mode"]["sdr"] = self.picture_mode_sdr
            if self.picture_mode_hdr10:
                table["picture_mode"]["hdr10"] = self.picture_mode_hdr10

        cms_axes = ["red", "green", "blue", "cyan", "magenta", "yellow"]
        for axis in cms_axes:
            table["cms"][axis] = {}
            for prop in ("HUE", "SAT", "LUM"):
                key = f"{axis}.{prop}"
                slot = self.cms_slots.get(key)
                if slot and slot.accepted_token:
                    table["cms"][axis][prop] = slot.accepted_token

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
        if self.picture_mode_command:
            lines.append(f"  command={self.picture_mode_command!r}  sdr={self.picture_mode_sdr!r}  hdr10={self.picture_mode_hdr10!r}")
        else:
            lines.append("  ✗ No picture mode command found")

        lines.append("\nCMS:")
        cms_axes = ["red", "green", "blue", "cyan", "magenta", "yellow"]
        for axis in cms_axes:
            for prop in ("HUE", "SAT", "LUM"):
                key = f"{axis}.{prop}"
                slot = self.cms_slots.get(key)
                if slot and slot.found:
                    lines.append(f"  {axis:8}.{prop}  ✓  {slot.accepted_token!r}={slot.current_value}")
                else:
                    lines.append(f"  {axis:8}.{prop}  ✗")

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
                    If None, uses WB_CANDIDATES and CMS_CANDIDATES.
        progress_cb: Optional callback called with status strings as the probe runs.
                     Used by the web server to stream progress via WebSocket.

    Returns:
        ProbeResult with all discovered token mappings.
    """
    def _report(msg: str) -> None:
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    wb_cands = (candidates or {}).get("white_balance") or WB_CANDIDATES
    cms_cands = (candidates or {}).get("cms") or CMS_CANDIDATES
    pm_cands = (candidates or {}).get("picture_mode") or PICTURE_MODE_CANDIDATES

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

    # ---- Picture Mode --------------------------------------------------------
    _report("Probing picture mode command…")
    for cmd_token in pm_cands.get("command", []):
        accepted, value = projector.probe_command(cmd_token)
        time.sleep(_INTER_PROBE_SLEEP)
        if accepted:
            result.picture_mode_command = cmd_token
            _report(f"  Picture mode command: '{cmd_token}' (current={value})")
            # Try SDR and HDR10 values — we can't test-set them safely, so use
            # the configured defaults for now.
            result.picture_mode_sdr = pm_cands.get("sdr_value", ["CINEMA"])[0]
            result.picture_mode_hdr10 = pm_cands.get("hdr10_value", ["HDR4"])[0]
            break
        else:
            _report(f"  Picture mode '{cmd_token}' rejected")
    if not result.picture_mode_command:
        _report("  No picture mode command found")

    # ---- CMS -----------------------------------------------------------------
    _report("Probing CMS axes…")
    cms_axes = ["red", "green", "blue", "cyan", "magenta", "yellow"]
    for axis in cms_axes:
        for prop in ("HUE", "SAT", "LUM"):
            key = f"{axis}.{prop}"
            slot = SlotResult(slot_id=f"cms.{key}")
            for token in cms_cands.get((axis, prop), []):
                slot.candidates_tried.append(token)
                accepted, value = projector.probe_command(token)
                time.sleep(_INTER_PROBE_SLEEP)
                if accepted:
                    slot.accepted_token = token
                    slot.current_value = value
                    _report(f"  CMS {axis}.{prop}: '{token}' = {value}")
                    break
                else:
                    _report(f"  CMS {axis}.{prop}: '{token}' rejected")
            if not slot.found:
                _report(f"  CMS {axis}.{prop}: no token found")
            result.cms_slots[key] = slot

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
