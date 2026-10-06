# ProjectorCal — Detailed Technical Design

This document captures the full design decisions, data flows, and protocol
specifications for the closed-loop calibration system. Read before modifying
any core module.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Host machine (Linux/macOS)            │
│                                                         │
│  ┌──────────┐   ┌────────────┐   ┌───────────────────┐ │
│  │ Browser  │◄──│  FastAPI   │◄──│  CalibrationEngine │ │
│  │  (SPA)   │   │  server.py │   │    engine.py       │ │
│  └──────────┘   └────────────┘   └─────────┬─────────┘ │
│       │WS            │                      │            │
│       └──────────────┘              ┌───────┼────────┐  │
│                                     │       │        │  │
│                              projector  colorimeter pgen│
│                              .py        .py        .py  │
└─────────────────────────────────────────────────────────┘
         │TCP:3629              │USB         │TCP:85
         ▼                      ▼            ▼
   ┌──────────┐          ┌──────────┐  ┌──────────┐
   │  Epson   │          │  CCDIS3  │  │ RPi 4 /  │
   │  5040UB  │          │colorimeter│  │PGenerator│
   └──────────┘          └──────────┘  └──────────┘
                                            │HDMI
                                            ▼
                                      ┌──────────┐
                                      │Projector  │
                                      │  screen   │
                                      └──────────┘
```

The engine controls all three hardware devices simultaneously:
1. Sends a patch RGB to PGenerator → displayed via HDMI on screen
2. Waits `patch_settle_ms` (default 500ms) for projector to stabilize
3. Triggers colorimeter measurement of the projected patch
4. Computes ΔE against target, calculates correction
5. Sends correction to projector via ESC/VP21
6. Repeats until ΔE < threshold or max iterations reached

---

## Module Responsibilities (strict layering)

```
cli.py
  └── server.py (FastAPI)
        ├── engine.py (calibration loop)
        │     ├── projector.py (ESC/VP21)
        │     ├── pgen.py (PatchDisplay adapter)
        │     ├── colorimeter.py (spotread)
        │     └── color_math.py (pure math)
        ├── profiles.py
        ├── discovery.py
        └── agents/ (Claude API)

config.py  ← loaded at startup, passed down by reference
```

No module imports from `server.py`. No module imports from `cli.py`.
`engine.py` does not import from `agents/` — the server calls agents after
the run completes.

---

## ESC/VP21 Protocol (full specification)

### Connection

```
TCP connect to projector:3629
Send: 455343 2F56502E 6E657410 03000000 0000  (16 bytes)
Read: 16 bytes (ACK — content ignored, just check length)
```

### Command format

```
SET:    b"TOKEN VALUE\r"     → response b":"         (success)
                             → response b"ERR"        (rejected)
QUERY:  b"TOKEN?\r"         → response b"TOKEN=VALUE:\r"
                             → response b"ERR"        (unknown token)
