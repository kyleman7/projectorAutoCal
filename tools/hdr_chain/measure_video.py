"""Measure the HDR10 pattern video as it plays (started by hand on the player).

Waits for the 3 s white sync flash, times the patches from its falling edge, and
takes readings inside each patch's window. Usage: measure_video.py <label>
Writes results_<label>.json.
"""
import json
import sys
import time

sys.path.insert(0, "/home/kyle/projectorAutoCal")
from projector_cal.colorimeter import Colorimeter, MeasurementError  # noqa: E402

LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
sched = json.load(open("patches.json"))
SYNC_END = sched["sync_start"] + 3          # white flash 5–8 s


def read(c):
    t0 = time.monotonic()
    try:
        xyz = c._single_measurement().as_tuple()
    except MeasurementError:
        xyz = (0.0, 0.0, 0.0)               # below the instrument floor (black)
    return t0, time.monotonic(), xyz


with Colorimeter(ccss_path="/home/kyle/projectorAutoCal/configs/ccss/epson_tw9400_5050ub_i1pro.ccss") as c:
    print("waiting for the sync flash — start playback now", flush=True)
    base = None
    while True:                              # rising edge
        _, t1, xyz = read(c)
        Y = xyz[1]
        if base is None:
            base = max(Y, 0.01)
        if Y > max(5 * base, 2.0):
            print(f"sync seen (Y {Y:.2f} vs {base:.3f})", flush=True)
            peak = Y
            break
        base = min(base, max(Y, 0.01))
    while True:                              # falling edge
        t0, t1, xyz = read(c)
        if xyz[1] < 0.3 * peak:
            break
    # the drop happened between the start and end of this reading
    video_t0 = (t0 + t1) / 2 - SYNC_END
    print(f"timeline locked (reading took {t1 - t0:.2f} s)", flush=True)

    results = []
    for p in sched["patches"]:
        win_lo = video_t0 + p["start"] + 3.0
        win_hi = video_t0 + p["start"] + p["dur"] - 0.7
        while time.monotonic() < win_lo:
            time.sleep(0.05)
        reads = []
        while len(reads) < 3:
            t0, t1, xyz = read(c)
            if t1 > win_hi:
                if not reads:
                    reads.append(xyz)        # keep one, even if it ran long
                break
            reads.append(xyz)
        n = len(reads)
        X, Y, Z = (sum(r[i] for r in reads) / n for i in range(3))
        s = X + Y + Z
        xy = (X / s, Y / s) if s > 0 else (None, None)
        results.append({**p, "xyz": [X, Y, Z], "xy": xy, "n": n})
        print(f"{p['name']:22} Y {Y:8.3f}  xy {xy[0] or 0:.4f},{xy[1] or 0:.4f}  n={n}", flush=True)

json.dump({"label": LABEL, "results": results}, open(f"results_{LABEL}.json", "w"), indent=1)
print("done", flush=True)
