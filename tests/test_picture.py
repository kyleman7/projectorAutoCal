"""Picture-setting snapshots, restore order, nudging, and profile round-trips."""

from __future__ import annotations

from projector_cal import picture
from projector_cal.profiles import CalibrationProfile, apply_profile, profile_from_projector
from projector_cal.projector import ProjectorError


class RecordingProjector:
    """Answers only the settings it holds; records every write in order."""

    def __init__(self, settings: dict, quantum: float = 1.0) -> None:
        self.settings = dict(settings)
        self.quantum = quantum
        self.writes: list[str] = []

    def get_setting(self, token, param=None):
        key = token if param is None else f"{token} {param}"
        if key not in self.settings:
            raise ProjectorError(f"ERR {key}?")
        return str(self.settings[key])

    def set_setting(self, token, value, param=None):
        key = token if param is None else f"{token} {param}"
        if key not in self.settings:
            raise ProjectorError(f"ERR {key}")
        self.writes.append(key)
        if isinstance(value, int) or str(value).isdigit():
            value = round(round(int(value) / self.quantum) * self.quantum)
        self.settings[key] = value

    def get_wb_gain(self, ch):
        return int(self.settings[f"GAIN{ch}"])

    def set_wb_gain(self, ch, value):
        self.set_setting(f"GAIN{ch}", value)


def test_read_settings_skips_unsupported_tokens():
    proj = RecordingProjector({"CMODE": "07", "BRIGHT": 128, "GAMMALV 03": 140})
    assert picture.read_settings(proj) == {"CMODE": "07", "BRIGHT": "128", "GAMMALV 03": "140"}


def test_apply_settings_restores_in_dependency_order():
    snapshot = {"GAMMALV 00": "130", "GAMMA": "F0", "BRIGHT": "120", "CMODE": "15", "GAINR": "126"}
    proj = RecordingProjector({k: 0 for k in snapshot})
    assert picture.apply_settings(proj, snapshot) == []
    # Color mode first (settings are stored per mode); gamma preset before its points
    assert proj.writes[0] == "CMODE"
    assert proj.writes.index("GAMMA") < proj.writes.index("GAMMALV 00")


def test_apply_settings_reports_rejected_keys():
    proj = RecordingProjector({"BRIGHT": 0})
    assert picture.apply_settings(proj, {"BRIGHT": "120", "MCFI": "00"}) == ["MCFI"]


def test_nudge_steps_past_quantization():
    proj = RecordingProjector({"BRIGHT": 126}, quantum=2.53)
    assert picture.nudge(proj, "BRIGHT", 1) == 129    # 127 and 128 read back as 126
    assert picture.nudge(proj, "BRIGHT", -1) == 126


def test_profile_snapshots_and_restores_every_setting():
    live = {"CMODE": "07", "BRIGHT": 130, "CONTRAST": 120, "GAINR": 126, "GAING": 111,
            "GAINB": 108, "OFFSETR": 124, "GAMMA": "F0", "GAMMALV 04": 142}
    profile = profile_from_projector("Living room", "sdr", RecordingProjector(live))
    assert profile.settings["GAMMALV 04"] == "142"
    assert profile.wb_gains == {"R": 126, "G": 111, "B": 108}

    target = RecordingProjector({k: 0 for k in live})
    assert apply_profile(profile, target) == []
    assert target.settings["BRIGHT"] == 130 and target.settings["GAMMALV 04"] == 142


def test_legacy_profile_without_snapshot_applies_gains():
    profile = CalibrationProfile(name="old", mode="sdr", created_at="2026-01-01T00:00:00",
                                 wb_gains={"R": 120, "G": 128, "B": 131}, cms_values={},
                                 final_delta_e={})
    proj = RecordingProjector({"GAINR": 0, "GAING": 0, "GAINB": 0})
    assert apply_profile(profile, proj) == []
    assert proj.writes == ["GAINR", "GAING", "GAINB"]
