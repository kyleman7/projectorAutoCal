"""ESC/VP21 projector driver for the Epson PowerLite Home Cinema 5040UB.

Supports two transports:
  - TCP (default): port 3629, requires ESC/VP.net 16-byte handshake on connect
  - RS-232C: 9600 8N1, no handshake, direct serial via pyserial

Both transports speak identical ESC/VP21 framing:
  SET:    b"TOKEN VALUE\\r"  → b":"  (success) or b"ERR"
  QUERY:  b"TOKEN?\\r"       → b"TOKEN=VALUE:\\r" or b"ERR"

Usage:
    # TCP (default)
    with ProjectorClient.from_config(config) as proj:
        proj.set_wb_gain("R", 130)

    # Serial
    with ProjectorClient.from_config(config) as proj:   # transport="serial" in config
        proj.set_wb_gain("R", 130)
"""

from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ESC/VP.net TCP handshake — 16-byte magic (TCP transport only)
_HANDSHAKE = bytes.fromhex("455343 2F56502E 6E657410 03000000 0000".replace(" ", ""))
_HANDSHAKE_ACK_LEN = 16
_CR = b"\r"
_ENCODING = "ascii"


class ProjectorError(Exception):
    """Raised on connection failures or unexpected projector responses."""


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Transport(Protocol):
    """Low-level byte transport — implemented by TCPTransport and SerialTransport."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def send(self, data: bytes) -> None: ...
    def recv_until_colon(self) -> str:
        """Read bytes until response ends with ':' or contains 'ERR'."""
        ...

    @property
    def is_connected(self) -> bool: ...


# ---------------------------------------------------------------------------
# TCP transport
# ---------------------------------------------------------------------------

class TCPTransport:
    """ESC/VP.net TCP transport (port 3629).

    Performs the 16-byte magic handshake on connect.
    Reconnects once on broken-pipe before raising ProjectorError.
    """

    def __init__(
        self,
        host: str,
        port: int = 3629,
        connection_timeout: float = 5.0,
        command_timeout: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connection_timeout = connection_timeout
        self.command_timeout = command_timeout
        self._sock: socket.socket | None = None

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
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
                    f"TCP handshake failed: expected {_HANDSHAKE_ACK_LEN} bytes, got {len(ack)}"
                )
            self._sock = sock
            logger.debug("TCP connected to %s:%d", self.host, self.port)
        except OSError as e:
            raise ProjectorError(
                f"Cannot connect to projector at {self.host}:{self.port}: {e}"
            ) from e

    def disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            logger.debug("TCP disconnected")

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise ProjectorError("TCP transport not connected")
        for attempt in range(2):
            try:
                self._sock.sendall(data)
                return
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                if attempt == 0:
                    logger.warning("TCP connection lost (%s); reconnecting once…", e)
                    self.disconnect()
                    self.connect()
                else:
                    raise ProjectorError(f"TCP send failed after reconnect: {e}") from e

    def recv_until_colon(self) -> str:
        if self._sock is None:
            raise ProjectorError("TCP transport not connected")
        buf = b""
        while True:
            chunk = self._sock.recv(256)
            if not chunk:
                raise ProjectorError("Connection closed by projector during receive")
            buf += chunk
            decoded = buf.decode(_ENCODING, errors="replace").rstrip("\r\n")
            if decoded.endswith(":") or "ERR" in decoded:
                return decoded


# ---------------------------------------------------------------------------
# Serial transport
# ---------------------------------------------------------------------------

class SerialTransport:
    """RS-232C serial transport for the Epson 5040UB (9600 8N1, no handshake).

    Requires pyserial: pip install pyserial  (or pip install projector-cal[serial])

    The serial port speaks the same ESC/VP21 framing as TCP. No magic handshake
    is needed — just open the port and start sending commands.

    Typical port names:
        Linux:   /dev/ttyUSB0  (USB-to-serial adapter)  or  /dev/ttyS0  (native)
        macOS:   /dev/cu.usbserial-XXXXXXXX
        Windows: COM3  (not supported as a calibration host, but listed for reference)

    FTDI-based USB-to-serial adapters (e.g. FTDI FT232R) are strongly recommended
    over Prolific PL2303 adapters for reliability on Linux/macOS.
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baud: int = 9600,
        command_timeout: float = 2.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.command_timeout = command_timeout
        self._ser = None

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self) -> None:
        if self.is_connected:
            return
        try:
            import serial  # pyserial
        except ImportError as e:
            raise ProjectorError(
                "pyserial is not installed. Run: pip install pyserial\n"
                "or: pip install 'projector-cal[serial]'"
            ) from e

        try:
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.command_timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # Flush any stale bytes from a previous session
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            self._ser = ser
            logger.debug("Serial connected: %s @ %d baud", self.port, self.baud)
        except Exception as e:
            raise ProjectorError(
                f"Cannot open serial port '{self.port}' at {self.baud} baud: {e}"
            ) from e

    def disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
            logger.debug("Serial disconnected: %s", self.port)

    def send(self, data: bytes) -> None:
        if not self.is_connected:
            raise ProjectorError("Serial transport not connected")
        for attempt in range(2):
            try:
                self._ser.write(data)
                self._ser.flush()
                return
            except Exception as e:
                if attempt == 0:
                    logger.warning("Serial write failed (%s); reconnecting once…", e)
                    self.disconnect()
                    self.connect()
                else:
                    raise ProjectorError(f"Serial send failed after reconnect: {e}") from e

    def recv_until_colon(self) -> str:
        if not self.is_connected:
            raise ProjectorError("Serial transport not connected")
        buf = b""
        deadline = time.monotonic() + self.command_timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf += chunk
                decoded = buf.decode(_ENCODING, errors="replace").rstrip("\r\n")
                if decoded.endswith(":") or "ERR" in decoded:
                    return decoded
        raise ProjectorError(
            f"Serial read timed out after {self.command_timeout}s "
            f"(received so far: {buf!r})"
        )


