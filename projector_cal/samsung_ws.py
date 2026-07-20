"""Samsung Tizen network conveniences for the KS8000 (NOT used for calibration).

The 2016 Tizen WebSocket API (port 8001) is key-presses only — a virtual
remote with no value read/write — so calibration goes over ExLink serial
(see samsung_exlink.py). This module covers the quality-of-life pieces:

- wake_on_lan(mac): power the TV on over the network.
- send_keys(host, keys): simulated remote presses via the `samsungtvws`
  package (optional dependency: pip install 'projector-cal[samsung]').
  First connection pops an "Allow" prompt on the TV — accept it once; the
  token is cached in token_file for subsequent connects.
- close_osd(host): mash RETURN a few times so no menu overlays the screen
  while the colorimeter measures.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path(__file__).parent.parent / "configs" / "samsung_ws_token.txt"


class SamsungWSError(Exception):
    """Raised when the TV can't be reached or samsungtvws is missing."""


def wake_on_lan(mac: str, broadcast: str = "255.255.255.255") -> None:
    """Send a WoL magic packet (the KS8000 supports network standby wake)."""
    clean = mac.replace(":", "").replace("-", "")
    if len(clean) != 12:
        raise SamsungWSError(f"Invalid MAC address: {mac!r}")
    payload = bytes.fromhex("FF" * 6 + clean * 16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(payload, (broadcast, 9))
    logger.info("WoL packet sent to %s", mac)


def send_keys(host: str, keys: list[str], token_file: str | Path | None = None) -> None:
    """Send remote key presses (e.g. ["KEY_RETURN"]) over the Tizen WS API."""
    try:
        from samsungtvws import SamsungTVWS
    except ImportError as e:
        raise SamsungWSError(
            "samsungtvws is not installed. Run: pip install 'projector-cal[samsung]'"
        ) from e

    token_file = str(token_file or _TOKEN_FILE)
    try:
        tv = SamsungTVWS(host=host, port=8002, token_file=token_file, timeout=10)
        for key in keys:
            tv.send_key(key)
        logger.debug("Sent keys to %s: %s", host, keys)
    except Exception as e:
        raise SamsungWSError(f"Could not send keys to TV at {host}: {e}") from e


def close_osd(host: str, presses: int = 3) -> None:
    """Back out of any on-screen menu so it doesn't overlay the measured patch."""
    send_keys(host, ["KEY_RETURN"] * presses)
