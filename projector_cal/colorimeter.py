"""ArgyllCMS spotread subprocess driver for the X-Rite ColorChecker Display Pro (CCDIS3).

Design:
- Spawns `spotread -e -d {display_index}` with ARGYLL_NOT_INTERACTIVE=1 in the environment.
- Uses os.openpty() to give spotread a pseudo-TTY controlling terminal so it doesn't
  stall waiting for user input.
- Triggers each measurement by writing b"\\n" to the pty master fd.
- Parses the XYZ result from the line "Result is XYZ: X Y Z".
- Takes 3 readings per measurement, rejects outliers by Y standard deviation, returns mean.
- Retries up to `measurement_retries` on parse failure or degenerate reads (Y < 0.01).

Platform note: os.openpty() / os.setsid() / termios are POSIX-only. On Windows this module
raises ImportError at import time with a clear message — tests should be skipped on Windows.
"""

from __future__ import annotations

import logging
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    raise ImportError(
        "colorimeter.py requires POSIX (Linux/macOS) — "
        "run spotread on the same machine as the Raspberry Pi or a Linux host."
    )

import fcntl  # noqa: E402 — POSIX only
import termios  # noqa: E402 — POSIX only

_XYZ_PATTERN = re.compile(r"Result is XYZ:\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)")
_READINGS_PER_MEASUREMENT = 3
_OUTLIER_STD_THRESHOLD = 2.0  # reject readings > this many std devs from mean Y
_MIN_Y = 0.01                  # readings with Y below this are considered degenerate
_READ_TIMEOUT = 0.5            # seconds per select() poll
_MAX_READ_BYTES = 4096


class MeasurementError(Exception):
    """Raised when a valid XYZ measurement cannot be obtained."""


@dataclass
class XYZReading:
    X: float
    Y: float
    Z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.X, self.Y, self.Z)


