# HDR signal-chain tools (2026-10-09)

Chain: Samsung UBD-KM85C → Denon AVR-S740H (CBL/SAT; Pi on front AUX1) → HDFury 4K Linker → Epson 5040UB HDMI1.

| File | Purpose |
|---|---|
| `make_patterns.py` | Builds `hdr10_patterns.mp4`: 44 full-field HDR10 patches (PQ grey ladder 0–1000 nits, P3 colours at 100/75/50 % sat, Rec.709 colours), 12 s each after an 11 s lead-in (black, 3 s white sync, black). Metadata matches the Black Panther UHD (P3-D65, 1000 nits, MaxCLL 0). Writes `patches.json` (schedule + codes). ~12 min to encode. |
| `measure_video.py <label>` | Measures the video as it plays: waits for the sync flash, then reads each patch inside its window → `results_<label>.json`. |
| `edid_dump.py <bin>…` | Minimal EDID decoder (no edid-decode installed). |
| `bdkey.sh <code> <desc>` | Samsung player `SendKeyCode` over UPnP (port 7676). Accepted but codes 1–129 and `KEY_*` names did nothing. |
| `edid/linker_custom_original.bin` | The Linker's original custom table 1 (600 MHz, 4K60 RGB — too much for the projector). |
| `edid/automix_projector.bin` | What automix presents with the projector downstream ("EPSON PJ"). |
| `edid/sdr_only_8bit.bin` | Projector EDID minus HDR, BT.2020 and deep colour → player converts HDR to SDR. |
| `edid/denon.bin` | The Denon's EDID (when it was downstream of the Linker). |

Gotchas found:
- ffmpeg must be told the raw input's colourspace (`-colorspace bt2020nc` etc. **before** `-i`), or it converts the colours from an assumed BT.601 matrix (greys survive, colours shift up to ~20 codes). Verify every patch by decoding after encoding.
- Encode with `qp=2`: the default quantiser shifted flat-patch levels.
- From USB the player outputs 4K **60** (no 24p switch) → 4:2:0 8-bit with a BT.709 flag even for HDR10, so USB playback can't reproduce the disc's HDR signal (4K24 4:2:2 12-bit BT.2020). Measure HDR modes from the Pi instead; USB is fine for the player's HDR→SDR conversion.
