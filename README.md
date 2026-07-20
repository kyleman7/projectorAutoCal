# ProjectorCal

Closed-loop calibration system for the **Epson PowerLite Home Cinema 5040UB**. Automatically measures color output with a hardware colorimeter, calculates corrections against industry-standard targets, and sends adjustments directly to the projector — iterating until every patch hits ΔE < 1.0.

Controlled entirely through a dark-themed web UI hosted on your local network while it runs.

---

## Hardware Required

| Device | Role |
|--------|------|
| **Epson PowerLite Home Cinema 5040UB** | Projector being calibrated |
| **X-Rite ColorChecker Display Pro (CCDIS3)** | Colorimeter — measures projected color |
| **Raspberry Pi 4** running [PGenerator](https://github.com/quietvoid/pgen_client) | Displays full-screen test patches via HDMI |
| **Linux or macOS host** | Runs this software (Windows not supported — colorimeter driver requires POSIX) |

The colorimeter connects to the host machine via USB. It mounts on a **tripod aimed at the center of the screen** (6–18 inches from the surface) — no flush contact needed for front-projection.

---

## What It Does

```
┌─────────────┐     patch RGB      ┌──────────────┐     HDMI      ┌──────────┐
│  ProjectorCal│ ────────────────► │  Raspberry Pi │ ────────────► │ Screen   │
│  (this app)  │                   │  PGenerator   │               └──────────┘
│              │ ◄──────────────── │               │                     │
│              │   XYZ measurement │  ┌──────────┐ │              projected light
│              │                   │  │  CCDIS3  │◄┘
│              │ ──ESC/VP21 TCP──► │  └──────────┘ │
│  Epson 5040UB│                   └──────────────┘
└─────────────┘
```

**Three calibration phases:**

1. **White Balance** — adjusts R/G/B gains to hit the D65 white point
2. **CMS (6-axis)** — corrects Hue, Saturation, and Luminance for red, green, blue, cyan, magenta, and yellow independently
3. **Verification** — final measurement pass across all 9 patches with no corrections applied

Supports both **SDR (Rec.709/D65)** and **HDR10 (DCI P3/D65)** targets. SDR and HDR settings on the projector are independent — calibrate each separately.

---

## Features

- **Auto-discovery** — scans your network via mDNS and parallel port scan to find the projector and Pi without manual IP entry
- **Command probe** — auto-discovers the projector's ESC/VP21 command tokens (which Epson doesn't publish) before first calibration
- **Profile management** — save, apply, and compare calibration snapshots; swap between SDR and HDR10 profiles in seconds
- **Dry-run mode** — runs the full calibration loop without sending corrections, for testing and previewing
- **Warm-up monitor** — tracks colorimeter Y readings over time and unlocks calibration only when the lamp is thermally stable
- **AI analysis** — Claude-powered post-run report with grade, issues, and recommendations; live anomaly detection during measurement; natural-language profile comparison
- **Web UI** — full control from any browser on the LAN; real-time progress via WebSocket

---

## Web UI

Four tabs, all updated live via WebSocket:

| Tab | What's here |
|-----|-------------|
| **Setup** | Device IPs, network scan, screen info, colorimeter placement checklist, warm-up monitor, pre-flight validation, command probe |
| **Run** | Mode (SDR/HDR10), phase selector, dry-run toggle, per-patch ΔE grid, progress bar, live log |
| **Before / After** | ΔE comparison table (initial vs. verified), AI analysis card with grade and recommendations |
| **Profiles** | Saved profiles grouped by mode, one-click apply to projector, AI-powered profile comparison |

---

## Installation

```bash
# Clone
git clone https://github.com/kyleman7/projectorAutoCal.git
cd projectorAutoCal

# Install (Python 3.11+ recommended)
pip install -e ".[dev]"

# Install ArgyllCMS (provides spotread)
# macOS:
brew install argyllcms
# Ubuntu/Debian:
sudo apt install argyll
```

To enable the optional AI features, either set your Anthropic API key in the
environment or paste it into the **AI Assistance** card at the bottom of the
Setup tab (the app runs fine without it):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## First-Time Setup

### 1. Start the server

```bash
python -m projector_cal
# or with a custom config:
projector-cal --config my_config.json --port 8080
```

Open the printed URL in any browser on your network (e.g. `http://192.168.1.50:8080`).

### 2. Connect devices

The **Setup** tab walks you through the whole flow — a progress strip at the top
tracks each step. Enter your projector and Pi IPs (or click **Scan Network** to
auto-discover them), then click **Save & Test**. Saving is what makes the
calibration run use these addresses.

### 3. Run the probe (first run only)

Click **Run Probe** (Setup, step 2). This queries the projector to discover which ESC/VP21 command tokens your specific firmware uses, and saves them to `configs/command_table.json`. You only need to do this once — calibration can't adjust anything without it, and the Run tab will warn you if it hasn't been done.

### 4. Warm up the projector

Allow at least **30 minutes** of warm-up time. The warm-up monitor tracks luminance stability and tells you when readings are consistent enough to calibrate accurately.

### 5. Position the colorimeter

- Remove the white diffuser cap from the CCDIS3
- Mount on a tripod, centered horizontally and vertically on the screen
- Aim the sensor face directly at the screen from 6–18 inches away
- Dim all room lights

