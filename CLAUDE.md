# ProjectorCal — Developer Notes for Claude Code

## Project Overview

Closed-loop projector calibration system for the **Epson PowerLite Home Cinema 5040UB**.

Hardware stack:
- **Colorimeter**: X-Rite ColorChecker Display Pro (model CCDIS3), USB, driven by ArgyllCMS `spotread`
- **Patch display**: Raspberry Pi 4 running PGenerator (`pgen_client`), connected over TCP
- **Projector**: Epson 5040UB, controlled via ESC/VP21 over TCP port 3629
- **Host machine**: Linux/macOS (colorimeter requires POSIX — `os.openpty()`)
- **Web UI**: FastAPI + WebSockets, accessible from any browser on the LAN

---

## File Structure

```
D:\projectorAutoCal\
├── CLAUDE.md                      ← this file
├── DESIGN.md                      ← detailed technical spec (read before coding)
├── pyproject.toml                 ← build config, entry point: projector-cal = "projector_cal.cli:main"
├── requirements.txt               ← pinned deps
├── .gitignore
├── configs/
│   ├── default_config.json        ← all runtime defaults
│   ├── ccss/                      ← colorimeter spectral corrections (see its README)
│   └── command_table.json         ← ESC/VP21 token map (populated by --probe)
├── profiles/                      ← saved calibration profiles (JSON, gitignored)
├── discovery/                     ← last_scan.json cache (gitignored)
├── static/
│   ├── index.html                 ← 4-tab SPA shell
│   ├── app.js                     ← all client-side logic
│   └── styles.css                 ← dark theme
├── projector_cal/
│   ├── __init__.py                ← __version__ = "0.1.0"
│   ├── __main__.py                ← allows `python -m projector_cal`
│   ├── cli.py                     ← argparse + uvicorn launch
│   ├── config.py                  ← dataclasses, load_config(), ConfigError
│   ├── color_math.py              ← XYZ↔Lab, ΔE2000, SDR/HDR10 targets
│   ├── projector.py               ← ESC/VP21 TCP driver
│   ├── probe.py                   ← command token auto-discovery
│   ├── colorimeter.py             ← ArgyllCMS spotread subprocess (POSIX only)
│   ├── pgen.py                    ← PGenerator TCP adapter + NullPatchDisplay
│   ├── engine.py                  ← calibration phases (baseline, levels, WB, gamma, CMS, verify)
│   ├── picture.py                 ← picture-setting registry: snapshot/restore/neutralize/nudge
│   ├── osd.py                     ← RGBCMY menu driver (remote-key presses) for automatic CMS
│   ├── profiles.py                ← save/load/apply/list/delete profiles (full settings snapshot)
│   ├── discovery.py               ← mDNS + parallel port scan
│   ├── server.py                  ← FastAPI app, REST + WebSocket
│   └── agents/
│       ├── __init__.py
│       ├── base.py                ← shared Anthropic client, MODEL_OPUS/HAIKU constants
│       ├── results_analyst.py     ← post-run analysis (Opus + adaptive thinking)
│       ├── anomaly_detector.py    ← live variance detection (Haiku)
│       ├── profile_advisor.py     ← profile comparison narrative (Opus)
│       └── setup_validator.py     ← pre-flight checklist (Haiku, rule-based fallback)
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_color_math.py
    ├── test_engine.py
    ├── test_picture.py
    ├── test_pgen.py
    └── test_probe.py
```

---

## Implementation Order (when rebuilding from scratch)

1. `config.py` + `color_math.py` — no hardware; fully unit-tested
2. `projector.py` + `probe.py` — socket only, no patch display
3. `colorimeter.py` — POSIX pty; skip on Windows
4. `pgen.py` — simple TCP + NullPatchDisplay
5. `engine.py` — depends on all hardware modules
6. `profiles.py` — pure Python, no hardware
7. `discovery.py` — network scan
8. `agents/` — Anthropic SDK; all four agents
9. `server.py` — FastAPI; depends on everything
10. `static/` — SPA; index.html + app.js + styles.css
11. `cli.py` + `__main__.py`

---

## Critical Technical Details

### ESC/VP21 Protocol

- TCP port **3629**
- Handshake bytes (send on connect, read 16-byte ACK):
  ```python
  bytes.fromhex("455343 2F56502E 6E657410 03000000 0000".replace(" ", ""))
  ```
- SET command: `b"TOKEN VALUE\r"` → response `b":"` (success) or `b"ERR"`
- QUERY command: `b"TOKEN?\r"` → response `b"TOKEN=VALUE:\r"`
- Reconnect **once** on broken-pipe before raising `ProjectorError`
- Always sleep `command_settle_ms` (default 200ms) after each SET

### ESC/VP21 Command Tokens

