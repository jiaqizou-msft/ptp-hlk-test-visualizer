"""Matplotlib visualization for PTP recordings.

Renders a single dashboard figure with the four views requested for PTP
analysis, plus a numerical metrics panel:

  1. Position (X/Y)        - 2D touchpad surface with per-contact colour traces
  2. Scan time / frame     - per-frame interval (ms) and report rate
  3. Pressure / signal     - pressure if reported, else confidence / contact size
  4. Contact timing        - Gantt-style timeline of each contact id
  5. Metrics summary       - linearity, jitter, resolution numbers

Use :func:`show_report` for a static analysis of a finished recording, or
:func:`live_dashboard` for an animated real-time view backed by
:class:`~ptp_metrics.live_capture.LiveCapture`.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .models import Recording, DeviceInfo
from . import metrics as M


_CONTACT_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706",
                   "#9333ea", "#0891b2", "#db2777", "#65a30d"]


def _color(cid: int) -> str:
    return _CONTACT_COLORS[cid % len(_CONTACT_COLORS)]


def _fmt(v, unit="", nd=3):
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}{unit}"


def build_figure(rec: Recording, report: Optional[M.MetricsReport] = None):
    """Build (but do not show) the full dashboard figure for a recording."""
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec

    report = report or M.compute_all(rec)
    tracks = M.extract_tracks(rec)
    dev = rec.device

    fig = plt.figure(figsize=(15, 9), constrained_layout=True)
    fig.suptitle(f"PTP Metrics  -  {dev.name}   ({rec.source})", fontsize=14, fontweight="bold")
    gs = GridSpec(3, 3, figure=fig, height_ratios=[1.3, 1, 1])

    ax_pos = fig.add_subplot(gs[0:2, 0:2])
    ax_scan = fig.add_subplot(gs[0, 2])
    ax_press = fig.add_subplot(gs[1, 2])
    ax_timeline = fig.add_subplot(gs[2, 0:2])
    ax_text = fig.add_subplot(gs[2, 2])

    _plot_position(ax_pos, rec, tracks, dev, report)
    _plot_scan_time(ax_scan, report.timing)
    _plot_pressure(ax_press, rec, tracks)
    _plot_timeline(ax_timeline, rec, report)
    _plot_metrics_text(ax_text, report)
    return fig


def show_report(rec: Recording, report: Optional[M.MetricsReport] = None,
                save_path: Optional[str] = None):
    import matplotlib.pyplot as plt
    fig = build_figure(rec, report)
    if save_path:
        fig.savefig(save_path, dpi=130)
    else:
        plt.show()
    return fig


# --------------------------------------------------------------------------- #
# Individual panels
# --------------------------------------------------------------------------- #
def _plot_position(ax, rec, tracks, dev: DeviceInfo, report):
    use_mm = bool(dev.x_counts_per_mm and dev.y_counts_per_mm)
    for t in tracks:
        color = _color(t.contact_id)
        first = True
        for sx, sy in M.iter_segments(t):
            if use_mm:
                xs = (sx - dev.x_logical_min) / dev.x_counts_per_mm
                ys = (sy - dev.y_logical_min) / dev.y_counts_per_mm
            else:
                xs, ys = sx, sy
            ax.plot(xs, ys, "-", color=color, lw=1.0, alpha=0.8,
                    label=f"contact {t.contact_id}" if first else None)
            ax.scatter(xs, ys, s=6, color=color, alpha=0.5)
            if len(xs):
                ax.scatter([xs[0]], [ys[0]], marker="o", s=40, edgecolor="k",
                           facecolor=color, zorder=5)
                ax.scatter([xs[-1]], [ys[-1]], marker="s", s=40, edgecolor="k",
                           facecolor=color, zorder=5)
            first = False
    # show best-fit linearity line for the worst drag segment
    if use_mm:
        for lr in report.linearity.per_segment:
            for t in tracks:
                if t.contact_id == lr.contact_id and len(t) > 2:
                    xs = (t.x - dev.x_logical_min) / dev.x_counts_per_mm
                    ys = (t.y - dev.y_logical_min) / dev.y_counts_per_mm
                    _draw_fit_line(ax, xs, ys)
                    break
    unit = "mm" if use_mm else "counts"
    ax.set_title("Position (X / Y)")
    ax.set_xlabel(f"X ({unit})")
    ax.set_ylabel(f"Y ({unit})")
    ax.invert_yaxis()  # touchpad origin top-left
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.3)
    if tracks:
        ax.legend(loc="upper right", fontsize=8)


def _draw_fit_line(ax, xs, ys):
    pts = np.column_stack([xs, ys])
    c = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    d = vt[0]
    proj = (pts - c) @ d
    p1 = c + d * proj.min()
    p2 = c + d * proj.max()
    ax.plot([p1[0], p2[0]], [p1[1], p2[1]], "k--", lw=1.0, alpha=0.6,
            label="best-fit (linearity)")


def _plot_scan_time(ax, timing: M.TimingMetrics):
    if timing.intervals_ms:
        iv = np.array(timing.intervals_ms)
        ax.plot(iv, "-", color="#0891b2", lw=1.0)
        if timing.mean_interval_ms:
            ax.axhline(timing.mean_interval_ms, color="k", ls="--", lw=0.8,
                       label=f"mean {timing.mean_interval_ms:.2f} ms")
        ax.fill_between(range(len(iv)),
                        timing.mean_interval_ms - (timing.std_interval_ms or 0),
                        timing.mean_interval_ms + (timing.std_interval_ms or 0),
                        color="#0891b2", alpha=0.15)
        ax.legend(fontsize=8)
        title = "Scan time (frame interval)"
        if timing.report_rate_hz:
            title += f"  ~{timing.report_rate_hz:.0f} Hz"
        ax.set_title(title)
    else:
        ax.set_title("Scan time (no timing data)")
        ax.text(0.5, 0.5, timing.note or "no data", ha="center", va="center",
                transform=ax.transAxes, fontsize=8, color="gray")
    ax.set_xlabel("frame")
    ax.set_ylabel("interval (ms)")
    ax.grid(True, alpha=0.3)


def _plot_pressure(ax, rec, tracks):
    has_pressure = any(c.pressure is not None for f in rec.frames for c in f.contacts)
    plotted = False
    for t in tracks:
        cid = t.contact_id
        series = []
        for f in rec.frames:
            for c in f.contacts:
                if c.contact_id == cid and c.tip:
                    if has_pressure and c.pressure is not None:
                        series.append(c.pressure)
                    elif c.width is not None and c.height is not None:
                        series.append((c.width + c.height) / 2.0)
                    else:
                        series.append(1.0 if c.confidence else 0.0)
        if series:
            ax.plot(series, "-", color=_color(cid), lw=1.0, label=f"contact {cid}")
            plotted = True
    if has_pressure:
        ax.set_title("Pressure")
        ax.set_ylabel("pressure (counts)")
    else:
        ax.set_title("Signal (contact size / confidence)")
        ax.set_ylabel("size or confidence")
    if not plotted:
        ax.text(0.5, 0.5, "no pressure/size data", ha="center", va="center",
                transform=ax.transAxes, fontsize=8, color="gray")
    ax.set_xlabel("sample")
    ax.grid(True, alpha=0.3)
    if plotted:
        ax.legend(fontsize=8)


def _plot_timeline(ax, rec, report: M.MetricsReport):
    ct = report.contact_timing
    use_ms = any(c.down_time_ms is not None for c in ct.contacts)
    for row, cl in enumerate(ct.contacts):
        if use_ms and cl.down_time_ms is not None and cl.up_time_ms is not None:
            start, end = cl.down_time_ms, cl.up_time_ms
        else:
            start, end = cl.first_frame, cl.last_frame
        ax.barh(row, max(end - start, 0.5), left=start, height=0.6,
                color=_color(cl.contact_id), alpha=0.8)
        ax.text(start, row, f" id {cl.contact_id} "
                            f"({cl.n_reports} rpts"
                            + (f", {cl.duration_ms:.0f} ms" if cl.duration_ms else "")
                            + ")", va="center", fontsize=8)
    ax.set_yticks(range(len(ct.contacts)))
    ax.set_yticklabels([f"c{c.contact_id}" for c in ct.contacts])
    ax.set_xlabel("time (ms)" if use_ms else "frame index")
    ax.set_title(f"Contact timing structure (max simultaneous: {ct.max_simultaneous})")
    ax.grid(True, axis="x", alpha=0.3)
    if not ct.contacts:
        ax.text(0.5, 0.5, "no contacts", ha="center", va="center",
                transform=ax.transAxes, color="gray")


def _plot_metrics_text(ax, report: M.MetricsReport):
    ax.axis("off")
    r, j, lin, tim = report.resolution, report.jitter, report.linearity, report.timing
    lines = [
        ("RESOLUTION", ""),
        ("  reported X", f"{_fmt(r.reported_x_counts_per_mm,' c/mm',1)}  "
                         f"({_fmt(r.reported_x_dpi,' dpi',0)})"),
        ("  reported Y", f"{_fmt(r.reported_y_counts_per_mm,' c/mm',1)}  "
                         f"({_fmt(r.reported_y_dpi,' dpi',0)})"),
        ("  empirical step X", f"{_fmt(r.empirical_x_step_mm,' mm',4)}"),
        ("  empirical step Y", f"{_fmt(r.empirical_y_step_mm,' mm',4)}"),
        ("JITTER (stationary)", ""),
        ("  worst RMS radial", f"{_fmt(j.worst_rms_radial_mm,' mm',4)}"),
        ("  worst peak-to-peak", f"{_fmt(j.worst_p2p_mm,' mm',4)}"),
        ("  worst mean dist ctr", f"{_fmt(j.worst_mean_dist_from_init_mm,' mm',4)}"),
        ("  segments", f"{len(j.per_segment)}"),
        ("LINEARITY (drag)", ""),
        ("  worst max deviation", f"{_fmt(lin.worst_max_dev_mm,' mm',4)}"),
        ("  worst RMS deviation", f"{_fmt(lin.worst_rms_dev_mm,' mm',4)}"),
        ("  segments", f"{len(lin.per_segment)}"),
        ("TIMING", ""),
        ("  report rate", f"{_fmt(tim.report_rate_hz,' Hz',1)}"),
        ("  mean interval", f"{_fmt(tim.mean_interval_ms,' ms',3)}"),
        ("  timing jitter (std)", f"{_fmt(tim.timing_jitter_ms,' ms',3)}"),
        ("  source", tim.source),
    ]
    y = 1.0
    ax.text(0.0, y, "PTP METRICS", fontsize=11, fontweight="bold",
            transform=ax.transAxes, va="top")
    y -= 0.06
    for label, value in lines:
        bold = value == ""
        ax.text(0.0, y, label, fontsize=9,
                fontweight="bold" if bold else "normal",
                transform=ax.transAxes, va="top")
        if value:
            ax.text(1.0, y, value, fontsize=9, ha="right",
                    transform=ax.transAxes, va="top", family="monospace")
        y -= 0.05
    notes = [n for n in (j.note, lin.note, r.note, tim.note) if n]
    if notes:
        ax.text(0.0, y - 0.01, "\n".join("• " + n for n in notes[:3]),
                fontsize=7, color="#b45309", transform=ax.transAxes, va="top",
                wrap=True)


# --------------------------------------------------------------------------- #
# Live animated dashboard
# --------------------------------------------------------------------------- #
def live_dashboard(window_seconds: float = 6.0, refresh_ms: int = 50):
    """Open a real-time dashboard fed by :class:`LiveCapture` (Windows only)."""
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from .live_capture import LiveCapture, is_supported

    if not is_supported():
        raise RuntimeError("Live dashboard requires Windows.")

    cap = LiveCapture()
    cap.start()

    fig, (ax_pos, ax_scan) = plt.subplots(1, 2, figsize=(13, 6))
    fig.suptitle("PTP Live Capture  (move a finger on the touchpad)", fontweight="bold")
    ax_pos.set_title("Position (X/Y)")
    ax_pos.set_aspect("equal", adjustable="datalim")
    ax_pos.invert_yaxis()
    ax_pos.grid(True, alpha=0.3)
    ax_scan.set_title("Scan time (frame interval, ms)")
    ax_scan.grid(True, alpha=0.3)
    text = ax_pos.text(0.02, 0.98, "", transform=ax_pos.transAxes, va="top",
                       fontsize=9, family="monospace",
                       bbox=dict(boxstyle="round", fc="white", alpha=0.7))

    def update(_):
        frames = list(cap.frames)
        if not frames:
            return
        rec = Recording(device=cap.device, frames=frames, source="live")
        tracks = M.extract_tracks(rec)
        dev = cap.device
        use_mm = bool(dev.x_counts_per_mm and dev.y_counts_per_mm)
        ax_pos.cla()
        ax_pos.set_title("Position (X/Y)")
        ax_pos.grid(True, alpha=0.3)
        ax_pos.invert_yaxis()
        recent = frames[-int(window_seconds * 150):]
        for t in tracks:
            color = _color(t.contact_id)
            for sx, sy in M.iter_segments(t):
                if use_mm:
                    xs = (sx - dev.x_logical_min) / dev.x_counts_per_mm
                    ys = (sy - dev.y_logical_min) / dev.y_counts_per_mm
                else:
                    xs, ys = sx, sy
                ax_pos.plot(xs, ys, "-", color=color, lw=1)
        timing = M.timing_metrics(rec)
        ax_scan.cla()
        ax_scan.set_title("Scan time (frame interval, ms)")
        ax_scan.grid(True, alpha=0.3)
        if timing.intervals_ms:
            ax_scan.plot(timing.intervals_ms[-400:], color="#0891b2", lw=1)
        res = M.resolution_metrics(rec, tracks)
        jit = M.jitter_metrics(rec, tracks)
        text.set_text(
            f"frames: {len(frames)}\n"
            f"rate:   {_fmt(timing.report_rate_hz,' Hz',1)}\n"
            f"res X:  {_fmt(res.reported_x_counts_per_mm,' c/mm',1)}\n"
            f"jitter: {_fmt(jit.worst_rms_radial_mm,' mm',3)}")
        ax_pos.add_artist(text)

    anim = FuncAnimation(fig, update, interval=refresh_ms, cache_frame_data=False)
    try:
        plt.show()
    finally:
        cap.stop()
    return anim