```

No newline in the response — read until `:` character or `ERR`.

### Settle time

After every SET command, sleep `command_settle_ms` (default 200ms).
The projector's internal adjustment circuits need time to stabilize.
Do not reduce below 100ms without testing convergence stability.

### Reconnect policy

On `BrokenPipeError` or `ConnectionResetError` during send:
1. Close socket
2. Reconnect (full handshake)
3. Retry the send once
4. If retry fails, raise `ProjectorError`

### White Balance tokens

From Epson's ESC/VP21 command list (sheet "TW9300", which covers the HC5040UB),
verified live:

| Channel | Token | Fallback candidates (other firmware) |
|---------|-------|--------------------------------------|
| R | `GAINR` | `WBALGAINR`, `WBGAINR`, `CRED`, `WBRED`, `BCRED`, `RGAIN` |
| G | `GAING` | `WBALGAING`, `WBGAING`, `CGRN`, `WBGRN`, `BCGRN`, `GGAIN` |
| B | `GAINB` | `WBALGAINB`, `WBGAINB`, `CBLU`, `WBBLU`, `BCBLU`, `BGAIN` |

Gain range: 0–255 (center = 128 = no offset). Only WB.G should be held
near 128 as the reference; R and B are adjusted relative to G. Stored values
are quantized to ~2.5-unit steps (≈101-step menu scale), so the engine widens
small corrections to `wb_gain_min_step` and reads each gain back after writing.

Settings commands only work with **Control4/AMX enabled** in the projector's
Network menu; otherwise everything but status queries returns `ERR`.

### Picture mode

`CMODE` sets the color mode (07 Natural for SDR); `DYNRANGE` sets the dynamic
range (01 = SDR, 05 = HDR Mode 4). The command table stores a list of `[command,
value]` pairs per mode. SDR runs force `DYNRANGE 01` — the projector's automatic
detection decoded the Pi's SDR signal as HDR, clipping everything above ~66 % —
and the server restores the previous `DYNRANGE` when the run ends.

### CMS

The 5040UB exposes **no** RGBCMY hue/saturation/brightness commands over
ESC/VP21 — not in Epson's list, and ~900 undocumented guesses all returned
`ERR`. CMS is therefore a guided manual phase (see Phase 2).

---

## Color Math

### Coordinate systems

```
xyY  — CIE chromaticity (x, y) + luminance (Y)
XYZ  — CIE tristimulus (device-independent, Y=100 scale)
Lab  — CIE L*a*b* (perceptually uniform, illuminant-relative)
```

### Conversion chain

```
Projector display → XYZ (measured by colorimeter)
                 → Lab (D65 illuminant) for ΔE computation
                 → xyY for chromaticity-based WB correction
```

### Critical: D65 vs D50

ArgyllCMS `spotread` reports Lab values under D50 (ICC standard illuminant).
Rec.709 and P3 color primaries are defined under D65.

**Rule**: ignore spotread's Lab values entirely. Convert raw XYZ to Lab under D65:

```python
# XYZColor is on the 0–1 nominal scale (reference white ≈ 0.95047, 1.0, 1.08883).
# Divide Y=100-scale values by 100 first — passing them raw yields L*≈522 for white.
xyz = XYZColor(float(X) / 100, float(Y) / 100, float(Z) / 100, illuminant="d65")
lab = convert_color(xyz, LabColor)
```

**The `float()` cast is mandatory.** python-colormath silently computes
wrong ΔE when passed numpy scalars — the cast forces Python-native floats.

**numpy compatibility**: python-colormath 3.0 calls `numpy.asscalar()`, removed
in numpy 1.23. `color_math.py` installs a shim (`np.asscalar = lambda a: a.item()`)
before importing colormath; without it every ΔE call raises AttributeError.

### Relative colorimetry (critical)

spotread returns **absolute** XYZ in cd/m². Every target in this project is
**relative to white = 100**. The engine therefore measures a reference white
(end of Phase 1, or a dedicated white measurement at the start of Phase 2/3)
and rescales every subsequent reading by `100 / Y_white` before computing Lab
or ΔE. Comparing raw absolute readings against the target tables makes ΔE
luminance-dominated and the loop cannot converge — a dim-but-perfect projector
would never pass.

### D65 reference white (Y=100 scale)

`X=95.047, Y=100.000, Z=108.883`

The white patch Lab target is `(100.0, 0.0, 0.0)` — any deviation in a/b
means the projector's white point is not D65.

### Calibration targets

**SDR (Rec.709 / D65)** — source: ITU-R BT.709:

| Patch | x | y | Y |
|-------|---|---|---|
| white | 0.3127 | 0.3290 | 100.0 |
| red | 0.6400 | 0.3300 | 21.26 |
| green | 0.3000 | 0.6000 | 71.52 |
| blue | 0.1500 | 0.0600 | 7.22 |

**HDR10 (DCI P3 / D65)** — source: SMPTE ST 432-1 + D65 white:

| Patch | x | y | Y |
|-------|---|---|---|
| white | 0.3127 | 0.3290 | 100.0 |
| red | 0.6800 | 0.3200 | 22.90 |
| green | 0.2650 | 0.6900 | 69.17 |
| blue | 0.1500 | 0.0600 | 7.93 |

HDR10 white is D65 — **not** DCI native (0.314, 0.351). The 5040UB targets
D65 in HDR mode.

Secondary targets (cyan, magenta, yellow) are **derived programmatically at
import time** by summing the XYZ of their constituent primaries
(`color_math._build_targets`) — never hardcode them; hardcoded HDR10
secondaries were once several ΔE off. Neutral targets (grey75, grey50) sit on
the D65 axis with gamma-2.2 luminance.

---

## Calibration Engine — Phase Detail

Run order (`engine.run(phases)`): **Baseline → Levels → White Balance → Gamma →
CMS → Verification**. Baseline and Verification always run; the others are
selected by `POST /api/run` `phase` (`all` or a single phase).

### Phase 0: Baseline

Snapshot every setting in `picture.PICTURE_SETTINGS` (`report.baseline`, `baseline`
event), then — unless dry-run — reset the settings that bias a calibration:
Color saturation / Tint / Skin tone to `INIT` (factory neutral) and image
enhancement preset / frame interpolation off. Auto Iris has no ESC/VP21 command,
so the user is told to turn it off.

### Levels: Brightness and Contrast (guided)

Visual, on PGenerator window patterns (`display_window`): a 2 % grey box on black
for brightness (lower until it vanishes, raise until just visible), a 98 % box on
white for contrast (raise until it blends, lower until just visible). The UI's
−/+ buttons step `BRIGHT` / `CONTRAST` through the engine (`picture.nudge`), and
**Done** moves on.

### Phase 1: White Balance (two-point)

Gains at the top (white) and offsets at the bottom (`wb_offset_patch`, grey30)
of the grey scale. Each also shifts the other point, so gain-then-offset rounds
repeat (up to `wb_two_point_rounds`) until a round needs no correction at
either. Both points use the same chromaticity loop, shown here for gains:

Goal: make the white patch chromaticity match D65 (x=0.3127, y=0.3290).

```
display white patch (RGB 255,255,255)
target_X, target_Y, target_Z = xyY_to_XYZ(0.3127, 0.3290, 100)   # loop-invariant

