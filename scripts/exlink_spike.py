#!/usr/bin/env python3
"""Interactive ExLink command discovery for the Samsung KS8000.

Run this once the ExLink cable is connected (USB-serial adapter + DB9→3.5mm
TRS into the TV's ExLink jack) and ExLink is enabled in the TV's service menu
(TV off → Mute-1-8-2-Power → Control → Sub Option → "EXT Link Support: ON").

What it does, one command at a time and only when you press Enter:
  1. Sends a candidate frame from configs/exlink_command_table.json.
  2. Prints the exact bytes sent and anything the TV sends back.
  3. Asks you whether the TV visibly reacted; your answer is written back to
     the table as verified: true/false.

It never sweeps unknown bytes automatically — TV service protocols can reach
settings you do not want touched. Start with the safe commands (volume, mute)
to prove the wiring, then try picture commands, then hunt WB.

Usage:
    python scripts/exlink_spike.py --port COM5            # Windows
    python scripts/exlink_spike.py --port /dev/ttyUSB1    # Linux/macOS
    python scripts/exlink_spike.py --port COM5 --try-cmd "0B 07 00" --value 25
        (send one ad-hoc frame — for discovering WB commands from forum dumps)

If nothing reacts: swap RX/TX on the 3.5mm cable (the most common problem),
re-check the service menu, and confirm the adapter shows up as a serial port.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from projector_cal.samsung_exlink import (  # noqa: E402
    SamsungExLinkClient,
    build_frame,
    frame_hex,
    load_exlink_table,
)

TABLE_PATH = Path(__file__).parent.parent / "configs" / "exlink_command_table.json"


def prompt_yn(msg: str) -> bool | None:
    """y → True, n → False, s/skip → None."""
    while True:
        ans = input(f"{msg} [y/n/s(kip)] ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        if ans in ("s", "skip", ""):
            return None


def test_named_commands(tv: SamsungExLinkClient, table: dict) -> bool:
    """Walk the command table interactively; returns True if anything changed."""
    changed = False
    for name, entry in table.get("commands", {}).items():
        if not entry.get("cmd"):
            print(f"\n-- {name}: no command bytes yet (source: {entry.get('source')}) — "
                  f"discover with --try-cmd, then add to the JSON")
            continue

        value = entry.get("fixed_value", 0)
        if "fixed_value" not in entry:
            raw = input(f"\n-- {name} (cmd {entry['cmd']}): value to send [default 10]: ").strip()
            value = int(raw) if raw else 10

        if prompt_yn(f"Send {name} = {value}?") is not True:
            continue

        frame = tv.send_command(name, value)
        print(f"   sent: {frame_hex(frame)}")
        reply = tv.read_pending()
        print(f"   reply: {frame_hex(reply) if reply else '(none)'}")

        reacted = prompt_yn("Did the TV visibly react as expected?")
        if reacted is not None:
            entry["verified"] = reacted
            changed = True
            print(f"   recorded verified={reacted}")
    return changed


def try_adhoc(tv: SamsungExLinkClient, cmd_hex: str, value: int) -> None:
    cmd = tuple(int(b, 16) for b in cmd_hex.split())
    if len(cmd) != 3:
        sys.exit("--try-cmd needs exactly 3 hex bytes, e.g. \"0B 07 00\"")
    frame = build_frame(cmd, value)
    print(f"About to send: {frame_hex(frame)}")
    if prompt_yn("Send it?") is not True:
        return
    tv._write(frame)  # noqa: SLF001 — spike tool, deliberate low-level access
    print(f"sent:  {frame_hex(frame)}")
    reply = tv.read_pending()
    print(f"reply: {frame_hex(reply) if reply else '(none)'}")
    print("If this adjusted a WB control, add it to configs/exlink_command_table.json "
          "under the matching wb_* entry (cmd + wire_offset) and mark verified: true.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="Serial port (COM5, /dev/ttyUSB1, …)")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--try-cmd", metavar="\"C1 C2 C3\"", help="Send one ad-hoc command (3 hex bytes)")
    ap.add_argument("--value", type=int, default=0, help="Value byte for --try-cmd")
    args = ap.parse_args()

    table = load_exlink_table(TABLE_PATH)
    tv = SamsungExLinkClient(
        port=args.port, baud=args.baud, command_table=table,
        command_settle_ms=300, allow_unverified=True,
    )
    with tv:
        print(f"Connected to {args.port} @ {args.baud} baud.")
        if args.try_cmd:
            try_adhoc(tv, args.try_cmd, args.value)
            return
        print("Walking the command table. Start with volume/mute to prove the wiring.")
        if test_named_commands(tv, table):
            TABLE_PATH.write_text(json.dumps(table, indent=2) + "\n")
            print(f"\nUpdated {TABLE_PATH}")


if __name__ == "__main__":
    main()
