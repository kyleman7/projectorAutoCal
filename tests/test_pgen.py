"""PGenClient against a fake PGenerator speaking the port-85 text protocol."""

from __future__ import annotations

import socket
import threading

import pytest

from projector_cal.pgen import PGenClient, PGenError, RGBPatch

_END = b"\x02\r"


class FakePGenerator:
    """Answers like PGenerator 2.x; records every command it receives."""

    def __init__(self, quant_range: str = "1", alive: str = "ALIVE") -> None:
        self.replies = {
            "IS_ALIVE": alive,
            "CMD:GET_MODE": "OK:0[3840x2160 30.00Hz 297.00MHz phsync,pvsync]",
            "CMD:GET_PGENERATOR_CONF_RGB_QUANT_RANGE": f"OK:{quant_range}",
        }
        self.received: list[str] = []
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self._srv.accept()
        buf = b""
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while _END in buf:
                    raw, buf = buf.split(_END, 1)
                    cmd = raw.decode()
                    self.received.append(cmd)
                    if cmd == "QUIT":
                        return
                    reply = self.replies.get(cmd, "OK")
                    conn.sendall(reply.encode() + _END)

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