class Colorimeter:
    """Drives spotread to take XYZ measurements from the CCDIS3.

    Usage:
        with Colorimeter(spotread_path="spotread", display_index=1) as c:
            X, Y, Z = c.measure()
    """

    def __init__(
        self,
        spotread_path: str = "spotread",
        display_index: int = 1,
        measurement_retries: int = 3,
        measurement_timeout: float = 30.0,
    ) -> None:
        self.spotread_path = spotread_path
        self.display_index = display_index
        self.measurement_retries = measurement_retries
        self.measurement_timeout = measurement_timeout
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None

    def start(self) -> None:
        """Spawn spotread with a pseudo-TTY controlling terminal."""
        if self._proc is not None:
            return

        master_fd, slave_fd = os.openpty()

        def _preexec() -> None:
            # Detach from any existing session so we can set a new controlling terminal
            os.setsid()
            # Set slave pty as the controlling terminal for spotread
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        env = dict(os.environ)
        env["ARGYLL_NOT_INTERACTIVE"] = "1"

        cmd = [
            self.spotread_path,
            "-e",               # emissive mode (projector screen)
            "-d", str(self.display_index),
        ]

        logger.info("Starting spotread: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                preexec_fn=_preexec,
                close_fds=True,
            )
        except FileNotFoundError as e:
            os.close(master_fd)
            os.close(slave_fd)
            raise MeasurementError(
                f"spotread not found at '{self.spotread_path}'. "
                "Install ArgyllCMS and ensure it's on PATH."
            ) from e

        # Close slave end in parent process — only child needs it
        os.close(slave_fd)
        self._proc = proc
        self._master_fd = master_fd

        # Wait for spotread to print its initialization prompt
        self._drain_output(timeout=5.0)
        logger.debug("spotread started (pid=%d)", proc.pid)

    def stop(self) -> None:
        """Terminate spotread and close the pty."""
        if self._proc is not None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                self._proc.wait(timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def __enter__(self) -> "Colorimeter":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def measure(self) -> tuple[float, float, float]:
        """Take a set of XYZ readings and return the filtered mean.

        Takes _READINGS_PER_MEASUREMENT readings, rejects outliers, returns
        (X, Y, Z) as plain Python floats on the Y=100 scale.

        Retries up to measurement_retries times on failure.

        Raises:
            MeasurementError: if a valid reading cannot be obtained.
        """
        if self._proc is None or self._master_fd is None:
            raise MeasurementError("Colorimeter not started — call start() or use context manager")

        for attempt in range(1, self.measurement_retries + 1):
            try:
                readings = self._take_readings(_READINGS_PER_MEASUREMENT)
                filtered = _filter_outliers(readings)
                if not filtered:
                    raise MeasurementError("All readings rejected as outliers")
                mean = _mean_xyz(filtered)
                logger.debug(
                    "Measurement OK: X=%.4f Y=%.4f Z=%.4f (from %d/%d readings)",
                    mean.X, mean.Y, mean.Z, len(filtered), len(readings),
                )
                return mean.as_tuple()
            except MeasurementError as e:
                if attempt >= self.measurement_retries:
                    raise
                logger.warning("Measurement attempt %d failed: %s; retrying…", attempt, e)

        raise MeasurementError("Exceeded measurement retries")  # pragma: no cover

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _take_readings(self, count: int) -> list[XYZReading]:
        """Trigger `count` individual spotread measurements and return them."""
        readings: list[XYZReading] = []
        for i in range(count):
            reading = self._single_measurement()
            readings.append(reading)
        return readings

    def _single_measurement(self) -> XYZReading:
        """Trigger one measurement by writing \\n to pty; parse the XYZ response."""
        assert self._master_fd is not None
        # Trigger measurement
        os.write(self._master_fd, b"\n")

        deadline = time.monotonic() + self.measurement_timeout
        buf = ""
        while time.monotonic() < deadline:
            chunk = self._read_chunk(timeout=_READ_TIMEOUT)
            if chunk:
                buf += chunk
                m = _XYZ_PATTERN.search(buf)
                if m:
                    X, Y, Z = float(m.group(1)), float(m.group(2)), float(m.group(3))
                    if Y < _MIN_Y:
                        raise MeasurementError(
                            f"Degenerate reading Y={Y:.4f} (< {_MIN_Y}) — "
                            "is the screen displaying a patch?"
                        )
                    return XYZReading(X=X, Y=Y, Z=Z)
                # Clear consumed lines but keep any partial last line
                lines = buf.split("\n")
                buf = lines[-1]

        raise MeasurementError(
            f"Timed out waiting for spotread measurement ({self.measurement_timeout}s)"
        )

    def _read_chunk(self, timeout: float) -> str:
        """Non-blocking read from pty master; returns decoded text or empty string."""
        assert self._master_fd is not None
        try:
            ready, _, _ = select.select([self._master_fd], [], [], timeout)
        except (OSError, ValueError):
            return ""
        if not ready:
            return ""
        try:
            data = os.read(self._master_fd, _MAX_READ_BYTES)
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _drain_output(self, timeout: float) -> str:
        """Read and discard pty output for up to `timeout` seconds."""
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            chunk = self._read_chunk(timeout=min(0.2, deadline - time.monotonic()))
            if chunk:
                buf += chunk
        return buf


# ---------------------------------------------------------------------------
# Statistics helpers (module-level for testability)
# ---------------------------------------------------------------------------

def _filter_outliers(readings: list[XYZReading]) -> list[XYZReading]:
    """Remove readings whose Y deviates more than _OUTLIER_STD_THRESHOLD std devs from mean.

    With only 3 readings, returns all if std dev is 0 or if fewer than 3 readings.
    """
    if len(readings) < 2:
        return readings
    ys = [r.Y for r in readings]
    mean_y = sum(ys) / len(ys)
    variance = sum((y - mean_y) ** 2 for y in ys) / len(ys)
    std_y = math.sqrt(variance)
    if std_y < 1e-9:
        return readings
    return [
        r for r in readings
        if abs(r.Y - mean_y) <= _OUTLIER_STD_THRESHOLD * std_y
    ]


def _mean_xyz(readings: list[XYZReading]) -> XYZReading:
    """Return the mean XYZ across a list of readings."""
    n = len(readings)
    return XYZReading(
        X=sum(r.X for r in readings) / n,
        Y=sum(r.Y for r in readings) / n,
        Z=sum(r.Z for r in readings) / n,
    )
