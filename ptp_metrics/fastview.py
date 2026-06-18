"""High-performance, native Tkinter rendering for live PTP visualization.

Why not matplotlib for the live view: matplotlib re-renders the whole figure
(Agg raster + axis layout) on every update, which becomes very laggy once a
session accumulates thousands of points. These widgets instead draw directly on
a ``tk.Canvas`` and update *incrementally* — each contact stroke is a single
persistent canvas line whose coordinates are extended as new samples arrive, so
per-frame work is O(new points), not O(total points).

Two widgets:

* :class:`TouchpadView`   - the X/Y surface with grid, per-contact traces, live
  contact markers, and optional spec overlays (jitter 0.5 mm box, etc).
* :class:`StripChart`     - a scrolling strip chart (used for scan-time interval).

Both use a fixed data->pixel transform when the physical pad size is known
(the normal live case), which is what makes fully-incremental drawing possible.
"""

from __future__ import annotations

import tkinter as tk
from typing import Dict, List, Optional, Tuple

_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706",
           "#9333ea", "#0891b2", "#db2777", "#65a30d"]


def color_for(cid: int) -> str:
    return _COLORS[int(cid) % len(_COLORS)]


class _Transform:
    """Maps data coordinates (mm or logical) to canvas pixels, preserving aspect."""

    def __init__(self):
        self.ok = False
        self.sx = self.sy = 1.0
        self.ox = self.oy = 0.0
        self.dxmin = self.dymin = 0.0

    def fit(self, w: int, h: int, dxmin, dxmax, dymin, dymax, pad: int = 18):
        dw = max(dxmax - dxmin, 1e-6)
        dh = max(dymax - dymin, 1e-6)
        avail_w = max(w - 2 * pad, 10)
        avail_h = max(h - 2 * pad, 10)
        s = min(avail_w / dw, avail_h / dh)        # uniform scale (aspect-correct)
        self.sx = self.sy = s
        # center within the canvas
        self.ox = pad + (avail_w - s * dw) / 2.0
        self.oy = pad + (avail_h - s * dh) / 2.0
        self.dxmin, self.dymin = dxmin, dymin
        self.dw, self.dh = dw, dh
        self.ok = True

    def px(self, x: float, y: float) -> Tuple[float, float]:
        return (self.ox + (x - self.dxmin) * self.sx,
                self.oy + (y - self.dymin) * self.sy)


class TouchpadView(tk.Canvas):
    """Incremental X/Y surface renderer."""

    def __init__(self, master, **kw):
        super().__init__(master, background="#0f172a", highlightthickness=0, **kw)
        self._tf = _Transform()
        self._bounds: Optional[Tuple[float, float, float, float]] = None
        self._unit = "mm"
        self._strokes: Dict[int, dict] = {}   # contact_id -> current open stroke
        self._all_line_ids: List[int] = []
        self._markers: Dict[int, int] = {}     # contact_id -> marker item id
        self._labels: Dict[int, int] = {}
        self._cw = self._ch = 0
        self._show_grid = True
        self._spec_overlay = False
        self._grid_mm = 5.0
        self.bind("<Configure>", self._on_resize)

    # ---- configuration ---------------------------------------------------- #
    def set_bounds(self, dxmin, dxmax, dymin, dymax, unit="mm"):
        """Fix the data extent (e.g. the pad size). Enables incremental drawing."""
        new = (dxmin, dxmax, dymin, dymax)
        if new != self._bounds or unit != self._unit:
            self._bounds = new
            self._unit = unit
            self._refit()
            self.redraw_background()
            # bounds changed -> existing pixel coords invalid; rebuild handled by caller clear

    def set_spec_overlay(self, on: bool):
        self._spec_overlay = on
        self.redraw_background()

    def is_ready(self) -> bool:
        """True once the data->pixel transform is valid (canvas has a size)."""
        return self._tf.ok

    # ---- lifecycle -------------------------------------------------------- #
    def clear(self):
        for lid in self._all_line_ids:
            self.delete(lid)
        self._all_line_ids.clear()
        self._strokes.clear()
        for mid in self._markers.values():
            self.delete(mid)
        for lid in self._labels.values():
            self.delete(lid)
        self._markers.clear()
        self._labels.clear()

    def _on_resize(self, ev):
        self._cw, self._ch = ev.width, ev.height
        self._refit()
        self.redraw_background()
        # existing strokes keep their (now stale) pixels until next clear/update;
        # caller should call rebuild_from on resize for correctness.

    def _refit(self):
        if self._bounds and self._cw > 2 and self._ch > 2:
            self._tf.fit(self._cw, self._ch, *self._bounds)

    # ---- background ------------------------------------------------------- #
    def redraw_background(self):
        self.delete("bg")
        if not self._tf.ok or not self._bounds:
            return
        dxmin, dxmax, dymin, dymax = self._bounds
        x0, y0 = self._tf.px(dxmin, dymin)
        x1, y1 = self._tf.px(dxmax, dymax)
        # pad surface
        self.create_rectangle(x0, y0, x1, y1, outline="#334155", width=2,
                              fill="#111827", tags="bg")
        if self._show_grid and self._unit == "mm":
            step = self._grid_mm
            gx = dxmin
            while gx <= dxmax + 1e-6:
                px, _ = self._tf.px(gx, dymin)
                self.create_line(px, y0, px, y1, fill="#1f2937", tags="bg")
                gx += step
            gy = dymin
            while gy <= dymax + 1e-6:
                _, py = self._tf.px(dxmin, gy)
                self.create_line(x0, py, x1, py, fill="#1f2937", tags="bg")
                gy += step
        # axis labels
        unit = self._unit
        self.create_text(x0 + 4, y0 + 2, anchor="nw", fill="#64748b",
                        text=f"0,0  ({unit})", font=("Segoe UI", 8), tags="bg")
        self.create_text(x1 - 4, y1 - 2, anchor="se", fill="#64748b",
                        text=f"{dxmax:.0f} x {dymax:.0f} {unit}",
                        font=("Segoe UI", 8), tags="bg")
        self.tag_lower("bg")

    # ---- incremental update ---------------------------------------------- #
    def add_point(self, contact_id: int, x: float, y: float, new_stroke: bool):
        """Append a sample to a contact's current stroke (pixels computed here)."""
        if not self._tf.ok:
            return
        px, py = self._tf.px(x, y)
        st = self._strokes.get(contact_id)
        if new_stroke or st is None:
            col = color_for(contact_id)
            lid = self.create_line(px, py, px, py, fill=col, width=2,
                                   capstyle="round", joinstyle="round", smooth=True)
            self._all_line_ids.append(lid)
            st = {"id": lid, "coords": [px, py]}
            self._strokes[contact_id] = st
        else:
            st["coords"].extend((px, py))
            # cap stroke length to keep coords() cheap on very long swipes
            if len(st["coords"]) > 4000:
                st["coords"] = st["coords"][-4000:]
            self.coords(st["id"], *st["coords"])
        # live marker + label
        self._update_marker(contact_id, px, py)

    def _update_marker(self, contact_id, px, py):
        col = color_for(contact_id)
        r = 6
        mid = self._markers.get(contact_id)
        if mid is None:
            mid = self.create_oval(px - r, py - r, px + r, py + r,
                                   outline="white", width=2, fill=col)
            self._markers[contact_id] = mid
            self._labels[contact_id] = self.create_text(
                px + 9, py - 9, text=f"{contact_id}", fill="white",
                font=("Segoe UI", 9, "bold"), anchor="w")
        else:
            self.coords(mid, px - r, py - r, px + r, py + r)
            self.coords(self._labels[contact_id], px + 9, py - 9)
            self.itemconfig(mid, fill=col)

    def hide_marker(self, contact_id):
        if contact_id in self._markers:
            self.delete(self._markers.pop(contact_id))
        if contact_id in self._labels:
            self.delete(self._labels.pop(contact_id))

    def end_stroke(self, contact_id):
        self._strokes.pop(contact_id, None)


