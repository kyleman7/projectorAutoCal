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
│   ├── engine.py                  ← calibration loop (phases 1-3)
│   ├── profiles.py                ← save/load/apply/list/delete profiles
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
    └── test_color_math.py
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

The 5040UB does NOT publish its token names. Run `--probe` mode first:
- Send `CANDIDATE?\r` for every known candidate token
- Record which respond with `CANDIDATE=VALUE:` instead of `ERR`
- Write verified tokens to `configs/command_table.json`

Token categories needed: `white_balance.{R,G,B}`, `picture_mode.{sdr,hdr10}`, `cms.{axis}.{HUE,SAT,LUM}`

Known candidates to try (across firmware versions):
- WB R: `["WBALGAINR", "WBGAINR", "CRED", "WBRED", "BCRED", "RGAIN"]`
- WB G: `["WBALGAING", "WBGAING", "CGRN", "WBGRN", "BCGRN", "GGAIN"]`
- WB B: `["WBALGAINB", "WBGAINB", "CBLU", "WBBLU", "BCBLU", "BGAIN"]`
- CMS Hue: `CMSRHUE/CMSGHUE/CMSBHUE/CMSCHUE/CMSMHUE/CMSYHUE` (or `CMSR1` etc.)

### ArgyllCMS spotread Subprocess

`spotread` is interactive and requires a controlling terminal. Approach:

```python
import os, fcntl, termios

master_fd, slave_fd = os.openpty()

def preexec():
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

proc = subprocess.Popen(
    ["spotread", "-e", "-d", str(display_index)],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    env={**os.environ, "ARGYLL_NOT_INTERACTIVE": "1"},
    preexec_fn=preexec, close_fds=True,
)
os.close(slave_fd)  # parent only needs master
```

- Trigger measurement: `os.write(master_fd, b"\n")`
- Parse output: `Result is XYZ: X Y Z` (regex)
- Take 3 readings per measurement, reject outliers by Y std dev, return mean
- **colorimeter.py is POSIX-only** — import fails gracefully on Windows with `ImportError`

### PGenerator Wire Protocol

Simple 3-byte TCP packet per patch: `[R, G, B]` (0–255 each).

```python
sock.sendall(bytes([r, g, b]))
```

No acknowledgment — connection staying open = success. Reconnect once on broken-pipe.

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

Phase 1 — White Balance:
- Display white patch, adjust R/G/B gains toward D65 (each reading normalized to Y=100 → chromaticity-only ΔE)
- G is the fixed reference channel (its error is 0 by construction); R and B move
- `gain_delta = -round(channel_err × step_scale × gain_range)` where `channel_err = (meas − target) / target`

Phase 2 — CMS (6 axes: red, green, blue, cyan, magenta, yellow):
- Display each primary/secondary patch
- Hue error: angular difference in Lab ab-plane (degrees), `target − measured`
- Sat error: chroma ratio `(chroma_target - chroma_meas) / chroma_target`
- Lum error: `(L_target - L_meas) / 100`
- Deltas move *toward* target assuming positive control = more hue-angle/sat/lum:
  `hue_delta = +round(hue_err_deg × step_scale)`, `sat_delta = +round(sat_err × step_scale × 10)`,
  `lum_delta = +round(lum_err × step_scale × 10)`. If a projector axis is inverted,
  the divergence guard reverts instead of walking away.

Phase 3 — Verification:
- Measure all 9 patches without applying corrections (white first — it re-anchors the reference)
- Record final ΔE for report

Convergence threshold: ΔE < 1.0 (configurable, default 1.0)
Max iterations per patch: 20 (configurable)

### 5040UB HDR Specifics

- HDR10 only (no HLG, no Dolby Vision)
- Peak brightness: ~100-110 nits
- HDR has 4 modes; Mode 4 is darkest/most accurate for calibration
- WB and CMS are accessible in HDR mode
- SDR and HDR settings are **completely separate** — calibrate each independently
- PMOD command (or firmware equivalent) switches picture mode

---

## WebSocket Event Schema

All engine events are broadcast as JSON objects. Key events:

```json
{"event": "phase_start",    "phase": "wb|cms|verify"}
{"event": "patch_start",    "patch": "white", "iteration": 1, "mode": "wb|cms"}
{"event": "measurement",    "patch": "white", "xyz": [X,Y,Z], "delta_e": 0.42}
{"event": "correction",     "patch": "white", "axis": "WB_R", "before": 128, "after": 131}
{"event": "patch_done",     "patch": "white", "delta_e": 0.38, "iterations": 4, "converged": true}
{"event": "phase_done",     "phase": "wb"}
{"event": "run_complete",   "report": {...}}
{"event": "error",          "message": "..."}
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
| POST   | `/api/run` | Start calibration (`{mode, phase, dry_run}`) |
| POST   | `/api/run/stop` | Send abort signal to engine |
| GET    | `/api/warmup` | Single colorimeter reading for warm-up monitor |
| GET    | `/api/profiles` | List all saved profiles |
| POST   | `/api/profiles` | Save current projector state as profile |
| DELETE | `/api/profiles/{filename}` | Delete a profile |
| POST   | `/api/profiles/{filename}/apply` | Apply profile to projector |
| POST   | `/api/profiles/compare` | AI comparison of two profiles (`{a: filename, b: filename}`) |
| POST   | `/api/discover` | Start mDNS + port scan |
| GET    | `/api/discover/last` | Load cached scan results |
| POST   | `/api/discover/confirm/{ip}` | Verify a specific device (`{device_type}`) |
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
PGenerator verification: successful TCP connection (no challenge/response protocol).

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
    "measurement_retries": 3, "measurement_timeout": 30.0
  },
  "pgen": { "host": "192.168.1.101", "port": 85, "patch_settle_ms": 500 },
  "calibration": {
    "delta_e_threshold": 1.0, "max_iterations_per_patch": 20,
    "wb_gain_center": 128, "wb_gain_range": [0, 255],
    "proportional_gain_initial": 0.8, "proportional_gain_minimum": 0.1,
    "cms_axis_order": ["red","green","blue","cyan","magenta","yellow"]
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

6. **PGenerator has no ACK**: After sending 3 bytes, there is no response. An open TCP connection and no `OSError` = success.

7. **command_table.json must be probed first**: The default file has placeholder token names. Run `/api/probe` (or `projector-cal --probe` if CLI mode added) before first calibration.

8. **SDR and HDR settings are independent on 5040UB**: Calibrating in SDR does not affect HDR settings and vice versa. Run separate calibration sessions for each mode.

9. **Phase 3 (verify) measures 9 patches** (white + 6 colors + grey75 + grey50). Phases 1 and 2 measure 1 and 6 patches respectively. The progress counter must account for all phases.

10. **Profile filenames**: Derived from `name_mode_timestamp.json`. The JS re-derives the filename client-side using the same Python logic — keep them in sync.

11. **colormath XYZColor is on the 0–1 scale**: reference white is (0.95047, 1.0, 1.08883). Passing Y=100-scale values makes white come out as L*≈522 and silently distorts every ΔE. `xyz_to_lab()` divides by 100 before constructing `XYZColor` — keep it that way.

12. **numpy ≥1.23 removed `asscalar`**, which python-colormath 3.0 still calls. `color_math.py` installs a shim (`np.asscalar = lambda a: a.item()`) before importing colormath. Without it every `delta_e` call raises AttributeError.

13. **Never rescale a patch target to Y=100 in `get_target_lab`**: target xyY luminances are already relative to white=100. Per-patch rescaling makes every target as bright as white (red would get L*=100 instead of ~53).

14. **spotread readings are absolute cd/m²** — always normalize against the measured reference white before comparing to targets (see Calibration Algorithm → Relative colorimetry).

15. **Secondary targets are derived, not hardcoded**: cyan/magenta/yellow xyY are computed at import time by summing the XYZ of their constituent primaries (`color_math._build_targets`). Hardcoded HDR10 secondaries were several ΔE off. Grey targets use display gamma 2.2.

16. **Dummy measurements are dry-run only**: `server.py` refuses a real (non-dry) run when the colorimeter module is unavailable. Driving corrections from random readings would walk the projector's settings randomly.

17. **CMS correction signs assume positive control = more hue-angle/sat/lum** and move toward the target (same convention as WB). The engine's divergence guard reverts to best-known values if a projector axis turns out inverted — do not reintroduce the old negated deltas, which moved *away* from target.

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
| **Setup** | IP config, network scan + device cards, screen info, colorimeter placement checklist, warm-up Y monitor, probe button, pre-flight validation |
| **Run** | Mode/phase selector, dry-run toggle, start/stop, patch grid with live ΔE, progress bar, live log |
| **Before/After** | ΔE comparison table (before = initial ΔE, after = verify pass), AI Analysis card (grade + issues + recommendations) |
| **Profiles** | Grouped by mode, apply/delete buttons, save-current-state, AI profile comparison |

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