### 6. Validate and calibrate

Click **Validate Setup** to run a pre-flight check. When all items pass, go to the **Run** tab, choose SDR or HDR10, and click **Start**.

---

## Configuration

The default config lives at `configs/default_config.json`. Override any value by passing a partial JSON file:

```json
{
  "projector": { "host": "192.168.1.100" },
  "pgen":      { "host": "192.168.1.101" },
  "calibration": { "delta_e_threshold": 1.0 }
}
```

```bash
projector-cal --config my_overrides.json
```

Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `projector.host` | `192.168.1.100` | Projector IP |
| `projector.command_settle_ms` | `200` | Delay after each ESC/VP21 SET command (ms) |
| `pgen.host` | `192.168.1.101` | Raspberry Pi IP |
| `pgen.patch_settle_ms` | `500` | Time to wait after displaying a patch before measuring (ms) |
| `calibration.delta_e_threshold` | `1.0` | Convergence target (ΔE < this = pass) |
| `calibration.max_iterations_per_patch` | `20` | Give up after this many iterations |

---

## Samsung KS8000 support (experimental)

The same closed loop can drive a **Samsung KS8000** (2016 SUHD) over its 3.5mm
**ExLink** serial port — the port Calman AutoCal uses on these sets. Select
*Samsung KS8000 TV (ExLink serial)* as the display device in the Setup tab.

Current status and constraints:

- **SDR + White Balance phase only** for now (2016 sets expose calibration
  controls in SDR; CMS command bytes are not yet mapped).
- **Hardware**: USB-serial adapter + DB9-to-3.5mm TRS cable into the ExLink
  jack; enable ExLink in the service menu (TV off → Mute-1-8-2-Power →
  Control → Sub Option → *EXT Link Support: ON*). If nothing responds, swap
  RX/TX on the cable — the most common wiring problem.
- **One-time command verification**: ExLink command bytes vary and are largely
  undocumented, so the driver refuses anything unverified. Run
  `python scripts/exlink_spike.py --port <port>` once — it sends candidate
  commands one at a time, asks you to confirm the TV reacted, and records the
  results in `configs/exlink_command_table.json`. The white-balance commands
  must be discovered this way (`--try-cmd` sends ad-hoc frames).
- **Write-only protocol**: the TV can't report its settings, so the driver
  tracks a shadow state. Reset *Expert Settings → White Balance* to defaults
  before the first run so the shadow state matches reality. The engine's
  divergence guard reverts to best-known values if a command behaves
  unexpectedly.
- Optional network conveniences (power-on via Wake-on-LAN, closing OSD menus)
  use the Tizen WebSocket remote API: `pip install "projector-cal[samsung]"`.

The PGenerator patch source and colorimeter loop are unchanged — note that on
an LCD panel the CCDIS3 uses the flush/contact mounting method, not the tripod.

---

## Running Tests

No hardware needed for the test suite:

```bash
pytest tests/ -v
```

Tests cover config validation, color math (including CIE2000 ΔE against the Sharma et al. 2005 reference pairs), patch target values for both SDR and HDR10, and the closed-loop engine itself — convergence, relative-colorimetry normalization, correction direction, divergence recovery, and dry-run isolation, all against a simulated projector.

---

## Project Structure

```
projector_cal/
├── config.py         # Typed config dataclasses, JSON loading
├── color_math.py     # XYZ↔Lab/xyY, CIE2000 ΔE, SDR/HDR10 targets
├── projector.py      # ESC/VP21 TCP driver
├── probe.py          # Command token auto-discovery
├── colorimeter.py    # ArgyllCMS spotread subprocess (POSIX only)
├── pgen.py           # PGenerator TCP adapter
├── engine.py         # Calibration loop (phases 1–3)
├── profiles.py       # Profile save/load/apply
├── discovery.py      # mDNS + port scan
├── server.py         # FastAPI + WebSocket server
├── cli.py            # Entry point
└── agents/
    ├── results_analyst.py   # Post-run analysis (Claude Opus)
    ├── anomaly_detector.py  # Live measurement anomalies (Claude Haiku)
    ├── profile_advisor.py   # Profile comparison (Claude Opus)
    └── setup_validator.py   # Pre-flight checklist (Claude Haiku)
```

See [DESIGN.md](DESIGN.md) for full protocol specifications, algorithm details, and data flow diagrams.

---

## Technical Notes

- **ESC/VP21** — proprietary Epson TCP protocol on port 3629; command tokens vary by firmware and must be probed
- **Color targets** — SDR uses Rec.709 primaries under D65; HDR10 uses DCI P3 primaries under D65 (not DCI-native white). The 5040UB covers ~100% DCI-P3 but not Rec.2020
- **Lab conversions** — always performed under D65 illuminant, not D50 (which is what ArgyllCMS uses internally)
- **Relative colorimetry** — the colorimeter reports absolute cd/m²; the engine measures a reference white and rescales every reading to white = 100 before comparing against targets
- **Correction algorithm** — proportional control with a clamped decaying step scale; a divergence guard reverts to the best-known settings if ΔE stops improving, so an unstable loop (or an inverted control axis) can't walk the projector away from target. No PID time-constant tuning required

---

## License

MIT