Source: Epson's ESC/VP21 command list
(https://files.support.epson.com/pdf/escvp21/escvp21all_en_revs_20250717.zip), sheet
"TW9300" — it covers the HC5040UB. Verified live against a 5040UB.

- **Control4/AMX must be enabled** in the projector's Network menu. Without it only
  status queries (`PWR?`, `LAMP?`, `SIGNAL?`) answer; every settings query returns `ERR`.
- WB gain: `GAINR` / `GAING` / `GAINB` (0–255). Offsets: `OFFSETR/G/B` (unused).
  Writes are **quantized to ~2.5-unit steps** (`GAINR 127` reads back 126).
- Picture mode: `CMODE` (06 Dynamic, 07 Natural, 0C Bright Cinema, 15 Cinema,
  22 Digital Cinema). HDR mode: `DYNRANGE` (00 Auto, 01 SDR, 02–05 HDR Mode 1–4).
- **No CMS (RGBCMY hue/sat/brightness) commands exist.** `CSEL?` is read-only;
  `KEY 89` only presses the remote's RGBCMY button. CMS is a guided manual phase.
- `SIGNAL?` is a status query — never treat it as a picture-mode command.

The probe (`/api/probe`) first checks `PWR?` (fails fast on a dead link), then tries
the WB candidates (`GAINx` first) and `CMODE?`/`DYNRANGE?`, and writes
`configs/command_table.json`:

```json
{
  "white_balance": {"R": "GAINR", "G": "GAING", "B": "GAINB"},
  "picture_mode": {"sdr": [["DYNRANGE", "01"], ["CMODE", "07"]], "hdr10": [["DYNRANGE", "05"]]}
}
```

`picture_mode.<mode>` is a list of `[command, value]` pairs sent in order. SDR forces
`DYNRANGE 01`: with automatic detection (`00`, or the undocumented `10` this projector
reports) the 5040UB decoded the Pi's SDR signal as HDR — every grey above ~66% (code
160) clipped to white and the shadows were crushed. The server reads `DYNRANGE` before
switching and restores it when the run ends, so everyday sources keep auto-switching.
Dry runs don't write, so they warn when `DYNRANGE` doesn't match the mode. A legacy bare
string value is still accepted and sent as `<picture_mode.command | hdr.picture_mode_command> <value>`.

### ArgyllCMS spotread Subprocess

`spotread` is interactive and requires a controlling terminal. Approach:

```python
import os, fcntl, termios

master_fd, slave_fd = os.openpty()

def preexec():
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

proc = subprocess.Popen(
    ["spotread", "-e", "-c", str(display_index)],  # -c = instrument; -d is "density"
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    env={**os.environ, "ARGYLL_NOT_INTERACTIVE": "1"},
    preexec_fn=preexec, close_fds=True,
)
os.close(slave_fd)  # parent only needs master
```

- `colorimeter.ccss_path` (default: the bundled Epson TW9400/5050UB CCSS) adds `-X <file>` —
  spectral correction for the lamp; without it the i1 Display Pro can be several ΔE off at white
- If spotread exits during startup or a measurement (bad -X file, instrument missing), its
  output is surfaced in a `MeasurementError` immediately instead of a 30 s timeout
- Trigger measurement: `os.write(master_fd, b"\n")`
- Parse output: `Result is XYZ: X Y Z` (regex)
- Take 3 readings per measurement, reject outliers by Y std dev, return mean
- **colorimeter.py is POSIX-only** — import fails gracefully on Windows with `ImportError`

### PGenerator Wire Protocol

Text commands on TCP port 85, each terminated by `b"\x02\r"`; every command gets
a reply with the same terminator (the protocol quietvoid/pgen_client speaks):

| Command | Reply |
|---------|-------|
| `IS_ALIVE` | `ALIVE` |
| `CMD:GET_MODE` | `OK:0[3840x2160 30.00Hz ...]` |
| `CMD:GET_PGENERATOR_CONF_RGB_QUANT_RANGE` | `OK:1` (1 = limited 16–235, 2 = full) |
| `CMD:GET_PGENERATOR_CONF_MAX_BPC` / `_COLOR_FORMAT` | `OK:8` / `OK:0` (0 RGB, 1 YCbCr 4:4:4, 2 4:2:2) |
| `CMD:SET_PGENERATOR_CONF_<NAME>:<value>` | `OK:` (IS_SDR, IS_HDR, COLORIMETRY 2=BT.709 9=BT.2020, MAX_BPC, EOTF 0=SDR 2=PQ, PRIMARIES 2=P3-D65, COLOR_FORMAT, …) |
| `RESTARTPGENERATOR:` | applies output settings; `CMD:GET_PGENERATOR_IS_EXECUTED` → `OK:Pid N` changes when back (~8 s) |
| `RGB=RECTANGLE;w,h;0;r,g,b;bg_r,bg_b,bg_g;0,0,x,y;-1` | `OK` (8-bit; background is r,b,g) |
| `RGB=RECTANGLE10bit;…` | same, 10-bit codes (non-RGB formats take an 8-bit background) |
| `QUIT` | — |