for each iteration:
    X, Y, Z = measure()
    scale = 100 / Y                       # normalize reading to Y=100:
    Xn, Yn, Zn = X·scale, Y·scale, Z·scale  # chromaticity-only comparison
    ΔE = delta_e_2000(lab(Xn, Yn, Zn), lab(target))
    converged / divergence-guard / anomaly checks (see shared loop below)

    r_err = (Xn - target_X) / target_X
    g_err = (Yn - target_Y) / target_Y     # ≡ 0 — G is the fixed reference
    b_err = (Zn - target_Z) / target_Z

    new_R = R_gain - round(r_err × step_scale × gain_range)
    new_G = G_gain - round(g_err × step_scale × gain_range)
    new_B = B_gain - round(b_err × step_scale × gain_range)

    clamp each to [wb_gain_range[0], wb_gain_range[1]]
    send corrections if changed
    step_scale = min(initial, max(minimum, step_scale × (ΔE / threshold) × 0.5))
```

The G channel acts as the luminance reference (its error is 0 by construction
after normalization); adjusting R and B corrects tint without shifting overall
brightness. The last white reading's Y becomes the reference white that anchors
relative colorimetry in later phases. Corrections smaller than `wb_gain_min_step`
are widened to it, and each value is read back after writing (the 5040UB
quantizes to ~2.5-unit steps).

### Gamma (SDR only)

Select `GAMMA F0` (custom), re-measure white, then for each `[grey, tone]` pair in
`gamma_points` run the shared loop on that grey, adjusting `GAMMALV <tone>`.
Readings are reduced to lightness only (Y on the white = 100 scale, placed on the
neutral axis), so ΔE is the L* error against gamma 2.2:

```
delta = +round((L_target − L_meas) / 100 × step_scale × 255)   # min step applies
```

The pairs come from measuring the 5040UB: each tone was raised by 30 and the
whole 10–90 % ramp re-read. Tones 00 and 08 are the curve's ends (no effect
inside 10–90 %); 01–07 sit roughly 10 % apart from black but overlap, and some
pull their neighbours the other way (05 lowers 50–60 %). So the pair set is
repeated (`gamma_passes`, default 2) until a pass needs no correction; greys
without a tone of their own (60, 70, 90 %) are only verified. Skipped in HDR10,
where the projector tone-maps PQ.

### Phase 2: CMS (6 axes)

Processes axes in `cms_axis_order` (default: red, green, blue, cyan, magenta, yellow).

**Automatic** (`cms_mode: "auto"`, default): `osd.RgbcmyMenu` drives the RGBCMY
menu with remote-key presses (`KEY 89` opens it with the cursor always on R, so
every axis starts from a known state; Enter, Up/Down, Left/Right ±1, Esc). Per
axis the engine measures, splits the error into hue °, chroma % and L*
(`cms_errors`), moves the control with the largest error-to-noise ratio by
`round(error / gain)` steps, and re-estimates that control's signed gain from
the measured effect — so it needs no prior knowledge of the menu's sensitivity
or direction. The divergence guard reverts to the best offsets; the menu is
always closed afterwards. Menu values can't be read back, so net steps per
control are reported (`cms_adjustments`).

**Guided** (`cms_mode: "guided"`, and always in dry runs):
For each axis the engine displays the
patch, measures (normalized against the Phase-1 reference white), and — while
ΔE ≥ threshold — emits a `cms_guidance` event and blocks until the user, having
adjusted the projector's RGBCMY menu by hand, answers `remeasure` or `next`
(`POST /api/run/action`). Abort releases the wait.

Error decomposition in Lab ab-plane (`engine.cms_guidance`):

```
hue_err     = degrees(atan2(b_tgt, a_tgt) - atan2(b_meas, a_meas)), normalized to (-180, 180]
sat_err_pct = (chroma_tgt - chroma_meas) / chroma_tgt × 100
lum_err     = L_tgt - L_meas
```

Components above 1°, 2 % and 1 L* become instructions, largest (relative to
those thresholds) first. Hue is phrased as "shift toward <neighbor>" on the Lab
hue wheel (red → yellow → green → cyan → blue → magenta → red): the menu's own
Hue +/- direction per axis is unknown, but the neighbor color is unambiguous.

### Phase 3: Verification

Read-only measurement pass. Measures 16 patches (white, 6 primaries/secondaries, and the grey ramp grey90 … grey10). No corrections applied. Results go into `CalibrationReport.verify_results`.

Ends with `display_black()`.

### Step scale decay (clamped)

```
step_scale_new = min(proportional_gain_initial,
                     max(proportional_gain_minimum,
                         step_scale × (ΔE / threshold) × 0.5))
