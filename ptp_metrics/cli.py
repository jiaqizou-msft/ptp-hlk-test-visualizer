"""Command-line interface for PTP Metrics.

Subcommands::

    python -m ptp_metrics demo                 # synthetic end-to-end demo
    python -m ptp_metrics analyze <dir|csv>    # analyze a ptrecorder dir / CSV
    python -m ptp_metrics record <dir>         # drive ptrecorder.exe (elevated)
    python -m ptp_metrics live                 # real-time capture + dashboard
    python -m ptp_metrics digiinfo             # run DigiInfo + parse device params
    python -m ptp_metrics visualizer           # launch Microsoft TouchpadVisualizer

Use ``--no-show`` to skip the GUI and ``--save report.png`` / ``--json out.json``
to write artifacts (handy on headless machines).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from typing import Optional

from . import metrics as M
from .models import DeviceInfo, Recording

# Default locations of the bundled Microsoft tools (relative to this repo root).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASE = os.path.dirname(_REPO)  # the folder that holds DigiInfo/, Touchpad Visualizer/, v2/v3

DEFAULT_PTRECORDER_V3 = os.path.join(_BASE, "v3_SurfaceTouchpadSolution", "ptrecorder.exe")
DEFAULT_PTRECORDER_V2 = os.path.join(_BASE, "v2_NonSurfaceTouchpadSolution", "ptrecorder.exe")
DEFAULT_DIGIINFO = os.path.join(_BASE, "DigiInfo", "DigiInfo.exe")
DEFAULT_VISUALIZER = os.path.join(_BASE, "Touchpad Visualizer", "win-x64", "TouchpadVisualizer.exe")


def _print_report(report: M.MetricsReport) -> None:
    d = report.to_dict()
    print("\n=== PTP METRICS REPORT ===")
    print(f"source : {d['source']}")
    dev = d["device"]
    print(f"device : {dev['name']}")
    if dev["width_mm"]:
        print(f"         {dev['width_mm']:.1f} x {dev['height_mm']:.1f} mm, "
              f"logical {dev['x_logical']} x {dev['y_logical']}")
    r = report.resolution
    print("\n-- Resolution --")
    print(f"  reported X : {r.reported_x_counts_per_mm and round(r.reported_x_counts_per_mm,2)} counts/mm "
          f"({r.reported_x_dpi and round(r.reported_x_dpi)} dpi)")
    print(f"  reported Y : {r.reported_y_counts_per_mm and round(r.reported_y_counts_per_mm,2)} counts/mm "
          f"({r.reported_y_dpi and round(r.reported_y_dpi)} dpi)")
    print(f"  empirical step : X {r.empirical_x_step_mm and round(r.empirical_x_step_mm,4)} mm, "
          f"Y {r.empirical_y_step_mm and round(r.empirical_y_step_mm,4)} mm")
    j = report.jitter
    print("\n-- Jitter (stationary) --")
    print(f"  worst RMS radial : {j.worst_rms_radial_mm and round(j.worst_rms_radial_mm,4)} mm")
    print(f"  worst peak-to-peak : {j.worst_p2p_mm and round(j.worst_p2p_mm,4)} mm")
    print(f"  segments analysed : {len(j.per_segment)}")
    if j.note:
        print(f"  note: {j.note}")
    lin = report.linearity
    print("\n-- Linearity (drag) --")
    print(f"  worst max deviation : {lin.worst_max_dev_mm and round(lin.worst_max_dev_mm,4)} mm")
    print(f"  worst RMS deviation : {lin.worst_rms_dev_mm and round(lin.worst_rms_dev_mm,4)} mm")
    print(f"  segments analysed : {len(lin.per_segment)}")
    if lin.note:
        print(f"  note: {lin.note}")
    t = report.timing
    print("\n-- Timing --")
    print(f"  report rate : {t.report_rate_hz and round(t.report_rate_hz,1)} Hz "
          f"(source: {t.source})")
    print(f"  mean interval : {t.mean_interval_ms and round(t.mean_interval_ms,3)} ms "
          f"(jitter std {t.timing_jitter_ms and round(t.timing_jitter_ms,3)} ms)")
    ct = report.contact_timing
    print("\n-- Contact timing --")
    print(f"  max simultaneous : {ct.max_simultaneous}")
    for cl in ct.contacts:
        dur = f"{cl.duration_ms:.0f} ms" if cl.duration_ms else f"{cl.n_reports} reports"
        print(f"   contact {cl.contact_id}: {cl.n_reports} reports, {dur}")
    print("==========================\n")


def _emit(rec: Recording, args) -> None:
    report = M.compute_all(rec)
    _print_report(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)
        print(f"JSON report written to {args.json}")
    if args.save or not args.no_show:
        try:
            from . import dashboard
            dashboard.show_report(rec, report, save_path=args.save)
            if args.save:
                print(f"Dashboard image written to {args.save}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not render dashboard: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_demo(args) -> int:
    from .synth import synth_recording
    rec = synth_recording(
        jitter_mm=args.jitter, linearity_error_mm=args.linearity,
        report_rate_hz=args.rate)
    print(f"Generated synthetic recording: {len(rec)} frames "
          f"(injected jitter {args.jitter} mm RMS, linearity bow {args.linearity} mm).")
    _emit(rec, args)
    return 0


def cmd_analyze(args) -> int:
    from .loaders import load_ptrecorder_dir, load_csv, run_digiinfo

    device: Optional[DeviceInfo] = None
    if args.digiinfo:
        digi = args.digiinfo if os.path.exists(args.digiinfo) else DEFAULT_DIGIINFO
        if os.path.exists(digi):
            _, device = run_digiinfo(digi)
            print(f"Device params from DigiInfo: {device.name}, "
                  f"{device.width_mm and round(device.width_mm,1)} x "
                  f"{device.height_mm and round(device.height_mm,1)} mm")
    if device is None and (args.width_mm or args.height_mm):
        device = DeviceInfo(name="user-specified")
    if args.width_mm:
        device.x_physical_mm = args.width_mm
    if args.height_mm:
        device.y_physical_mm = args.height_mm

    if os.path.isfile(args.path):
        rec = load_csv(args.path, device)
    else:
        rec = load_ptrecorder_dir(args.path, device)
    print(f"Loaded {len(rec)} frames from {args.path}")
    if device is not None:
        rec.device = _merge_device(rec.device, device)
    # If the HID logical range is unknown (no DigiInfo/descriptor) but we know
    # the physical size, estimate the logical range from the data so resolution
    # and mm-based metrics still work (approximate).
    from .loaders import infer_logical_range
    if (rec.device.width_mm or rec.device.height_mm) and \
            abs(rec.device.x_logical_max - rec.device.x_logical_min) <= 1:
        if infer_logical_range(rec):
            print("[note] HID logical range unknown; estimated from observed data "
                  "extents. For exact resolution, use --digiinfo or live capture.")
    _emit(rec, args)
    return 0


def _merge_device(base: DeviceInfo, override: DeviceInfo) -> DeviceInfo:
    for attr in ("x_physical_mm", "y_physical_mm", "max_contacts"):
        v = getattr(override, attr)
        if v:
            setattr(base, attr, v)
    if override.name and override.name != "Unknown PTP device":
        base.name = override.name
    return base


def cmd_record(args) -> int:
    exe = args.ptrecorder or (DEFAULT_PTRECORDER_V3 if args.surface else DEFAULT_PTRECORDER_V2)
    if not os.path.exists(exe):
        print(f"[error] ptrecorder not found: {exe}", file=sys.stderr)
        return 2
    os.makedirs(args.dir, exist_ok=True)
    cmd = [exe, "/dir", args.dir] + (["/etw"] if args.etw else [])
    print("ptrecorder requires elevation (it opens the HID device and ETW).")
    print("Running:", " ".join(cmd))
    print("Touch/drag the pad to record; close ptrecorder when done, then run:")
    print(f"   python -m ptp_metrics analyze \"{args.dir}\"")
    try:
        return subprocess.call(cmd)
    except OSError as e:
        print(f"[error] failed to launch ptrecorder: {e}", file=sys.stderr)
        print("Tip: launch an elevated terminal (Run as administrator) and retry.",
              file=sys.stderr)
        return 2


def cmd_live(args) -> int:
    try:
        from . import dashboard
        from .live_capture import is_supported
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        return 2
    if not is_supported():
        print("[error] Live capture requires Windows.", file=sys.stderr)
        return 2
    if args.no_show:
        # headless: capture for a fixed duration then report
        import time
        from .live_capture import LiveCapture
        cap = LiveCapture()
        cap.start()
        print(f"Capturing for {args.seconds:.0f}s ... touch/drag the pad now.")
        time.sleep(args.seconds)
        cap.stop()
        rec = Recording(device=cap.device, frames=list(cap.frames), source="live")
        print(f"Captured {len(rec)} frames.")
        _emit(rec, args)
    else:
        dashboard.live_dashboard()
    return 0


def cmd_digiinfo(args) -> int:
    from .loaders import run_digiinfo
    exe = args.digiinfo or DEFAULT_DIGIINFO
    if not os.path.exists(exe):
        print(f"[error] DigiInfo not found: {exe}", file=sys.stderr)
        return 2
    text, device = run_digiinfo(exe)
    print(text)
    print("\n-- Parsed device parameters --")
    print(json.dumps({
        "name": device.name,
        "width_mm": device.width_mm,
        "height_mm": device.height_mm,
        "x_counts_per_mm": device.x_counts_per_mm,
        "y_counts_per_mm": device.y_counts_per_mm,
        "x_dpi": device.x_dpi, "y_dpi": device.y_dpi,
        "max_contacts": device.max_contacts,
    }, indent=2, default=str))
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main()


def cmd_visualizer(args) -> int:
    exe = args.visualizer or DEFAULT_VISUALIZER
    if not os.path.exists(exe):
        print(f"[error] TouchpadVisualizer not found: {exe}", file=sys.stderr)
        return 2
    print(f"Launching {exe}")
    try:
        subprocess.Popen([exe])
    except OSError as e:
        print(f"[error] failed to launch: {e}", file=sys.stderr)
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ptp_metrics",
                                description="Real-time numerical PTP metrics "
                                            "(linearity, jitter, resolution) + visualization.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--save", metavar="PNG", help="save dashboard image instead of/with showing")
        sp.add_argument("--json", metavar="FILE", help="write metrics report as JSON")
        sp.add_argument("--no-show", action="store_true", help="do not open the GUI window")

    sp = sub.add_parser("demo", help="synthetic end-to-end demo (no hardware needed)")
    sp.add_argument("--jitter", type=float, default=0.08, help="injected jitter RMS (mm)")
    sp.add_argument("--linearity", type=float, default=0.15, help="injected linearity bow (mm)")
    sp.add_argument("--rate", type=float, default=133.0, help="report rate (Hz)")
    add_common(sp)
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("analyze", help="analyze a ptrecorder directory or CSV")
    sp.add_argument("path", help="ptrecorder /dir output folder, or a CSV file")
    sp.add_argument("--digiinfo", nargs="?", const=DEFAULT_DIGIINFO,
                    help="run DigiInfo (optionally path) to fetch device params")
    sp.add_argument("--width-mm", type=float, help="sensor width in mm (for resolution)")
    sp.add_argument("--height-mm", type=float, help="sensor height in mm")
    add_common(sp)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("record", help="drive ptrecorder.exe to capture (needs elevation)")
    sp.add_argument("dir", help="output directory for ptrecorder")
    sp.add_argument("--surface", action="store_true", help="use the v3 Surface ptrecorder")
    sp.add_argument("--etw", action="store_true", help="pass /etw to ptrecorder")
    sp.add_argument("--ptrecorder", help="explicit path to ptrecorder.exe")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("live", help="real-time capture + dashboard (Windows)")
    sp.add_argument("--seconds", type=float, default=10.0, help="headless capture duration")
    add_common(sp)
    sp.set_defaults(func=cmd_live)

    sp = sub.add_parser("digiinfo", help="run DigiInfo and parse device parameters")
    sp.add_argument("--digiinfo", help="path to DigiInfo.exe")
    sp.set_defaults(func=cmd_digiinfo)

    sp = sub.add_parser("visualizer", help="launch Microsoft TouchpadVisualizer.exe")
    sp.add_argument("--visualizer", help="path to TouchpadVisualizer.exe")
    sp.set_defaults(func=cmd_visualizer)

    sp = sub.add_parser("gui", help="launch the interactive GUI (live + record + analyze)")
    sp.set_defaults(func=cmd_gui)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
