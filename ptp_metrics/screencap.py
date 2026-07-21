"""Screen recording of the live visualization window (video + on-screen stats).

Grabs the app window's screen region on a fixed cadence in a background thread
and encodes it to an ``.mp4`` (OpenCV ``mp4v`` codec — no external ffmpeg needed).
Because the whole window is captured, the recording includes the touchpad trace,
the strip charts *and* the metrics / spec-check panel exactly as shown.

Design mirrors :class:`ptp_metrics.logger.StreamLogger`: a lightweight object you
``start()`` and ``stop()``; the heavy work runs off the UI thread so the render
loop never stalls.

Dependencies (both ship as small wheels and bundle cleanly with PyInstaller):
  * Pillow — ``PIL.ImageGrab`` for the screen grab (already present via matplotlib).
  * OpenCV — ``cv2.VideoWriter`` for the encoder.

The process is intentionally left DPI-*unaware* (the default for the packaged
app), so Tk window coordinates and the grabbed screen pixels share the same
virtualized 96-dpi space and line up without extra scaling math.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Tuple

# bbox is (x, y, width, height) in screen pixels
BBox = Tuple[int, int, int, int]


def _make_pil_grabber() -> Callable[[BBox], "object"]:
    """Return ``grab(bbox) -> HxWx3 BGR uint8 ndarray`` using Pillow + numpy."""
    from PIL import ImageGrab
    import numpy as np

    def grab(bbox: BBox):
        x, y, w, h = bbox
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        arr = np.asarray(img)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return None
        # RGB(A) -> BGR for OpenCV
        return arr[:, :, 2::-1].copy()

    return grab


class ScreenRecorder:
    """Capture a window region to an MP4 on a background thread.

    Parameters
    ----------
    path:
        Output ``.mp4`` file.
    bbox_fn:
        Callable returning the current ``(x, y, w, h)`` window rectangle in
        screen pixels. Queried every frame so the capture follows the window if
        it is moved; the frame *size* is locked at the first grab.
    fps:
        Target capture rate. 15 fps keeps files small while staying smooth.
    grab_fn:
        Optional custom grabber (mainly for tests); defaults to the Pillow one.
    """

    def __init__(self, path: str, bbox_fn: Callable[[], BBox], fps: int = 15,
                 grab_fn: Optional[Callable[[BBox], "object"]] = None):
        self.path = path
        self.bbox_fn = bbox_fn
        self.fps = max(1, int(fps))
        self._grab = grab_fn
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._writer = None
        self._size: Optional[Tuple[int, int]] = None
        self.frames_written = 0
        self.error: Optional[Exception] = None
        self._t0 = 0.0
        self._t_end = 0.0

    # ------------------------------------------------------------------ api
    def start(self):
        # Validate dependencies up front so failures surface immediately.
        import cv2  # noqa: F401  (raises ImportError with a clear message)
        if self._grab is None:
            self._grab = _make_pil_grabber()
        self._running = True
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="PTPScreenRec")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._release()
        self._t_end = time.perf_counter()

    @property
    def duration_s(self) -> float:
        end = self._t_end or time.perf_counter()
        return max(0.0, end - self._t0) if self._t0 else 0.0

    # ------------------------------------------------------------------ internals
    def _run(self):
        import cv2
        period = 1.0 / self.fps
        next_t = time.perf_counter()
        try:
            while self._running:
                bbox = self._normalize(self.bbox_fn())
                if bbox is not None:
                    frame = self._grab(bbox)
                    if frame is not None and frame.size:
                        self._write(cv2, frame)
                next_t += period
                slack = next_t - time.perf_counter()
                if slack > 0:
                    time.sleep(slack)
                else:
                    next_t = time.perf_counter()  # fell behind; resync
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            self._release()

    def _write(self, cv2, frame):
        h, w = frame.shape[:2]
        if self._writer is None:
            # lock to even dimensions (mp4v requires them)
            w -= w % 2
            h -= h % 2
            if w < 2 or h < 2:
                return
            self._size = (w, h)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(self.path, fourcc, float(self.fps), (w, h))
            if not self._writer.isOpened():
                raise RuntimeError("Could not open video writer (codec unavailable).")
        tw, th = self._size
        if (w, h) != (frame.shape[1], frame.shape[0]) or (frame.shape[1], frame.shape[0]) != (tw, th):
            frame = cv2.resize(frame, (tw, th))
        self._writer.write(frame)
        self.frames_written += 1

    def _release(self):
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception:
                pass
            self._writer = None

    @staticmethod
    def _normalize(bbox: Optional[BBox]) -> Optional[BBox]:
        if not bbox:
            return None
        x, y, w, h = (int(round(v)) for v in bbox)
        if w <= 1 or h <= 1:
            return None
        return (x, y, w, h)
