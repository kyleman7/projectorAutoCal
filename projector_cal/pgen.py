"""PGenerator TCP patch display adapter.

PGenerator is a Raspberry Pi service that displays full-screen color patches via
HDMI. The projector's HDMI input shows the patch; the colorimeter measures the
projected result.

Wire protocol (TCP port 85, as spoken by quietvoid/pgen_client): text commands,
each terminated by b"\\x02\\r"; every command gets a reply with the same terminator.
  IS_ALIVE                         → "ALIVE"
  CMD:GET_MODE                     → "OK:0[3840x2160 30.00Hz ...]"
  CMD:GET_PGENERATOR_CONF_RGB_QUANT_RANGE → "OK:1" (1 = limited 16–235, 2 = full)
  RGB=RECTANGLE;w,h;0;r,g,b;bg_r,bg_b,bg_g;0,0,x,y;-1   (8-bit; note the bg order)
  RGB=RECTANGLE10bit;…                                   (same, 10-bit codes)
  CMD:SET_PGENERATOR_CONF_<NAME>:<value> → "OK:"         (output settings)
  RESTARTPGENERATOR:                                     (apply output settings)
  QUIT

Patches carry full-range codes at 8 or 10 bits and are mapped to the Pi's output
depth and range (limited = 16–235 / 64–940), so "white" is always peak white.

configure_output() puts the Pi in the SDR or HDR10 signal mode a run needs and
restarts PGenerator only when something changed.

Connection is established at context entry and kept open for the session. A single
reconnect is attempted on broken-pipe before raising PGenError.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_CMD_END = b"\x02\r"

# PGenerator output settings (CMD:GET/SET_PGENERATOR_CONF_<name>) per signal mode.
# Values follow quietvoid/pgen_client: colorimetry 2 = BT.709, 9 = BT.2020;
# EOTF 0 = SDR gamma, 2 = PQ; primaries 2 = P3-D65. HDR10 needs 10-bit for PQ.
# HDR metadata (mastering/MaxCLL/MaxFALL) is left as configured on the Pi.
_OUTPUT_PRESETS: dict[str, dict[str, str]] = {
    "sdr": {
        "IS_SDR": "1", "IS_HDR": "0", "IS_LL_DOVI": "0", "IS_STD_DOVI": "0", "DV_STATUS": "0",
        "COLORIMETRY": "2", "MAX_BPC": "8", "EOTF": "0",
    },
    "hdr10": {
        "IS_SDR": "0", "IS_HDR": "1", "IS_LL_DOVI": "0", "IS_STD_DOVI": "0", "DV_STATUS": "0",
        "COLORIMETRY": "9", "MAX_BPC": "10", "EOTF": "2", "PRIMARIES": "2",
    },
}
COLOR_FORMATS = {"rgb": "0", "ycbcr444": "1", "ycbcr422": "2"}


class PGenError(Exception):
    """Raised on PGenerator connection or protocol failures."""


@dataclass(frozen=True)
class RGBPatch:
    """A full-range RGB color patch (8-bit 0–255, or 10-bit 0–1023 for HDR)."""
    r: int
    g: int
    b: int
    bits: int = 8

    def __post_init__(self) -> None:
        if self.bits not in (8, 10):
            raise ValueError(f"RGBPatch.bits must be 8 or 10, got {self.bits}")
        top = (1 << self.bits) - 1
        for name, val in (("r", self.r), ("g", self.g), ("b", self.b)):
            if not (0 <= val <= top):
                raise ValueError(f"RGBPatch.{name} must be 0–{top}, got {val}")


@runtime_checkable
class PatchDisplay(Protocol):
    """Protocol (interface) for patch display adapters — swappable."""

    def display_patch(self, patch: RGBPatch) -> None:
        """Display a color patch and wait for it to settle."""
        ...

    def display_black(self) -> None:
        """Display a black patch (between measurements or at shutdown)."""
        ...

    def display_window(self, window: RGBPatch, background: RGBPatch, size: float = 0.25) -> None:
        """Display a centred box (`size` of each dimension) over a background."""
        ...

    def connect(self) -> None:
        """Establish the connection to the patch display device."""
        ...

    def disconnect(self) -> None:
        """Close the connection."""
        ...


class PGenClient:
    """TCP adapter for the PGenerator service running on a Raspberry Pi.

    Implements the PatchDisplay protocol.

    Usage:
        with PGenClient(host="192.168.1.101", port=85, patch_settle_ms=500) as pgen:
            pgen.display_patch(RGBPatch(255, 0, 0))
            X, Y, Z = colorimeter.measure()
            pgen.display_black()
    """

    def __init__(
        self,
        host: str,
        port: int = 85,
        patch_settle_ms: int = 500,
        connection_timeout: float = 5.0,
        command_timeout: float = 10.0,
        restart_timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.patch_settle_ms = patch_settle_ms
        self.connection_timeout = connection_timeout
        self.command_timeout = command_timeout
        self.restart_timeout = restart_timeout
        self._sock: socket.socket | None = None
        self._size: tuple[int, int] = (1920, 1080)
        self._limited_range = False
        self._bits = 8
        self._format = COLOR_FORMATS["rgb"]

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open TCP connection, confirm it is PGenerator, read its output mode."""
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connection_timeout
            )
        except OSError as e:
            raise PGenError(
                f"Cannot connect to PGenerator at {self.host}:{self.port}: {e}"
            ) from e
        sock.settimeout(self.command_timeout)
        self._sock = sock
        try:
            alive = self._command("IS_ALIVE")
            if alive != "ALIVE":
                raise PGenError(f"{self.host}:{self.port} is not PGenerator (IS_ALIVE → {alive!r})")
            self._read_output()
        except Exception:
            self._close()
            raise
        logger.info("Connected to PGenerator at %s:%d (%dx%d, %d-bit, %s range)", self.host, self.port,
                    *self._size, self._bits, "limited" if self._limited_range else "full")

    def configure_output(self, mode: Literal["sdr", "hdr10"], color_format: str) -> dict[str, str]:
        """Put the Pi's HDMI output in the signal mode a run needs.

        Only settings that differ are written, and PGenerator is restarted
        (which is what applies them) only when something changed.

        Returns:
            The settings that were changed (empty when already configured).
        """
        if color_format not in COLOR_FORMATS:
            raise ValueError(f"color_format must be one of {sorted(COLOR_FORMATS)}")
        wanted = {**_OUTPUT_PRESETS[mode], "COLOR_FORMAT": COLOR_FORMATS[color_format]}
        changed = {
            name: value for name, value in wanted.items()
            if self._query(f"GET_PGENERATOR_CONF_{name}") != value
        }
        if not changed:
            return {}
        for name, value in changed.items():
            reply = self._command(f"CMD:SET_PGENERATOR_CONF_{name}:{value}")
            if not reply.startswith("OK"):
                raise PGenError(f"PGenerator rejected {name}={value}: {reply!r}")
        logger.info("PGenerator output → %s (%s); restarting PGenerator", mode, changed)
        self._restart()
        return changed

    def disconnect(self) -> None:
        """Blank the screen and close the TCP connection."""
        try:
            if self._sock is not None:
                self._send_patch(RGBPatch(0, 0, 0))
                self._sock.sendall(b"QUIT" + _CMD_END)
        except Exception:
            pass
        self._close()

    def __enter__(self) -> "PGenClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # PatchDisplay implementation
    # ------------------------------------------------------------------

    def display_patch(self, patch: RGBPatch) -> None:
        """Send a color patch to PGenerator and wait for settle time.

        Args:
            patch: RGB values to display.

        Raises:
            PGenError: on send failure after one reconnect attempt.
        """
        self._send_patch(patch)
        if self.patch_settle_ms > 0:
            time.sleep(self.patch_settle_ms / 1000.0)
        logger.debug("Patch: R=%d G=%d B=%d (settled %dms)", patch.r, patch.g, patch.b, self.patch_settle_ms)

    def display_black(self) -> None:
        """Display black (0,0,0) with settle time.

        Used between patches and at the end of a calibration run.
        """
        self.display_patch(RGBPatch(0, 0, 0))

    def display_window(self, window: RGBPatch, background: RGBPatch, size: float = 0.25) -> None:
        """Display a centred box over a background (guided levels patterns)."""
        W, H = self._size
        w, h = round(W * size), round(H * size)
        self._send_patch(window, background, (w, h), ((W - w) // 2, (H - h) // 2))
        if self.patch_settle_ms > 0:
            time.sleep(self.patch_settle_ms / 1000.0)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_output(self) -> None:
        """Read resolution, range, bit depth and color format from the Pi."""
        mode = self._query("GET_MODE")
        m = re.search(r"(\d+)x(\d+)", mode)
        if m:
            self._size = (int(m.group(1)), int(m.group(2)))
        else:
            logger.warning("Unrecognized PGenerator mode %r — assuming %dx%d", mode, *self._size)
        self._limited_range = self._query("GET_PGENERATOR_CONF_RGB_QUANT_RANGE") == "1"
        # Any depth above 8 takes 10-bit patch codes (12-bit output included)
        self._bits = 10 if int(self._query("GET_PGENERATOR_CONF_MAX_BPC") or 8) >= 10 else 8
        self._format = self._query("GET_PGENERATOR_CONF_COLOR_FORMAT")

    def _restart(self) -> None:
        """Restart the PGenerator software and wait until it is running again."""
        old_pid = self._query("GET_PGENERATOR_IS_EXECUTED")
        try:
            self._command("RESTARTPGENERATOR:")
        except (PGenError, OSError):
            self._close()  # the daemon may drop the connection while restarting
        deadline = time.monotonic() + self.restart_timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                if self._sock is None:
                    self.connect()
                pid = self._query("GET_PGENERATOR_IS_EXECUTED")
            except (PGenError, OSError):
                self._close()
                continue
            if pid != old_pid and "Pid" in pid:
                self._read_output()
                time.sleep(2.0)  # let the HDMI sink re-lock to the new signal
                return
        raise PGenError(f"PGenerator did not come back within {self.restart_timeout:.0f}s of a restart")

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _command(self, cmd: str) -> str:
        """Send one command and return its reply without the terminator."""
        if self._sock is None:
            raise PGenError("Not connected — call connect() first")
        try:
            self._sock.sendall(cmd.encode("ascii") + _CMD_END)
            buf = b""
            while not buf.endswith(_CMD_END):
                chunk = self._sock.recv(8192)
                if not chunk:
                    raise PGenError(f"PGenerator closed the connection after {cmd!r}")
                buf += chunk
        except socket.timeout as e:
            raise PGenError(f"PGenerator did not answer {cmd!r}") from e
        return buf[: -len(_CMD_END)].decode("ascii", errors="replace")

    def _query(self, name: str) -> str:
        """Send CMD:<name> and return the value after "OK:"."""
        reply = self._command(f"CMD:{name}")
        if not reply.startswith("OK:"):
            raise PGenError(f"PGenerator rejected CMD:{name}: {reply!r}")
        return reply[3:]

    def _code(self, v: int, src_bits: int = 8) -> int:
        """Map a full-range code at `src_bits` to the Pi's output depth and range."""
        out = self._bits - 8
        x = v / ((1 << src_bits) - 1)
        if self._limited_range:
            return (16 << out) + round(x * (219 << out))
        return round(x * ((1 << self._bits) - 1))

    def _send_patch(
        self,
        patch: RGBPatch,
        background: RGBPatch | None = None,
        size: tuple[int, int] | None = None,
        position: tuple[int, int] = (0, 0),
    ) -> None:
        """Send a rectangle (full-screen by default); reconnect once on a dropped connection."""
        r, g, b = (self._code(c, patch.bits) for c in (patch.r, patch.g, patch.b))
        # Full-field default: background matches the patch so any sizing
        # mismatch is still a uniform field
        bg = background or patch
        br, bg_g, bb = (self._code(c, bg.bits) for c in (bg.r, bg.g, bg.b))
        if self._bits == 10 and self._format != COLOR_FORMATS["rgb"]:
            # PGenerator takes 10-bit backgrounds only in RGB; 8-bit otherwise
            br, bg_g, bb = (round(c / 4) for c in (br, bg_g, bb))
        w, h = size or self._size
        x, y = position
        rect = "RECTANGLE" if self._bits == 8 else "RECTANGLE10bit"
        # PGenerator takes the background as r,b,g
        cmd = f"RGB={rect};{w},{h};0;{r},{g},{b};{br},{bb},{bg_g};0,0,{x},{y};-1"
        for attempt in range(2):
            try:
                self._command(cmd)
                return
            except (PGenError, OSError) as e:
                if attempt == 0 and self._sock is not None:
                    logger.warning("PGenerator connection lost (%s); reconnecting…", e)
                    self._close()
                    self.connect()
                else:
                    raise PGenError(f"PGenerator send failed after reconnect: {e}") from e


class NullPatchDisplay:
    """No-op adapter for dry-run and testing — displays nothing, just sleeps settle time."""

    def __init__(self, patch_settle_ms: int = 500) -> None:
        self.patch_settle_ms = patch_settle_ms

    def connect(self) -> None:
        logger.info("[NullPatchDisplay] connect")

    def disconnect(self) -> None:
        logger.info("[NullPatchDisplay] disconnect")

    def display_patch(self, patch: RGBPatch) -> None:
        logger.info("[NullPatchDisplay] patch R=%d G=%d B=%d", patch.r, patch.g, patch.b)
        if self.patch_settle_ms > 0:
            time.sleep(self.patch_settle_ms / 1000.0)

    def display_black(self) -> None:
        self.display_patch(RGBPatch(0, 0, 0))

    def display_window(self, window: RGBPatch, background: RGBPatch, size: float = 0.25) -> None:
        logger.info("[NullPatchDisplay] window %s on %s", window, background)
        if self.patch_settle_ms > 0:
            time.sleep(self.patch_settle_ms / 1000.0)

    def __enter__(self) -> "NullPatchDisplay":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()
