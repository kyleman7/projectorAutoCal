"""Unit tests for the Samsung ExLink driver and its engine integration.

No hardware: a FakeSerial records frames, and the engine test simulates the
TV's optics as a function of the driver's shadow-state WB gains.
"""

from __future__ import annotations

import pytest

from projector_cal.color_math import D65_WHITE_XYZ
from projector_cal.config import CalibrationConfig, ConfigError, DeviceConfig
from projector_cal.engine import CalibrationEngine
from projector_cal.samsung_exlink import (
    ExLinkError,
    SamsungExLinkClient,
    build_frame,
    exlink_wb_verified,
    frame_hex,
)
from tests.test_engine import FakeDisplay


class FakeSerial:
    """Minimal pyserial stand-in that records written frames."""

    def __init__(self):
        self.is_open = True
        self.frames: list[bytes] = []
        self.in_waiting = 0

    def write(self, data):
        self.frames.append(bytes(data))

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def read(self, n):
        return b""


def make_table(verified: bool = True) -> dict:
    return {
        "commands": {
            "wb_gain_r": {"cmd": [11, 7, 0], "wire_offset": 50, "verified": verified},
            "wb_gain_g": {"cmd": [11, 8, 0], "wire_offset": 50, "verified": verified},
            "wb_gain_b": {"cmd": [11, 9, 0], "wire_offset": 50, "verified": verified},
        },
        "baseline": {
            "wb_gains": {"R": 0, "G": 0, "B": 0},
            "wb_gain_range": [-50, 50],
            "wb_gain_center": 0,
        },
    }


def make_client(table: dict | None = None, **kwargs) -> tuple[SamsungExLinkClient, FakeSerial]:
    tv = SamsungExLinkClient(
        port="FAKE", command_table=table or make_table(), command_settle_ms=0, **kwargs
    )
    fake = FakeSerial()
    tv._ser = fake
    return tv, fake


class TestFrameBuilding:
    def test_power_on_reference_frame(self):
        """Known-good example from the public docs: 08 22 00 00 00 02 D4."""
        frame = build_frame((0, 0, 0), 2)
        assert frame == bytes.fromhex("082200000002D4")
        assert frame_hex(frame) == "08 22 00 00 00 02 D4"

    def test_frame_sums_to_zero_mod_256(self):
        for cmd, value in [((11, 7, 0), 47), ((1, 0, 0), 100), ((2, 0, 0), 0)]:
            frame = build_frame(cmd, value)
            assert len(frame) == 7
            assert sum(frame) % 256 == 0

    def test_out_of_range_bytes_rejected(self):
        with pytest.raises(ExLinkError):
            build_frame((0, 0, 256), 0)
        with pytest.raises(ExLinkError):
            build_frame((0, 0, 0), -1)


class TestCommandGating:
    def test_unverified_command_refused(self):
        tv, fake = make_client(make_table(verified=False))
        with pytest.raises(ExLinkError, match="not been verified"):
            tv.set_wb_gain("R", 5)
        assert fake.frames == []

    def test_unknown_command_refused(self):
        tv, fake = make_client()
        with pytest.raises(ExLinkError, match="No ExLink command bytes"):
            tv.send_command("wb_offset_r", 5)

    def test_allow_unverified_for_spike(self):
        tv, fake = make_client(make_table(verified=False), allow_unverified=True)
        tv.set_wb_gain("R", 5)
        assert len(fake.frames) == 1

    def test_cms_not_supported(self):
        tv, _ = make_client()
        with pytest.raises(ExLinkError, match="CMS is not yet supported"):
            tv.get_cms("red", "HUE")
        with pytest.raises(ExLinkError, match="CMS is not yet supported"):
            tv.set_cms("red", "HUE", 1)


class TestShadowState:
    def test_get_returns_baseline_before_any_write(self):
        tv, _ = make_client()
        assert tv.get_wb_gain("R") == 0
        assert tv.get_wb_gain("G") == 0
        assert tv.get_wb_gain("B") == 0

    def test_set_updates_shadow_and_sends_offset_wire_value(self):
        tv, fake = make_client()
        tv.set_wb_gain("R", -3)
        assert tv.get_wb_gain("R") == -3
        # wire value = logical -3 + wire_offset 50 = 47
        assert fake.frames[-1] == build_frame((11, 7, 0), 47)

    def test_out_of_device_range_rejected(self):
        tv, fake = make_client()
        with pytest.raises(ExLinkError, match="outside device range"):
            tv.set_wb_gain("B", 51)
        assert fake.frames == []


class TestVerifiedHelper:
    def test_all_verified(self):
        assert exlink_wb_verified(make_table(verified=True)) is True

    def test_unverified_or_missing(self):
        assert exlink_wb_verified(make_table(verified=False)) is False
        table = make_table(verified=True)
        table["commands"]["wb_gain_b"]["cmd"] = None
        assert exlink_wb_verified(table) is False


class TestDeviceConfig:
    def test_defaults_to_epson(self):
        assert DeviceConfig().type == "epson_5040ub"

    def test_samsung_requires_port(self):
        with pytest.raises(ConfigError, match="exlink_port"):
            DeviceConfig(type="samsung_ks8000_exlink", exlink_port="")

    def test_unknown_type_rejected(self):
        with pytest.raises(ConfigError, match="device.type"):
            DeviceConfig(type="lg_oled")


class TestEngineIntegration:
    def test_wb_loop_converges_through_shadow_state_driver(self):
        """Full closed loop against a simulated KS8000: the panel has an
        inherent red push / blue deficit; the engine must drive the ExLink
        shadow gains to compensate, reading state only from the shadow."""
        tv, fake = make_client()

        def measure():
            # 1 gain count = 1% channel output; panel error: +8% red, -6% blue
            fr = 1 + (tv.get_wb_gain("R") + 8) / 100
            fg = 1 + tv.get_wb_gain("G") / 100
            fb = 1 + (tv.get_wb_gain("B") - 6) / 100
            brightness = 0.35  # ~35 cd/m² white — normalization must handle it
            return (
                D65_WHITE_XYZ[0] * fr * brightness,
                D65_WHITE_XYZ[1] * fg * brightness,
                D65_WHITE_XYZ[2] * fb * brightness,
            )

        config = CalibrationConfig(
            delta_e_threshold=1.0,
            max_iterations_per_patch=20,
            wb_gain_center=0,
            wb_gain_range=[-50, 50],
            proportional_gain_initial=0.8,
            proportional_gain_minimum=0.1,
            cms_axis_order=["red"],
        )
        engine = CalibrationEngine(
            projector=tv, display=FakeDisplay(), measure=measure, config=config,
        )
        result = engine._phase1_white_balance()

        assert result.converged, f"ΔE stayed at {result.final_delta_e}"
        assert tv.get_wb_gain("R") < 0     # red pulled down
        assert tv.get_wb_gain("B") > 0     # blue pushed up
        assert tv.get_wb_gain("G") == 0    # G is the fixed reference channel
        # Every write went out as a well-formed frame
        assert fake.frames and all(len(f) == 7 and sum(f) % 256 == 0 for f in fake.frames)