```

When ΔE = 2× threshold: the raw formula is neutral (2 × 0.5 = 1.0 — no change).
Above that it would *grow* the step without bound and destabilize the loop —
which is why the result is clamped to the initial value.
When ΔE = threshold: scale halves each iteration.
When ΔE < threshold: loop exits before the next decay.

This prevents oscillation when close to target without requiring PID tuning.

### Shared loop safety (both correction phases)

Both phases run through one driver (`engine._correction_loop`) which adds two
protections around the proportional controller:

- **Divergence guard** — if ΔE fails to improve for 3 consecutive iterations,
  the loop reverts the projector to the best-known settings and stops the
  patch. This bounds the damage from an unstable loop or a wrong
  control-direction assumption.
- **Honest final result** — when a loop ends without converging (divergence
  revert or max iterations), the settings changed *after* the last reading, so
  the engine re-measures once and reports ΔE/XYZ for the projector's actual
  final state. This also keeps the Phase-1 white reference accurate.

An optional anomaly hook (see Agents) is consulted every 3rd iteration and may
retry a measurement or abort the patch.

---

## ArgyllCMS spotread Subprocess

### Why pseudo-TTY

`spotread` checks whether it has a controlling terminal. Without one it
either refuses to start or stalls on first measurement. Using `subprocess.PIPE`
does not give it a controlling terminal — it only gives it a pipe for stdin.

`os.openpty()` creates a master/slave pty pair. We give the slave to `spotread`
as its stdin/stdout/stderr, and use `os.setsid()` + `TIOCSCTTY` in `preexec_fn`
to make it the process's controlling terminal. The parent reads/writes the master.

### Measurement trigger

Spotread waits for Enter to take each reading. We simulate this:

```python
os.write(master_fd, b"\n")
```

Then read from `master_fd` via `select()` until the pattern
`Result is XYZ: X Y Z` appears (regex match).

### Averaging

Three readings per reported measurement:
1. Take 3 individual readings
2. Compute mean Y across all 3
3. Compute std dev of Y; reject any reading where `|Y - mean_Y| > 2σ`
4. Return mean of remaining readings

This handles single photon-noise outliers without inflating uncertainty.

### Warm-up monitor

The web UI polls `/api/warmup` every 30 seconds. It takes a single reading
and tracks the last 20 Y values. When variance over the last 3 readings drops
below 0.5%, the warm-up badge turns green and "Validate Setup" becomes active.

---

## PGenerator Protocol

Text commands on TCP port 85, each terminated by `b"\x02\r"`; every command gets
a reply with the same terminator (the protocol quietvoid/pgen_client speaks):

| Command | Reply |
|---------|-------|
| `IS_ALIVE` | `ALIVE` |
| `CMD:GET_MODE` | `OK:0[3840x2160 30.00Hz ...]` |
| `CMD:GET_PGENERATOR_CONF_RGB_QUANT_RANGE` | `OK:1` (1 = limited 16–235, 2 = full) |
| `RGB=RECTANGLE;w,h;0;r,g,b;bg_r,bg_b,bg_g;0,0,x,y;-1` | `OK` (8-bit; background is r,b,g) |
| `QUIT` | — |

`PGenClient.connect()` checks `IS_ALIVE`, reads the resolution and RGB range, and
every patch is sent full-screen. Patch values are full-range 8-bit and are scaled
to 16–235 when the Pi outputs limited range (`16 + round(v × 219 / 255)`), so white
is always peak white. Reconnect once on a dropped connection.

Each run first sets the Pi's output to match the mode (`configure_output`): SDR /
BT.709 / 8-bit / RGB, or HDR10 (PQ, BT.2020, P3 primaries, 10-bit, YCbCr 4:2:2),
restarting PGenerator only if a setting changed. HDR10 patches are 10-bit PQ codes
from `color_math.hdr_patch_code10`: each patch's target colour at its relative
luminance, with white at `hdr_white_nits` (100 cd/m²).

After sending, sleep `patch_settle_ms` (default 500ms) before measuring.

The `PatchDisplay` Protocol (abstract interface) allows swapping in:
- `PGenClient` — real hardware
- `NullPatchDisplay` — dry-run / testing (sleeps, logs, returns immediately)

To add an HTTP-based adapter (if a future pgen_client version exposes REST),
subclass and implement `display_patch()`, `display_black()`, `connect()`, `disconnect()`.

---

## Network Discovery

### mDNS

Listens for 3 seconds for:
- `_epsonprojector._tcp.local.` — projector
- `_pgenerator._tcp.local.` — PGenerator

The 5040UB may or may not announce mDNS depending on network mode settings.
PGenerator announces if built with mDNS support.

### Port scan

Parallel TCP connection attempts across all /24 hosts:

```python
with ThreadPoolExecutor(max_workers=50) as executor:
    futures = {executor.submit(connect_check, ip, port, timeout=0.5): (ip, port) ...}
