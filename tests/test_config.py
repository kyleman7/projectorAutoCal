"""Unit tests for config.py."""

import json
import pytest
from pathlib import Path

from projector_cal.config import (
    CalibrationConfig,
    ColorimeterConfig,
    Config,
    ConfigError,
    HdrConfig,
    PGenConfig,
    ProjectorConfig,
    load_command_table,
    load_config,
)


# ---- ProjectorConfig ----------------------------------------------------------

class TestProjectorConfig:
    def test_valid(self):
        c = ProjectorConfig(host="192.168.1.100")
        assert c.port == 3629
        assert c.connection_timeout == 5.0

    def test_empty_host_raises(self):
        with pytest.raises(ConfigError, match="host is required"):
            ProjectorConfig(host="")

    def test_bad_port_raises(self):
        with pytest.raises(ConfigError, match="port must be"):
            ProjectorConfig(host="192.168.1.1", port=99999)

    def test_zero_timeout_raises(self):
        with pytest.raises(ConfigError, match="connection_timeout"):
            ProjectorConfig(host="192.168.1.1", connection_timeout=0)

    def test_negative_settle_raises(self):
        with pytest.raises(ConfigError, match="command_settle_ms"):
            ProjectorConfig(host="192.168.1.1", command_settle_ms=-1)


# ---- ColorimeterConfig --------------------------------------------------------

class TestColorimeterConfig:
    def test_defaults(self):
        c = ColorimeterConfig()
        assert c.spotread_path == "spotread"
        assert c.measurement_retries == 3

    def test_empty_path_raises(self):
        with pytest.raises(ConfigError, match="spotread_path"):
            ColorimeterConfig(spotread_path="")

    def test_bad_display_index_raises(self):
        with pytest.raises(ConfigError, match="display_index"):
            ColorimeterConfig(display_index=0)


# ---- PGenConfig ---------------------------------------------------------------

class TestPGenConfig:
    def test_valid(self):
        c = PGenConfig(host="192.168.1.101")
        assert c.port == 85

    def test_empty_host_raises(self):
        with pytest.raises(ConfigError, match="host is required"):
            PGenConfig(host="")


# ---- CalibrationConfig --------------------------------------------------------

class TestCalibrationConfig:
    def test_defaults(self):
        c = CalibrationConfig()
        assert c.delta_e_threshold == 1.0
        assert len(c.cms_axis_order) == 6

    def test_bad_threshold_raises(self):
        with pytest.raises(ConfigError, match="delta_e_threshold"):
            CalibrationConfig(delta_e_threshold=0)

    def test_bad_gain_range_raises(self):
        with pytest.raises(ConfigError, match="wb_gain_range"):
            CalibrationConfig(wb_gain_range=[100, 50])

    def test_bad_gain_range_length_raises(self):
        with pytest.raises(ConfigError, match="wb_gain_range"):
            CalibrationConfig(wb_gain_range=[0])

    def test_unknown_axis_raises(self):
        with pytest.raises(ConfigError, match="unknown axis"):
            CalibrationConfig(cms_axis_order=["red", "ultraviolet"])

    def test_gain_minimum_exceeds_initial_raises(self):
        with pytest.raises(ConfigError, match="proportional_gain_minimum"):
            CalibrationConfig(proportional_gain_initial=0.5, proportional_gain_minimum=0.8)


# ---- HdrConfig ----------------------------------------------------------------

class TestHdrConfig:
    def test_defaults(self):
        c = HdrConfig()
        assert c.picture_mode_command == "PMOD"
        assert c.hdr10_mode_value == "HDR4"

    def test_empty_command_raises(self):
        with pytest.raises(ConfigError, match="picture_mode_command"):
            HdrConfig(picture_mode_command="")


# ---- load_config --------------------------------------------------------------

class TestLoadConfig:
    def test_loads_defaults(self):
        cfg = load_config()
        assert cfg.projector.host == "192.168.1.100"
        assert cfg.pgen.port == 85
        assert cfg.calibration.delta_e_threshold == 1.0
        assert cfg.hdr.picture_mode_command == "PMOD"
        assert cfg.log_level == "INFO"

    def test_user_override_merges(self, tmp_path):
        override = {"projector": {"host": "10.0.0.55"}, "log_level": "DEBUG"}
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(override))
        cfg = load_config(str(p))
        assert cfg.projector.host == "10.0.0.55"
        assert cfg.projector.port == 3629  # default preserved
        assert cfg.log_level == "DEBUG"

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path/config.json")

    def test_bad_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{invalid json}")
        with pytest.raises(ConfigError, match="Invalid JSON"):
            load_config(str(p))

    def test_invalid_log_level_raises(self, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"log_level": "VERBOSE"}))
        with pytest.raises(ConfigError, match="log_level"):
            load_config(str(p))

    def test_log_level_uppercased(self, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"log_level": "debug"}))
        cfg = load_config(str(p))
        assert cfg.log_level == "DEBUG"


# ---- load_command_table -------------------------------------------------------

class TestLoadCommandTable(object):
    def test_loads_default_table(self):
        table = load_command_table()
        assert "white_balance" in table
        assert "cms" in table

    def test_custom_path(self, tmp_path):
        data = {"white_balance": {"R": "WBGAINR", "G": "WBGAING", "B": "WBGAINB"}}
        p = tmp_path / "ct.json"
        p.write_text(json.dumps(data))
        table = load_command_table(str(p))
        assert table["white_balance"]["R"] == "WBGAINR"

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_command_table("/nonexistent/command_table.json")

    def test_bad_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json")
        with pytest.raises(ConfigError, match="Invalid JSON"):
            load_command_table(str(p))

    def test_note_marks_placeholder_table_as_incomplete(self):
        """The shipped placeholder file keeps its _note marker, and that marker
        makes is_command_table_complete report 'not probed yet' even though
        every slot is filled (with unverified guesses)."""
        from projector_cal.probe import is_command_table_complete

        table = load_command_table()
        assert "_note" in table
        assert is_command_table_complete(table) is False

        probed = {k: v for k, v in table.items() if k != "_note"}
        assert is_command_table_complete(probed) is True
