# PTP HLK Test Visualizer

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)
![Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4)
![License: Microsoft Internal](https://img.shields.io/badge/License-Microsoft%20Internal-brightgreen)

Real-time **numerical** Precision Touchpad (PTP) quality metrics visualization — measuring **linearity, jitter, resolution, report rate**, and **positional stability** with professional-grade dashboards and real-time live capture.

## Why This Tool?

The Windows HLK PTP qualification tests only provide a **pass/fail** verdict. **PTP HLK Test Visualizer** fills the gap by delivering the actual numbers (in mm, DPI, Hz) so hardware partners tuning **sensor pitch, firmware, and signal processing** can see *exactly how much* linearity error or jitter improved (or regressed) between builds — not just whether a threshold was crossed.

### Key Features

- **Real-time live capture** on any Windows 10/11 x64 device (no special hardware needed)
- **Native Tkinter UI** with smooth incremental canvas rendering (60× faster than matplotlib)
- **Screen recording** — capture the live visualization *and* the metrics/spec panel to an MP4
- **Streaming logger** — background thread writes CSV/JSONL without dropping frames
- **Microsoft spec PASS/FAIL** — evaluate against official Precision Touchpad thresholds
- **Professional dashboards** — matplotlib-based PNG reports + JSON export with embedded metrics
- **Offline analysis** — replay CSV, JSONL, or live recordings
- **Cross-platform CLI** — generate reports headless on any OS (Linux/Mac for offline, Windows for live capture)

## What It Measures

Following the Windows Precision Touchpad specification and HID usage model:

- **Input Resolution** — counts-per-millimetre and DPI per axis, derived from HID descriptor logical/physical ranges
- **Report Rate** — frame rate (Hz) from HID Scan Time field or host timestamps; min threshold: ≥125 Hz
- **Stationary Jitter** — position noise RMS (radial) and peak-to-peak (mm) while a contact is stationary; max threshold: ≤0.5 mm
- **Linearity** — maximum and RMS perpendicular deviation from a best-fit line during drags (mm); max threshold: ≤0.5 mm
- **Positional Delta** — per-frame maximum movement jump (mm), detecting outliers and signal issues; max threshold: ≤0.5 mm
- **Contact Timing** — per-contact down/up/lifetime, report counts, and maximum simultaneous contacts

## Installation

### From Source (Windows / Mac / Linux)

```bash
git clone https://github.com/jiaqizou-msft/ptp-hlk-test-visualizer.git
cd ptp-hlk-test-visualizer
python -m pip install -r requirements.txt
```

**Requirements**: Python 3.12+, numpy, tkinter (built into Python on most systems)

### Standalone Executable (Windows 10/11 x64 only)

Download the pre-built executable from [Releases](../../releases) — no Python installation needed:

1. Download `PTPMetrics-v0.1.0-win-x64.zip`
2. Extract the folder
3. Double-click `PTPMetrics.exe`

The executable bundles Python and all dependencies; a brief Windows Defender or SmartScreen warning is normal on first run (unsigned binary).

## Usage: Interactive GUI

**Recommended for real-time analysis and interactive exploration.**

```bash
python -m ptp_metrics gui
```

### GUI Controls

| Control | Function |
|---------|----------|
| **▶ Start Live** | Begin real-time touchpad capture; live trace rendering on incremental Tkinter canvas (smooth even after hours) |
| **● Record / ■ Stop Rec** | Stream the session to disk (CSV or JSONL) via background writer thread; never drops frames |
| **◉ Rec Video / ■ Stop Video** | Screen-record the visualization + live metrics/spec panel to an MP4 (background encoder, 15 fps) |
| **Clear** | Wipe display and capture buffer (works mid-session) |
| **Save CSV…** | Export contact trace data in canonical schema |
| **Save Report…** | Generate PNG dashboard + JSON report with embedded spec evaluations |
| **Open Recording…** | Load and replay CSV, JSONL, or ptrecorder folder exports offline |
| **Spec Checks Panel** | Live PASS/FAIL verdicts against Microsoft thresholds |
| **W/H mm + Apply** | Set touchpad physical dimensions for metric calculations |
| **Spec Overlay** | Toggle grid lines and threshold visualization |

### Performance Architecture

The live path uses **native Tkinter Canvas rendering** (no matplotlib in hot path), decoupled from capture:

- **Render loop**: ~30 fps (≈4–15 ms per frame, shown in status bar)
- **Metrics recompute**: ~4 Hz (250 ms cadence)
- **Rendering**: O(new points) per frame via persistent per-contact polylines

*Note: Early iterations using matplotlib re-rasterized the entire figure every 60 ms over all accumulated points, resulting in quadratic degradation. Native canvas rendering stays smooth indefinitely.*

## Usage: Command Line

For **headless operation** (CI/CD, batch analysis, or remote systems):

```bash
# Synthetic demo (no hardware needed — validates whole pipeline)
python -m ptp_metrics demo --save report.png --json report.json

# Analyze an offline recording
python -m ptp_metrics analyze path/to/recording.csv --json report.json

# Real-time capture (Windows only; no elevation needed)
python -m ptp_metrics live --width-mm 100 --height-mm 70 --json report.json
```

### Common Flags

- `--width-mm` / `--height-mm` — touchpad physical dimensions in millimeters (auto-detected in live mode where possible)
- `--save <file.png>` — write dashboard image
- `--json <file.json>` — write metrics and spec evaluations as JSON
- `--no-show` — suppress GUI window (for scripts/CI)

## Data Formats

### CSV (Canonical Schema)

```
Frame,ScanTime,ContactCount,Button,ContactId,X,Y,TipSwitch,Confidence,Width,Height,Pressure,HostTimestamp
0,100,1,0,0,500,300,1,255,15,15,128,1718711400000
0,100,1,0,1,510,310,1,255,14,14,130,1718711400000
1,200,1,0,0,501,301,1,255,15,15,129,1718711400001
```

- **One row per contact per frame**
- Compatible with Excel, pandas, R, etc.
- Readable by offline analysis on any OS

### JSONL (JSON Lines)

```json
{"i":0,"scan":100,"cc":1,"btn":0,"t":1718711400000,"contacts":[{"id":0,"x":500,"y":300,"tip":1,"conf":255,"w":15,"h":15,"p":128}]}
{"i":1,"scan":200,"cc":1,"btn":0,"t":1718711400001,"contacts":[{"id":0,"x":501,"y":301,"tip":1,"conf":255,"w":15,"h":15,"p":129}]}
```

- **One compact JSON object per line**
- Lossless, machine-friendly
- Smaller file size than CSV

### JSON Report

```json
{
  "device": { "name": "...", "width_mm": 100.0, "height_mm": 70.0 },
  "metrics": { "resolution_dpi_x": 352, "report_rate_hz": 125 },
  "spec": {
    "overall": "FAIL",
    "checks": [
      { "name": "Input Resolution", "status": "PASS", "value": 352, "limit": 300 },
      { "name": "Stationary Jitter", "status": "FAIL", "value": 0.6, "limit": 0.5 }
    ]
  },
  "dashboard_png": "report.png"
}
```

## Recommended Capture Gesture

For meaningful linearity **and** jitter metrics from a single session:

1. **Hold still** (~0.5 seconds) — captures stationary jitter noise
2. **Drag** one smooth straight line across the pad (≥30–60 mm) — captures linearity
3. **Lift**

Repeat at different positions and angles to characterize the entire sensor surface.

## Architecture & Modules

```
ptp_metrics/
  models.py          # Contact / Frame / DeviceInfo / Recording data classes
  hid_descriptor.py  # Parse HID report descriptor -> device parameters
  loaders.py         # Load CSV / JSONL / ptrecorder folders
  metrics.py         # Compute resolution, jitter, linearity, report rate, etc.
  synth.py           # Generate synthetic recordings with known ground truth
  live_capture.py    # Windows Raw Input + HidP — real-time PTP capture
  spec.py            # Evaluate metrics against Microsoft thresholds
  dashboard.py       # Generate matplotlib PNG report + Tkinter dashboards
  export.py          # Serialize Recording to CSV / JSONL
  fastview.py        # High-performance native Tkinter canvas rendering
  logger.py          # Streaming CSV/JSONL writer (background thread)
  screencap.py       # Screen-record the visualization to MP4 (background thread)
  gui.py             # Interactive GUI (Tkinter + fastview)
  cli.py             # Command-line interface (`python -m ptp_metrics ...`)
tests/
  test_metrics.py    # Validate engine recovers injected ground truth
  test_gui_smoke.py  # Smoke tests for GUI and logging
  test_screencap.py  # Validate the MP4 screen-capture encoder
```

## Notes & Limitations

- **Live capture is Windows-only** — uses Raw Input + HidP (Windows APIs)
- **Offline analysis** works on any OS (CSV/JSONL can be analyzed on Mac/Linux)
- **Physical dimensions required** for mm-based metrics (resolution, jitter, linearity)
  - Auto-detected in live capture via HID descriptor
  - Otherwise pass `--width-mm` and `--height-mm`
- **This is an analysis tool, not an HLK certification** — provides transparent numbers and plots, not official verdicts
- Precision Touchpad spec data from: Microsoft Windows Hardware Quality Labs (HQL) and Precision Touchpad device certification docs

## Building the Standalone Executable

To rebuild the `.exe` for distribution:

```bash
python build_exe.py --clean
```

This produces:
- `dist/PTPMetrics/PTPMetrics.exe` — main executable
- `dist/PTPMetrics/_internal/` — bundled Python runtime and libraries
- `release/PTPMetrics-v0.1.0-win-x64.zip` — shareable release archive

**Note**: Uses `--onedir` mode (not `--onefile`) to avoid Windows Defender Application Control (WDAC) blocking due to temp-extracted DLLs.

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Specific test
python -m pytest tests/test_metrics.py::test_linearity_recovered -v
```

All tests validate that the metrics engine correctly recovers injected ground-truth values.

## References

- [Windows Hardware Compatibility Program — Precision Touchpad Devices](https://microsoft.com/en-us/hardware)
- [Human Interface Devices (HID) Specification](https://www.usb.org/document-library/hid-specification-10)
- [Windows Precision Touchpad Specification](https://docs.microsoft.com/en-us/windows-hardware/design/component-guidelines/precision-touchpad-devices)

## License

Microsoft Internal — See LICENSE file

## Feedback & Contributions

This tool is maintained by the Microsoft Precision Touchpad certification team. For issues, improvements, or questions, please file an issue in this repository.
