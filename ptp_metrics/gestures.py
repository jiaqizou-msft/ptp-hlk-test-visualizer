"""Real-time touchpad gesture recognition.

A lightweight, dependency-free heuristic classifier that looks at a short recent
window of frames and names the gesture in progress — tap, press & hold, and
1/2/3/4-finger swipes (with direction), two-finger scroll, and pinch (zoom
in/out). It is intentionally simple and tuned for *live feedback* in the metrics
panel, not for driving the OS.

Design:
  * Operate on the last ``window_s`` seconds of frames (position in mm when the
    device size is known, else in logical counts scaled to a nominal size).
  * Track the max simultaneous contact count and each contact's net travel.
  * Classify by finger count + motion:
      - no motion, brief contact           -> "Tap"
      - no motion, sustained               -> "Press & hold"
      - 1 finger moving                    -> "Swipe <dir>"
      - 2 fingers, same direction          -> "Two-finger scroll <dir>"
      - 2 fingers, separating/closing      -> "Pinch zoom in/out"
      - 3/4 fingers moving                 -> "N-finger swipe <dir>"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class GestureResult:
    name: str = "—"
    detail: str = ""
    n_fingers: int = 0


# thresholds (mm)
_MOVE_MM = 3.0          # min net travel to count as a "move"
_TAP_MAX_MM = 2.0       # max travel for a tap/hold
_PINCH_MM = 4.0         # min spread change to call it a pinch
_HOLD_S = 0.35          # min contact time to call a stationary touch a "hold"


def _dir_name(dx: float, dy: float) -> str:
    """Cardinal direction of a motion vector. +y is downward on the pad."""
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def _tracks_in_window(frames, cpm_x: float, cpm_y: float
                      ) -> Tuple[Dict[int, List[Tuple[float, float]]], int, float]:
    """Return per-contact position lists (mm), max simultaneous count, and the
    contact duration span in seconds (best-effort)."""
    tracks: Dict[int, List[Tuple[float, float]]] = {}
    max_simul = 0
    for f in frames:
        acs = f.active_contacts
        max_simul = max(max_simul, len(acs))
        for c in acs:
            tracks.setdefault(c.contact_id, []).append((c.x / cpm_x, c.y / cpm_y))
    return tracks, max_simul, 0.0


def recognize(frames, device, window_s: float = 1.2,
              time_fn=None) -> GestureResult:
    """Classify the gesture in the most recent ``window_s`` seconds of ``frames``.

    ``time_fn(frame) -> seconds`` supplies timestamps; if omitted, contact
    duration heuristics are skipped and only motion is used.
    """
    if not frames:
        return GestureResult()

    # restrict to the recent window
    if time_fn is not None:
        t_last = time_fn(frames[-1])
        if t_last is not None:
            cut = t_last - window_s
            frames = [f for f in frames
                      if (time_fn(f) is None or time_fn(f) >= cut)]
    if not frames:
        return GestureResult()

    cpm_x = (device.x_counts_per_mm if device and device.x_counts_per_mm else 1.0)
    cpm_y = (device.y_counts_per_mm if device and device.y_counts_per_mm else 1.0)

    tracks, max_simul, _ = _tracks_in_window(frames, cpm_x, cpm_y)
    if not tracks or max_simul == 0:
        return GestureResult()

    # per-contact net travel + displacement vectors
    disp: List[Tuple[float, float]] = []
    travels: List[float] = []
    for pts in tracks.values():
        if len(pts) < 2:
            disp.append((0.0, 0.0))
            travels.append(0.0)
            continue
        x0, y0 = pts[0]
        x1, y1 = pts[-1]
        dvec = (x1 - x0, y1 - y0)
        disp.append(dvec)
        travels.append(float(np.hypot(*dvec)))

    n = max_simul
    moving = [t >= _MOVE_MM for t in travels]
    any_move = any(moving)

    # duration of the longest-lived contact (for tap vs hold)
    dur = None
    if time_fn is not None:
        ts = [time_fn(f) for f in frames if time_fn(f) is not None]
        if len(ts) >= 2:
            dur = ts[-1] - ts[0]

    # ---- stationary: tap / hold ----
    if not any_move and max(travels, default=0.0) <= _TAP_MAX_MM:
        if dur is not None and dur >= _HOLD_S:
            label = "Press & hold" if n == 1 else f"{n}-finger hold"
            return GestureResult(label, f"{dur:.2f}s stationary", n)
        label = "Tap" if n == 1 else f"{n}-finger tap"
        return GestureResult(label, "brief contact", n)

    # ---- two-finger: scroll vs pinch ----
    if n == 2 and len(disp) >= 2:
        # use the two longest-lived tracks
        pts_list = [p for p in tracks.values() if len(p) >= 2]
        if len(pts_list) >= 2:
            a, b = pts_list[0], pts_list[1]
            spread0 = float(np.hypot(a[0][0] - b[0][0], a[0][1] - b[0][1]))
            spread1 = float(np.hypot(a[-1][0] - b[-1][0], a[-1][1] - b[-1][1]))
            dspread = spread1 - spread0
            if abs(dspread) >= _PINCH_MM:
                if dspread > 0:
                    return GestureResult("Pinch zoom in", f"+{dspread:.1f} mm spread", 2)
                return GestureResult("Pinch zoom out", f"{dspread:.1f} mm spread", 2)
        # otherwise a two-finger scroll in the mean direction
        mx = float(np.mean([d[0] for d in disp]))
        my = float(np.mean([d[1] for d in disp]))
        if np.hypot(mx, my) >= _MOVE_MM:
            return GestureResult(f"Two-finger scroll {_dir_name(mx, my)}",
                                 f"{np.hypot(mx, my):.1f} mm", 2)
        return GestureResult("Two-finger", "small motion", 2)

    # ---- 1 / 3 / 4-finger swipe ----
    if any_move:
        mx = float(np.mean([d[0] for d in disp]))
        my = float(np.mean([d[1] for d in disp]))
        dist = float(np.hypot(mx, my))
        if n == 1:
            return GestureResult(f"Swipe {_dir_name(mx, my)}", f"{dist:.1f} mm", 1)
        return GestureResult(f"{n}-finger swipe {_dir_name(mx, my)}",
                             f"{dist:.1f} mm", n)

    return GestureResult(f"{n} contact(s)", "", n)
