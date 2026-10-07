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
    command_timeout: float = 5.0       # the 5040UB can take >2 s to ack while processing HDR
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
    # Spectral correction for the light source (spotread -X): a .ccss
    # (preferred for the i1 Display Pro) or .ccmx. Empty = uncorrected.
    ccss_path: str = ""

    def __post_init__(self) -> None:
        if self.ccss_path and not self.ccss_path.lower().endswith((".ccss", ".ccmx")):
            raise ConfigError("colorimeter.ccss_path must be a .ccss or .ccmx file")
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
    # Switch the Pi's HDMI output to the run's signal mode (SDR / HDR10)
    configure_output: bool = True
    # rgb | ycbcr444 | ycbcr422. HDR10 must be RGB: with YCbCr the Pi labels
    # the signal BT.2020-RGB (colorimetry 9) and the 5040UB decodes it as
    # BT.709 (orange red); BT.2020-YCC (10) gave a green screen. 4K30 RGB
    # 10-bit locks fine on the 5040UB (verified 2026-10-07).
    sdr_color_format: str = "rgb"
    hdr_color_format: str = "rgb"

    def __post_init__(self) -> None:
        if not self.host:
            raise ConfigError("pgen.host is required")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"pgen.port must be 1–65535, got {self.port}")
        if self.patch_settle_ms < 0:
            raise ConfigError("pgen.patch_settle_ms must be >= 0")
        for name in ("sdr_color_format", "hdr_color_format"):
            if getattr(self, name) not in ("rgb", "ycbcr444", "ycbcr422"):
                raise ConfigError(f"pgen.{name} must be rgb, ycbcr444 or ycbcr422")


