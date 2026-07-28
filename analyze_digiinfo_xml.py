"""One-off: parse a DigiInfo XML packet log (e.g. the B3 recording) into a
Recording and emit the PTP metrics report (JSON) + dashboard (PNG).

Usage:
    python analyze_digiinfo_xml.py <input.xml> [out_prefix]

DigiInfo logs a stream of <packet> elements under <events>; each packet carries
x/y (logical counts), down (tip switch), confidence, contactid, and scantime
(HID Scan Time, 100 us units). Device resolution comes from the multi-touch
<digitizer> block (the one that owns a contactid property).
"""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use("Agg")

from ptp_metrics.models import Contact, DeviceInfo, Frame, Recording
from ptp_metrics import metrics as M
from ptp_metrics import spec as SPEC
from ptp_metrics import dashboard
from ptp_metrics.loaders import load_digiinfo_xml


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    prefix = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(path)[0] + "_report"

    rec = load_digiinfo_xml(path)
    print(f"Loaded {len(rec.frames)} frames from {path}")
    print(f"Device: {rec.device.name}  "
          f"{rec.device.width_mm:.1f} x {rec.device.height_mm:.1f} mm  "
          f"({rec.device.x_counts_per_mm:.1f} x {rec.device.y_counts_per_mm:.1f} counts/mm)")

    report = M.compute_all(rec)
    ev = SPEC.evaluate(rec, report)

    png = prefix + ".png"
    dashboard.show_report(rec, report, save_path=png)

    payload = report.to_dict()
    payload["spec"] = {
        "overall": ev.overall,
        "checks": [c.__dict__ for c in ev.checks],
    }
    js = prefix + ".json"
    with open(js, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    j, tim, cont = report.jitter, report.timing, report.continuity
    print("\n== Summary ==")
    print(f"  report rate       : {tim.report_rate_hz and round(tim.report_rate_hz, 1)} Hz "
          f"(clock: {tim.source})")
    print(f"  jitter RMS radial : {j.worst_rms_radial_mm and round(j.worst_rms_radial_mm, 4)} mm")
    print(f"  jitter peak-peak  : {j.worst_p2p_mm and round(j.worst_p2p_mm, 4)} mm")
    print(f"  mean dist from ctr: "
          f"{j.worst_mean_dist_from_init_mm and round(j.worst_mean_dist_from_init_mm, 4)} mm")
    print(f"  stationary segs   : {len(j.per_segment)}")
    print(f"  swipe break-ups   : {cont.dropout_count}")
    print(f"  spec overall      : {ev.overall}")
    print(f"\nWrote: {png}\n       {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