`PGenClient.connect()` checks `IS_ALIVE` and reads resolution, RGB range, bit depth
and color format. `RGBPatch` carries full-range codes at 8 or 10 bits; they are
mapped to the Pi's depth and range (limited: 16–235 / 64–940), so white is always
peak white. Patches are full-screen; `display_window` draws a centred box.

At the start of every run the server calls `configure_output(mode, color_format)`
(`pgen.configure_output`, default on): SDR → SDR, BT.709, 8-bit, `pgen.sdr_color_format`
(rgb); HDR10 → HDR, PQ, BT.2020, P3 primaries, 10-bit, `pgen.hdr_color_format`
(ycbcr422 — 10-bit RGB/4:4:4 at 4K30 may exceed the 5040UB's HDMI bandwidth; 4:2:2
loses nothing on full-field patches). Only differing settings are written, and
PGenerator is restarted only if something changed. HDR metadata is left as set on
the Pi. Reconnect once on a dropped connection.

### HDR10 patches

HDR10 is absolute: PQ (ST 2084) in a BT.2020 container. `color_math.hdr_patch_code10`
converts each patch's target XYZ (white = 100) to linear BT.2020, scales white to
`calibration.hdr_white_nits` (default 100 cd/m²) and PQ-encodes per channel → 10-bit
codes. Every patch is therefore its target colour at the target's *relative*
luminance, so the existing relative targets (P3-D65 colors, gamma-2.2 greys) apply
unchanged in HDR. Levels patterns use the same relative luminance PQ-encoded. In SDR,
patches stay 8-bit (10-bit adds nothing: patches and targets are defined on 8-bit codes).

### Color Science

**Critical: use D65, not D50, for Lab conversions.**

spotread outputs Lab under D50, but Rec.709 and P3 targets are specified under D65.
**Always convert raw XYZ directly to Lab under D65, ignoring spotread's Lab output.**

```python
from colormath.color_objects import XYZColor, LabColor
from colormath.color_conversions import convert_color

# ALWAYS cast to float() before constructing LabColor — numpy scalar bug
xyz = XYZColor(float(X), float(Y), float(Z), illuminant="d65")
lab = convert_color(xyz, LabColor)
```

D65 white reference (Y=100 scale): `(95.047, 100.000, 108.883)`

**SDR Targets (Rec.709/D65):**
| Patch   | x      | y      | Y      |
|---------|--------|--------|--------|
| white   | 0.3127 | 0.3290 | 100.0  |
| red     | 0.6400 | 0.3300 | 21.26  |
| green   | 0.3000 | 0.6000 | 71.52  |
| blue    | 0.1500 | 0.0600 | 7.22   |

**HDR10 Targets (P3-D65):**
| Patch   | x      | y      | Y      |
|---------|--------|--------|--------|
| white   | 0.3127 | 0.3290 | 100.0  |
| red     | 0.6800 | 0.3200 | 22.90  |
| green   | 0.2650 | 0.6900 | 69.17  |
| blue    | 0.1500 | 0.0600 | 7.93   |

**HDR10 white is D65 (0.3127, 0.3290) — NOT DCI native (0.314, 0.351).**

The 5040UB covers ~100% DCI-P3 but NOT Rec.2020. Target P3-D65 for HDR10.

### Calibration Algorithm

Proportional control with decaying step scale (not PID — no time constants needed):

```
step_scale = initial (0.8)

for each iteration:
    measure XYZ → normalize to reference white → compute Lab → compute ΔE
    if ΔE < threshold: converged; break
    if ΔE did not improve for 3 consecutive iterations: revert to best-known settings; break
    compute correction proportional to error × step_scale
    apply correction to projector
    step_scale = min(initial, max(minimum, step_scale × (ΔE / threshold) × 0.5))
```

**Relative colorimetry (critical):** spotread returns absolute XYZ in cd/m², but all
targets are relative to white = 100. The engine measures a reference white (end of
phase 1, or a dedicated white measurement at the start of phase 2/3) and rescales
every reading by `100 / Y_white` before comparing to targets. Never compare raw
absolute readings to the target tables — ΔE becomes dominated by a bogus
luminance error and the loop cannot converge.

**Step clamp:** the raw decay formula *grows* when ΔE > 2× threshold, so it is
clamped to the initial value. A divergence guard (3 consecutive non-improving
iterations → revert to best-known settings and stop the patch) protects against
unstable loops and wrong control-direction assumptions.

`engine.run(phases)` always runs **Baseline** first and **Verification** last; the
requested phases run in between in canonical order `levels → wb → gamma → cms`
(`engine.PHASES`). `POST /api/run` `phase` is `all` or one of those.

**Passes:** levels, WB and gamma move each other, so they repeat (`calibration_passes`,
default 3) until a pass emits no `correction` event (`pass_done {pass, corrections}`); CMS
runs once afterwards. The engine wraps `on_event` to count corrections and to collect
`report.setting_changes` (`{axis: [first before, last after]}`).