@dataclass
class CalibrationConfig:
    delta_e_threshold: float = 1.0
    max_iterations_per_patch: int = 20
    wb_gain_center: int = 128
    wb_gain_range: list[int] = field(default_factory=lambda: [0, 255])
    # Smallest gain change that actually registers: the 5040UB stores gains in
    # ~2.5-unit steps, so a ±1 write is accepted but reads back unchanged.
    wb_gain_min_step: int = 3
    proportional_gain_initial: float = 0.8
    proportional_gain_minimum: float = 0.1
    cms_axis_order: list[str] = field(
        default_factory=lambda: ["red", "green", "blue", "cyan", "magenta", "yellow"]
    )
    # Two-point white balance: offsets are tuned on this grey; gain/offset
    # rounds repeat until neither point needs a correction
    wb_offset_patch: str = "grey30"
    # HDR10: dark greys are too dim to measure reliably (30 % ≈ 0.3 cd/m²)
    hdr_wb_offset_patch: str = "grey50"
    # HDR10 balances gains on white alone: the tone mapping pulls the channels
    # further apart between 80 % and 100 % than in SDR, so averaging with 80 %
    # left HDR white tinted (ΔE 3–4)
    hdr_wb_gain_patches: list[str] = field(default_factory=lambda: ["white"])
    # HDR10 CMS controls: the projector's HDR processing can't reach P3
    # saturation (every primary 6–12 % short even at +30), so only hue is
    # corrected — chasing saturation just drives the controls to their limits
    hdr_cms_controls: list[str] = field(default_factory=lambda: ["HUE"])
    # Greys whose mean chromaticity the gains are balanced on ("white" alone =
    # classic high point; adding e.g. "grey80" splits mid-scale tint error)
    wb_gain_patches: list[str] = field(default_factory=lambda: ["white"])
    wb_two_point_rounds: int = 3
    # ΔE the correction loops aim for; None = delta_e_threshold (the
    # verification pass/fail line). Finer targets cost iterations.
    correction_target: float | None = None
    # Levels, WB and gamma repeat until a pass makes no change (at most this many)
    calibration_passes: int = 3
    # "measured" sets Brightness/Contrast with the colorimeter; "guided" by eye
    levels_mode: str = "measured"
    # WB: pick the Color Temp preset nearest D65 first, and keep the strongest
    # gain at the ceiling (raw 126 = menu 50, neutral) so correcting white
    # only lowers the other channels instead of dimming all three
    ctemp_select: bool = True
    wb_maximize_light: bool = True
    wb_gain_ceiling: int = 126
    # CMS: "auto" drives the RGBCMY menu with remote-key presses; "guided"
    # tells the user what to change (dry runs are always guided)
    cms_mode: str = "auto"
    # Auto CMS: reset each control to its default (50) first, and judge each
    # axis on these saturation levels together (100 = the full colour). The
    # projector's CMS rotates mid-saturations differently from 100 %, so
    # optimising 100 % alone can wreck the in-between colours.
    cms_reset: bool = True
    cms_sat_levels: list[int] = field(default_factory=lambda: [75, 100])
    # Reset color/tint/skin tone and processing to neutral at the start of a run
    apply_baseline_neutral: bool = True
    # HDR10 patches: luminance (cd/m²) of the "white" stimulus; every other
    # patch is PQ-encoded at its target luminance relative to it
    hdr_white_nits: float = 100.0

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
        if self.wb_gain_min_step < 1:
            raise ConfigError("calibration.wb_gain_min_step must be >= 1")
        if not (0 < self.proportional_gain_initial <= 1.0):
            raise ConfigError("calibration.proportional_gain_initial must be in (0, 1]")
        if not (0 < self.proportional_gain_minimum <= self.proportional_gain_initial):
            raise ConfigError(
                "calibration.proportional_gain_minimum must be in (0, proportional_gain_initial]"
            )
        if self.wb_offset_patch not in {f"grey{p}" for p in range(10, 100, 10)}:
            raise ConfigError("calibration.wb_offset_patch must be grey10…grey90")
        if not (0 < self.hdr_white_nits <= 10000):
            raise ConfigError("calibration.hdr_white_nits must be in (0, 10000]")
        if not self.wb_gain_patches or any(
            p != "white" and p not in {f"grey{g}" for g in (5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95)}
            for p in self.wb_gain_patches
        ):
            raise ConfigError("calibration.wb_gain_patches must be 'white' and/or ramp greys")
        if self.wb_two_point_rounds < 1:
            raise ConfigError("calibration.wb_two_point_rounds must be >= 1")
        if not self.hdr_cms_controls or any(c not in ("HUE", "SAT", "LUM") for c in self.hdr_cms_controls):
            raise ConfigError("calibration.hdr_cms_controls must be drawn from HUE, SAT, LUM")
        if self.cms_mode not in ("auto", "guided"):
            raise ConfigError("calibration.cms_mode must be auto or guided")
        if not self.cms_sat_levels or any(v not in (25, 50, 75, 100) for v in self.cms_sat_levels):
            raise ConfigError("calibration.cms_sat_levels must be drawn from 25, 50, 75, 100")
        if self.correction_target is not None and not (0 < self.correction_target <= self.delta_e_threshold):
            raise ConfigError("calibration.correction_target must be in (0, delta_e_threshold]")
        if self.calibration_passes < 1:
            raise ConfigError("calibration.calibration_passes must be >= 1")
        if self.levels_mode not in ("measured", "guided"):
            raise ConfigError("calibration.levels_mode must be measured or guided")
        lo, hi = self.wb_gain_range
        if not (lo <= self.wb_gain_ceiling <= hi):
            raise ConfigError("calibration.wb_gain_ceiling must be inside wb_gain_range")
        valid_axes = {"red", "green", "blue", "cyan", "magenta", "yellow"}
        for axis in self.cms_axis_order:
            if axis not in valid_axes:
                raise ConfigError(f"calibration.cms_axis_order: unknown axis '{axis}'")


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
            command_timeout=float(proj_raw.get("command_timeout", 5.0)),
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
            ccss_path=str(col_raw.get("ccss_path", "")),
        )

        pgen_raw = data.get("pgen", {})
        pgen = PGenConfig(
            host=pgen_raw.get("host", ""),
            port=int(pgen_raw.get("port", 85)),
            patch_settle_ms=int(pgen_raw.get("patch_settle_ms", 500)),
            configure_output=bool(pgen_raw.get("configure_output", True)),
            sdr_color_format=str(pgen_raw.get("sdr_color_format", "rgb")),
            hdr_color_format=str(pgen_raw.get("hdr_color_format", "rgb")),
        )

        cal_raw = data.get("calibration", {})
        cal = CalibrationConfig(
            delta_e_threshold=float(cal_raw.get("delta_e_threshold", 1.0)),
            max_iterations_per_patch=int(cal_raw.get("max_iterations_per_patch", 20)),
            wb_gain_center=int(cal_raw.get("wb_gain_center", 128)),
            wb_gain_range=list(cal_raw.get("wb_gain_range", [0, 255])),
            wb_gain_min_step=int(cal_raw.get("wb_gain_min_step", 3)),
            proportional_gain_initial=float(cal_raw.get("proportional_gain_initial", 0.8)),
            proportional_gain_minimum=float(cal_raw.get("proportional_gain_minimum", 0.1)),
            cms_axis_order=list(
                cal_raw.get("cms_axis_order", ["red", "green", "blue", "cyan", "magenta", "yellow"])
            ),
            wb_offset_patch=str(cal_raw.get("wb_offset_patch", "grey30")),
            hdr_wb_offset_patch=str(cal_raw.get("hdr_wb_offset_patch", "grey50")),
            hdr_wb_gain_patches=[str(v) for v in cal_raw.get("hdr_wb_gain_patches", ["white"])],
            hdr_cms_controls=[str(v).upper() for v in cal_raw.get("hdr_cms_controls", ["HUE"])],
            wb_gain_patches=[str(v) for v in cal_raw.get("wb_gain_patches", ["white"])],
            wb_two_point_rounds=int(cal_raw.get("wb_two_point_rounds", 3)),
            correction_target=(None if cal_raw.get("correction_target") is None
                               else float(cal_raw["correction_target"])),
            calibration_passes=int(cal_raw.get("calibration_passes", 3)),
            levels_mode=str(cal_raw.get("levels_mode", "measured")),
            ctemp_select=bool(cal_raw.get("ctemp_select", True)),
            wb_maximize_light=bool(cal_raw.get("wb_maximize_light", True)),
            wb_gain_ceiling=int(cal_raw.get("wb_gain_ceiling", 126)),
            cms_mode=str(cal_raw.get("cms_mode", "auto")),
            cms_reset=bool(cal_raw.get("cms_reset", True)),
            cms_sat_levels=[int(v) for v in cal_raw.get("cms_sat_levels", [75, 100])],
            apply_baseline_neutral=bool(cal_raw.get("apply_baseline_neutral", True)),
            hdr_white_nits=float(cal_raw.get("hdr_white_nits", 100.0)),
        )

        hdr_raw = data.get("hdr", {})
        hdr = HdrConfig(
            picture_mode_command=hdr_raw.get("picture_mode_command", "PMOD"),
            hdr10_mode_value=hdr_raw.get("hdr10_mode_value", "HDR4"),
            mode_switch_settle_ms=int(hdr_raw.get("mode_switch_settle_ms", 3000)),
        )

        return Config(
            projector=proj,
            colorimeter=col,
            pgen=pgen,
            calibration=cal,
            hdr=hdr,
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
