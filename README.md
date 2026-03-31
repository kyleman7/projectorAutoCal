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

Set your Anthropic API key to enable AI features (optional — app runs fine without it):

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

In the **Setup** tab, enter your projector and Pi IPs — or click **Scan Network** to auto-discover them.

### 3. Run the probe (first run only)

Click **Run Probe** in the Setup tab. This queries the projector to discover which ESC/VP21 command tokens your specific firmware uses, and saves them to `configs/command_table.json`. You only need to do this once.

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

Tests cover config validation, color math (including CIE2000 ΔE against the Sharma et al. 2005 reference pairs), and patch target values for both SDR and HDR10.

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
- **Correction algorithm** — proportional control with decaying step scale; no PID time-constant tuning required

---

## License

MIT