**Targets:** correction loops (WB, gamma, auto CMS) stop at `correction_target`
(default config 0.5); verification pass/fail stays at `delta_e_threshold` (1.0).

Baseline (every run):
- `picture.read_settings` snapshots every setting in `picture.PICTURE_SETTINGS`
  (color mode, dynamic range, lamp power, brightness, contrast, color/tint/skin tone,
  color temp, gains, offsets, gamma preset, 9 custom gamma points, processing) → `baseline` event, `report.baseline`
- Unless dry-run (or `apply_baseline_neutral: false`): DENSITY/TINT/FCOLOR → `INIT`
  (factory neutral), IMGPRESET and MCFI → `00` (off)
- Auto Iris is not in Epson's command list — the UI and the levels step tell the user to turn it off

Levels (`levels_mode: "measured"`, default; dry runs and `"guided"` use the by-eye flow):
- Brightness: measure white, black (code 0) and near-black (code 10, ~4%). Brightness offsets
  the signal *before* gamma, so the black→near-black step shrinks steadily as Brightness
  drops (it is not constant — an earlier version assumed so and stopped far too high, 136
  vs ~124). The reference is the gamma target step `(10/255)^2.2 × white`: walk down while
  the measured step keeps ≥ 80% of it (up, if already below), keep the lowest such value.
  Judged with `GAMMA 22` (preset "0") swapped in and the custom curve restored afterwards —
  measured through the custom curve, Brightness and gamma fed each other across passes
  (136 → 114 → 103 while gamma raised every point ~25)
- Contrast: measure 100% and 96% (code 245). Unclipped, Y96/Y100 = (245/255)^2.2; clipping
  pushes it toward 1 and one channel clipping first shows as a 96%-vs-100% chromaticity
  shift. Walk up while ratio ≤ gamma + 1% and shift ≤ ΔE 1.0 (or down if already clipping)
- Room light raises black: the 2026-10-06 mode survey measured only ~160–280:1 on/off
  contrast, so near-black judgements sit on an ambient-light floor
- Guided flow: 2% box on black / 98% box on white (`display_window`), `manual_step` events,
  UI −/+ → `POST /api/run/action {action: "adjust", token, direction}` → `picture.nudge`

Phase 1 — White Balance (two-point):
- Pass 1 (`ctemp_select`): gains reset to `wb_gain_ceiling` (126 = menu 50, neutral), then a
  1-D search over the Color Temp preset (`CTEMP`, ~10 positions across 0–255 → `nudge` tries
  up to 40 raw units) for the white nearest D65 — the gains then barely have to move
- Offsets use `_wb_offset_point`: one channel per move, a few notches, per-notch effect
  re-estimated (signed) from every move — the gains' proportional formula overshot badly
  (one blue notch: ΔE 3.6 → 13.7 at 30% grey)
- Gains on white, then offsets (`OFFSETR/G/B`) on `wb_offset_patch` (default grey30),
  repeated up to `wb_two_point_rounds` (3) until a round needs no correction at either
  point; falls back to gain-only if the projector rejects `OFFSETR?`
- Each reading normalized to Y=100 → chromaticity-only ΔE against D65
- `delta = -round(channel_err × step_scale × gain_range)` where `channel_err = (meas − target) / target`
- `wb_maximize_light`: after each gain correction all three shift so the strongest channel
  sits at `wb_gain_ceiling` — white comes from lowering the others, never from dimming all
  (first real run, without it: R 126→96, B 108→86 with G fixed at 111)
- A nonzero delta smaller than `wb_gain_min_step` (default 3) is widened to it — the
  5040UB ignores ±1 writes, which would stall the loop and trip the divergence guard
- After each write the value is read back; the loop tracks what the projector stored

