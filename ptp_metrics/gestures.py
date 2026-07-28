"""Gesture recognition grounded in the HID / PTP report and OS (hidclass) events.

This does **not** re-invent gesture recognition from raw geometry. Instead it
reads the fields the Windows Precision Touchpad stack actually reports:

  * **Finger count** comes from the HID **Contact Count** usage (0x0D:0x54) that
    the device puts in every PTP report — the same value ``hidclass.sys`` / the
    PTP driver use. We take the max reported contact count across the recent
    window as the number of fingers, never a geometric guess.
  * **Button / click** comes from the HID **Button** field in the report.
  * **Two-finger scroll** is what the OS synthesizes from a PTP two-finger drag:
    ``hidclass`` -> the PTP driver emits mouse **wheel / hwheel** events. Live
    capture picks those up from Raw Input (``RIM_TYPEMOUSE`` + ``RI_MOUSE_WHEEL``
    / ``RI_MOUSE_HWHEEL``) and passes them here, so scroll is reported straight
    from the OS, not inferred.
  * **Palm rejection** compares the PTP contact against the OS **cursor motion**
    (also from Raw Input). A palm generates PTP reports; if the OS cursor does
    **not** move, the stack rejected it (success). If the cursor **does** move
    while a palm is in contact, rejection failed. The palm itself is identified
    from the HID **Confidence** bit (0 = device flags a non-finger contact) or an
    unusually large contact footprint (Width/Height).

Motion **direction** for swipes still uses the contact positions from the report
(there is no HID field for "swipe direction"), but the *classification* (how
many fingers, button, scroll, palm) is taken from the report / OS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# A wheel event as seen from OS raw input: (timestamp_s, dx, dy) where dy>0 is a
# scroll-down notch and dx>0 is scroll-right (in wheel-delta units / 120).
WheelEvent = Tuple[float, float, float]
# A cursor-move event from OS raw input: (timestamp_s, dx, dy) in mouse units.
CursorEvent = Tuple[float, float, float]


@dataclass
class GestureResult:
    name: str = "—"
    detail: str = ""
    n_fingers: int = 0
    source: str = ""      # classification source: "HID", "OS wheel", "HID+OS", ...


# thresholds
_MOVE_MM = 3.0          # min net travel (mm) to count a contact as "moving"
_TAP_MAX_MM = 2.0       # max travel for a tap/hold
_PINCH_MM = 4.0         # min spread change (mm) to call it a pinch
_HOLD_S = 0.35          # min contact time to call a stationary touch a "hold"
_PALM_MM = 13.0         # contact width/height (mm) above which it's palm-like
_CURSOR_MOVE_PX = 4.0   # OS cursor travel (px) that counts as "the cursor moved"


def _dir_name(dx: float, dy: float) -> str:
    """Cardinal direction of a motion vector. +y is downward on the pad."""
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def _is_palm(c, cpm_x: Optional[float], cpm_y: Optional[float]) -> bool:
    """A contact is palm-like if the device cleared its Confidence bit (it thinks
    the contact is not an intentional finger) or the footprint is large."""
    if not getattr(c, "confidence", True):
        return True
    if cpm_x and cpm_y:
        w_mm = (c.width or 0.0) / cpm_x
        h_mm = (c.height or 0.0) / cpm_y
        if w_mm >= _PALM_MM or h_mm >= _PALM_MM:
            return True
    return False


def _palm_state(frames, device) -> Tuple[bool, bool]:
    """Return (palm_seen, finger_seen) across the window's active contacts."""
    cpm_x = device.x_counts_per_mm if device and device.x_counts_per_mm else None
    cpm_y = device.y_counts_per_mm if device and device.y_counts_per_mm else None
    palm = finger = False
    for f in frames:
        for c in f.active_contacts:
            if _is_palm(c, cpm_x, cpm_y):
                palm = True
            else:
                finger = True
    return palm, finger


def _cursor_travel(cursor_events: Optional[Sequence[CursorEvent]],
                   t_last: Optional[float], window_s: float) -> float:
    """Total OS cursor travel (px) within the window."""
    if not cursor_events:
        return 0.0
    total = 0.0
    for (t, dx, dy) in cursor_events:
        if t_last is None or t is None or (t_last - t) <= window_s:
            total += float(np.hypot(dx, dy))
    return total


def _reported_finger_count(frames) -> int:
    """Authoritative finger count from the HID Contact Count field.

    Falls back to the number of tip-down contacts only if the report did not
    carry a Contact Count value.
    """
    best = 0
    saw_field = False
    for f in frames:
        cc = getattr(f, "contact_count", None)
        if cc is not None:
            saw_field = True
            best = max(best, int(cc))
    if saw_field:
        return best
    for f in frames:
        best = max(best, len(f.active_contacts))
    return best


def _recent_wheel(wheel_events: Optional[Sequence[WheelEvent]],
                  t_last: Optional[float], window_s: float
                  ) -> Tuple[float, float]:
    """Sum of (dx, dy) wheel deltas within the window. (0,0) if none."""
    if not wheel_events:
        return (0.0, 0.0)
    sx = sy = 0.0
    for (t, dx, dy) in wheel_events:
        if t_last is None or t is None or (t_last - t) <= window_s:
            sx += dx
            sy += dy
    return (sx, sy)


