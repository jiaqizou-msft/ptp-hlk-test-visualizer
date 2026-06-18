"""Quick live-capture diagnostic: prints frame/contact counts as you touch.

Run:  python -m ptp_metrics.diag   (then touch/drag the pad)
Stops after --seconds (default 12) and prints a summary.
"""
from __future__ import annotations

import argparse
import sys
import time

from .live_capture import LiveCapture, is_supported


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args(argv)
    if not is_supported():
        print("Live capture requires Windows.")
        return 2

    state = {"n": 0, "contacts": 0, "max": 0}

    def on_frame(f):
        state["n"] += 1
        ac = len(f.active_contacts)
        state["contacts"] += ac
        state["max"] = max(state["max"], ac)
        if state["n"] % 50 == 1 or ac > 1:
            xy = ", ".join(f"id{c.contact_id}:({c.x:.0f},{c.y:.0f})" for c in f.active_contacts)
            print(f"frame {state['n']:5d}  contacts={ac}  scan={f.scan_time}  {xy}")

    cap = LiveCapture(on_frame=on_frame)
    cap.start()
    print(f"Capturing {args.seconds:.0f}s — TOUCH / DRAG the touchpad now...")
    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            time.sleep(0.2)
    finally:
        cap.stop()
    dev = cap.device
    print("\n--- summary ---")
    print(f"frames captured : {state['n']}")
    print(f"max simultaneous: {state['max']}")
    print(f"device logical  : X[{dev.x_logical_min},{dev.x_logical_max}] "
          f"Y[{dev.y_logical_min},{dev.y_logical_max}]")
    print(f"device size mm  : {dev.width_mm} x {dev.height_mm}")
    print(f"counts/mm       : X {dev.x_counts_per_mm} Y {dev.y_counts_per_mm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