Gamma (SDR only; skipped with a warning in HDR10, where the projector tone-maps PQ):
- Sets `GAMMA F0` (custom); fits all 9 `GAMMALV` tones together to the 11-grey ramp
  (`GREY_RAMP` 5, 10…90, 95 — 5% and 95% reach the curve's end tones 00 and 08)
- Jacobian dL*/dunit (11 × 9) measured once per run: each tone bumped by 20, ramp re-read,
  restored (~10 min). Then per iteration: measure white + ramp, solve the damped least
  squares `(JᵀJ + 0.02·mean(diag JᵀJ)·I) Δ = Jᵀe` for all tones (clip ±15, drop |Δ|<1.5),
  write, re-measure, Broyden-update J. Stops at `correction_target` (per-grey ΔL as
  CIEDE2000), when no tone can move a full step, or reverts to the best tones after 3
  non-improving iterations. Later passes reuse J
- Measured on the 5040UB (+30 units): tones 01–07 sit roughly 10% apart from black and
  overlap; 05 pulls 50–60% down; 07 is weak (+4% at 70–80%). Reviewers called the custom
  curve "mostly ineffective" — the measurements show it works, just with overlap

Phase 2 — CMS (6 axes: red, green, blue, cyan, magenta, yellow). The 5040UB has no CMS
commands, so `calibration.cms_mode` picks how:

Automatic (`"auto"`, default; needs `cms_menu`, never in dry runs):
- `osd.RgbcmyMenu` drives the menu with `KEY xx` presses, mapped with a webcam:
  Esc×3 (close anything) → `KEY 89` opens the RGBCMY grid with the cursor ALWAYS on R →
  Down×row → Enter opens the colour panel (Hue selected) → Up/Down picks Hue/Saturation/
  Brightness → Right/Left = ±1 menu step → Esc×3 closes. Right on Hue = toward the next
  colour on the hue wheel (the slider is labelled, e.g. red M◀▶Y)
- The open panel doesn't change readings at screen centre, so the loop measures with it open
- `cms_reset` (default on): each control is first returned to 50 with the remote's Default key
  (`KEY 88` resets only the selected control, no confirmation) — absolute values are then
  known (`RgbcmyMenu.values` → `report.cms_values`, storable in profiles)
- `cms_sat_levels` (default `[75, 100]`): every iteration measures the axis at each level;
  the loop minimises the mean ΔE and converges when the worst level is under target. On the
  5040UB, correcting 100% alone (red Hue +19) left red at 75% saturation ΔE 7.7 (hue off by
  14°) — the projector's CMS rotates mid-saturations differently from full colour
- Loop per axis: measure → `cms_errors` (hue °, chroma %, L*) → move the control with the
  largest error/noise ratio by `round(err / gain)` steps (≤ 8, ≤ ±30 total) → re-measure.
  Each control's gain (error per step, signed) is re-estimated from every move above the
  noise floor, so unknown sensitivities and inverted controls are learned. Divergence guard
  reverts to the best offsets. The menu is always closed afterwards (also on abort)
- Menu end stops (0/100): two moves in one direction with no measurable effect block that
  direction, emit a `warning`, and list the control in `report.cms_at_limit` — its step count
  overstates the real travel (first real run: cyan saturation 75 + 30 steps → stuck at 100).
  A single no-effect move is tolerated (red brightness has a dead zone just below 61)
- Values can't be read back: `RgbcmyMenu.offsets` / `report.cms_adjustments` record net steps
- Verified live: red ΔE 3.11 → 0.95 in 7 readings (~3 min); step counts exact (webcam-checked)

Guided (`"guided"`, dry runs, or no menu driver):
- Display each patch, measure; if ΔE ≥ threshold emit `cms_guidance` and block until
  the user answers via `POST /api/run/action` (`remeasure` or `next`); abort releases the wait
- Guidance (`engine.cms_guidance`), largest error first: hue as "shift toward <neighbor>"
  (Lab hue wheel red→yellow→green→cyan→blue→magenta; convention-free, since the menu's
  Hue +/- direction is unknown), saturation as chroma % low/high, brightness as L* low/high
- No writes to the projector, no step decay, no divergence guard in this phase
- Non-interactive engines (tests, `interactive=False`) measure each axis once

Phase 3 — Verification:
- Measure 36 patches without applying corrections: white (first — it re-anchors the
  reference), the 6 colors, the grey ramp grey95…grey5, and 25/50/75% saturation sweeps of
  each color (`red_sat50`: the colour blended with white in linear light — 1 on its own
  channels, 1 − sat on the others, gamma-2.2 encoded; target from the same additive model)
- Record final ΔE for report

Convergence threshold: ΔE < 1.0 (configurable, default 1.0)
Max iterations per patch: 20 (configurable)

### 5040UB HDR Specifics

- HDR10 only (no HLG, no Dolby Vision)
- Peak brightness: ~100-110 nits
- HDR has 4 modes; Mode 4 is darkest/most accurate for calibration
- WB and CMS are accessible in HDR mode
- SDR and HDR settings are **completely separate** — calibrate each independently
- `DYNRANGE 05` (HDR Mode 4) is forced for HDR10 runs; the Pi is switched to an HDR10 signal
- The gamma phase is skipped in HDR10 (the projector tone-maps PQ; gamma 2.2 doesn't apply)
- Unverified: that the 5040UB locks to 4K30 10-bit 4:2:2 HDR from the Pi

---

## WebSocket Event Schema

All engine events are broadcast as JSON objects. Key events:

```json
{"event": "phase_start",    "phase": "levels|wb|gamma|cms|verify", "patch_count": 9}
{"event": "baseline",       "settings": {"CMODE": "07", "GAMMALV 03": "140", ...}, "neutralized": {"DENSITY": "INIT", ...}}
{"event": "manual_step",    "step": "brightness|contrast", "title": "...", "instructions": ["..."],
                            "controls": [{"token": "BRIGHT", "label": "Brightness", "value": "128"}]}
{"event": "manual_step_done", "step": "brightness"}
{"event": "pgen_output",    "mode": "sdr|hdr10", "color_format": "rgb", "changed": {"MAX_BPC": "10", ...}}
{"event": "patch_start",    "patch": "white", "iteration": 1, "mode": "wb|cms|verify"}
{"event": "measurement",    "patch": "white", "xyz": [X,Y,Z], "delta_e": 0.42}
{"event": "correction",     "patch": "white", "axis": "WB_R", "before": 128, "after": 131}
{"event": "cms_guidance",   "patch": "red", "iteration": 1, "delta_e": 3.2,
                            "guidance": [{"control": "hue|saturation|brightness", "error": -4.1, "instruction": "Shift Hue toward magenta (4.1° off)"}]}
{"event": "patch_done",     "patch": "white", "delta_e": 0.38, "iterations": 4, "converged": true}
   // patch_done delta_e is null when the patch was never measured (aborted run)
{"event": "phase_done",     "phase": "wb"}
{"event": "run_complete",   "report": {...}}
{"event": "error",          "message": "..."}
{"event": "warning",        "message": "..."}   // non-fatal; the run continues
{"event": "probe_progress", "message": "WB.R: found 'WBALGAINR' = 128"}
{"event": "probe_done",     "missing": [], "summary": [...]}
{"event": "discovery_start","method": "mdns+port_scan", "subnet": "192.168.1.0/24"}
{"event": "discovery_found","device": {"device_type": "projector", "ip": "...", ...}}
{"event": "discovery_done", "total": 2, "devices": [...]}
{"event": "agent_result",   "agent": "results_analyst", "result": {...}}
{"event": "setup_check",    "ready": true, "checklist": [...], "blocking_issues": []}
{"event": "profile_applied","profile": "My SDR Profile"}
```

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET    | `/api/config` | Return current config as JSON |
| POST   | `/api/config` | Update config (body: `{config: {...}}`) |
| POST   | `/api/probe` | Start probe in background thread |
| POST   | `/api/run` | Start calibration (`{mode, phase: all\|levels\|wb\|gamma\|cms, dry_run}`) |
| POST   | `/api/run/stop` | Send abort signal to engine |
| POST   | `/api/run/action` | Answer a guided prompt: `{action: "remeasure"\|"next"}` for CMS, `{action: "adjust", token, direction: ±1}` or `"next"` for a manual step |
| GET    | `/api/warmup` | Single colorimeter reading for warm-up monitor |
| GET    | `/api/profiles` | List all saved profiles |
| POST   | `/api/profiles` | Save current projector state as profile |
| DELETE | `/api/profiles/{filename}` | Delete a profile |
| POST   | `/api/profiles/{filename}/apply` | Apply profile to projector |
| POST   | `/api/profiles/compare` | AI comparison of two profiles (`{a: filename, b: filename}`) |
| POST   | `/api/discover` | Start mDNS + port scan |
| GET    | `/api/discover/last` | Load cached scan results |
| POST   | `/api/discover/confirm/{ip}` | Verify a specific device (`{device_type}`) |
| GET    | `/api/setup/status` | Lightweight readiness facts: `{command_table_complete, colorimeter_available}` — drives the guided-setup UI |
| POST   | `/api/setup/validate` | Run pre-flight checklist |
| POST   | `/api/test-connection` | Test projector or pgen TCP connection |
| WS     | `/ws` | WebSocket for live event stream |

---

## Claude API Agents

```python
MODEL_OPUS  = "claude-opus-4-8"        # complex reasoning
MODEL_HAIKU = "claude-haiku-4-5-20251001"  # fast/cheap, real-time
```

Structured output uses the current API shape:
`output_config={"format": {"type": "json_schema", "schema": SCHEMA}}`
(no nested `json_schema`/`name`/`strict` wrapper — that shape is obsolete).

| Agent | Model | When called | Output schema |
|-------|-------|-------------|---------------|
| `results_analyst` | Opus + adaptive thinking | After run completes | `{summary, issues, recommendations, overall_grade}` |
| `anomaly_detector` | Haiku | Every 3rd iteration if Y variance > 2% | `{anomaly, type, action, reason}` |
| `profile_advisor` | Opus + adaptive thinking | When user compares two profiles | `{narrative, delta_e_delta, verdict, notable_changes}` |
| `setup_validator` | Haiku (rule-based fallback) | Setup Wizard Step 4 | `{ready, checklist, blocking_issues}` |

All agents use structured JSON output via `output_config.format.type = "json_schema"`.
All agents degrade gracefully if `ANTHROPIC_API_KEY` is not set.

Billing note: Haiku agents cost ~$0.001/call; Opus ~$0.05-0.10/call.
Estimated total per calibration session: ~$0.10–0.15.

---

## Network Discovery

Two strategies, run concurrently:

1. **mDNS** (`zeroconf`): listens for `_epsonprojector._tcp.local.` and `_pgenerator._tcp.local.`
2. **Port scan**: connects to all /24 subnet hosts on ports 3629 (projector) and 85 (pgen) using `ThreadPoolExecutor(max_workers=50)`, 0.5s connect timeout

Projector verification: full ESC/VP.net handshake (send magic bytes, read 16-byte ACK).
PGenerator verification: `IS_ALIVE` → `ALIVE` handshake (via `PGenClient.connect()`).

Results cached to `discovery/last_scan.json` (gitignored).

---

## Colorimeter Placement (CCDIS3)

**Front-projection mode (this project):**
- Mount on tripod, aimed at screen center
- Distance: 6–18 inches from screen surface
- Remove white diffuser cap from sensor
- Does NOT require flush contact with screen
- Room must be dark (dim all ambient lights)
- The suction-cup flush method is for rear-projection or display panels only

---

## Configuration (default_config.json)

```json
{
  "projector": {
    "host": "192.168.1.100", "port": 3629,
    "connection_timeout": 5.0, "command_timeout": 2.0, "command_settle_ms": 200
  },
  "colorimeter": {
    "spotread_path": "spotread", "display_index": 1,
    "measurement_retries": 3, "measurement_timeout": 30.0,
    "ccss_path": "configs/ccss/epson_tw9400_5050ub_i1pro.ccss"
  },
  "pgen": { "host": "192.168.1.101", "port": 85, "patch_settle_ms": 500,
            "configure_output": true, "sdr_color_format": "rgb", "hdr_color_format": "ycbcr422" },
  "calibration": {
    "delta_e_threshold": 1.0, "max_iterations_per_patch": 20,
    "wb_gain_center": 128, "wb_gain_range": [0, 255], "wb_gain_min_step": 3,
    "proportional_gain_initial": 0.8, "proportional_gain_minimum": 0.1,
    "cms_axis_order": ["red","green","blue","cyan","magenta","yellow"],
    "wb_offset_patch": "grey30", "wb_two_point_rounds": 3,
    "correction_target": 0.5, "calibration_passes": 3, "levels_mode": "measured",
    "ctemp_select": true, "wb_maximize_light": true, "wb_gain_ceiling": 126, "cms_mode": "auto",
    "apply_baseline_neutral": true,
    "hdr_white_nits": 100
  },
  "hdr": {
    "picture_mode_command": "PMOD", "hdr10_mode_value": "HDR4",
    "mode_switch_settle_ms": 3000
  },
  "log_level": "INFO"
}
```

---

## Known Pitfalls / Do Not Reintroduce

1. **numpy scalar bug in colormath**: Always cast XYZ values to `float()` before passing to `XYZColor()` or `LabColor()`. Numpy scalars produce silently wrong ΔE results.

2. **D50 vs D65**: spotread's Lab output is under D50. Rec.709 and P3 targets are under D65. Never use spotread's Lab values directly — always re-derive Lab from raw XYZ under D65.

3. **HDR10 white point is D65, not DCI**: P3-D65 uses white point (0.3127, 0.3290). DCI-native white is (0.314, 0.351). The 5040UB targets D65 in HDR.

4. **spotread interactivity**: spotread stalls without a controlling terminal. The `os.openpty()` + `os.setsid()` + `TIOCSCTTY` approach is load-bearing. Do not replace with `subprocess.PIPE`.

5. **colorimeter.py is POSIX-only**: `import colorimeter` raises `ImportError` on Windows. `server.py` catches this and falls back to a dummy measure function for dry-run testing.

6. **PGenerator speaks a text protocol, not raw bytes**: the old 3-byte `[R,G,B]` packet was silently ignored by the Pi — no patch ever appeared. Use `RGB=RECTANGLE;…` + `\x02\r`, and respect the Pi's RGB quant range (limited = 16–235).

7. **command_table.json must be probed first**: The default file has Epson's documented tokens, unverified against the user's projector. Run `/api/probe` (or `projector-cal --probe` if CLI mode added) before first calibration. The placeholder file carries a `"_note"` key which `load_command_table` deliberately preserves — `probe.is_command_table_complete()` treats its presence as "not probed yet" (the probe writes the table without it). Do not strip `_note` on load.

8. **SDR and HDR settings are independent on 5040UB**: Calibrating in SDR does not affect HDR settings and vice versa. Run separate calibration sessions for each mode.

9. **Progress counting comes from `phase_start.patch_count`**: WB 2 (white + offset grey; 1 if gain-only), gamma 11, CMS 6, verify 36, levels 0 — per pass, so the bar grows with each pass. Every patch the counter expects must get exactly one `patch_done` — WB rounds emit theirs once, after the last round.

10. **Profile filenames**: Derived from `name_mode_timestamp.json`. The JS re-derives the filename client-side using the same Python logic — keep them in sync.

11. **colormath XYZColor is on the 0–1 scale**: reference white is (0.95047, 1.0, 1.08883). Passing Y=100-scale values makes white come out as L*≈522 and silently distorts every ΔE. `xyz_to_lab()` divides by 100 before constructing `XYZColor` — keep it that way.

12. **numpy ≥1.23 removed `asscalar`**, which python-colormath 3.0 still calls. `color_math.py` installs a shim (`np.asscalar = lambda a: a.item()`) before importing colormath. Without it every `delta_e` call raises AttributeError.

13. **Never rescale a patch target to Y=100 in `get_target_lab`**: target xyY luminances are already relative to white=100. Per-patch rescaling makes every target as bright as white (red would get L*=100 instead of ~53).

14. **spotread readings are absolute cd/m²** — always normalize against the measured reference white before comparing to targets (see Calibration Algorithm → Relative colorimetry).

15. **Secondary targets are derived, not hardcoded**: cyan/magenta/yellow xyY are computed at import time by summing the XYZ of their constituent primaries (`color_math._build_targets`). Hardcoded HDR10 secondaries were several ΔE off. Grey targets use display gamma 2.2.

16. **Dry run = no projector writes, but patches still display**: a dry run drives PGenerator (falling back to NullPatchDisplay with a `warning` only if the Pi is unreachable) so readings mean something. **Dummy measurements are dry-run only**: `server.py` refuses a real (non-dry) run when the colorimeter module is unavailable. Driving corrections from random readings would walk the projector's settings randomly.

17. **No RGBCMY commands over ESC/VP21** (checked against Epson's list and ~900 undocumented guesses). Automatic CMS goes through the OSD with `KEY` presses (`osd.py`); values can't be read back, so profiles store `cms_values: {}` and `apply_profile` only logs old CMS values. Don't reintroduce `get_cms`/`set_cms`. The user must not touch the remote during an automatic CMS phase — a stray key desynchronizes the menu cursor (each axis re-homes, which limits the damage).

18. **Gain writes are quantized** (~2.5 units): ±1 writes are accepted but change nothing. Keep `wb_gain_min_step` and the read-back after `set_wb_gain`.

19. **Control4/AMX off ⇒ all settings ERR**: if the probe finds `PWR?` answering but no WB token, that is the cause — not wrong token names.

20. **Epson stores image settings per color mode**: restoring a snapshot must set `CMODE` first, and `GAMMA` before the `GAMMALV` points (`picture.PICTURE_SETTINGS` order). Applying a snapshot in another order silently writes values into the wrong mode.

21. **Verified on hardware 2026-10-06**: `GAMMALV? xx` replies `GAMMALV=66` (no parameter echo); higher GAMMALV brightens its tone (tone 00 +27 → 10% grey +18%); `INIT` gives DENSITY 126 / TINT 126 / FCOLOR 93 (factory defaults). Tone ↔ grey-level map: see the Gamma phase above. The tracked CCSS shifted a white reading by ΔE 2.1 vs uncorrected.

22. **DYNRANGE auto-detection misreads the Pi's SDR as HDR** (see Picture mode above): never measure SDR without `DYNRANGE 01`. Symptom: 70/80/90% grey read the same as white.

---

## Build & Run

```bash
# Install
cd D:\projectorAutoCal
pip install -e ".[dev]"

# Run tests (no hardware needed)
pytest tests/ -v

# Start server
python -m projector_cal
# or: projector-cal --config my_config.json --port 8080 --verbose

# First run: probe the projector for command tokens
# → Setup tab → "Run Probe" button

# Then: warm up projector 30 min, place colorimeter, click Validate, then Start
```

---

## Web UI — 4 Tabs

| Tab | Key features |
|-----|-------------|
| **Setup** | Guided flow: workflow progress strip, then numbered steps — 1 connections (Save & Test persists fields via POST /api/config; the run reads the saved config, not the fields), 2 command probe, 3 screen info (optional), 4 placement checklist, 5 warm-up + pre-flight validation; AI settings card at the bottom. Step checkmarks driven by GET /api/setup/status + client state |
| **Run** | Readiness banner (warns if command table unprobed or colorimeter unavailable on host), mode/phase selector with first-run guidance, dry-run toggle, start/stop (Start re-saves Setup fields first), guided levels card (instructions, −/+ per control, Done), guided-CMS card (instructions + Re-measure / Accept & next), patch grid (white, colors, grey ramp) with live ΔE, progress bar, live log |
| **Before/After** | ΔE comparison table (before = initial ΔE, after = verify pass), AI Analysis card (grade + issues + recommendations) |
| **Profiles** | Grouped by mode, apply/delete buttons, save-current-state (full picture-settings snapshot; apply restores it in dependency order — CMODE first, GAMMA before GAMMALV), AI profile comparison |

All tabs receive real-time updates via WebSocket (`/ws`).

---

## Dependencies

```
python-colormath>=3.0.0
numpy>=1.24
fastapi>=0.110
uvicorn[standard]>=0.27
anthropic>=0.25
zeroconf>=0.130
netifaces>=0.11
```
