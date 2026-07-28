"""PTP Metrics GUI — high-performance live visualization, recording and analysis.

Rendering uses native Tkinter canvases (see :mod:`ptp_metrics.fastview`) that
update *incrementally*, so the live view stays smooth even after long sessions —
unlike a matplotlib figure that re-rasterizes everything each frame.

Architecture:
  * Capture runs in a background thread (:class:`LiveCapture`).
  * A ~30 fps render loop consumes only the *new* frames since the last tick and
    extends each contact's stroke on the canvas (O(new points)).
  * Metrics + spec PASS/FAIL are recomputed on a slower (~4 Hz) cadence.
  * Recording streams frames straight to disk via :class:`StreamLogger`
    (background writer thread; never blocks capture).

matplotlib is used only for the offline "Save Report" PNG dashboard.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import metrics as M
from . import spec as SPEC
from . import gestures as GEST
from .models import Recording, DeviceInfo
from .export import export_csv
from .fastview import TouchpadView, StripChart, Sparkbars, color_for
from .logger import StreamLogger
from .screencap import ScreenRecorder

# dark theme palette
BG = "#0b1220"
PANEL = "#111827"
FG = "#e5e7eb"
MUTED = "#94a3b8"
ACCENT = "#22d3ee"
GOOD = "#22c55e"
BAD = "#ef4444"
WARN = "#f59e0b"

# pressure-source options (force-haptic pads report HID Tip Pressure; ordinary
# pads don't report pressure at all)
PRESSURE_OFF = "Off"
PRESSURE_HID = "HID Pressure"


def _fmt(v, unit="", nd=3):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


class PTPMetricsApp(tk.Tk):
    RENDER_MS = 33          # ~30 fps
    METRICS_EVERY = 0.25    # seconds between metric recomputes

    def __init__(self):
        super().__init__()
        self.title("PTP Metrics — Precision Touchpad Analyzer")
        self.geometry("1360x860")
        self.minsize(1100, 720)
        self.configure(background=BG)

        # state
        self._cap = None
        self._live = False
        self._loaded: Optional[Recording] = None
        self._logger: Optional[StreamLogger] = None
        self._record_path: Optional[str] = None
        self._screenrec: Optional[ScreenRecorder] = None
        self._video_path: Optional[str] = None
        self._cached_bbox: Optional[Tuple[int, int, int, int]] = None
        self._cached_screen: Optional[Tuple[int, int]] = None

        self._cursor = 0                       # next frame index to render
        self._open_strokes: Dict[int, int] = {}   # contact_id -> last frame index drawn
        self._bounds_set = False
        self._unit = "mm"
        self._alive = True
        self._after_id = None
        self._last_metrics_t = 0.0
        self._last_report: Optional[M.MetricsReport] = None
        self._spec_rows: Dict[str, dict] = {}
        self._pressure_readout: Optional[str] = None

        self._setup_style()
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.after(self.RENDER_MS, self._tick)

    # ------------------------------------------------------------------ style
    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Head.TLabel", background=PANEL, foreground=FG,
                        font=("Segoe UI", 11, "bold"))
        style.configure("TButton", padding=6)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TButton", background=[("active", "#1f2937")])

    # ------------------------------------------------------------------ layout
    def _build_widgets(self):
        # toolbar
        tb = ttk.Frame(self, padding=(10, 8))
        tb.pack(side=tk.TOP, fill=tk.X)

        self.btn_live = ttk.Button(tb, text="▶  Start Live", command=self.toggle_live)
        self.btn_live.pack(side=tk.LEFT, padx=3)
        self.btn_record = ttk.Button(tb, text="●  Record", command=self.toggle_record,
                                     state=tk.DISABLED)
        self.btn_record.pack(side=tk.LEFT, padx=3)
        self.btn_video = ttk.Button(tb, text="◉  Rec Video", command=self.toggle_video)
        self.btn_video.pack(side=tk.LEFT, padx=3)
        self.btn_clear = ttk.Button(tb, text="Clear", command=self.clear_data)
        self.btn_clear.pack(side=tk.LEFT, padx=3)

        ttk.Separator(tb, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Button(tb, text="Save CSV…", command=self.save_csv).pack(side=tk.LEFT, padx=3)
        ttk.Button(tb, text="Save Report…", command=self.save_report).pack(side=tk.LEFT, padx=3)
        ttk.Button(tb, text="Save All…", command=self.save_all).pack(side=tk.LEFT, padx=3)
        ttk.Button(tb, text="Open Recording…", command=self.open_recording).pack(side=tk.LEFT, padx=3)

        ttk.Separator(tb, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Label(tb, text="W mm").pack(side=tk.LEFT)
        self.var_w = tk.StringVar()
        ttk.Entry(tb, textvariable=self.var_w, width=6).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(tb, text="H mm").pack(side=tk.LEFT)
        self.var_h = tk.StringVar()
        ttk.Entry(tb, textvariable=self.var_h, width=6).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Button(tb, text="Apply size", command=self.apply_size).pack(side=tk.LEFT, padx=2)

        self.var_spec = tk.BooleanVar(value=True)
        ttk.Checkbutton(tb, text="Spec overlay", variable=self.var_spec,
                        command=self._toggle_overlay).pack(side=tk.LEFT, padx=10)

        ttk.Separator(tb, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Label(tb, text="Pressure").pack(side=tk.LEFT)
        self.var_pressure_src = tk.StringVar(value=PRESSURE_OFF)
        pcombo = ttk.Combobox(tb, textvariable=self.var_pressure_src, width=14,
                              state="readonly",
                              values=[PRESSURE_OFF, PRESSURE_HID])
        pcombo.pack(side=tk.LEFT, padx=(2, 8))
        pcombo.bind("<<ComboboxSelected>>", lambda e: self._on_pressure_change())

        ttk.Separator(tb, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Label(tb, text="Avg window").pack(side=tk.LEFT)
        self.var_window = tk.StringVar()
        win_entry = ttk.Entry(tb, textvariable=self.var_window, width=5)
        win_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(tb, text="sec (blank = all)", foreground=MUTED).pack(side=tk.LEFT)
        win_entry.bind("<Return>", lambda e: self._apply_window())
        win_entry.bind("<FocusOut>", lambda e: self._apply_window())

        # status bar
        sb = ttk.Frame(self, padding=(10, 4))
        sb.pack(side=tk.BOTTOM, fill=tk.X)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(sb, textvariable=self.status).pack(side=tk.LEFT)
        self.fps_var = tk.StringVar(value="")
        ttk.Label(sb, textvariable=self.fps_var, foreground=MUTED).pack(side=tk.RIGHT)

        # body: left plots, right metrics
        body = ttk.Frame(self, padding=(8, 4))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # touchpad view (big)
        self.view = TouchpadView(left, height=460)
        self.view.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 6))

        # bottom strip charts
        strips = ttk.Frame(left)
        strips.pack(side=tk.TOP, fill=tk.X)
        self.scan_chart = StripChart(strips, title="Scan time — frame interval (ms)",
                                     target=1000.0 / 125.0, height=150)
        self.scan_chart.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        self.count_chart = Sparkbars(strips, title="Active contacts", height=150, width=240)
        self.count_chart.pack(side=tk.LEFT, fill=tk.Y)

        # right panel
        right = ttk.Frame(body, style="Panel.TFrame", width=380)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)
        self._build_metrics_panel(right)

    def _build_metrics_panel(self, parent):
        ttk.Label(parent, text="PTP Spec Checks", style="Head.TLabel").pack(
            anchor=tk.W, padx=14, pady=(12, 2))
        ttk.Label(parent, text="Informational vs. Microsoft thresholds — not a "
                              "certification verdict.", style="Muted.TLabel",
                  wraplength=340, justify=tk.LEFT).pack(anchor=tk.W, padx=14)

        self.overall_var = tk.StringVar(value="—")
        ov = tk.Label(parent, textvariable=self.overall_var, bg=PANEL, fg=MUTED,
                      font=("Segoe UI", 13, "bold"))
        ov.pack(anchor=tk.W, padx=14, pady=(6, 6))
        self._overall_label = ov

        table = tk.Frame(parent, bg=PANEL)
        table.pack(fill=tk.X, padx=10)
        checks = ["Input Resolution", "Report Rate", "Stationary Jitter",
                  "Linearity", "Positional Delta"]
        for name in checks:
            row = tk.Frame(table, bg=PANEL)
            row.pack(fill=tk.X, pady=2)
            dot = tk.Canvas(row, width=14, height=14, bg=PANEL, highlightthickness=0)
            dot.create_oval(2, 2, 12, 12, fill="#475569", outline="", tags="d")
            dot.pack(side=tk.LEFT, padx=(2, 8))
            nm = tk.Label(row, text=name, bg=PANEL, fg=FG, width=16, anchor="w",
                          font=("Segoe UI", 10, "bold"))
            nm.pack(side=tk.LEFT)
            val = tk.Label(row, text="—", bg=PANEL, fg=MUTED, anchor="w",
                           font=("Consolas", 10))
            val.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._spec_rows[name] = {"dot": dot, "val": val}

        # contact-continuity flag: highlighted when a fast swipe breaks up
        self.dropout_var = tk.StringVar(value="Contact continuity: —")
        self._dropout_label = tk.Label(
            parent, textvariable=self.dropout_var, bg=PANEL, fg=MUTED,
            font=("Segoe UI", 10, "bold"), anchor="w", justify=tk.LEFT,
            wraplength=340)
        self._dropout_label.pack(anchor=tk.W, fill=tk.X, padx=14, pady=(8, 0))

        # live gesture recognition
        ttk.Separator(parent, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=(10, 6))
        grow = tk.Frame(parent, bg=PANEL)
        grow.pack(fill=tk.X, padx=14)
        tk.Label(grow, text="Gesture:", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.gesture_var = tk.StringVar(value="—")
        self._gesture_label = tk.Label(grow, textvariable=self.gesture_var, bg=PANEL,
                                       fg=ACCENT, font=("Segoe UI", 12, "bold"),
                                       anchor="w")
        self._gesture_label.pack(side=tk.LEFT, padx=(8, 0))
        self.gesture_detail_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.gesture_detail_var, bg=PANEL, fg=MUTED,
                 font=("Consolas", 9), anchor="w").pack(anchor=tk.W, padx=14)

        ttk.Separator(parent, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(parent, text="Measurements", style="Head.TLabel").pack(
            anchor=tk.W, padx=14, pady=(0, 4))
        self.meas = tk.Text(parent, height=20, bg="#0b1220", fg=FG,
                            font=("Consolas", 10), borderwidth=0,
                            highlightthickness=0, padx=12, pady=6, wrap="word")
        self.meas.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 8))
        self.meas.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------ source
    def _source(self) -> Tuple[List, Optional[DeviceInfo]]:
        if self._live and self._cap is not None:
            dev = self._cap.device
            self._apply_size_override(dev)
            return self._cap.frames, dev
        if self._loaded is not None:
            return self._loaded.frames, self._loaded.device
        return [], None

    def _apply_size_override(self, dev: DeviceInfo):
        try:
            if self.var_w.get().strip():
                dev.x_physical_mm = float(self.var_w.get())
            if self.var_h.get().strip():
                dev.y_physical_mm = float(self.var_h.get())
        except (ValueError, AttributeError):
            pass

    def _ensure_bounds(self, dev: DeviceInfo) -> bool:
        if self._bounds_set or dev is None:
            return self._bounds_set
        if dev.width_mm and dev.height_mm:
            self.view.set_bounds(0, dev.width_mm, 0, dev.height_mm, unit="mm")
            self._unit = "mm"
            self._bounds_set = True
        elif abs(dev.x_logical_max - dev.x_logical_min) > 1:
            self.view.set_bounds(dev.x_logical_min, dev.x_logical_max,
                                 dev.y_logical_min, dev.y_logical_max, unit="counts")
            self._unit = "counts"
            self._bounds_set = True
        if self._bounds_set:
            self.view.set_spec_overlay(self.var_spec.get())
        return self._bounds_set

    def _disp_xy(self, dev: DeviceInfo, c) -> Tuple[float, float]:
        if self._unit == "mm" and dev.x_counts_per_mm and dev.y_counts_per_mm:
            return ((c.x - dev.x_logical_min) / dev.x_counts_per_mm,
                    (c.y - dev.y_logical_min) / dev.y_counts_per_mm)
        return c.x, c.y

    # ------------------------------------------------------------------ live
    def toggle_live(self):
        self.stop_live() if self._live else self.start_live()

    def start_live(self):
        try:
            from .live_capture import LiveCapture, is_supported
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Live capture", f"Cannot import live capture:\n{e}")
            return
        if not is_supported():
            messagebox.showerror("Live capture", "Live capture requires Windows.")
            return
        try:
            self._cap = LiveCapture(on_frame=self._on_frame)
            self._cap.start()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Live capture", f"Failed to start:\n{e}")
            self._cap = None
            return
        self._loaded = None
        self._reset_view()
        self._live = True
        self.btn_live.configure(text="■  Stop Live")
        self.btn_record.configure(state=tk.NORMAL)
        self.status.set("Live — touch/drag the touchpad.")

    def stop_live(self):
        if self._logger is not None:
            self._stop_recording()
        if self._cap is not None:
            try:
                self._cap.stop()
            except Exception:
                pass
            self._cap = None
        self._live = False
        self.btn_live.configure(text="▶  Start Live")
        self.btn_record.configure(state=tk.DISABLED)
        self.status.set("Live stopped.")

    def _on_frame(self, frame):
        # capture-thread callback: only stream to disk here (cheap, queued)
        if self._logger is not None:
            self._logger.write(frame)

    # ------------------------------------------------------------------ record
    def toggle_record(self):
        if self._logger is not None:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if not self._live:
            return
        default = f"ptp_capture_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path = filedialog.asksaveasfilename(
            title="Stream recording to…", defaultextension=".csv",
            initialfile=default,
            filetypes=[("CSV", "*.csv"), ("JSON lines", "*.jsonl")])
        if not path:
            return
        try:
            self._logger = StreamLogger(path)
            self._logger.start()
            self._record_path = path
            self.btn_record.configure(text="■  Stop Rec")
            self.status.set(f"Recording → {os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Record", f"Failed:\n{e}")
            self._logger = None

    def _stop_recording(self):
        if self._logger is None:
            return
        lg = self._logger
        self._logger = None
        try:
            lg.stop()
        except Exception:
            pass
        self.btn_record.configure(text="●  Record")
        self.status.set(f"Saved {lg.frames_written} frames → {self._record_path}")

    # ------------------------------------------------------------------ video record
    def toggle_video(self):
        if self._screenrec is not None:
            self._stop_video()
        else:
            self._start_video()

    def _window_bbox(self) -> Tuple[int, int, int, int]:
        """Screen rectangle (x, y, w, h) of the app's whole window.

        Uses the *outer* window geometry (title bar + borders included) so the
        recording captures the entire application window, not just the content
        area. Coordinates are in logical Tk pixels; the recorder scales them to
        physical pixels for the grab. Must be called on the **main thread** — it
        touches Tk. The value is cached so the recorder's background thread can
        read it Tk-free.
        """
        try:
            # winfo_rootx/y = top-left of the *client* area; the window manager
            # frame (title bar + borders) sits above/around it. Expand to the
            # outer frame using the geometry offsets so nothing is clipped.
            self.update_idletasks()
            cx, cy = self.winfo_rootx(), self.winfo_rooty()
            cw, ch = self.winfo_width(), self.winfo_height()
            ox, oy = self.winfo_x(), self.winfo_y()          # outer top-left
            rx, ry = self.winfo_rootx(), self.winfo_rooty()  # client top-left
            # border thickness left/right, title-bar height (top)
            bx = max(0, rx - ox)
            by = max(0, ry - oy)
            x = ox
            y = oy
            w = cw + 2 * bx
            h = ch + by + bx  # bottom border ~= side border thickness
            self._cached_bbox = (x, y, w, h)
        except Exception:
            pass
        return self._cached_bbox or (0, 0, 0, 0)

    def _screen_size(self) -> Tuple[int, int]:
        """Tk logical screen size — read on the main thread and cached."""
        try:
            self._cached_screen = (self.winfo_screenwidth(),
                                   self.winfo_screenheight())
        except Exception:
            pass
        return self._cached_screen or (0, 0)

    def _start_video(self):
        # dependency preflight so we can give a friendly, actionable message
        try:
            import cv2  # noqa: F401
        except Exception:  # noqa: BLE001
            messagebox.showerror(
                "Screen recording",
                "Video recording needs OpenCV.\n\nInstall it with:\n"
                "    python -m pip install opencv-python")
            return
        default = f"ptp_visualization_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        path = filedialog.asksaveasfilename(
            title="Record visualization to…", defaultextension=".mp4",
            initialfile=default, filetypes=[("MP4 video", "*.mp4")])
        if not path:
            return
        try:
            self._window_bbox()  # seed the cache on the main thread
            # recorder reads the cached rect only — never calls Tk off-thread
            rec = ScreenRecorder(path, lambda: self._cached_bbox, fps=15,
                                 screen_size_fn=self._screen_size)
            rec.start()
            self._screenrec = rec
            self._video_path = path
            self.btn_video.configure(text="■  Stop Video")
            self.status.set(f"Recording video → {os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Screen recording", f"Failed to start:\n{e}")
            self._screenrec = None

    def _stop_video(self):
        if self._screenrec is None:
            return
        rec = self._screenrec
        self._screenrec = None
        try:
            rec.stop()
        except Exception:
            pass
        self.btn_video.configure(text="◉  Rec Video")
        if rec.error is not None:
            messagebox.showerror("Screen recording", f"Recording error:\n{rec.error}")
            self.status.set("Video recording failed.")
        else:
            self.status.set(
                f"Saved video: {rec.frames_written} frames "
                f"({rec.duration_s:.1f}s) → {os.path.basename(self._video_path or '')}")

    # ------------------------------------------------------------------ clear
    def _reset_view(self):
        self.view.clear()
        self._cursor = 0
        self._open_strokes.clear()
        self._bounds_set = False
        self._last_report = None

    def clear_data(self):
        """Clear the display AND the underlying buffer (works during live)."""
        if self._live and self._cap is not None:
            self._cap.frames.clear()
            try:
                self._cap._frame_index = 0
            except Exception:
                pass
        self._loaded = None
        self._reset_view()
        self.scan_chart.update_series([])
        self.count_chart.update_series([])
        self._pressure_readout = None
        self.gesture_var.set("—")
        self.gesture_detail_var.set("")
        self._render_metrics(None, 0)
        self.status.set("Cleared.")

    def _toggle_overlay(self):
        self.view.set_spec_overlay(self.var_spec.get())

    def _on_pressure_change(self):
        src = self.var_pressure_src.get()
        if src == PRESSURE_OFF:
            self._pressure_readout = None
            self.status.set("Pressure readout off.")
            return
        if src == PRESSURE_HID and not self._buffer_has_pressure():
            self.status.set("Pressure = HID selected — but this data has no HID "
                            "pressure (typical for non-force-haptic pads).")
        else:
            self.status.set(f"Pressure source: {src}.")

    def _apply_window(self):
        w = self._window_seconds()
        if w is None:
            self.status.set("Averaging window: all data (no rolling limit).")
        else:
            self.status.set(f"Averaging window: last {w:g} s "
                            "(older samples discarded).")

    def _buffer_has_pressure(self) -> bool:
        frames, _ = self._source()
        for f in (frames or [])[-200:]:
            for c in f.active_contacts:
                if c.pressure is not None:
                    return True
        return False

    def _window_seconds(self) -> Optional[float]:
        """Rolling-window length in seconds, or None to keep everything."""
        s = self.var_window.get().strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
        return v if v > 0 else None

    @staticmethod
    def _frame_time_s(f) -> Optional[float]:
        if f.host_timestamp is not None:
            return float(f.host_timestamp)
        if f.scan_time is not None:
            return float(f.scan_time) * 100e-6  # 100us units -> seconds
        return None

    # ------------------------------------------------------------------ save/load
    def _current_recording(self) -> Optional[Recording]:
        frames, dev = self._source()
        if not frames:
            return None
        if self._loaded is not None and not self._live:
            return self._loaded
        frames = self._window_slice(frames)
        return Recording(device=dev, frames=list(frames), source="live")

    def _window_slice(self, frames):
        """Return only the frames within the rolling window (live only)."""
        if not self._live:
            return frames
        w = self._window_seconds()
        if w is None:
            return frames
        return self._slice_by_window(frames, w, self._frame_time_s)

    @staticmethod
    def _slice_by_window(frames, w, time_fn):
        """Tail of ``frames`` whose timestamp is within ``w`` seconds of the last."""
        if w is None or len(frames) < 2:
            return frames
        t_last = time_fn(frames[-1])
        if t_last is None:
            return frames
        cutoff = t_last - w
        for i in range(len(frames)):
            ts = time_fn(frames[i])
            if ts is not None and ts >= cutoff:
                return frames[i:]
        return frames[-1:]

    def save_csv(self):
        rec = self._current_recording()
        if rec is None or not rec.frames:
            messagebox.showinfo("Save CSV", "No data to save yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Save CSV", defaultextension=".csv",
            initialfile=f"ptp_capture_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            export_csv(rec, path)
            self.status.set(f"Saved {len(rec.frames)} frames → {path}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Save CSV", f"Failed:\n{e}")

    def save_report(self):
        rec = self._current_recording()
        if rec is None or not rec.frames:
            messagebox.showinfo("Save Report", "No data to analyze yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Save report PNG (JSON written alongside)",
            defaultextension=".png",
            initialfile=f"ptp_report_{datetime.now():%Y%m%d_%H%M%S}.png",
            filetypes=[("PNG", "*.png")])
        if not path:
            return
        try:
            import json
            from . import dashboard
            report = M.compute_all(rec)
            dashboard.show_report(rec, report, save_path=path)
            jpath = os.path.splitext(path)[0] + ".json"
            payload = report.to_dict()
            payload["spec"] = [c.__dict__ for c in SPEC.evaluate(rec, report).checks]
            with open(jpath, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            self.status.set(f"Report → {os.path.basename(path)} + .json")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Save Report", f"Failed:\n{e}")

    def save_all(self):
        """One-click: write CSV + PNG/JSON report (and finalize the screen video
        if one is recording) to a chosen folder with a common base name."""
        rec = self._current_recording()
        if rec is None or not rec.frames:
            messagebox.showinfo("Save All", "No data to save yet.")
            return
        folder = filedialog.askdirectory(title="Save All — choose an output folder")
        if not folder:
            return
        base = f"ptp_{datetime.now():%Y%m%d_%H%M%S}"
        stem = os.path.join(folder, base)
        written = []
        errors = []

        # 1) CSV
        try:
            export_csv(rec, stem + ".csv")
            written.append(os.path.basename(stem + ".csv"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"CSV: {e}")

        # 2) PNG dashboard + JSON report
        try:
            import json
            from . import dashboard
            report = M.compute_all(rec)
            dashboard.show_report(rec, report, save_path=stem + ".png")
            payload = report.to_dict()
            payload["spec"] = [c.__dict__ for c in SPEC.evaluate(rec, report).checks]
            with open(stem + ".json", "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            written.append(os.path.basename(stem + ".png"))
            written.append(os.path.basename(stem + ".json"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Report: {e}")

        # 3) Video — finalize an in-progress recording into this folder
        if self._screenrec is not None:
            try:
                self._stop_video()
                if self._video_path and os.path.exists(self._video_path):
                    dst = stem + ".mp4"
                    try:
                        import shutil
                        shutil.copy2(self._video_path, dst)
                        written.append(os.path.basename(dst))
                    except Exception:
                        written.append(os.path.basename(self._video_path))
            except Exception as e:  # noqa: BLE001
                errors.append(f"Video: {e}")

        msg = "Saved: " + ", ".join(written) if written else "Nothing written."
        if errors:
            msg += "\nErrors: " + "; ".join(errors)
        self.status.set(f"Save All → {folder}  ({len(written)} file(s))")
        if errors:
            messagebox.showwarning("Save All", msg)
        else:
            note = "" if self._screenrec is not None or ".mp4" in " ".join(written) \
                else "\n(No video: start 'Rec Video' before Save All to include one.)"
            messagebox.showinfo("Save All", msg + note)

    def open_recording(self):
        path = filedialog.askopenfilename(
            title="Open recording (CSV / JSONL / DigiInfo XML / ptrecorder file)",
            filetypes=[("Recordings", "*.csv *.tsv *.txt *.log *.jsonl *.xml"),
                       ("DigiInfo XML", "*.xml"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            rec = self._load_any(path)
            if self._live:
                self.stop_live()
            self._loaded = rec
            self._reset_view()
            self.status.set(f"Loaded {len(rec.frames)} frames from {os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Open recording", f"Failed:\n{e}")

    def _load_any(self, path: str) -> Recording:
        from .loaders import (load_csv, load_ptrecorder_dir, infer_logical_range,
                              load_digiinfo_xml, is_digiinfo_xml)
        dev = DeviceInfo()
        self._apply_size_override(dev)
        if path.lower().endswith(".jsonl"):
            rec = self._load_jsonl(path, dev)
        elif path.lower().endswith(".xml") or is_digiinfo_xml(path):
            # DigiInfo XML carries its own device resolution; don't clobber it
            # with a blank size unless the user typed an override.
            xdev = dev if (dev.x_physical_mm or dev.y_physical_mm) else None
            rec = load_digiinfo_xml(path, xdev)
            return rec
        else:
            try:
                rec = load_csv(path, dev)
            except Exception:
                rec = load_ptrecorder_dir(os.path.dirname(path), dev)
        if (dev.width_mm or dev.height_mm) and abs(rec.device.x_logical_max - rec.device.x_logical_min) <= 1:
            infer_logical_range(rec)
        elif abs(rec.device.x_logical_max - rec.device.x_logical_min) <= 1:
            infer_logical_range(rec, force=True)
        return rec

    def _load_jsonl(self, path: str, dev: DeviceInfo) -> Recording:
        import json
        from .models import Frame, Contact
        frames = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                o = json.loads(line)
                contacts = [Contact(contact_id=c["id"], x=c["x"], y=c["y"],
                                    tip=bool(c.get("tip", 1)),
                                    confidence=bool(c.get("conf", 1)),
                                    width=c.get("w"), height=c.get("h"),
                                    pressure=c.get("p"))
                            for c in o.get("contacts", [])]
                frames.append(Frame(index=o.get("i", len(frames)),
                                    scan_time=o.get("scan"),
                                    contacts=contacts,
                                    contact_count=o.get("cc"),
                                    button=bool(o.get("btn", 0)),
                                    host_timestamp=o.get("t")))
        return Recording(device=dev, frames=frames, source=path)

    def apply_size(self):
        # force the view to re-fit with new size next tick
        self._reset_view()
        self.status.set("Applied sensor size.")

    # ------------------------------------------------------------------ render loop
    def _tick(self):
        if not self._alive:
            return
        t0 = time.perf_counter()
        try:
            self._render_new_frames()
            if self._screenrec is not None:
                self._window_bbox()   # refresh cached rect on the main thread
            now = time.perf_counter()
            if now - self._last_metrics_t >= self.METRICS_EVERY:
                self._last_metrics_t = now
                self._update_metrics_and_charts()
        except Exception:
            traceback.print_exc()
        dt = (time.perf_counter() - t0) * 1000
        self.fps_var.set(f"render {dt:4.1f} ms")
        self._after_id = self.after(self.RENDER_MS, self._tick)

    def _render_new_frames(self):
        frames, dev = self._source()
        if dev is None or not frames:
            return
        if not self._ensure_bounds(dev):
            return  # waiting for device size
        if not self.view.is_ready():
            return  # canvas not sized yet; don't consume frames prematurely
        n = len(frames)
        if self._cursor > n:           # buffer was cleared
            self._reset_view()
            return
        # process up to a cap per tick to stay responsive on big offline loads
        end = min(n, self._cursor + 5000)
        self._render_range(frames, dev, self._cursor, end)
        self._cursor = end

    def _render_range(self, frames, dev, start, end):
        """Draw frames[start:end] incrementally onto the touchpad view."""
        for i in range(start, end):
            f = frames[i]
            present: Set[int] = set()
            for c in f.active_contacts:
                present.add(c.contact_id)
                x, y = self._disp_xy(dev, c)
                last = self._open_strokes.get(c.contact_id)
                new_stroke = last is None or (f.index - last) > 1
                self.view.add_point(c.contact_id, x, y, new_stroke)
                self._open_strokes[c.contact_id] = f.index
            # lift detection: any open contact missing from this frame
            for cid in list(self._open_strokes.keys()):
                if cid not in present:
                    self.view.end_stroke(cid)
                    self.view.hide_marker(cid)
                    del self._open_strokes[cid]

    def _rebuild_trace(self):
        """Clear and redraw the whole trace from the current buffer (used after a
        rolling-window trim or a contact-size toggle)."""
        frames, dev = self._source()
        self.view.clear()
        self._open_strokes.clear()
        if dev is None or not frames:
            self._cursor = 0
            return
        if not self._ensure_bounds(dev) or not self.view.is_ready():
            # can't draw yet; let the normal render loop pick these up later
            self._cursor = 0
            return
        self._render_range(frames, dev, 0, len(frames))
        self._cursor = len(frames)

    def _apply_rolling_window(self):
        """Drop live-buffer frames older than the rolling window (frees memory and
        keeps metrics bounded). No-op when the window is unset or not live."""
        if not (self._live and self._cap is not None):
            return
        w = self._window_seconds()
        if w is None:
            return
        frames = self._cap.frames
        if len(frames) < 2:
            return
        t_last = self._frame_time_s(frames[-1])
        t_first = self._frame_time_s(frames[0])
        if t_last is None or t_first is None:
            return
        cutoff = t_last - w
        # hysteresis: only trim once we're >1 s past the window, to avoid churn
        if cutoff - t_first < 1.0:
            return
        keep_from = 0
        for i, f in enumerate(frames):
            ts = self._frame_time_s(f)
            if ts is not None and ts >= cutoff:
                keep_from = i
                break
        if keep_from <= 0:
            return
        del frames[:keep_from]
        self._rebuild_trace()

    def _update_metrics_and_charts(self):
        self._apply_rolling_window()
        rec = self._current_recording()
        if rec is None or not rec.frames:
            return
        timing = M.timing_metrics(rec)
        self.scan_chart.update_series(timing.intervals_ms or [],
                                      ymax=max((timing.mean_interval_ms or 8) * 2.5, 10))
        ct = M.contact_timing_metrics(rec, timing)
        self.count_chart.update_series(ct.contact_count_series or [])
        report = M.compute_all(rec)
        self._last_report = report
        self._pressure_readout = self._compute_pressure(rec)
        ev = SPEC.evaluate(rec, report)
        self._render_spec(ev)
        self._render_metrics(report, len(rec.frames))
        self._render_gesture(rec)

    def _render_gesture(self, rec: Recording):
        """Classify and display the current gesture (live) in the panel."""
        # only meaningful for live capture / recent motion
        try:
            g = GEST.recognize(rec.frames, rec.device, window_s=1.2,
                               time_fn=self._frame_time_s)
        except Exception:
            g = GEST.GestureResult()
        self.gesture_var.set(g.name)
        self.gesture_detail_var.set(g.detail)
        self._gesture_label.configure(fg=ACCENT if g.name != "—" else MUTED)

    def _compute_pressure(self, rec: Recording) -> Optional[str]:
        """Latest HID pressure readout string, or None."""
        src = self.var_pressure_src.get()
        if src == PRESSURE_OFF or not rec.frames:
            return None
        for f in reversed(rec.frames[-60:]):
            acs = f.active_contacts
            if not acs:
                continue
            vals = [c.pressure for c in acs if c.pressure is not None]
            if vals:
                return f"{max(vals):.0f} (HID)"
            return "n/a (device has no HID pressure)"
        return None

    def _render_spec(self, ev: SPEC.SpecEvaluation):
        colors = {SPEC.PASS: GOOD, SPEC.FAIL: BAD, SPEC.UNKNOWN: "#475569"}
        for c in ev.checks:
            row = self._spec_rows.get(c.name)
            if not row:
                continue
            row["dot"].itemconfig("d", fill=colors.get(c.status, "#475569"))
            row["val"].configure(text=c.detail or c.limit,
                                 fg=colors.get(c.status, MUTED))
        ov = ev.overall
        self.overall_var.set(f"Overall: {ov}")
        self._overall_label.configure(fg=colors.get(ov, MUTED))

    def _render_metrics(self, report: Optional[M.MetricsReport], n_frames: int):
        self.meas.configure(state=tk.NORMAL)
        self.meas.delete("1.0", tk.END)
        if report is None:
            self.meas.insert(tk.END, "No data.")
            self.meas.configure(state=tk.DISABLED)
            self._render_dropout_flag(None)
            return
        r, j, lin, tim = report.resolution, report.jitter, report.linearity, report.timing
        cont = report.continuity
        d = report.device
        lines = [
            f"frames        {n_frames}",
            f"device        {d['name'][:30]}",
            f"size mm       {_fmt(d['width_mm'],'',1)} x {_fmt(d['height_mm'],'',1)}",
            f"resolution    X {_fmt(r.reported_x_dpi,' dpi',0)}  Y {_fmt(r.reported_y_dpi,' dpi',0)}",
            f"  counts/mm   X {_fmt(r.reported_x_counts_per_mm,'',1)}  Y {_fmt(r.reported_y_counts_per_mm,'',1)}",
            f"  step (emp)  X {_fmt(r.empirical_x_step_mm,' mm',4)}",
            "",
            f"jitter RMS    {_fmt(j.worst_rms_radial_mm,' mm',4)}",
            f"jitter p-p    {_fmt(j.worst_p2p_mm,' mm',4)}",
            f"mean dist ctr {_fmt(j.worst_mean_dist_from_init_mm,' mm',4)}",
            f"  segments    {len(j.per_segment)}",
            "",
            f"linearity max {_fmt(lin.worst_max_dev_mm,' mm',4)}",
            f"linearity rms {_fmt(lin.worst_rms_dev_mm,' mm',4)}",
            f"  segments    {len(lin.per_segment)}",
            "",
            f"swipe breakups {cont.dropout_count}",
            f"  max missing  {cont.max_missing_frames} frame(s)",
            "",
            f"report rate   {_fmt(tim.report_rate_hz,' Hz',1)}",
            f"mean interval {_fmt(tim.mean_interval_ms,' ms',3)}",
            f"timing jitter {_fmt(tim.timing_jitter_ms,' ms',3)}",
            f"clock         {tim.source}",
        ]
        win = self._window_seconds()
        if win is not None:
            lines.append(f"avg window    last {win:g} s")
        if self._pressure_readout is not None:
            lines.append(f"pressure      {self._pressure_readout}")
        self.meas.insert(tk.END, "\n".join(lines))
        self.meas.configure(state=tk.DISABLED)
        self._render_dropout_flag(cont)

    def _render_dropout_flag(self, cont: "M.ContinuityMetrics"):
        if cont is None or cont.contacts_analyzed == 0:
            self.dropout_var.set("Contact continuity: —")
            self._dropout_label.configure(fg=MUTED)
            return
        n = cont.dropout_count
        if n == 0:
            self.dropout_var.set("Contact continuity: OK (continuous)")
            self._dropout_label.configure(fg=GOOD)
        else:
            self.dropout_var.set(
                f"⚠ Contact BROKE UP {n}× (max {cont.max_missing_frames} "
                f"frame(s) missing) — spotty contact during swipe")
            self._dropout_label.configure(fg=BAD)


    # ------------------------------------------------------------------ close
    def _on_close(self):
        self._alive = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        if self._logger is not None:
            try:
                self._logger.stop()
            except Exception:
                pass
        if self._screenrec is not None:
            try:
                self._screenrec.stop()
            except Exception:
                pass
        if self._cap is not None:
            try:
                self._cap.stop()
            except Exception:
                pass
        self.destroy()


def main():
    app = PTPMetricsApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
