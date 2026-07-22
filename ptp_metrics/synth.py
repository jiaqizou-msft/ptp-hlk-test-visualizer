"""Synthetic PTP recording generator.

Produces realistic :class:`Recording` data so the whole pipeline (metrics +
visualization) can be exercised without touchpad hardware, and so the metrics
can be validated against known ground-truth (a known jitter / linearity error /
resolution will be recovered by the engine).

A typical synthetic gesture sequence:
    1. finger DOWN, held still       -> exercises jitter
    2. straight-line drag            -> exercises linearity
    3. finger UP
"""

from __future__ import annotations

import numpy as np

from .models import Contact, DeviceInfo, Frame, Recording, SCAN_TIME_UNIT_US


def default_device() -> DeviceInfo:
    """A plausible Precision Touchpad: ~105 x 65 mm, ~1000 counts/cm."""
    return DeviceInfo(
        name="Synthetic PTP (105x65 mm)",
        x_logical_min=0, x_logical_max=10500,
        y_logical_min=0, y_logical_max=6500,
        x_physical_mm=105.0, y_physical_mm=65.0,
        max_contacts=5,
        scan_time_unit_us=SCAN_TIME_UNIT_US,
    )


def _quantize(value: np.ndarray, step: float) -> np.ndarray:
    if step <= 0:
        return value
    return np.round(value / step) * step


def synth_recording(
    device: DeviceInfo | None = None,
    report_rate_hz: float = 133.0,
    timing_jitter_ms: float = 0.4,
    stationary_ms: float = 600.0,
    drag_ms: float = 900.0,
    jitter_mm: float = 0.08,
    linearity_error_mm: float = 0.15,
    resolution_step_counts: float = 4.0,
    drag_len_mm: float = 60.0,
    drag_angle_deg: float = 20.0,
    seed: int = 7,
    drop_drag_frames: tuple[int, ...] = (),
) -> Recording:
    """Build a synthetic down/hold/drag/up recording with known properties.

    The injected ``jitter_mm`` (RMS) and ``linearity_error_mm`` (sinusoidal bow)
    are what the metrics engine should approximately recover.

    ``drop_drag_frames`` lists drag-sample positions (0-based within the drag)
    whose report is *omitted* while the finger keeps moving — simulating a
    spotty-contact dropout. The global frame index still advances across the
    omission, so the contact's track shows a gap the continuity metric detects.
    """
    rng = np.random.default_rng(seed)
    dev = device or default_device()
    cpm_x = dev.x_counts_per_mm or 100.0
    cpm_y = dev.y_counts_per_mm or 100.0
    drop_set = set(drop_drag_frames)

    dt_ms = 1000.0 / report_rate_hz
    n_still = max(2, int(stationary_ms / dt_ms))
    n_drag = max(2, int(drag_ms / dt_ms))

    # start near pad centre (mm)
    cx_mm, cy_mm = 30.0, 30.0

    frames: list[Frame] = []
    scan_t_us = 0.0
    idx = 0
    cid = 1

    def push(x_mm, y_mm):
        nonlocal scan_t_us, idx
        # quantise to device resolution then add measurement jitter
        x_c = _quantize(np.array(x_mm * cpm_x), resolution_step_counts)
        y_c = _quantize(np.array(y_mm * cpm_y), resolution_step_counts)
        st_counts = round(scan_t_us / dev.scan_time_unit_us) % 65536
        frames.append(Frame(
            index=idx,
            scan_time=float(st_counts),
            contact_count=1,
            contacts=[Contact(contact_id=cid, x=float(x_c), y=float(y_c),
                              tip=True, confidence=True,
                              width=120.0, height=120.0)],
        ))
        idx += 1
        interval = dt_ms + rng.normal(0.0, timing_jitter_ms)
        scan_t_us += max(0.1, interval) * 1000.0

    # 1) stationary hold (jitter)
    for _ in range(n_still):
        jx = rng.normal(0.0, jitter_mm)
        jy = rng.normal(0.0, jitter_mm)
        push(cx_mm + jx, cy_mm + jy)

    # 2) straight-line drag with a sinusoidal bow (linearity error) + jitter
    ang = np.deg2rad(drag_angle_deg)
    ux, uy = np.cos(ang), np.sin(ang)        # travel direction
    nx, ny = -np.sin(ang), np.cos(ang)       # perpendicular
    for k in range(n_drag):
        s = drag_len_mm * k / (n_drag - 1)            # along-line distance
        bow = linearity_error_mm * np.sin(np.pi * k / (n_drag - 1))  # perpendicular bow
        jx = rng.normal(0.0, jitter_mm)
        jy = rng.normal(0.0, jitter_mm)
        x_mm = cx_mm + s * ux + bow * nx + jx
        y_mm = cy_mm + s * uy + bow * ny + jy
        if k in drop_set:
            # dropped report: advance the frame index + clock, emit nothing
            idx += 1
            scan_t_us += max(0.1, dt_ms + rng.normal(0.0, timing_jitter_ms)) * 1000.0
            continue
        push(x_mm, y_mm)

    rec = Recording(device=dev, frames=frames, source="synthetic")
    return rec
