"""RGBCMY menu driver key sequences (mapped on a 5040UB with a webcam)."""

from __future__ import annotations

from projector_cal.osd import RgbcmyMenu


class KeyRecorder:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def press_key(self, code: str) -> None:
        self.keys.append(code)


def test_open_axis_homes_then_walks_to_the_row():
    proj = KeyRecorder()
    menu = RgbcmyMenu(proj, key_delay=0)  # type: ignore[arg-type]
    menu.open_axis("blue")
    # Esc×3 (close anything) → KEY 89 (grid, cursor on R) → Down×2 → Enter
    assert proj.keys == ["05", "05", "05", "89", "36", "36", "16"]


def test_step_selects_the_control_then_presses_left_or_right():
    proj = KeyRecorder()
    menu = RgbcmyMenu(proj, key_delay=0)  # type: ignore[arg-type]
    menu.open_axis("red")
    proj.keys.clear()
    menu.step("SAT", -2)     # Hue → Saturation, then Left×2
    menu.step("HUE", 1)      # back up to Hue, Right×1
    menu.step("LUM", 0)      # select only
    assert proj.keys == ["36", "37", "37", "35", "38", "36", "36"]
    assert menu.offsets["red"] == {"HUE": 1, "SAT": -2, "LUM": 0}


def test_close_backs_out_completely():
    proj = KeyRecorder()
    menu = RgbcmyMenu(proj, key_delay=0)  # type: ignore[arg-type]
    menu.open_axis("cyan")
    proj.keys.clear()
    menu.close()
    assert proj.keys == ["05", "05", "05"]
