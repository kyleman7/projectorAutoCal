"""Samsung ExLink serial driver for the KS8000 (2016 SUHD, Tizen).

The KS8000's 3.5mm ExLink jack speaks a binary serial protocol (9600 8N1) —
the same port Calman AutoCal uses on 2016 SUHD sets to drive 2-pt white
balance, 10-pt grayscale, and CMS in SDR. Frames are 7 bytes:

    0x08 0x22 <c1> <c2> <c3> <value> <checksum>

where checksum is the two's complement of the byte sum (so the whole frame
sums to 0 mod 256). Example: power-on is ``08 22 00 00 00 02 D4``.

Differences from the Epson ESC/VP21 driver that shape this module:

- **Write-only**: the TV does not reliably report values back, so the client
  keeps a *shadow state* seeded from a documented baseline. The user must
  reset the TV's picture settings to that baseline before the first run (the
  UI instructs this); after that every write updates the shadow copy.
- **Unverified commands are refused**: only entries marked ``verified: true``
  in configs/exlink_command_table.json may be sent during calibration. The
  publicly documented ExLink commands cover power/volume/source; the
  calibration-level commands must be confirmed on the actual TV first via
  scripts/exlink_spike.py (manual-confirm, one command at a time — never
  auto-swept, since service protocols can reach dangerous settings).
- **WB only for now**: CMS on this panel is "Color Space Custom" (per-primary
  R/G/B coordinates, not HUE/SAT/LUM) and is deferred; get_cms/set_cms raise.

Enable ExLink on the TV: service menu (Mute-1-8-2-Power with TV off) →
Control → Sub Option → "EXT Link Support: ON". RX/TX swap on the 3.5mm cable
is the most common wiring problem.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_DEFAULT_TABLE_PATH = Path(__file__).parent.parent / "configs" / "exlink_command_table.json"

FRAME_HEADER = (0x08, 0x22)


class ExLinkError(Exception):
    """Raised on ExLink connection failures, unverified commands, or bad values."""


# ---------------------------------------------------------------------------
# Frame building (pure functions — unit tested without hardware)
# ---------------------------------------------------------------------------

def build_frame(cmd: tuple[int, int, int], value: int) -> bytes:
    """Build a 7-byte ExLink frame: header + 3 command bytes + value + checksum.

    The checksum is the two's complement of the sum of the first six bytes,
    making the whole frame sum to 0 (mod 256).
    """
    for i, b in enumerate(cmd):
        if not (0 <= b <= 0xFF):
            raise ExLinkError(f"Command byte {i} out of range: {b}")
    if not (0 <= value <= 0xFF):
        raise ExLinkError(f"Value byte out of range: {value}")
    payload = bytes([*FRAME_HEADER, *cmd, value])
    checksum = (0x100 - (sum(payload) & 0xFF)) & 0xFF
    return payload + bytes([checksum])


def frame_hex(frame: bytes) -> str:
    """Human-readable hex for logs: '08 22 00 00 00 02 D4'."""
    return " ".join(f"{b:02X}" for b in frame)


# ---------------------------------------------------------------------------
# Command table
# ---------------------------------------------------------------------------

def load_exlink_table(path: str | Path | None = None) -> dict:
    """Load configs/exlink_command_table.json (commands + baseline + candidates)."""
    resolved = Path(path) if path else _DEFAULT_TABLE_PATH
    try:
        with open(resolved) as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise ExLinkError(f"ExLink command table not found: {resolved}") from e
    except json.JSONDecodeError as e:
        raise ExLinkError(f"Invalid JSON in ExLink command table '{resolved}': {e}") from e


def exlink_wb_verified(table: dict) -> bool:
    """True when all three WB gain commands are confirmed on the real TV."""
    cmds = table.get("commands", {})
    return all(
        cmds.get(f"wb_gain_{ch}", {}).get("verified") and cmds.get(f"wb_gain_{ch}", {}).get("cmd")
        for ch in ("r", "g", "b")
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SamsungExLinkClient:
    """ExLink client exposing the same device interface the engine consumes.

    Usage:
        with SamsungExLinkClient(port="/dev/ttyUSB1", command_table=table) as tv:
            tv.set_wb_gain("R", -3)
    """

    #: Consulted by server.py to gate run phases and skip projector-only steps.
    capabilities = {"wb": True, "cms": False, "picture_mode": False}

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        command_table: dict | None = None,
        command_settle_ms: int = 200,
        allow_unverified: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.command_settle_ms = command_settle_ms
        self.allow_unverified = allow_unverified
        self._table = command_table or {}
        self._ser = None
        # Shadow state: the protocol is write-only, so current values are what
        # we last wrote — seeded from the documented baseline the user resets
        # the TV to before the first run.
        baseline = self._table.get("baseline", {})
        self._shadow_wb: dict[str, int] = dict(baseline.get("wb_gains", {"R": 0, "G": 0, "B": 0}))

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self) -> None:
        if self.is_connected:
            return
        try:
            import serial  # pyserial
        except ImportError as e:
            raise ExLinkError(
                "pyserial is not installed. Run: pip install pyserial "
                "(or: pip install 'projector-cal[samsung]')"
            ) from e
        try:
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
            )
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            self._ser = ser
            logger.debug("ExLink connected: %s @ %d baud", self.port, self.baud)
        except Exception as e:
            raise ExLinkError(
                f"Cannot open ExLink serial port '{self.port}' at {self.baud} baud: {e}"
            ) from e

    def disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
            logger.debug("ExLink disconnected: %s", self.port)

    def __enter__(self) -> "SamsungExLinkClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    def _entry(self, name: str) -> dict:
        entry = self._table.get("commands", {}).get(name)
        if not entry or not entry.get("cmd"):
            raise ExLinkError(
                f"No ExLink command bytes for '{name}' — discover them with "
                "scripts/exlink_spike.py first"
            )
        if not entry.get("verified") and not self.allow_unverified:
            raise ExLinkError(
                f"ExLink command '{name}' has not been verified on this TV — "
                "run scripts/exlink_spike.py to confirm it before calibrating"
            )
        return entry

    def send_command(self, name: str, value: int = 0) -> bytes:
        """Send a named command from the table; returns the frame that was sent."""
        entry = self._entry(name)
        wire_value = value + int(entry.get("wire_offset", 0))
        frame = build_frame(tuple(entry["cmd"]), wire_value)
        self._write(frame)
        logger.debug("ExLink SEND %-14s value=%-4d frame=%s", name, value, frame_hex(frame))
        if self.command_settle_ms > 0:
            time.sleep(self.command_settle_ms / 1000.0)
        return frame

    def _write(self, frame: bytes) -> None:
        if not self.is_connected:
            raise ExLinkError("ExLink not connected")
        for attempt in range(2):
            try:
                self._ser.write(frame)
                self._ser.flush()
                return
            except Exception as e:
                if attempt == 0:
                    logger.warning("ExLink write failed (%s); reconnecting once…", e)
                    self.disconnect()
                    self.connect()
                else:
                    raise ExLinkError(f"ExLink send failed after reconnect: {e}") from e

    def read_pending(self) -> bytes:
        """Return any bytes the TV sent back (usually none — used by the spike)."""
        if not self.is_connected:
            return b""
        try:
            waiting = self._ser.in_waiting
            return self._ser.read(waiting) if waiting else b""
        except Exception:
            return b""

    # ------------------------------------------------------------------
    # Engine device interface (matches ProjectorClient's surface)
    # ------------------------------------------------------------------

    def get_wb_gain(self, channel: Literal["R", "G", "B"]) -> int:
        """Return the shadow value — ExLink is write-only, so this is what we
        last wrote (or the documented baseline before any writes)."""
        return self._shadow_wb[channel]

    def set_wb_gain(self, channel: Literal["R", "G", "B"], value: int) -> None:
        lo, hi = self._table.get("baseline", {}).get("wb_gain_range", [-50, 50])
        if not (lo <= value <= hi):
            raise ExLinkError(f"WB gain {channel}={value} outside device range [{lo}, {hi}]")
        self.send_command(f"wb_gain_{channel.lower()}", value)
        self._shadow_wb[channel] = value
        logger.info("ExLink WB %s → %d", channel, value)

    def get_cms(self, axis: str, prop: str) -> int:
        raise ExLinkError(
            "CMS is not yet supported on the Samsung ExLink driver — "
            "run the White Balance phase only"
        )

    def set_cms(self, axis: str, prop: str, value: int) -> None:
        raise ExLinkError(
            "CMS is not yet supported on the Samsung ExLink driver — "
            "run the White Balance phase only"
        )

    def set_picture_mode(self, mode: str, fallback_command: str | None = None) -> None:
        raise ExLinkError("Picture-mode switching over ExLink is not verified yet")
