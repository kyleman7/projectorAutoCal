"""Build an HDR10 test-pattern video for measuring a player's HDR handling.

Full-field patches, BT.2020 PQ, metadata matching Black Panther's UHD disc
(P3-D65 mastering display, 1000 nits peak / 0.0001 min, MaxCLL/MaxFALL unset).

Timeline (seconds): 0-5 black, 5-8 sync white (100 nits), 8-11 black,
then each patch for PATCH_S seconds. patches.json records the schedule.
"""
import json
import subprocess
import sys

import numpy as np

sys.path.insert(0, "/home/kyle/projectorAutoCal")
from projector_cal import color_math as cm  # noqa: E402

W, H = 3840, 2160
FPS = "24000/1001"
PATCH_S = 12
LEAD = [("black", 5), ("sync", 3), ("black", 3)]

GREY_NITS = [0, 0.1, 0.5, 1, 2, 5, 10, 20, 35, 50, 75, 100, 150, 200, 300, 400, 600, 800, 1000]
COLORS = ["red", "green", "blue", "cyan", "magenta", "yellow"]


def grey_codes(nits):
    c = cm.pq_code10(nits)
    return (c, c, c)


def sdr709_codes(name, white_nits=100.0):
    """A Rec.709 primary/secondary (white = 100 nits), in the BT.2020 PQ container."""
    X, Y, Z = cm.xyY_to_XYZ(*cm.get_target_xyY(name, "sdr"))
    rgb = [sum(m * v for m, v in zip(row, (X, Y, Z))) / 100.0 for row in cm._XYZ_TO_BT2020]
    return tuple(cm.pq_code10(max(c, 0.0) * white_nits) for c in rgb)


patches = []  # (name, rgb10 full range, target xyY or None, nominal nits)
for n in GREY_NITS:
    patches.append((f"grey_{n}nit", grey_codes(n), (0.3127, 0.3290), n))
for c in COLORS:
    patches.append((f"p3_{c}", cm.hdr_patch_code10(c, 100), cm.get_target_xyY(c, "hdr10")[:2], None))
for sat in (75, 50):
    for c in COLORS:
        name = f"{c}_sat{sat}"
        patches.append((f"p3_{name}", cm.hdr_patch_code10(name, 100), cm.get_target_xyY(name, "hdr10")[:2], None))
for c in COLORS:
    patches.append((f"r709_{c}", sdr709_codes(c), cm.get_target_xyY(c, "sdr")[:2], None))
patches.append(("grey_100nit_end", grey_codes(100), (0.3127, 0.3290), 100))


def ycbcr10(rgb):
    """Full-range 10-bit R'G'B' -> limited-range 10-bit Y'CbCr, BT.2020 non-constant luminance."""
    r, g, b = (v / 1023 for v in rgb)
    kr, kb = 0.2627, 0.0593
    y = kr * r + (1 - kr - kb) * g + kb * b
    cb = (b - y) / (2 * (1 - kb))
    cr = (r - y) / (2 * (1 - kr))
    return (round(64 + 876 * y), round(512 + 896 * cb), round(512 + 896 * cr))


def frame(yuv):
    y, u, v = yuv
    return (np.full(W * H, y, "<u2").tobytes() + np.full(W * H // 4, u, "<u2").tobytes()
            + np.full(W * H // 4, v, "<u2").tobytes())


timeline = [("black", (0, 0, 0), 5), ("sync", grey_codes(100), 3), ("black", (0, 0, 0), 3)]
timeline[0] = ("black", grey_codes(0), 5)
timeline[2] = ("black", grey_codes(0), 3)
schedule = []
t = sum(d for _, _, d in timeline)
for name, rgb, xy, nits in patches:
    timeline.append((name, rgb, PATCH_S))
    schedule.append({"name": name, "start": t, "dur": PATCH_S, "rgb10": rgb,
                     "yuv10": ycbcr10(rgb), "target_xy": xy, "nits": nits})
    t += PATCH_S

json.dump({"sync_start": 5, "patch_s": PATCH_S, "fps": FPS, "patches": schedule},
          open("patches.json", "w"), indent=1)

x265 = ("hdr10=1:hdr10-opt=0:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:"
        "colormatrix=bt2020nc:range=limited:"
        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
        "max-cll=0,0:qp=2:keyint=24:min-keyint=1:log-level=error")
cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
       "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{W}x{H}", "-framerate", FPS,
       # tag the raw input too, or ffmpeg converts it from an assumed BT.601 matrix
       "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
       "-color_range", "tv", "-i", "-",
       "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", x265,
       "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
       "-colorspace", "bt2020nc", "-color_range", "tv", "-tag:v", "hvc1",
       "hdr10_patterns.mp4"]
fps = 24000 / 1001
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
total_frames = 0
for name, rgb, dur in timeline:
    f = frame(ycbcr10(rgb))
    n = round(dur * fps)
    for _ in range(n):
        proc.stdin.write(f)
    total_frames += n
proc.stdin.close()
proc.wait()
print("frames", total_frames, "duration", round(total_frames / fps, 1), "s, exit", proc.returncode)
