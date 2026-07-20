"""Configuration loading, validation, and dataclasses for projector_cal."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "configs" / "default_config.json"
_DEFAULT_COMMAND_TABLE_PATH = Path(__file__).parent.parent / "configs" / "command_table.json"


class ConfigError(Exception):
    """Raised when configuration is missing required fields or contains invalid values."""


@dataclass
class ProjectorConfig:
    host: str = "192.168.1.100"
    port: int = 3629
    connection_timeout: float = 5.0
    command_timeout: float = 2.0
    command_settle_ms: int = 200
    # Transport selection
    transport: Literal["tcp", "serial"] = "tcp"
    serial_port: str = "/dev/ttyUSB0"
    serial_baud: int = 9600

    def __post_init__(self) -> None:
        if self.transport not in ("tcp", "serial"):
            raise ConfigError("projector.transport must be 'tcp' or 'serial'")
        if self.transport == "tcp":
            if not self.host:
                raise ConfigError("projector.host is required when transport is 'tcp'")
            if not (1 <= self.port <= 65535):
                raise ConfigError(f"projector.port must be 1–65535, got {self.port}")
            if self.connection_timeout <= 0:
                raise ConfigError("projector.connection_timeout must be positive")
        if self.transport == "serial":
            if not self.serial_port:
                raise ConfigError("projector.serial_port is required when transport is 'serial'")
            if self.serial_baud not in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
                raise ConfigError(
                    f"projector.serial_baud {self.serial_baud} is not a standard baud rate"
                )
        if self.command_timeout <= 0:
            raise ConfigError("projector.command_timeout must be positive")
        if self.command_settle_ms < 0:
            raise ConfigError("projector.command_settle_ms must be >= 0")


@dataclass
class ColorimeterConfig:
    spotread_path: str = "spotread"
    display_index: int = 1
    measurement_retries: int = 3
    measurement_timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.spotread_path:
            raise ConfigError("colorimeter.spotread_path is required")
        if self.display_index < 1:
            raise ConfigError("colorimeter.display_index must be >= 1")
        if self.measurement_retries < 1:
            raise ConfigError("colorimeter.measurement_retries must be >= 1")
        if self.measurement_timeout <= 0:
            raise ConfigError("colorimeter.measurement_timeout must be positive")


@dataclass
class PGenConfig:
    host: str
    port: int = 85
    patch_settle_ms: int = 500

    def __post_init__(self) -> None:
        if not self.host:
            raise ConfigError("pgen.host is required")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"pgen.port must be 1–65535, got {self.port}")
        if self.patch_settle_ms < 0:
            raise ConfigError("pgen.patch_settle_ms must be >= 0")


@dataclass
class CalibrationConfig:
    delta_e_threshold: float = 1.0
    max_iterations_per_patch: int = 20
    wb_gain_center: int = 128
    wb_gain_range: list[int] = field(default_factory=lambda: [0, 255])
    proportional_gain_initial: float = 0.8
    proportional_gain_minimum: float = 0.1
    cms_axis_order: list[str] = field(
        default_factory=lambda: ["red", "green", "blue", "cyan", "magenta", "yellow"]
    )

    def __post_init__(self) -> None:
        if self.delta_e_threshold <= 0:
            raise ConfigError("calibration.delta_e_threshold must be positive")
        if self.max_iterations_per_patch < 1:
            raise ConfigError("calibration.max_iterations_per_patch must be >= 1")
        if len(self.wb_gain_range) != 2:
            raise ConfigError("calibration.wb_gain_range must be [min, max]")
        lo, hi = self.wb_gain_range
        if lo >= hi:
            raise ConfigError("calibration.wb_gain_range[0] must be < [1]")
        if not (0 < self.proportional_gain_initial <= 1.0):
            raise ConfigError("calibration.proportional_gain_initial must be in (0, 1]")
        if not (0 < self.proportional_gain_minimum <= self.proportional_gain_initial):
            raise ConfigError(
                "calibration.proportional_gain_minimum must be in (0, proportional_gain_initial]"
            )
        valid_axes = {"red", "green", "blue", "cyan", "magenta", "yellow"}
        for axis in self.cms_axis_order:
            if axis not in valid_axes:
                raise ConfigError(f"calibration.cms_axis_order: unknown axis '{axis}'")


@dataclass
class DeviceConfig:
    """Which display device the calibration run drives.

    "epson_5040ub" uses the ProjectorConfig transport (TCP/serial ESC/VP21).
    "samsung_ks8000_exlink" drives the KS8000 over its 3.5mm ExLink serial
    port (write-only; WB phase only until CMS commands are verified).
    """
    type: Literal["epson_5040ub", "samsung_ks8000_exlink"] = "epson_5040ub"
    exlink_port: str = "/dev/ttyUSB1"
    exlink_baud: int = 9600

    def __post_init__(self) -> None:
        if self.type not in ("epson_5040ub", "samsung_ks8000_exlink"):
            raise ConfigError(
                f"device.type must be 'epson_5040ub' or 'samsung_ks8000_exlink', got '{self.type}'"
            )
        if self.type == "samsung_ks8000_exlink" and not self.exlink_port:
            raise ConfigError("device.exlink_port is required for the Samsung ExLink device")


@dataclass
class HdrConfig:
    picture_mode_command: str = "PMOD"
    hdr10_mode_value: str = "HDR4"
    mode_switch_settle_ms: int = 3000

    def __post_init__(self) -> None:
        if not self.picture_mode_command:
            raise ConfigError("hdr.picture_mode_command is required")
        if not self.hdr10_mode_value:
            raise ConfigError("hdr.hdr10_mode_value is required")
        if self.mode_switch_settle_ms < 0:
            raise ConfigError("hdr.mode_switch_settle_ms must be >= 0")


@dataclass
class Config:
    projector: ProjectorConfig
    colorimeter: ColorimeterConfig
    pgen: PGenConfig
    calibration: CalibrationConfig
    hdr: HdrConfig
    device: DeviceConfig = field(default_factory=DeviceConfig)
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigError(f"log_level must be one of {valid_levels}, got '{self.log_level}'")
        self.log_level = self.log_level.upper()


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_defaults() -> dict:
    try:
        with open(_DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in default config: {e}") from e


def load_config(path: str | None = None) -> Config:
    """Load and validate configuration from a JSON file merged with defaults.

    Args:
        path: Path to a JSON config file. If None, uses only the built-in defaults.

    Returns:
        Validated Config dataclass.

    Raises:
        ConfigError: On missing required fields, invalid values, or bad JSON.
    """
    data = _load_defaults()

    if path is not None:
        try:
            with open(path) as f:
                user_data = json.load(f)
        except FileNotFoundError as e:
            raise ConfigError(f"Config file not found: {path}") from e
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file '{path}': {e}") from e
        data = _deep_merge(data, user_data)

    return _build_config(data)


def load_config_from_dict(overrides: dict) -> Config:
    """Merge an in-memory config dict into the built-in defaults and validate.

    Used by the web UI's POST /api/config so updates don't round-trip through
    a temp file.
    """
    return _build_config(_deep_merge(_load_defaults(), overrides or {}))


def _build_config(data: dict) -> Config:
    """Construct and validate a Config from a fully-merged dict."""
    try:
        proj_raw = data.get("projector", {})
        proj = ProjectorConfig(
            host=proj_raw.get("host", "192.168.1.100"),
            port=int(proj_raw.get("port", 3629)),
            connection_timeout=float(proj_raw.get("connection_timeout", 5.0)),
            command_timeout=float(proj_raw.get("command_timeout", 2.0)),
            command_settle_ms=int(proj_raw.get("command_settle_ms", 200)),
            transport=proj_raw.get("transport", "tcp"),
            serial_port=proj_raw.get("serial_port", "/dev/ttyUSB0"),
            serial_baud=int(proj_raw.get("serial_baud", 9600)),
        )

        col_raw = data.get("colorimeter", {})
        col = ColorimeterConfig(
            spotread_path=col_raw.get("spotread_path", "spotread"),
            display_index=int(col_raw.get("display_index", 1)),
            measurement_retries=int(col_raw.get("measurement_retries", 3)),
            measurement_timeout=float(col_raw.get("measurement_timeout", 30.0)),
        )

        pgen_raw = data.get("pgen", {})
        pgen = PGenConfig(
            host=pgen_raw.get("host", ""),
            port=int(pgen_raw.get("port", 85)),
            patch_settle_ms=int(pgen_raw.get("patch_settle_ms", 500)),
        )

        cal_raw = data.get("calibration", {})
        cal = CalibrationConfig(
            delta_e_threshold=float(cal_raw.get("delta_e_threshold", 1.0)),
            max_iterations_per_patch=int(cal_raw.get("max_iterations_per_patch", 20)),
            wb_gain_center=int(cal_raw.get("wb_gain_center", 128)),
            wb_gain_range=list(cal_raw.get("wb_gain_range", [0, 255])),
            proportional_gain_initial=float(cal_raw.get("proportional_gain_initial", 0.8)),
            proportional_gain_minimum=float(cal_raw.get("proportional_gain_minimum", 0.1)),
            cms_axis_order=list(
                cal_raw.get("cms_axis_order", ["red", "green", "blue", "cyan", "magenta", "yellow"])
            ),
        )

        hdr_raw = data.get("hdr", {})
        hdr = HdrConfig(
            picture_mode_command=hdr_raw.get("picture_mode_command", "PMOD"),
            hdr10_mode_value=hdr_raw.get("hdr10_mode_value", "HDR4"),
            mode_switch_settle_ms=int(hdr_raw.get("mode_switch_settle_ms", 3000)),
        )

        dev_raw = data.get("device", {})
        device = DeviceConfig(
            type=dev_raw.get("type", "epson_5040ub"),
            exlink_port=dev_raw.get("exlink_port", "/dev/ttyUSB1"),
            exlink_baud=int(dev_raw.get("exlink_baud", 9600)),
        )

        return Config(
            projector=proj,
            colorimeter=col,
            pgen=pgen,
            calibration=cal,
            hdr=hdr,
            device=device,
            log_level=data.get("log_level", "INFO"),
        )

    except (TypeError, ValueError) as e:
        raise ConfigError(f"Config value error: {e}") from e


def load_command_table(path: str | None = None) -> dict:
    """Load the ESC/VP21 command token table.

    Args:
        path: Path to command_table.json. Defaults to configs/command_table.json.

    Returns:
        Dict with keys: white_balance, picture_mode, cms.

    Raises:
        ConfigError: On missing file or bad JSON.
    """
    resolved = Path(path) if path else _DEFAULT_COMMAND_TABLE_PATH
    try:
        with open(resolved) as f:
            table = json.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"Command table not found: {resolved}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in command table '{resolved}': {e}") from e

    # Keep the "_note" annotation: its presence marks the shipped placeholder
    # file, which lets is_command_table_complete() report "not probed yet".
    # The probe overwrites the file without it. Command lookups access known
    # keys only, so the extra key is harmless downstream.
    return table