# ---------------------------------------------------------------------------
# ProjectorClient — transport-agnostic ESC/VP21 driver
# ---------------------------------------------------------------------------

class ProjectorClient:
    """ESC/VP21 command client — works over TCP or RS-232C.

    Prefer constructing via ProjectorClient.from_tcp() or ProjectorClient.from_serial()
    rather than passing a transport directly.

    Usage (context manager):
        with ProjectorClient.from_tcp("192.168.1.100") as proj:
            proj.set_wb_gain("R", 130)

        with ProjectorClient.from_serial("/dev/ttyUSB0") as proj:
            proj.set_wb_gain("R", 130)
    """

    def __init__(
        self,
        transport: Transport,
        command_settle_ms: int = 200,
        command_table: dict | None = None,
    ) -> None:
        self._transport = transport
        self.command_settle_ms = command_settle_ms
        self._command_table: dict = command_table or {}

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_tcp(
        cls,
        host: str,
        port: int = 3629,
        connection_timeout: float = 5.0,
        command_timeout: float = 2.0,
        command_settle_ms: int = 200,
        command_table: dict | None = None,
    ) -> "ProjectorClient":
        transport = TCPTransport(
            host=host,
            port=port,
            connection_timeout=connection_timeout,
            command_timeout=command_timeout,
        )
        return cls(transport, command_settle_ms=command_settle_ms, command_table=command_table)

    @classmethod
    def from_serial(
        cls,
        port: str = "/dev/ttyUSB0",
        baud: int = 9600,
        command_timeout: float = 2.0,
        command_settle_ms: int = 200,
        command_table: dict | None = None,
    ) -> "ProjectorClient":
        transport = SerialTransport(
            port=port,
            baud=baud,
            command_timeout=command_timeout,
        )
        return cls(transport, command_settle_ms=command_settle_ms, command_table=command_table)

    @classmethod
    def from_config(cls, config: "ProjectorConfig", command_table: dict | None = None) -> "ProjectorClient":  # type: ignore[name-defined]
        """Construct the appropriate transport from a ProjectorConfig dataclass."""
        if config.transport == "serial":
            return cls.from_serial(
                port=config.serial_port,
                baud=config.serial_baud,
                command_timeout=config.command_timeout,
                command_settle_ms=config.command_settle_ms,
                command_table=command_table,
            )
        return cls.from_tcp(
            host=config.host,
            port=config.port,
            connection_timeout=config.connection_timeout,
            command_timeout=config.command_timeout,
            command_settle_ms=config.command_settle_ms,
            command_table=command_table,
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._transport.connect()

    def disconnect(self) -> None:
        self._transport.disconnect()

    def __enter__(self) -> "ProjectorClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Low-level send/receive
    # ------------------------------------------------------------------

    def _send_raw(self, data: bytes) -> str:
        self._transport.send(data)
        return self._transport.recv_until_colon()

    def _send_command(self, command: str, parameter: str) -> None:
        """Send SET command; raise ProjectorError if projector returns ERR."""
        payload = f"{command} {parameter}\r".encode(_ENCODING)
        logger.debug("SET  %s %s", command, parameter)
        response = self._send_raw(payload)
        if "ERR" in response:
            raise ProjectorError(
                f"Projector rejected command '{command} {parameter}': {response}"
            )
        if self.command_settle_ms > 0:
            time.sleep(self.command_settle_ms / 1000.0)

    def _send_query(self, command: str) -> str:
        """Send QUERY; return VALUE string or raise ProjectorError on ERR."""
        payload = f"{command}?\r".encode(_ENCODING)
        logger.debug("GET  %s?", command)
        response = self._send_raw(payload)
        if "ERR" in response:
            raise ProjectorError(
                f"Projector rejected query '{command}?': {response}"
            )
        prefix = f"{command}="
        if response.startswith(prefix):
            return response[len(prefix):].rstrip(":")
        raise ProjectorError(
            f"Unexpected query response for '{command}?': {response!r}"
        )

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
        token = self._wb_token(channel)
        value = self._send_query(token)
        try:
            return int(value)
        except ValueError:
            raise ProjectorError(
                f"Non-integer WB gain response for {channel}: {value!r}"
            )

    def set_wb_gain(self, channel: Literal["R", "G", "B"], value: int) -> None:
        token = self._wb_token(channel)
        self._send_command(token, str(value))
        logger.info("WB %s → %d", channel, value)

    # ------------------------------------------------------------------
    # CMS
    # ------------------------------------------------------------------

    def get_cms(self, axis: str, prop: Literal["HUE", "SAT", "LUM"]) -> int:
        token = self._cms_token(axis, prop)
        value = self._send_query(token)
        try:
            return int(value)
        except ValueError:
            raise ProjectorError(
                f"Non-integer CMS response for {axis}.{prop}: {value!r}"
            )

    def set_cms(self, axis: str, prop: Literal["HUE", "SAT", "LUM"], value: int) -> None:
        token = self._cms_token(axis, prop)
        self._send_command(token, str(value))
        logger.info("CMS %s.%s → %d", axis, prop, value)

    # ------------------------------------------------------------------
    # Picture Mode
    # ------------------------------------------------------------------

    def set_picture_mode(self, mode: Literal["sdr", "hdr10"]) -> None:
        token = self._picture_mode_token(mode)
        self._send_command(token, "")
        logger.info("Picture mode → %s (token '%s')", mode, token)

    # ------------------------------------------------------------------
    # Probe helper
    # ------------------------------------------------------------------

    def probe_command(self, candidate: str) -> tuple[bool, str | None]:
        """Query a candidate token; return (accepted, value) or (False, None)."""
        try:
            payload = f"{candidate}?\r".encode(_ENCODING)
            self._transport.send(payload)
            response = self._transport.recv_until_colon()
        except ProjectorError:
            return (False, None)

        if "ERR" in response:
            return (False, None)
        prefix = f"{candidate}="
        if response.startswith(prefix):
            return (True, response[len(prefix):].rstrip(":"))
        return (False, None)


# ---------------------------------------------------------------------------
# Convenience: list available serial ports
# ---------------------------------------------------------------------------

def list_serial_ports() -> list[dict]:
    """Return available serial ports on this machine.

    Used by the web UI Setup tab to populate the serial port dropdown.
    Returns empty list if pyserial is not installed.
    """
    try:
        from serial.tools import list_ports
        return [
            {
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid,
            }
            for p in list_ports.comports()
        ]
    except ImportError:
        return []
