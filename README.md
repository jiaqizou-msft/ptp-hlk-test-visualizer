# PTP Metrics

Real-time **numerical** Precision Touchpad (PTP) quality metrics — **linearity,
jitter, and resolution** — with rich visualization of **scan time (frame
timing)**, **position (X/Y)**, **pressure/signal**, and **contact timing
structure**.

The Windows HLK PTP tests only give a **pass/fail** verdict. This tool fills the
gap: it produces the actual numbers (in mm, DPI, Hz) so partners tuning **sensor
pitch, firmware, and signal processing** can see *how much* linearity error or
jitter changed between builds — not just whether a threshold was crossed.

It wraps the Microsoft tools already in this folder and adds the analysis +
visualization layer on top:

| Tool (already here)              | Role                                              | This project uses it via            |
| -------------------------------- | ------------------------------------------------- | ----------------------------------- |
| `v3_SurfaceTouchpadSolution\ptrecorder.exe` | Records raw PTP HID reports to a folder | `ptp_metrics record` / `analyze`    |
| `v2_NonSurfaceTouchpadSolution\ptrecorder.exe` | Same, for non-Surface touchpads      | `--surface` off                     |
| `DigiInfo\DigiInfo.exe`          | Dumps the digitizer HID descriptor / params       | `ptp_metrics digiinfo` / `--digiinfo` |
| `Touchpad Visualizer\...\TouchpadVisualizer.exe` | Live on-screen surface visualizer | `ptp_metrics visualizer`            |

## What it computes

All definitions follow the Windows Precision Touchpad / HID usage model.

- **Resolution** — counts-per-millimetre and DPI per axis, derived from the HID
  descriptor logical/physical ranges
  ($\text{res} = \frac{\text{LogicalMax}-\text{LogicalMin}}{(\text{PhysMax}-\text{PhysMin})\cdot 10^{\text{UnitExp}}}$),
  **plus** the *empirical* quantisation step actually observed in the data.
- **Jitter** — position noise while a contact is held **stationary**, reported as
  RMS (radial) and peak-to-peak in mm. Stationary segments are detected as runs
  where the contact stays within a small window of its own centroid.
- **Linearity** — maximum and RMS perpendicular deviation of a straight-line
  **drag** from its total-least-squares best-fit line, in mm.
- **Scan time / frame timing** — report rate (Hz) and frame-interval statistics
  from the HID **Scan Time** field (unit = 100 µs, with 16-bit wrap handling), or
  host timestamps in live mode.
- **Contact timing structure** — per-contact down/up/lifetime, report counts and
  cadence, and maximum simultaneous contacts.

## Install

```powershell
cd PTPMetrics
python -m pip install -r requirements.txt
```

Requires Python 3.10+, `numpy`, and `matplotlib`.

## GUI (recommended) + standalone .exe

The easiest way to use the tool is the interactive GUI:

```powershell
python -m ptp_metrics gui
```

It provides:

- **▶ Start Live** — real-time capture rendered on a native, GPU-light Tkinter
  canvas that updates *incrementally* (each contact stroke is one persistent
  polyline extended per frame), so it stays smooth for long sessions. Live
  contact markers, scan-time interval strip chart and active-contact bars.
- **● Record / ■ Stop Rec** — streams the session straight to disk (CSV or JSONL)
  via a background writer thread; never drops the capture rate.
- **Clear** — empties both the display and the capture buffer (works mid-session).
- **Save CSV… / Save Report…** — export the buffer, or a PNG dashboard + JSON
  (the JSON also embeds the spec PASS/FAIL checks).
- **Open Recording…** — load and replay a CSV / JSONL / ptrecorder file offline.
- **Spec checks panel** — live PASS/FAIL vs. the Microsoft thresholds:
  resolution ≥ 300 DPI, report rate ≥ 125 Hz, stationary jitter ≤ 0.5 mm,
  linearity ≤ 0.5 mm, per-report positional delta ≤ 0.5 mm. (Informational — not
  an official certification verdict.)
- **W/H mm + Apply, Spec overlay** — set sensor size and toggle the grid overlay.

### Performance notes

The live path uses **no matplotlib** — matplotlib re-rasterizes the whole figure
each frame, which is what caused the earlier lag. Rendering is decoupled from
capture (≈30 fps draw, ≈4 Hz metrics) and is O(new points) per frame. matplotlib
is used only for the offline **Save Report** PNG.

### Build a shareable standalone executable

No Python needed on the target machine:

```powershell
python build_exe.py --clean
```

This produces:

