"""Map computed metrics onto the Microsoft Precision Touchpad spec thresholds.

These thresholds are taken from the Windows Hardware "Touchpad Tests" component
guidelines (himetric units: 1 mm = 100 himetric):

  * Stationary Jitter   : stationary contact must not move > 0.5 mm.
  * Linearity           : path deviation from a straight line <= 0.5 mm;
                          no backward travel; no duplicate packets while moving.
  * Input Resolution    : reported resolution >= 300 DPI; single-contact report
                          rate >= 125 Hz; per-report positional delta <= 0.5 mm.
  * Ghost Reporting     : zero contacts during idle.

This produces *informational* PASS/FAIL/UNKNOWN status for partner tuning — it is
not an official certification verdict (those require the PT3 rig + PTLogo).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .models import Recording
from . import metrics as M

# Spec thresholds
JITTER_MAX_MM = 0.5
LINEARITY_MAX_MM = 0.5
RESOLUTION_MIN_DPI = 300.0
REPORT_RATE_MIN_HZ = 125.0
POS_DELTA_MAX_MM = 0.5

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


@dataclass
class Check:
    name: str
    status: str           # PASS / FAIL / UNKNOWN
    value: Optional[float]
    limit: str
    detail: str = ""


@dataclass
class SpecEvaluation:
    checks: List[Check] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        if all(c.status == PASS for c in self.checks) and self.checks:
            return PASS
        return UNKNOWN


def _max_positional_delta_mm(rec: Recording, tracks) -> Optional[float]:
    """Largest jump between consecutive reports of one contact (resolution test)."""
    dev = rec.device
    if not (dev.x_counts_per_mm and dev.y_counts_per_mm):
        return None
    worst = 0.0
    found = False
    for t in tracks:
        for sx, sy in M.iter_segments(t):
            if len(sx) < 2:
                continue
            dx = np.diff(sx) / dev.x_counts_per_mm
            dy = np.diff(sy) / dev.y_counts_per_mm
            d = np.hypot(dx, dy)
            if d.size:
                worst = max(worst, float(d.max()))
                found = True
    return worst if found else None


def evaluate(rec: Recording, report: Optional[M.MetricsReport] = None) -> SpecEvaluation:
    report = report or M.compute_all(rec)
    tracks = M.extract_tracks(rec)
    ev = SpecEvaluation()

    # Resolution >= 300 DPI
    dpi = None
    r = report.resolution
    if r.reported_x_dpi and r.reported_y_dpi:
        dpi = min(r.reported_x_dpi, r.reported_y_dpi)
    ev.checks.append(Check(
        "Input Resolution", _ge(dpi, RESOLUTION_MIN_DPI), dpi,
        f">= {RESOLUTION_MIN_DPI:.0f} DPI",
        f"{dpi:.0f} DPI" if dpi else "device physical size unknown"))

    # Report rate >= 125 Hz
    rate = report.timing.report_rate_hz
    ev.checks.append(Check(
        "Report Rate", _ge(rate, REPORT_RATE_MIN_HZ), rate,
        f">= {REPORT_RATE_MIN_HZ:.0f} Hz",
        f"{rate:.1f} Hz" if rate else "no timing data"))

    # Stationary jitter <= 0.5 mm (use peak-to-peak displacement)
    j = report.jitter
    jit = j.worst_p2p_mm if j.worst_p2p_mm is not None else None
    ev.checks.append(Check(
        "Stationary Jitter", _le(jit, JITTER_MAX_MM), jit,
        f"<= {JITTER_MAX_MM} mm",
        f"{jit:.3f} mm peak-to-peak" if jit is not None
        else (j.note or "hold a finger still to measure")))

    # Linearity <= 0.5 mm
    lin = report.linearity
    ld = lin.worst_max_dev_mm
    ev.checks.append(Check(
        "Linearity", _le(ld, LINEARITY_MAX_MM), ld,
        f"<= {LINEARITY_MAX_MM} mm",
        f"{ld:.3f} mm max deviation" if ld is not None
        else (lin.note or "swipe a straight line to measure")))

    # Per-report positional delta <= 0.5 mm
    pd = _max_positional_delta_mm(rec, tracks)
    ev.checks.append(Check(
        "Positional Delta", _le(pd, POS_DELTA_MAX_MM), pd,
        f"<= {POS_DELTA_MAX_MM} mm/report",
        f"{pd:.3f} mm max jump" if pd is not None else "no motion / size unknown"))

    return ev


def _ge(v, limit) -> str:
    if v is None:
        return UNKNOWN
    return PASS if v >= limit else FAIL


def _le(v, limit) -> str:
    if v is None:
        return UNKNOWN
    return PASS if v <= limit else FAIL