```

50 workers × /24 (254 hosts) × 2 ports = 508 concurrent attempts.
At 0.5s timeout, worst case = ~5 seconds total scan time.

### Verification

Every discovered candidate (mDNS or port scan) is verified:
- **Projector**: sends ESC/VP.net handshake, checks 16-byte ACK
- **PGenerator**: successful TCP connect (no challenge/response)

`confirmed=True` means handshake passed. `confirmed=False` means port was
open but handshake failed (could be a different service on that port).

---

## WebSocket Event Flow

```
Browser                    server.py              engine.py
   │                          │                       │
   │──── WS connect ─────────►│                       │
   │                          │                       │
   │──── POST /api/run ──────►│                       │
   │                          │──── start thread ────►│
   │                          │                       │── display patch
   │                          │                       │── measure
   │◄─── {measurement} ───────│◄── on_event() ────────│
   │◄─── {correction} ────────│◄── on_event() ────────│── send correction
   │◄─── {patch_done} ────────│◄── on_event() ────────│
   │                          │                       │── next patch...
   │◄─── {run_complete} ──────│◄── on_event() ────────│
   │                          │                       │
```

`on_event()` is thread-safe: uses `loop.call_soon_threadsafe(queue.put_nowait, event)`
to bridge from the engine thread to the asyncio event loop, where `_ws_broadcaster`
drains the queue and sends to all connected WebSocket clients.

---

## Profile Schema

```json
{
  "name": "Post-calibration SDR",
  "mode": "sdr",
  "created_at": "2025-04-01T14:23:00+00:00",
  "wb_gains": {"R": 131, "G": 128, "B": 124},
  "cms_values": {
    "red":     {"HUE": -2, "SAT": 3,  "LUM": 0},
    "green":   {"HUE": 1,  "SAT": -1, "LUM": 2},
    "blue":    {"HUE": 0,  "SAT": 2,  "LUM": -1},
    "cyan":    {"HUE": 3,  "SAT": 0,  "LUM": 1},
    "magenta": {"HUE": -1, "SAT": 1,  "LUM": 0},
    "yellow":  {"HUE": 2,  "SAT": -2, "LUM": 3}
  },
  "final_delta_e": {
    "white": 0.42, "red": 0.71, "green": 0.38, "blue": 0.55,
    "cyan": 0.63, "magenta": 0.49, "yellow": 0.58,
    "grey75": 0.31, "grey50": 0.44
  },
  "screen_info": {
    "diagonal_inches": 120,
    "material": "White Matte",
    "throw_distance": 12.5
  }
}
```

Filename: `{safe_name}_{mode}_{timestamp}.json`
Example: `Post_calibration_SDR_sdr_20250401_142300.json`

---

## Agent API Notes

### Output format

All agents request structured output through the shared helper
`agents.base.request_structured(model, prompt, schema, max_tokens, thinking)`,
which sends:

```python
output_config={"format": {"type": "json_schema", "schema": SCHEMA}}
```

(No nested `json_schema`/`name`/`strict` wrapper — that older shape is
obsolete and rejected by the current API. Schemas enforce structure via
`additionalProperties: false` + `required` inside the schema itself.)

The helper also extracts the reply — thinking blocks (`block.type ==
"thinking"`) come before text blocks; it returns `json.loads()` of the first
`block.type == "text"` block. Don't duplicate this parsing in agent modules.

### Adaptive thinking

`thinking={"type": "adaptive"}` lets the model decide when extended thinking
is worth the extra tokens. Only used for Opus agents on complex reasoning tasks
(results_analyst, profile_advisor). Not used on Haiku.

### Cost estimates (2025 pricing)

| Agent | Model | Calls/session | Est. cost/session |
|-------|-------|---------------|-------------------|
| results_analyst | Opus | 1 | ~$0.08 |
| profile_advisor | Opus | 0–2 | ~$0.06 |
| anomaly_detector | Haiku | 0–10 | ~$0.01 |
| setup_validator | Haiku | 1–3 | ~$0.003 |
| **Total** | | | **~$0.10–0.15** |

All agents return `{"error": "...", "available": False}` on any failure —
the UI handles this gracefully and calibration continues without AI features.
