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

**Calibration phases** (every run starts by recording all picture settings and resetting Color, Tint, Skin tone and image processing to neutral):

1. **Brightness / Contrast (guided)** — test patterns on screen; step the projector's black and white level from the browser until the pattern looks right
2. **White Balance (two-point)** — adjusts R/G/B gains on white and R/G/B offsets on a dark grey until both sit on the D65 white point
3. **Gamma (SDR)** — tunes the projector's 9 custom-gamma points so a 10–90% grey ramp tracks gamma 2.2
4. **CMS (6-axis, guided)** — measures red, green, blue, cyan, magenta, and yellow and tells you which Hue / Saturation / Brightness control to change in the projector's RGBCMY menu (the 5040UB has no network commands for CMS), re-measuring after each adjustment
5. **Verification** — final measurement pass over white, the six colors, and a 10–90% grey ramp, with no corrections applied

Turn **Auto Iris** off in the projector's Image menu before calibrating — it can't be controlled over the network and changes light output between patches.

Supports both **SDR (Rec.709/D65)** and **HDR10 (DCI P3/D65)** targets. SDR and HDR settings on the projector are independent — calibrate each separately.

---

## Features

- **Auto-discovery** — scans your network via mDNS and parallel port scan to find the projector and Pi without manual IP entry
- **Command probe** — confirms the projector accepts the white-balance and picture-mode ESC/VP21 commands before first calibration
- **Profile management** — save, apply, and compare full picture-setting snapshots (color mode, brightness, contrast, gains, offsets, gamma, …); swap between SDR and HDR10 profiles in seconds
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

First enable **Control4/AMX** in the projector's Network menu — without it the 5040UB rejects every settings command over the network. Then click **Run Probe** (Setup, step 2). This checks which ESC/VP21 command tokens your projector accepts and saves them to `configs/command_table.json`. You only need to do this once — calibration can't adjust anything without it, and the Run tab will warn you if it hasn't been done.

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

- **ESC/VP21** — Epson's control protocol on TCP port 3629; settings commands need Control4/AMX enabled, gains are quantized to ~2.5-unit steps, and there are no CMS commands
- **Color targets** — SDR uses Rec.709 primaries under D65; HDR10 uses DCI P3 primaries under D65 (not DCI-native white). The 5040UB covers ~100% DCI-P3 but not Rec.2020
- **Lab conversions** — always performed under D65 illuminant, not D50 (which is what ArgyllCMS uses internally)
- **Relative colorimetry** — the colorimeter reports absolute cd/m²; the engine measures a reference white and rescales every reading to white = 100 before comparing against targets
- **Correction algorithm** — proportional control with a clamped decaying step scale; a divergence guard reverts to the best-known settings if ΔE stops improving, so an unstable loop (or an inverted control axis) can't walk the projector away from target. No PID time-constant tuning required

---

## License

MIT