- `dist/PTPMetrics.exe` — single-file windowed app (~36 MB)
- `release/PTPMetrics-v0.1.0-win-x64.zip` — exe + README + release notes

Just share the zip; double-click `PTPMetrics.exe` on any Windows 10/11 x64 machine.

## Command line


```powershell
# 1) No hardware needed — synthetic demo proving the whole pipeline.
python -m ptp_metrics demo

# 2) Record with Microsoft's ptrecorder (run from an ELEVATED terminal).
python -m ptp_metrics record .\capture --surface
#    ... drag one finger across the pad (linearity) and hold it still (jitter),
#    close ptrecorder, then:

# 3) Analyze the recording and open the dashboard.
python -m ptp_metrics analyze .\capture --digiinfo

# 4) Real-time capture + live dashboard (Windows, no elevation needed).
python -m ptp_metrics live

# 5) Launch the bundled Microsoft tools.
python -m ptp_metrics visualizer
python -m ptp_metrics digiinfo
```

Useful flags on `demo` / `analyze` / `live`:

- `--save report.png` — write the dashboard image
- `--json report.json` — write the numerical report as JSON
- `--no-show` — headless (don't open a GUI window)
- `--width-mm` / `--height-mm` — supply sensor size if DigiInfo can't (needed for
  mm-based resolution/jitter/linearity)

## Recommended capture gesture

For meaningful linearity **and** jitter from one recording:

1. Place one finger and **hold it still** for ~0.5 s (jitter).
2. **Drag** it in one smooth straight stroke across the pad, ≥ ~30–60 mm
   (linearity).
3. Lift.

Repeat at different positions/angles to characterise the whole sensor.

## How metrics map to the four requested visualizations

| Requested view            | Dashboard panel                          | Driven by                         |
| ------------------------- | ---------------------------------------- | --------------------------------- |
| Scan time (frame timing)  | top-right: per-frame interval + rate     | `metrics.timing_metrics`          |
| Position (X/Y)            | large left: 2D surface + best-fit line   | `metrics.extract_tracks`          |
| Pressure                  | middle-right: pressure / size / confidence | per-contact `pressure`/`width`  |
| Contact timing structure  | bottom-left: per-contact Gantt timeline  | `metrics.contact_timing_metrics`  |

## ptrecorder output format

`ptrecorder.exe /dir <dir>` writes its parsed report data into `<dir>`. The
loader (`loaders.load_ptrecorder_dir`) auto-detects the data file and maps
columns by **keyword** (so it tolerates layout differences between builds). It
understands both:

- **long** layout — one row per contact, grouped by a frame/report index, and
- **wide** layout — per-contact indexed columns (`X0,Y0,X1,Y1,…`).

Recognised column keywords include: `Frame/Report/Index`, `ScanTime`,
`ContactId`, `ContactCount`, `X`, `Y`, `TipSwitch`, `Confidence`, `Width`,
`Height`, `Pressure`, `Button`, `Timestamp`. The canonical export schema (also
what `export.export_csv` writes) is:

```
Frame,ScanTime,ContactCount,Button,ContactId,X,Y,TipSwitch,Confidence,Width,Height,Pressure,HostTimestamp
```

If your ptrecorder build emits a different format, point `analyze` at the file
directly and, if needed, adjust `CANONICAL_SCHEMA` in `ptp_metrics/loaders.py`.

## Module map

```
ptp_metrics/
  models.py          # Contact / Frame / DeviceInfo / Recording + unit helpers
  hid_descriptor.py  # parse HID report descriptor + DigiInfo text -> DeviceInfo
  loaders.py         # ptrecorder dir / CSV / DigiInfo loaders (keyword mapping)
  metrics.py         # resolution / jitter / linearity / timing / contact timing
  synth.py           # synthetic recordings with known ground truth
  live_capture.py    # Windows Raw Input + HidP real-time PTP capture (ctypes)
  dashboard.py       # matplotlib report + live dashboards
  export.py          # Recording -> canonical CSV
  cli.py             # `python -m ptp_metrics ...`
tests/
  test_metrics.py    # validates the engine recovers injected ground truth
```

## Limitations / notes

- `ptrecorder.exe` requires **elevation** (it opens the HID device and ETW). Run
  `record` from an *Administrator* terminal.
- Live capture and DigiInfo parsing are **Windows-only**.
- Reported resolution / mm-based jitter & linearity need the sensor's physical
  size. It comes automatically from `live` capture (HID preparsed data) and,
  where possible, from `DigiInfo`; otherwise pass `--width-mm` / `--height-mm`.
- This is an analysis aid, **not** an HLK replacement — there is no official
  certification verdict, just transparent numbers and plots.