class StripChart(tk.Canvas):
    """Lightweight scrolling line chart for a single scalar series (e.g. ms)."""

    def __init__(self, master, title="", target=None, **kw):
        super().__init__(master, background="#0f172a", highlightthickness=0, **kw)
        self._title = title
        self._target = target          # optional reference line value
        self._cw = self._ch = 0
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, ev):
        self._cw, self._ch = ev.width, ev.height

    def update_series(self, values: List[float], ymax: Optional[float] = None):
        self.delete("all")
        w, h = self._cw, self._ch
        if w < 4 or h < 4 or not values:
            return
        pad = 22
        vis = values[-300:]
        vmax = ymax if ymax is not None else (max(vis) * 1.2 if vis else 1.0)
        vmax = max(vmax, 1e-6)
        # gridlines + y labels
        for frac in (0.0, 0.5, 1.0):
            yy = h - pad - frac * (h - 2 * pad)
            self.create_line(pad, yy, w - 4, yy, fill="#1f2937")
            self.create_text(pad - 3, yy, anchor="e", fill="#64748b",
                            text=f"{frac*vmax:.0f}", font=("Segoe UI", 7))
        # target line
        if self._target is not None and self._target <= vmax:
            ty = h - pad - (self._target / vmax) * (h - 2 * pad)
            self.create_line(pad, ty, w - 4, ty, fill="#f59e0b", dash=(4, 3))
        # series polyline (single create_line call)
        n = len(vis)
        sx = (w - pad - 6) / max(n - 1, 1)
        pts = []
        for i, v in enumerate(vis):
            x = pad + i * sx
            y = h - pad - (min(v, vmax) / vmax) * (h - 2 * pad)
            pts.extend((x, y))
        if len(pts) >= 4:
            self.create_line(*pts, fill="#22d3ee", width=1)
        if self._title:
            self.create_text(pad, 8, anchor="nw", fill="#94a3b8",
                            text=self._title, font=("Segoe UI", 9, "bold"))


class Sparkbars(tk.Canvas):
    """Tiny bar gauge for contact-count over recent frames."""

    def __init__(self, master, title="", **kw):
        super().__init__(master, background="#0f172a", highlightthickness=0, **kw)
        self._title = title
        self._cw = self._ch = 0
        self.bind("<Configure>", lambda e: (setattr(self, "_cw", e.width), setattr(self, "_ch", e.height)))

    def update_series(self, values: List[int]):
        self.delete("all")
        w, h = self._cw, self._ch
        if w < 4 or h < 4 or not values:
            return
        pad = 18
        vis = values[-120:]
        vmax = max(max(vis), 1)
        n = len(vis)
        bw = (w - pad - 4) / max(n, 1)
        for i, v in enumerate(vis):
            x = pad + i * bw
            bh = (v / vmax) * (h - 2 * pad)
            self.create_rectangle(x, h - pad - bh, x + max(bw - 1, 1), h - pad,
                                 fill="#34d399", outline="")
        if self._title:
            self.create_text(pad, 6, anchor="nw", fill="#94a3b8",
                            text=self._title, font=("Segoe UI", 9, "bold"))
