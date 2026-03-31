"""PGenerator TCP patch display adapter.

PGenerator (quietvoid/pgen_client) is a Raspberry Pi service that displays full-screen
color patches via HDMI. The projector's HDMI input shows the patch; the colorimeter
measures the projected result.

Wire protocol: pgen_client uses a simple binary TCP protocol. Each packet is 3 bytes:
  byte 0: R (0–255)
  byte 1: G (0–255)
  byte 2: B (0–255)

If a future fork exposes an HTTP API, swap the adapter by subclassing PatchDisplay.

Connection is established at context entry and kept open for the session. A single
reconnect is attempted on broken-pipe before raising PGenError.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class PGenError(Exception):
    """Raised on PGenerator connection or protocol failures."""


@dataclass(frozen=True)
class RGBPatch:
    """An 8-bit RGB color patch to display on screen."""
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for name, val in (("r", self.r), ("g", self.g), ("b", self.b)):
            if not (0 <= val <= 255):
                raise ValueError(f"RGBPatch.{name} must be 0–255, got {val}")

    def as_bytes(self) -> bytes:
        return bytes([self.r, self.g, self.b])


@runtime_checkable
class PatchDisplay(Protocol):
    """Protocol (interface) for patch display adapters — swappable."""

    def display_patch(self, patch: RGBPatch) -> None:
        """Display a color patch and wait for it to settle."""
        ...

    def display_black(self) -> None:
        """Display a black patch (between measurements or at shutdown)."""
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
    ) -> None:
        self.host = host
        self.port = port
        self.patch_settle_ms = patch_settle_ms
        self.connection_timeout = connection_timeout
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open TCP connection to PGenerator."""
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connection_timeout
            )
            sock.settimeout(5.0)
            self._sock = sock
            logger.debug("Connected to PGenerator at %s:%d", self.host, self.port)
        except OSError as e:
            raise PGenError(
                f"Cannot connect to PGenerator at {self.host}:{self.port}: {e}"
            ) from e

    def disconnect(self) -> None:
        """Close the TCP connection after blanking the screen."""
        try:
            if self._sock is not None:
                self._send_patch(RGBPatch(0, 0, 0))
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            logger.debug("Disconnected from PGenerator")

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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send_patch(self, patch: RGBPatch) -> None:
        """Send 3-byte RGB packet; reconnect once on broken pipe."""
        if self._sock is None:
            raise PGenError("Not connected — call connect() first")
        data = patch.as_bytes()
        for attempt in range(2):
            try:
                self._sock.sendall(data)
                return
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                if attempt == 0:
                    logger.warning("PGenerator connection lost (%s); reconnecting…", e)
                    self._sock = None
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

    def __enter__(self) -> "NullPatchDisplay":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()
