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
                          "CMODE": "07", "DYNRANGE": "10", "LUMINANCE": "02"})
    result = run_probe(proj)  # type: ignore[arg-type]
    assert result.wb_slots["R"].accepted_token == "GAINR"
    assert result.missing_slots() == []
    table = result.to_command_table()
    assert table["picture_mode"]["hdr10"] == [["CMODE", "15"], ["LUMINANCE", "00"], ["DYNRANGE", "02"]]
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


def test_memory_save_and_recall_use_bank_02_and_hex_slots():
    transport = FakeTransport()
    proj = ProjectorClient(transport, command_settle_ms=0, command_table={})  # type: ignore[arg-type]
    proj.save_memory(1)
    proj.recall_memory(10)
    assert transport.sent == [b"PUSHMEM 02 01\r", b"POPMEM 02 0A\r"]


def test_memory_slot_out_of_range_is_rejected():
    import pytest
    proj = ProjectorClient(FakeTransport(), command_settle_ms=0, command_table={})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        proj.save_memory(11)


def test_tcp_client_recovers_from_a_late_reply():
    """A reply that arrives after its command timed out must not be read as the
    answer to the next command (seen live: DYNRANGE? got 'CMODE=15')."""
    import socket as _socket
    import threading as _threading
    import time as _time

    from projector_cal.projector import _HANDSHAKE_ACK_LEN

    srv = _socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    slow_once = {"A": True}

    def serve():
        while True:
            conn, _ = srv.accept()
            conn.recv(64)
            conn.sendall(b"\x00" * _HANDSHAKE_ACK_LEN)
            buf = b""
            while True:
                chunk = conn.recv(64)
                if not chunk:
                    break
                buf += chunk
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    token = line.decode().rstrip("?")
                    if token == "A" and slow_once["A"]:
                        slow_once["A"] = False
                        _time.sleep(0.5)                 # answers after the client gave up
                    conn.sendall(f"{token}={'1' if token == 'A' else '2'}\r:".encode())

    _threading.Thread(target=serve, daemon=True).start()
    client = ProjectorClient.from_tcp("127.0.0.1", port=port, command_timeout=0.3, command_settle_ms=0)
    with client:
        assert client.get_setting("A") == "1"     # timed out once, retried on a fresh connection
        assert client.get_setting("B") == "2"     # not the stale 'A=1'
