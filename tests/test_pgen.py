"""PGenClient against a fake PGenerator speaking the port-85 text protocol."""

from __future__ import annotations

import socket
import threading

import pytest

from projector_cal.pgen import PGenClient, PGenError, RGBPatch

_END = b"\x02\r"


class FakePGenerator:
    """Answers like PGenerator 2.x; records every command it receives.

    Output settings live in `conf`; SET commands change them, and
    RESTARTPGENERATOR: bumps the PID like a software restart.
    """

    def __init__(self, quant_range: str = "1", alive: str = "ALIVE", **conf: str) -> None:
        self.alive = alive
        self.conf = {"RGB_QUANT_RANGE": quant_range, "MAX_BPC": "8", "COLOR_FORMAT": "1",
                     "IS_SDR": "1", "IS_HDR": "0", "IS_LL_DOVI": "0", "IS_STD_DOVI": "0",
                     "DV_STATUS": "0", "COLORIMETRY": "2", "EOTF": "0", "PRIMARIES": "0", **conf}
        self.pid = 100
        self.received: list[str] = []
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _reply(self, cmd: str) -> str:
        if cmd == "IS_ALIVE":
            return self.alive
        if cmd == "CMD:GET_MODE":
            return "OK:0[3840x2160 30.00Hz 297.00MHz phsync,pvsync]"
        if cmd == "CMD:GET_PGENERATOR_IS_EXECUTED":
            return f"OK:Pid {self.pid}"
        if cmd == "RESTARTPGENERATOR:":
            self.pid += 1
            return "OK:"
        if cmd.startswith("CMD:GET_PGENERATOR_CONF_"):
            return "OK:" + self.conf[cmd.removeprefix("CMD:GET_PGENERATOR_CONF_")]
        if cmd.startswith("CMD:SET_PGENERATOR_CONF_"):
            name, value = cmd.removeprefix("CMD:SET_PGENERATOR_CONF_").split(":")
            self.conf[name] = value
            return "OK:"
        return "OK"

    def _serve(self) -> None:
        while True:
            conn, _ = self._srv.accept()
            buf = b""
            with conn:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while _END in buf:
                        raw, buf = buf.split(_END, 1)
                        cmd = raw.decode()
                        self.received.append(cmd)
                        if cmd == "QUIT":
                            break
                        conn.sendall(self._reply(cmd).encode() + _END)

    def patches(self) -> list[str]:
        return [c for c in self.received if c.startswith("RGB=")]


def test_limited_range_scales_to_16_235_full_screen():
    fake = FakePGenerator(quant_range="1")
    with PGenClient("127.0.0.1", fake.port, patch_settle_ms=0) as pgen:
        pgen.display_patch(RGBPatch(255, 0, 128))
    assert fake.patches()[0] == "RGB=RECTANGLE;3840,2160;0;235,16,126;235,126,16;0,0,0,0;-1"
    # disconnect blanks the screen, then says goodbye
    assert fake.patches()[-1].split(";")[3] == "16,16,16"
    assert fake.received[-1] == "QUIT"


def test_full_range_sends_values_unchanged():
    fake = FakePGenerator(quant_range="2")
    with PGenClient("127.0.0.1", fake.port, patch_settle_ms=0) as pgen:
        pgen.display_patch(RGBPatch(255, 0, 128))
    assert fake.patches()[0].split(";")[3] == "255,0,128"


def test_rejects_a_server_that_is_not_pgenerator():
    fake = FakePGenerator(alive="NOPE")
    with pytest.raises(PGenError, match="not PGenerator"):
        PGenClient("127.0.0.1", fake.port).connect()


def test_window_pattern_is_centred_over_background():
    fake = FakePGenerator(quant_range="2")
    with PGenClient("127.0.0.1", fake.port, patch_settle_ms=0) as pgen:
        pgen.display_window(RGBPatch(5, 5, 5), RGBPatch(0, 0, 0), size=0.25)
    # 960x540 box at (1440, 810) on a 3840x2160 black field
    assert fake.patches()[0] == "RGB=RECTANGLE;960,540;0;5,5,5;0,0,0;0,0,1440,810;-1"


def test_ten_bit_limited_output_scales_to_64_940():
    fake = FakePGenerator(quant_range="1", MAX_BPC="10", COLOR_FORMAT="0")
    with PGenClient("127.0.0.1", fake.port, patch_settle_ms=0) as pgen:
        pgen.display_patch(RGBPatch(1023, 0, 520, bits=10))
        pgen.display_patch(RGBPatch(255, 255, 255))           # 8-bit patch on a 10-bit output
    first, second = fake.patches()[:2]
    assert first.startswith("RGB=RECTANGLE10bit;") and first.split(";")[3] == "940,64,509"
    assert second.split(";")[3] == "940,940,940"


def test_ten_bit_non_rgb_background_is_sent_as_8_bit():
    fake = FakePGenerator(quant_range="2", MAX_BPC="10", COLOR_FORMAT="2")
    with PGenClient("127.0.0.1", fake.port, patch_settle_ms=0) as pgen:
        pgen.display_patch(RGBPatch(1023, 1023, 1023, bits=10))
    assert fake.patches()[0].split(";")[3:5] == ["1023,1023,1023", "256,256,256"]


def test_configure_output_sets_only_differences_and_restarts():
    fake = FakePGenerator()
    with PGenClient("127.0.0.1", fake.port, patch_settle_ms=0, restart_timeout=10) as pgen:
        changed = pgen.configure_output("hdr10", "ycbcr422")
        assert changed == {"IS_SDR": "0", "IS_HDR": "1", "COLORIMETRY": "9", "MAX_BPC": "10",
                           "EOTF": "2", "PRIMARIES": "2", "COLOR_FORMAT": "2"}
        assert fake.pid == 101
        assert pgen._bits == 10  # re-read after the restart
        assert pgen.configure_output("hdr10", "ycbcr422") == {}   # already configured
    assert fake.pid == 101
    assert "CMD:SET_PGENERATOR_CONF_IS_LL_DOVI:0" not in fake.received
