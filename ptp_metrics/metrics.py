"""PTP quality-metrics engine.

Computes the numerical metrics that the HLK Precision Touchpad tests only report
as pass/fail:

* **Resolution** - reported (from the HID descriptor) and *empirical* (smallest
  quantisation step actually observed), in counts/mm and DPI.
* **Jitter** - position noise while a contact is held stationary, as RMS,
  peak-to-peak, and the mean L2 distance from the initial contact point, in
  millimetres (PTP "stationary jitter").
* **Linearity** - maximum / RMS perpendicular deviation of a straight-line drag
  from its best-fit line, in millimetres.
* **Scan time / frame timing** - report rate (Hz) and frame-interval statistics
  derived from the HID Scan Time field (and host timestamps when present).
* **Contact timing structure** - per-contact down/up/lifetime and report cadence.

All routines work on a :class:`~ptp_metrics.models.Recording`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from .models import Recording, DeviceInfo, Frame, SCAN_TIME_UNIT_US


# --------------------------------------------------------------------------- #
# Per-contact track extraction
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    """The time series of one contact across the recording."""

    contact_id: int
    frame_idx: np.ndarray            # frame indices where the contact was active
    x: np.ndarray                    # logical counts
    y: np.ndarray                    # logical counts
    scan_time_us: np.ndarray         # per-sample scan time in microseconds (may be NaN)
    host_time_s: np.ndarray          # per-sample host timestamp in seconds (may be NaN)

    def __len__(self) -> int:
        return len(self.x)


def iter_segments(track: "Track", max_frame_gap: float = 1.0):
    """Yield (x, y) sub-arrays of a track that are contiguous in time.

    A single contact id is reused across separate strokes (finger lifts and
    touches down again). Those strokes are separated by gaps in the global frame
    index. Splitting on those gaps prevents drawing a connecting line between the
    end of one stroke and the start of the next (the "fan from origin" artifact).
    """
    n = len(track)
    if n == 0:
        return
    fi = track.frame_idx
    start = 0
    for i in range(1, n):
        if fi[i] - fi[i - 1] > max_frame_gap:
            yield track.x[start:i], track.y[start:i]
            start = i
    yield track.x[start:n], track.y[start:n]


def extract_tracks(rec: Recording) -> List[Track]:
    """Split a recording into per-contact position tracks (active/tip-down only)."""
    buckets: Dict[int, Dict[str, list]] = {}
    for f in rec.frames:
        st = f.scan_time_us
        ht = f.host_timestamp
        for c in f.contacts:
            if not c.tip:
                continue
            b = buckets.setdefault(c.contact_id, {"i": [], "x": [], "y": [], "st": [], "ht": []})
            b["i"].append(f.index)
            b["x"].append(c.x)
            b["y"].append(c.y)
            b["st"].append(st if st is not None else np.nan)
            b["ht"].append(ht if ht is not None else np.nan)
    tracks = []
    for cid, b in sorted(buckets.items()):
        tracks.append(Track(
            contact_id=cid,
            frame_idx=np.asarray(b["i"], dtype=float),
            x=np.asarray(b["x"], dtype=float),
            y=np.asarray(b["y"], dtype=float),
            scan_time_us=np.asarray(b["st"], dtype=float),
            host_time_s=np.asarray(b["ht"], dtype=float),
        ))
    return tracks


def _unwrap_scan_time(st_us: np.ndarray, unit_us: float = SCAN_TIME_UNIT_US) -> np.ndarray:
    """Scan time is a 16-bit counter (0..65535 in 100us units) that wraps.

    Returns a monotonically increasing microsecond series (NaNs preserved).
    """
    out = np.array(st_us, dtype=float)
    valid = ~np.isnan(out)
    if valid.sum() < 2:
        return out
    counts = out / unit_us
    wrap = 65536.0
    offset = 0.0
    prev = None
    result = np.full_like(out, np.nan)
    for i in range(len(out)):
        if np.isnan(counts[i]):
            continue
        c = counts[i]
        if prev is not None and c + offset < prev - 1:
            offset += wrap
        val = c + offset
        result[i] = val * unit_us
        prev = val
    return result


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
@dataclass
class ResolutionMetrics:
    reported_x_counts_per_mm: Optional[float] = None
    reported_y_counts_per_mm: Optional[float] = None
    reported_x_dpi: Optional[float] = None
    reported_y_dpi: Optional[float] = None
    empirical_x_step_counts: Optional[float] = None
    empirical_y_step_counts: Optional[float] = None
    empirical_x_step_mm: Optional[float] = None
    empirical_y_step_mm: Optional[float] = None
    note: str = ""


def _min_positive_step(values: np.ndarray) -> Optional[float]:
    """Smallest non-zero absolute first difference (the quantisation step)."""
    if len(values) < 2:
        return None
    d = np.abs(np.diff(values))
    d = d[d > 1e-9]
    if d.size == 0:
        return None
    # use a robust low percentile to avoid single-sample artefacts
    return float(np.percentile(d, 1))


def resolution_metrics(rec: Recording, tracks: Optional[List[Track]] = None) -> ResolutionMetrics:
    tracks = tracks if tracks is not None else extract_tracks(rec)
    dev = rec.device
    m = ResolutionMetrics(
        reported_x_counts_per_mm=dev.x_counts_per_mm,
        reported_y_counts_per_mm=dev.y_counts_per_mm,
        reported_x_dpi=dev.x_dpi,
        reported_y_dpi=dev.y_dpi,
    )
    xs = np.concatenate([t.x for t in tracks]) if tracks else np.array([])
    ys = np.concatenate([t.y for t in tracks]) if tracks else np.array([])
    # empirical step should be measured per-track to avoid cross-contact jumps
    x_steps = [s for s in (_min_positive_step(t.x) for t in tracks) if s]
    y_steps = [s for s in (_min_positive_step(t.y) for t in tracks) if s]
    if x_steps:
        m.empirical_x_step_counts = float(np.min(x_steps))
        if dev.x_counts_per_mm:
            m.empirical_x_step_mm = m.empirical_x_step_counts / dev.x_counts_per_mm
    if y_steps:
        m.empirical_y_step_counts = float(np.min(y_steps))
        if dev.y_counts_per_mm:
            m.empirical_y_step_mm = m.empirical_y_step_counts / dev.y_counts_per_mm
    if not dev.x_counts_per_mm:
        m.note = ("Reported resolution unavailable: device physical size unknown. "
                  "Provide DeviceInfo physical extents or DigiInfo output.")
    return m


# --------------------------------------------------------------------------- #
# Stationary jitter
# --------------------------------------------------------------------------- #
@dataclass
class JitterResult:
    contact_id: int
    n_samples: int
    rms_x_mm: Optional[float]
    rms_y_mm: Optional[float]
    rms_radial_mm: Optional[float]
    p2p_x_mm: Optional[float]
    p2p_y_mm: Optional[float]
    mean_dist_from_init_mm: Optional[float]
    rms_x_counts: float
    rms_y_counts: float
    p2p_x_counts: float
    p2p_y_counts: float
    mean_dist_from_init_counts: float


@dataclass
class JitterMetrics:
    per_segment: List[JitterResult] = field(default_factory=list)
    worst_rms_radial_mm: Optional[float] = None
    worst_p2p_mm: Optional[float] = None
    worst_mean_dist_from_init_mm: Optional[float] = None
    note: str = ""


def _stationary_segments(t: Track, dev: DeviceInfo,
                         move_thresh_mm: float = 1.0,
                         min_len: int = 8) -> List[Tuple[int, int]]:
    """Find index ranges where the contact stays within a small window (held still).

    A slow straight-line drag has small *per-sample* steps but large cumulative
    travel, so a speed threshold alone would wrongly flag it as stationary.
    Instead we grow a segment only while every sample remains within
    ``move_thresh_mm`` of that segment's running centroid; a drag quickly walks
    outside the window and closes the segment.
    """
    n = len(t)
    if n < min_len:
        return []
    cpm_x = dev.x_counts_per_mm or 1.0
    cpm_y = dev.y_counts_per_mm or 1.0
    xs_mm = t.x / cpm_x
    ys_mm = t.y / cpm_y

    segments: List[Tuple[int, int]] = []
    start = 0
    sx = xs_mm[0]
    sy = ys_mm[0]
    cnt = 1
    for i in range(1, n):
        cx = sx / cnt
        cy = sy / cnt
        if np.hypot(xs_mm[i] - cx, ys_mm[i] - cy) <= move_thresh_mm:
            sx += xs_mm[i]
            sy += ys_mm[i]
            cnt += 1
        else:
            if cnt >= min_len:
                segments.append((start, i - 1))
            start = i
            sx = xs_mm[i]
            sy = ys_mm[i]
            cnt = 1
    if cnt >= min_len:
        segments.append((start, n - 1))
    return segments


def jitter_metrics(rec: Recording, tracks: Optional[List[Track]] = None,
                   move_thresh_mm: float = 1.0, min_len: int = 8) -> JitterMetrics:
    tracks = tracks if tracks is not None else extract_tracks(rec)
    dev = rec.device
    out = JitterMetrics()
    has_mm = bool(dev.x_counts_per_mm and dev.y_counts_per_mm)
    for t in tracks:
        for (a, b) in _stationary_segments(t, dev, move_thresh_mm, min_len):
            xs = t.x[a:b + 1]
            ys = t.y[a:b + 1]
            rms_xc = float(np.std(xs))
            rms_yc = float(np.std(ys))
            p2p_xc = float(np.ptp(xs))
            p2p_yc = float(np.ptp(ys))
            # mean L2 (Euclidean) distance of each sample from the *initial*
            # point of contact (the first sample of the held segment).
            dist_from_init_c = float(np.mean(
                np.hypot(xs - xs[0], ys - ys[0])))
            cpm_x = dev.x_counts_per_mm
            cpm_y = dev.y_counts_per_mm
            rms_xm = rms_xc / cpm_x if cpm_x else None
            rms_ym = rms_yc / cpm_y if cpm_y else None
            rms_rad = (float(np.sqrt(rms_xm ** 2 + rms_ym ** 2))
                       if (rms_xm is not None and rms_ym is not None) else None)
            # distance uses an isotropic mm scale (mean of both axes) when the
            # per-axis counts/mm differ, so the L2 norm stays meaningful in mm.
            mean_dist_mm = None
            if cpm_x and cpm_y:
                cpm_mean = 0.5 * (cpm_x + cpm_y)
                mean_dist_mm = dist_from_init_c / cpm_mean
            res = JitterResult(
                contact_id=t.contact_id,
                n_samples=len(xs),
                rms_x_mm=rms_xm, rms_y_mm=rms_ym, rms_radial_mm=rms_rad,
                p2p_x_mm=(p2p_xc / cpm_x if cpm_x else None),
                p2p_y_mm=(p2p_yc / cpm_y if cpm_y else None),
                mean_dist_from_init_mm=mean_dist_mm,
                rms_x_counts=rms_xc, rms_y_counts=rms_yc,
                p2p_x_counts=p2p_xc, p2p_y_counts=p2p_yc,
                mean_dist_from_init_counts=dist_from_init_c,
            )
            out.per_segment.append(res)
    if out.per_segment and has_mm:
        out.worst_rms_radial_mm = max(
            (r.rms_radial_mm for r in out.per_segment if r.rms_radial_mm is not None),
            default=None)
        p2ps = []
        for r in out.per_segment:
            if r.p2p_x_mm is not None and r.p2p_y_mm is not None:
                p2ps.append(np.hypot(r.p2p_x_mm, r.p2p_y_mm))
        out.worst_p2p_mm = float(max(p2ps)) if p2ps else None
        out.worst_mean_dist_from_init_mm = max(
            (r.mean_dist_from_init_mm for r in out.per_segment
             if r.mean_dist_from_init_mm is not None),
            default=None)
    if not out.per_segment:
        out.note = ("No stationary segment found (need a finger held still for "
                    f">= {min_len} reports moving < {move_thresh_mm} mm).")
    elif not has_mm:
        out.note = "Jitter in counts only; supply physical size for mm values."
    return out


# --------------------------------------------------------------------------- #
# Linearity
# --------------------------------------------------------------------------- #
@dataclass
class LinearityResult:
    contact_id: int
    n_samples: int
    travel_mm: Optional[float]
    max_dev_mm: Optional[float]
    rms_dev_mm: Optional[float]
    max_dev_counts: float
    rms_dev_counts: float


@dataclass
class LinearityMetrics:
    per_segment: List[LinearityResult] = field(default_factory=list)
    worst_max_dev_mm: Optional[float] = None
    worst_rms_dev_mm: Optional[float] = None
    note: str = ""


def _line_deviations(xs: np.ndarray, ys: np.ndarray) -> Tuple[np.ndarray, float]:
    """Perpendicular distance of each point to the total-least-squares best-fit
    line (PCA principal axis). Returns (deviations, travel_length) in input units.
    """
    pts = np.column_stack([xs, ys])
    centroid = pts.mean(axis=0)
    centred = pts - centroid
    # principal direction via SVD
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    direction = vt[0]
    normal = np.array([-direction[1], direction[0]])
    dev = centred @ normal
    proj = centred @ direction
    travel = float(proj.max() - proj.min())
    return dev, travel


def _drag_segments(t: Track, dev: DeviceInfo,
                   min_travel_mm: float = 10.0, min_len: int = 10) -> List[Tuple[int, int]]:
    """Find long, mostly-monotonic movement segments suitable for linearity."""
    if len(t) < min_len:
        return []
    cpm_x = dev.x_counts_per_mm or 1.0
    cpm_y = dev.y_counts_per_mm or 1.0
    # whole-track travel; PTP linearity is usually one stroke per recording
    xs_mm = t.x / cpm_x
    ys_mm = t.y / cpm_y
    travel = float(np.hypot(xs_mm[-1] - xs_mm[0], ys_mm[-1] - ys_mm[0]))
    if travel >= min_travel_mm:
        return [(0, len(t) - 1)]
    return []


def linearity_metrics(rec: Recording, tracks: Optional[List[Track]] = None,
                      min_travel_mm: float = 10.0, min_len: int = 10) -> LinearityMetrics:
    tracks = tracks if tracks is not None else extract_tracks(rec)
    dev = rec.device
    out = LinearityMetrics()
    has_mm = bool(dev.x_counts_per_mm and dev.y_counts_per_mm)
    for t in tracks:
        for (a, b) in _drag_segments(t, dev, min_travel_mm, min_len):
            xs = t.x[a:b + 1]
            ys = t.y[a:b + 1]
            if has_mm:
                xs_u = xs / dev.x_counts_per_mm
                ys_u = ys / dev.y_counts_per_mm
                dev_arr, travel = _line_deviations(xs_u, ys_u)
                max_dev_mm = float(np.max(np.abs(dev_arr)))
                rms_dev_mm = float(np.sqrt(np.mean(dev_arr ** 2)))
                # also express in counts using average resolution
                avg_cpm = 0.5 * (dev.x_counts_per_mm + dev.y_counts_per_mm)
                max_dev_c = max_dev_mm * avg_cpm
                rms_dev_c = rms_dev_mm * avg_cpm
            else:
                dev_arr, travel = _line_deviations(xs, ys)
                max_dev_mm = rms_dev_mm = None
                max_dev_c = float(np.max(np.abs(dev_arr)))
                rms_dev_c = float(np.sqrt(np.mean(dev_arr ** 2)))
                travel = None
            out.per_segment.append(LinearityResult(
                contact_id=t.contact_id, n_samples=len(xs),
                travel_mm=travel if has_mm else None,
                max_dev_mm=max_dev_mm, rms_dev_mm=rms_dev_mm,
                max_dev_counts=max_dev_c, rms_dev_counts=rms_dev_c,
            ))
    if out.per_segment and has_mm:
        out.worst_max_dev_mm = max(r.max_dev_mm for r in out.per_segment if r.max_dev_mm is not None)
        out.worst_rms_dev_mm = max(r.rms_dev_mm for r in out.per_segment if r.rms_dev_mm is not None)
    if not out.per_segment:
        out.note = (f"No straight-line drag >= {min_travel_mm} mm found. "
                    "Swipe a single finger across the pad in one stroke.")
    elif not has_mm:
        out.note = "Linearity in counts only; supply physical size for mm values."
    return out


# --------------------------------------------------------------------------- #
# Scan time / frame timing
# --------------------------------------------------------------------------- #
@dataclass
class TimingMetrics:
    n_frames: int = 0
    source: str = ""           # "scan_time" or "host_time" or "none"
    mean_interval_ms: Optional[float] = None
    std_interval_ms: Optional[float] = None
    min_interval_ms: Optional[float] = None
    max_interval_ms: Optional[float] = None
    report_rate_hz: Optional[float] = None
    timing_jitter_ms: Optional[float] = None   # std dev of intervals
    intervals_ms: List[float] = field(default_factory=list)
    timestamps_ms: List[float] = field(default_factory=list)
    note: str = ""


def timing_metrics(rec: Recording) -> TimingMetrics:
    out = TimingMetrics(n_frames=len(rec.frames))
    if len(rec.frames) < 2:
        out.note = "Need >= 2 frames for timing."
        return out

    st = np.array([f.scan_time_us if f.scan_time is not None else np.nan
                   for f in rec.frames], dtype=float)
    ht = np.array([f.host_timestamp if f.host_timestamp is not None else np.nan
                   for f in rec.frames], dtype=float)

    times_ms: Optional[np.ndarray] = None
    if np.isfinite(st).sum() >= 2:
        unwrapped = _unwrap_scan_time(st, rec.device.scan_time_unit_us)
        times_ms = unwrapped / 1000.0
        out.source = "scan_time"
    elif np.isfinite(ht).sum() >= 2:
        times_ms = (ht - np.nanmin(ht)) * 1000.0
        out.source = "host_time"
    else:
        out.source = "none"
        out.note = "No scan-time or host-timestamp data; cannot derive timing."
        return out

    valid = np.isfinite(times_ms)
    tms = times_ms[valid]
    intervals = np.diff(tms)
    intervals = intervals[intervals > 0]
    if intervals.size == 0:
        out.note = "Timestamps present but non-increasing; cannot derive timing."
        return out

    out.intervals_ms = intervals.tolist()
    out.timestamps_ms = (tms - tms[0]).tolist()
    out.mean_interval_ms = float(np.mean(intervals))
    out.std_interval_ms = float(np.std(intervals))
    out.min_interval_ms = float(np.min(intervals))
    out.max_interval_ms = float(np.max(intervals))
    out.timing_jitter_ms = out.std_interval_ms
    if out.mean_interval_ms > 0:
        out.report_rate_hz = 1000.0 / out.mean_interval_ms
    return out


# --------------------------------------------------------------------------- #
# Contact timing structure
# --------------------------------------------------------------------------- #
@dataclass
class ContactLife:
    contact_id: int
    first_frame: int
    last_frame: int
    n_reports: int
    down_time_ms: Optional[float]
    up_time_ms: Optional[float]
    duration_ms: Optional[float]
    mean_report_interval_ms: Optional[float]


@dataclass
class ContactTimingMetrics:
    contacts: List[ContactLife] = field(default_factory=list)
    max_simultaneous: int = 0
    contact_count_series: List[int] = field(default_factory=list)
    note: str = ""


def _frame_times_ms(rec: Recording, timing: TimingMetrics) -> Dict[int, float]:
    """Return {frame_index: time_ms} using the same clock the timing chose."""
    out: Dict[int, float] = {}
    if timing.source == "none":
        return out
    if timing.source == "scan_time":
        st = np.array([f.scan_time_us if f.scan_time is not None else np.nan
                       for f in rec.frames], dtype=float)
        t_ms = _unwrap_scan_time(st, rec.device.scan_time_unit_us) / 1000.0
    else:  # host_time
        t_ms = np.array([f.host_timestamp * 1000.0 if f.host_timestamp is not None else np.nan
                         for f in rec.frames], dtype=float)
    if not np.isfinite(t_ms).any():
        return out
    base = np.nanmin(t_ms)
    for f, tt in zip(rec.frames, t_ms):
        if np.isfinite(tt):
            out[f.index] = float(tt - base)
    return out


def contact_timing_metrics(rec: Recording, timing: Optional[TimingMetrics] = None) -> ContactTimingMetrics:
    timing = timing or timing_metrics(rec)
    out = ContactTimingMetrics()
    frame_time_ms = _frame_times_ms(rec, timing)

    buckets: Dict[int, List[int]] = {}
    for f in rec.frames:
        out.contact_count_series.append(len(f.active_contacts))
        for c in f.active_contacts:
            buckets.setdefault(c.contact_id, []).append(f.index)
    out.max_simultaneous = max(out.contact_count_series) if out.contact_count_series else 0

    for cid, idxs in sorted(buckets.items()):
        idxs.sort()
        first, last = idxs[0], idxs[-1]
        dt = frame_time_ms.get(first)
        ut = frame_time_ms.get(last)
        dur = (ut - dt) if (dt is not None and ut is not None) else None
        mean_int = (dur / (len(idxs) - 1)) if (dur is not None and len(idxs) > 1) else None
        out.contacts.append(ContactLife(
            contact_id=cid, first_frame=first, last_frame=last,
            n_reports=len(idxs), down_time_ms=dt, up_time_ms=ut,
            duration_ms=dur, mean_report_interval_ms=mean_int,
        ))
    if not out.contacts:
        out.note = "No active contacts found."
    return out


# --------------------------------------------------------------------------- #
# Full report
# --------------------------------------------------------------------------- #
@dataclass
class MetricsReport:
    source: str
    device: dict
    resolution: ResolutionMetrics
    jitter: JitterMetrics
    linearity: LinearityMetrics
    timing: TimingMetrics
    contact_timing: ContactTimingMetrics

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "device": self.device,
            "resolution": asdict(self.resolution),
            "jitter": asdict(self.jitter),
            "linearity": asdict(self.linearity),
            "timing": {k: v for k, v in asdict(self.timing).items()
                       if k not in ("intervals_ms", "timestamps_ms")},
            "contact_timing": asdict(self.contact_timing),
        }


def compute_all(rec: Recording) -> MetricsReport:
    tracks = extract_tracks(rec)
    timing = timing_metrics(rec)
    return MetricsReport(
        source=rec.source,
        device={
            "name": rec.device.name,
            "x_logical": [rec.device.x_logical_min, rec.device.x_logical_max],
            "y_logical": [rec.device.y_logical_min, rec.device.y_logical_max],
            "width_mm": rec.device.width_mm,
            "height_mm": rec.device.height_mm,
            "x_counts_per_mm": rec.device.x_counts_per_mm,
            "y_counts_per_mm": rec.device.y_counts_per_mm,
            "max_contacts": rec.device.max_contacts,
        },
        resolution=resolution_metrics(rec, tracks),
        jitter=jitter_metrics(rec, tracks),
        linearity=linearity_metrics(rec, tracks),
        timing=timing,
        contact_timing=contact_timing_metrics(rec, timing),
    )
