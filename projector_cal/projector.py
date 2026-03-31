"""ESC/VP21 TCP socket driver for the Epson PowerLite Home Cinema 5040UB.

Protocol details:
- TCP port 3629
- Handshake: send ESC/VP.net magic bytes, read 16-byte acknowledgment
- Commands: b"COMMAND PARAMETER\\r" → response b":" (success) or b"ERR"
- Queries:  b"COMMAND?\\r"          → response b"COMMAND=VALUE:\\r"
- Reconnect once on broken-pipe before raising ProjectorError
"""

from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from typing import Generator, Literal

logger = logging.getLogger(__name__)

# ESC/VP.net handshake — 16-byte magic
_HANDSHAKE = bytes.fromhex("455343 2F56502E 6E657410 03000000 0000".replace(" ", ""))
_HANDSHAKE_ACK_LEN = 16
_CR = b"\r"
_ENCODING = "ascii"


class ProjectorError(Exception):
    """Raised on connection failures or unexpected projector responses."""


class ProjectorClient:
    """Low-level ESC/VP21 TCP client.

    Usage (preferred):
        with ProjectorClient(host, port, timeout) as proj:
            proj.set_wb_gain("R", 130)

    Or manually:
        proj = ProjectorClient(host, port, timeout)
        proj.connect()
        ...
        proj.disconnect()
    """

    def __init__(
        self,
        host: str,
        port: int = 3629,
        connection_timeout: float = 5.0,
        command_timeout: float = 2.0,
        command_settle_ms: int = 200,
        command_table: dict | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.connection_timeout = connection_timeout
        self.command_timeout = command_timeout
        self.command_settle_ms = command_settle_ms
        self._command_table: dict = command_table or {}
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open TCP connection and perform ESC/VP.net handshake."""
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connection_timeout
            )
            sock.settimeout(self.command_timeout)
            sock.sendall(_HANDSHAKE)
            ack = sock.recv(_HANDSHAKE_ACK_LEN)
            if len(ack) < _HANDSHAKE_ACK_LEN:
                sock.close()
                raise ProjectorError(
                    f"Handshake failed: expected {_HANDSHAKE_ACK_LEN} bytes, got {len(ack)}"
                )
            self._sock = sock
            logger.debug("Connected to projector at %s:%d", self.host, self.port)
        except OSError as e:
            raise ProjectorError(f"Cannot connect to projector at {self.host}:{self.port}: {e}") from e

    def disconnect(self) -> None:
        """Close the TCP connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            logger.debug("Disconnected from projector")

    def __enter__(self) -> "ProjectorClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Low-level send/receive
    # ------------------------------------------------------------------

    def _send_raw(self, data: bytes) -> str:
        """Send bytes and return the response string (sans trailing \\r).

        Reconnects once on broken-pipe / connection-reset before giving up.
        """
        if self._sock is None:
            raise ProjectorError("Not connected — call connect() first")
        for attempt in range(2):
            try:
                self._sock.sendall(data)
                return self._recv_response()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                if attempt == 0:
                    logger.warning("Connection lost (%s); reconnecting once…", e)
                    self.disconnect()
                    self.connect()
                else:
                    raise ProjectorError(f"Send failed after reconnect: {e}") from e
        raise ProjectorError("Unreachable")  # pragma: no cover

    def _recv_response(self) -> str:
        """Read bytes from socket until ':' or newline, return decoded string."""
        buf = b""
        assert self._sock is not None
        while True:
            chunk = self._sock.recv(256)
            if not chunk:
                raise ProjectorError("Connection closed by projector during receive")
            buf += chunk
            # Response ends with ':' (success), 'ERR', or 'ERR:\r'
            decoded = buf.decode(_ENCODING, errors="replace").rstrip("\r\n")
            if decoded.endswith(":") or "ERR" in decoded:
                return decoded

    def _send_command(self, command: str, parameter: str) -> None:
        """Send SET command; raise ProjectorError if projector returns ERR."""
        payload = f"{command} {parameter}\r".encode(_ENCODING)
        logger.debug("SET  %s %s", command, parameter)
        response = self._send_raw(payload)
        if "ERR" in response:
            raise ProjectorError(f"Projector rejected command '{command} {parameter}': {response}")
        if self.command_settle_ms > 0:
            time.sleep(self.command_settle_ms / 1000.0)

    def _send_query(self, command: str) -> str:
        """Send QUERY; return the VALUE string or raise ProjectorError on ERR."""
        payload = f"{command}?\r".encode(_ENCODING)
        logger.debug("GET  %s?", command)
        response = self._send_raw(payload)
        if "ERR" in response:
            raise ProjectorError(f"Projector rejected query '{command}?': {response}")
        # Expected: "COMMAND=VALUE:"
        prefix = f"{command}="
        if response.startswith(prefix):
            return response[len(prefix):].rstrip(":")
        raise ProjectorError(f"Unexpected query response for '{command}?': {response!r}")

    # ------------------------------------------------------------------
    # Command table helpers
    # ------------------------------------------------------------------

    def _wb_token(self, channel: Literal["R", "G", "B"]) -> str:
        try:
            return self._command_table["white_balance"][channel]
        except KeyError:
            raise ProjectorError(
                f"No command token for white_balance.{channel} — run probe first"
            )

    def _cms_token(self, axis: str, prop: Literal["HUE", "SAT", "LUM"]) -> str:
        try:
            return self._command_table["cms"][axis][prop]
        except KeyError:
            raise ProjectorError(
                f"No command token for cms.{axis}.{prop} — run probe first"
            )

    def _picture_mode_token(self, mode: Literal["sdr", "hdr10"]) -> str:
        try:
            return self._command_table["picture_mode"][mode]
        except KeyError:
            raise ProjectorError(
                f"No command token for picture_mode.{mode} — run probe first"
            )

    # ------------------------------------------------------------------
    # White Balance
    # ------------------------------------------------------------------

    def get_wb_gain(self, channel: Literal["R", "G", "B"]) -> int:
        """Read current white balance gain for the given channel (R/G/B).

        Returns:
            Integer gain value (typically 0–255 on the 5040UB).
        """
        token = self._wb_token(channel)
        value = self._send_query(token)
        try:
            return int(value)
        except ValueError:
            raise ProjectorError(f"Non-integer WB gain response for {channel}: {value!r}")

    def set_wb_gain(self, channel: Literal["R", "G", "B"], value: int) -> None:
        """Set white balance gain for the given channel.

        Args:
            channel: "R", "G", or "B"
            value: Gain value (clamped by the engine to the configured range)
        """
        token = self._wb_token(channel)
        self._send_command(token, str(value))
        logger.info("WB %s → %d", channel, value)

    # ------------------------------------------------------------------
    # CMS (6-axis Color Management System)
    # ------------------------------------------------------------------

    def get_cms(self, axis: str, prop: Literal["HUE", "SAT", "LUM"]) -> int:
        """Read current CMS value for an axis/property.

        Args:
            axis: "red" | "green" | "blue" | "cyan" | "magenta" | "yellow"
            prop: "HUE" | "SAT" | "LUM"

        Returns:
            Integer CMS value.
        """
        token = self._cms_token(axis, prop)
        value = self._send_query(token)
        try:
            return int(value)
        except ValueError:
            raise ProjectorError(f"Non-integer CMS response for {axis}.{prop}: {value!r}")

    def set_cms(self, axis: str, prop: Literal["HUE", "SAT", "LUM"], value: int) -> None:
        """Set CMS value for an axis/property.

        Args:
            axis: "red" | "green" | "blue" | "cyan" | "magenta" | "yellow"
            prop: "HUE" | "SAT" | "LUM"
            value: Correction value (range depends on projector firmware)
        """
        token = self._cms_token(axis, prop)
        self._send_command(token, str(value))
        logger.info("CMS %s.%s → %d", axis, prop, value)

    # ------------------------------------------------------------------
    # Picture Mode
    # ------------------------------------------------------------------

    def set_picture_mode(self, mode: Literal["sdr", "hdr10"]) -> None:
        """Switch the projector's active picture mode.

        For SDR calibration: switches to the Cinema/SDR mode.
        For HDR10 calibration: switches to the HDR Mode 4 (most accurate tone mapping).

        Args:
            mode: "sdr" or "hdr10"
        """
        token = self._picture_mode_token(mode)
        self._send_command(token, "")
        logger.info("Picture mode → %s (sent token '%s')", mode, token)

    # ------------------------------------------------------------------
    # Probe helpers (used by probe.py, not by the calibration engine)
    # ------------------------------------------------------------------

    def probe_command(self, candidate: str) -> tuple[bool, str | None]:
        """Query a candidate command token to see if the projector accepts it.

        Args:
            candidate: ESC/VP21 command token to test (e.g., "WBALGAINR")

        Returns:
            (accepted, current_value) — accepted is True if the projector
            returned a valid VALUE response; current_value is the parsed
            value string or None if rejected.
        """
        if self._sock is None:
            raise ProjectorError("Not connected")
        try:
            payload = f"{candidate}?\r".encode(_ENCODING)
            self._sock.sendall(payload)
            response = self._recv_response()
        except ProjectorError:
            return (False, None)
        except OSError:
            return (False, None)

        if "ERR" in response:
            return (False, None)
        prefix = f"{candidate}="
        if response.startswith(prefix):
            value = response[len(prefix):].rstrip(":")
            return (True, value)
        return (False, None)