def recognize(frames, device, window_s: float = 1.2, time_fn=None,
              wheel_events: Optional[Sequence[WheelEvent]] = None,
              cursor_events: Optional[Sequence[CursorEvent]] = None) -> GestureResult:
    """Classify the gesture in the most recent ``window_s`` seconds of ``frames``.

    ``wheel_events`` are OS-synthesized mouse wheel notches captured from Raw
    Input (the OS's own recognition of a two-finger scroll). ``cursor_events``
    are OS cursor-move deltas from Raw Input, used to judge palm rejection.
    ``time_fn(frame)`` supplies timestamps for the window / hold heuristics.
    """
    if not frames:
        return GestureResult()

    t_last = time_fn(frames[-1]) if time_fn is not None else None
    if time_fn is not None and t_last is not None:
        cut = t_last - window_s
        frames = [f for f in frames
                  if (time_fn(f) is None or time_fn(f) >= cut)]
    if not frames:
        return GestureResult()

    # --- palm rejection: PTP contact vs OS cursor motion --------------------
    palm_seen, finger_seen = _palm_state(frames, device)
    if palm_seen and not finger_seen:
        n_palm = _reported_finger_count(frames)
        if cursor_events is None:
            # no OS cursor data (e.g. offline recording): report detection only
            return GestureResult("Palm detected",
                                 "low-confidence / large contact", n_palm,
                                 source="HID")
        moved = _cursor_travel(cursor_events, t_last, window_s)
        if moved >= _CURSOR_MOVE_PX:
            return GestureResult("Palm NOT rejected",
                                 f"palm contact moved cursor {moved:.0f}px",
                                 n_palm, source="HID+OS")
        return GestureResult("Palm rejected",
                             "palm contact, cursor still", n_palm,
                             source="HID+OS")

    # --- OS-synthesized scroll (hidclass -> PTP driver -> wheel) -------------
    wx, wy = _recent_wheel(wheel_events, t_last, window_s)
    if abs(wx) >= 1.0 or abs(wy) >= 1.0:
        return GestureResult(f"Two-finger scroll {_dir_name(wx, wy)}",
                             "OS wheel", 2, source="OS wheel")

    # --- finger count straight from the HID Contact Count field --------------
    n = _reported_finger_count(frames)
    if n == 0:
        return GestureResult()

    button_down = any(bool(getattr(f, "button", False)) for f in frames)

    cpm_x = (device.x_counts_per_mm if device and device.x_counts_per_mm else 1.0)
    cpm_y = (device.y_counts_per_mm if device and device.y_counts_per_mm else 1.0)

    # position tracks — used only for motion magnitude/direction; the finger
    # *count* above is from HID Contact Count.
    tracks = {}
    for f in frames:
        for c in f.active_contacts:
            tracks.setdefault(c.contact_id, []).append((c.x / cpm_x, c.y / cpm_y))

    disp: List[Tuple[float, float]] = []
    travels: List[float] = []
    for pts in tracks.values():
        if len(pts) < 2:
            disp.append((0.0, 0.0))
            travels.append(0.0)
            continue
        d = (pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
        disp.append(d)
        travels.append(float(np.hypot(*d)))

    any_move = any(t >= _MOVE_MM for t in travels)

    dur = None
    if time_fn is not None:
        ts = [time_fn(f) for f in frames if time_fn(f) is not None]
        if len(ts) >= 2:
            dur = ts[-1] - ts[0]

    # click from the HID button field
    if button_down and not any_move:
        return GestureResult("Click" if n <= 1 else f"{n}-finger click",
                             "HID button", n, source="HID")

    # stationary: tap / hold (finger count from HID)
    if not any_move and (max(travels, default=0.0) <= _TAP_MAX_MM):
        if dur is not None and dur >= _HOLD_S:
            label = "Press & hold" if n == 1 else f"{n}-finger hold"
            return GestureResult(label, f"{dur:.2f}s stationary (HID count {n})",
                                 n, source="HID")
        label = "Tap" if n == 1 else f"{n}-finger tap"
        return GestureResult(label, f"HID contact count {n}", n, source="HID")

    # two-finger (from HID): pinch vs pan
    if n == 2:
        pts_list = [p for p in tracks.values() if len(p) >= 2]
        if len(pts_list) >= 2:
            a, b = pts_list[0], pts_list[1]
            spread0 = float(np.hypot(a[0][0] - b[0][0], a[0][1] - b[0][1]))
            spread1 = float(np.hypot(a[-1][0] - b[-1][0], a[-1][1] - b[-1][1]))
            dspread = spread1 - spread0
            if abs(dspread) >= _PINCH_MM:
                if dspread > 0:
                    return GestureResult("Pinch zoom in",
                                         f"+{dspread:.1f} mm (HID count 2)", 2,
                                         source="HID")
                return GestureResult("Pinch zoom out",
                                     f"{dspread:.1f} mm (HID count 2)", 2,
                                     source="HID")
        mx = float(np.mean([d[0] for d in disp])) if disp else 0.0
        my = float(np.mean([d[1] for d in disp])) if disp else 0.0
        if np.hypot(mx, my) >= _MOVE_MM:
            return GestureResult(f"Two-finger pan {_dir_name(mx, my)}",
                                 f"{np.hypot(mx, my):.1f} mm (HID count 2)", 2,
                                 source="HID")
        return GestureResult("Two-finger", "HID contact count 2", 2, source="HID")

    # 1 / 3 / 4-finger swipe (count from HID, direction from positions)
    if any_move:
        mx = float(np.mean([d[0] for d in disp])) if disp else 0.0
        my = float(np.mean([d[1] for d in disp])) if disp else 0.0
        dist = float(np.hypot(mx, my))
        if n == 1:
            return GestureResult(f"Swipe {_dir_name(mx, my)}",
                                 f"{dist:.1f} mm (HID count 1)", 1, source="HID")
        return GestureResult(f"{n}-finger swipe {_dir_name(mx, my)}",
                             f"{dist:.1f} mm (HID count {n})", n, source="HID")

    return GestureResult(f"{n} contact(s)", f"HID contact count {n}", n, source="HID")
