"""Unit tests for probe.run_probe and picture-mode switching."""

from __future__ import annotations

import pytest

from projector_cal.probe import is_command_table_complete, run_probe
from projector_cal.projector import ProjectorClient, ProjectorError


class DeadProjector:
    """Every query times out, like a miswired serial link."""

    def __init__(self) -> None:
        self.probed: list[str] = []

    def get_power_status(self) -> str:
        raise ProjectorError("Serial read timed out after 2.0s (received so far: b'')")

    def probe_command(self, candidate: str) -> tuple[bool, str | None]:
        self.probed.append(candidate)
        return (False, None)


class LiveProjector:
    """Answers PWR? and accepts only the tokens it was given."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens

    def get_power_status(self) -> str:
        return "01"

    def probe_command(self, candidate: str) -> tuple[bool, str | None]:
        if candidate in self.tokens:
            return (True, self.tokens[candidate])
        return (False, None)


def test_dead_link_raises_before_probing():
    proj = DeadProjector()
    with pytest.raises(ProjectorError, match="not responding"):
        run_probe(proj)  # type: ignore[arg-type]
    assert proj.probed == []


def test_live_projector_resolves_tokens():
    proj = LiveProjector({"GAINR": "126", "GAING": "111", "GAINB": "108",
                          "CMODE": "07", "DYNRANGE": "10"})
    result = run_probe(proj)  # type: ignore[arg-type]
    assert result.wb_slots["R"].accepted_token == "GAINR"
    assert result.missing_slots() == []
    table = result.to_command_table()
    assert table["picture_mode"]["hdr10"] == [["DYNRANGE", "05"]]
    assert "cms" not in table
    assert is_command_table_complete(table)


def test_settings_locked_projector_hints_control4():
    """PWR? answers but every settings query is ERR — Control4/AMX is off."""
    messages: list[str] = []
    result = run_probe(LiveProjector({}), progress_cb=messages.append)  # type: ignore[arg-type]
    assert len(result.missing_slots()) == 3
    assert result.to_command_table()["picture_mode"] == {}
    assert any("Control4/AMX" in m for m in messages)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv_until_colon(self) -> str:
        return ":"


def test_set_picture_mode_sends_command_pairs_in_order():
    transport = FakeTransport()
    table = {"picture_mode": {"sdr": [["DYNRANGE", "00"], ["CMODE", "07"]]}}
    proj = ProjectorClient(transport, command_settle_ms=0, command_table=table)  # type: ignore[arg-type]
    proj.set_picture_mode("sdr")
    assert transport.sent == [b"DYNRANGE 00\r", b"CMODE 07\r"]


def test_set_picture_mode_legacy_value_uses_command():
    transport = FakeTransport()
    table = {"picture_mode": {"command": "PMOD", "hdr10": "HDR4"}}
    proj = ProjectorClient(transport, command_settle_ms=0, command_table=table)  # type: ignore[arg-type]
    proj.set_picture_mode("hdr10")
    assert transport.sent == [b"PMOD HDR4\r"]
