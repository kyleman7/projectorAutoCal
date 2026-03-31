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

### White Balance tokens (per firmware)

The 5040UB firmware does not publish its token names. Known candidates:

| Channel | Candidates (try in order) |
|---------|--------------------------|
| R | `WBALGAINR`, `WBGAINR`, `CRED`, `WBRED`, `BCRED`, `RGAIN` |
| G | `WBALGAING`, `WBGAING`, `CGRN`, `WBGRN`, `BCGRN`, `GGAIN` |
| B | `WBALGAINB`, `WBGAINB`, `CBLU`, `WBBLU`, `BCBLU`, `BGAIN` |

Gain range: 0–255 (center = 128 = no offset). Only WB.G should be held
near 128 as the reference; R and B are adjusted relative to G.

### CMS tokens

Six axes × three properties = 18 tokens. Pattern: `CMS{axis_initial}{prop_num}`
or `CMS{axis_initial}{prop_name}`. Examples:

| Axis | HUE | SAT | LUM |
|------|-----|-----|-----|
| Red | `CMSRHUE` | `CMSRSAT` | `CMSRLUM` |
| Green | `CMSGHUE` | `CMSGSAT` | `CMSGLUM` |
| Blue | `CMSBHUE` | `CMSBSAT` | `CMSBLUM` |
| Cyan | `CMSCHUE` | `CMSCSAT` | `CMSCLUM` |
| Magenta | `CMSMHUE` | `CMSMSAT` | `CMSMLUM` |
| Yellow | `CMSYHUE` | `CMSYSAT` | `CMSYLUM` |

CMS values are typically signed integers centered at 0, range ±50 or ±100
depending on firmware. The engine reads current values before adjusting.

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
xyz = XYZColor(float(X), float(Y), float(Z), illuminant="d65")
lab = convert_color(xyz, LabColor)
```

**The `float()` cast is mandatory.** python-colormath silently computes
wrong ΔE when passed numpy scalars — the cast forces Python-native floats.

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

Secondary and neutral targets (cyan, magenta, yellow, grey75, grey50) are
derived from the primaries and stored in `color_math.py`.

---

## Calibration Engine — Phase Detail

### Phase 1: White Balance

Goal: make the white patch chromaticity match D65 (x=0.3127, y=0.3290).

```
display white patch (RGB 255,255,255)

for each iteration:
    X, Y, Z = measure()
    meas_x, meas_y, _ = XYZ_to_xyY(X, Y, Z)
    target_X, target_Y, target_Z = xyY_to_XYZ(0.3127, 0.3290, Y)  # keep Y fixed

    r_err = (X - target_X) / target_X
    g_err = (Y - target_Y) / target_Y
    b_err = (Z - target_Z) / target_Z

    new_R = R_gain - round(r_err × step_scale × gain_range)
    new_G = G_gain - round(g_err × step_scale × gain_range)
    new_B = B_gain - round(b_err × step_scale × gain_range)

    clamp each to [wb_gain_range[0], wb_gain_range[1]]
    send corrections if changed
    step_scale = max(min_gain, step_scale × (ΔE / threshold) × 0.5)
```

The G channel acts as the luminance reference. Holding G near 128 while
adjusting R and B corrects tint without shifting overall brightness.

### Phase 2: CMS (6 axes)

Processes axes in `cms_axis_order` (default: red, green, blue, cyan, magenta, yellow).

Error decomposition in Lab ab-plane:

```
hue_meas  = atan2(b_meas, a_meas)
hue_tgt   = atan2(b_tgt,  a_tgt)
hue_err   = degrees(hue_tgt - hue_meas), normalized to (-180, 180)

chroma_meas = sqrt(a_meas² + b_meas²)
chroma_tgt  = sqrt(a_tgt²  + b_tgt²)
sat_ratio   = (chroma_tgt - chroma_meas) / chroma_tgt

lum_err     = (L_tgt - L_meas) / 100

hue_delta = -round(hue_err × step_scale)
sat_delta = -round(sat_ratio × step_scale × 10)
lum_delta = -round(lum_err × step_scale × 10)
```

The ×10 scaling on sat/lum reflects typical CMS range (±50 or ±100)
vs typical Hue range (±180 degrees → ±50 units).

### Phase 3: Verification

Read-only measurement pass. Measures all 9 patches (white + 6 primaries/secondaries + grey75 + grey50). No corrections applied. Results go into `CalibrationReport.verify_results`.

Ends with `display_black()`.

### Step scale decay

```
step_scale_new = max(proportional_gain_minimum,
                     step_scale × (ΔE / threshold) × 0.5)
```

When ΔE = 2× threshold: scale reduces by 0 (2/1 × 0.5 = 1.0 — no change).
When ΔE = threshold: scale halves each iteration.
When ΔE < threshold: loop exits before next decay.

This prevents oscillation when close to target without requiring PID tuning.

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

PGenerator (`quietvoid/pgen_client`) listens for 3-byte TCP packets on port 85:

```
byte 0: R (0-255)
byte 1: G (0-255)
byte 2: B (0-255)
```

No acknowledgment. The patch displays immediately on HDMI output.
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

All agents use `output_config.format.type = "json_schema"` for structured output.
The `json_schema.strict = True` flag enforces the schema — unknown keys are rejected.

To parse the response:

```python
for block in response.content:
    if hasattr(block, "text"):
        return json.loads(block.text)
```

Thinking blocks (`type="thinking"`) come before text blocks — skip them.

### Adaptive thinking

`thinking={"type": "adaptive"}` lets the model decide when extended thinking
is worth the extra tokens. Only used for Opus agents on complex reasoning tasks
(results_analyst, profile_advisor). Never use on Haiku — not supported.

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
