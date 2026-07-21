"""Screen recording of the live visualization window (video + on-screen stats).

Grabs the app window's screen region on a fixed cadence in a background thread
and encodes it to an ``.mp4`` (OpenCV ``mp4v`` codec — no external ffmpeg needed).
Because the whole window is captured, the recording includes the touchpad trace,
the strip charts *and* the metrics / spec-check panel exactly as shown.

Design mirrors :class:`ptp_metrics.logger.StreamLogger`: a lightweight object you
``start()`` and ``stop()``; the heavy work runs off the UI thread so the render
loop never stalls.

DPI handling
------------
On a high-DPI display with Windows display scaling (e.g. 150%), Tk's ``winfo_*``
geometry is reported in *logical* (scaled) pixels, while ``PIL.ImageGrab``
captures the desktop in *physical* pixels. Passing the logical rectangle
straight to ImageGrab therefore grabs only the top-left fraction of the window.
To stay correct on any scaling factor without altering the app's appearance, the
recorder *calibrates* once at start: it grabs the full screen (physical) and
compares its width to Tk's reported logical screen width. The resulting scale
converts the logical window rectangle into the physical pixels ImageGrab expects.

Dependencies (both ship as small wheels and bundle cleanly with PyInstaller):
  * Pillow — ``PIL.ImageGrab`` for the screen grab (already present via matplotlib).
  * OpenCV — ``cv2.VideoWriter`` for the encoder.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Tuple

# bbox is (x, y, width, height) in *logical* Tk pixels
BBox = Tuple[int, int, int, int]
# logical screen size (width, height) in Tk pixels
Size = Tuple[int, int]


def _make_pil_grabber() -> Callable[[Tuple[int, int, int, int]], "object"]:
    """Return ``grab(phys_bbox) -> HxWx3 BGR uint8 ndarray`` using Pillow + numpy.

    ``phys_bbox`` is ``(x, y, w, h)`` in *physical* screen pixels.
    """
    from PIL import ImageGrab
    import numpy as np

    def grab(phys_bbox):
        x, y, w, h = phys_bbox
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        arr = np.asarray(img)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return None
        # RGB(A) -> BGR for OpenCV
        return arr[:, :, 2::-1].copy()

    return grab


def _full_screen_physical_size() -> Optional[Tuple[int, int]]:
    """Physical pixel size of the whole (virtual) desktop via ImageGrab."""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        return img.width, img.height
    except Exception:
        return None



class ScreenRecorder:
    """Capture a window region to an MP4 on a background thread.

    Parameters
    ----------
    path:
        Output ``.mp4`` file.
    bbox_fn:
        Callable returning the current ``(x, y, w, h)`` window rectangle in
        *logical* Tk pixels. Queried every frame so the capture follows the
        window if it is moved; the frame *size* is locked at the first grab.
    fps:
        Target capture rate. 15 fps keeps files small while staying smooth.
    screen_size_fn:
        Optional callable returning Tk's *logical* screen size ``(w, h)`` (i.e.
        ``winfo_screenwidth()/winfo_screenheight()``). Used to calibrate the
        logical→physical scale so the full window is captured under Windows
        display scaling. If omitted, a scale of 1.0 is assumed.
    grab_fn:
        Optional custom grabber taking a *physical* bbox (mainly for tests);
        defaults to the Pillow one.
    """

    def __init__(self, path: str, bbox_fn: Callable[[], BBox], fps: int = 15,
                 screen_size_fn: Optional[Callable[[], Size]] = None,
                 grab_fn: Optional[Callable[[Tuple[int, int, int, int]], "object"]] = None):
        self.path = path
        self.bbox_fn = bbox_fn
        self.fps = max(1, int(fps))
        self.screen_size_fn = screen_size_fn
        self._grab = grab_fn
        self._scale = 1.0
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
        self._scale = self._calibrate_scale()
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

    @property
    def scale(self) -> float:
        return self._scale

    # ------------------------------------------------------------------ internals
    def _calibrate_scale(self) -> float:
        """Ratio of physical screen pixels to Tk logical pixels (>= 1.0)."""
        if self.screen_size_fn is None:
            return 1.0
        try:
            logical = self.screen_size_fn()
        except Exception:
            return 1.0
        if not logical or not logical[0]:
            return 1.0
        phys = _full_screen_physical_size()
        if not phys or not phys[0]:
            return 1.0
        scale = phys[0] / float(logical[0])
        # guard against nonsense; scaling is always >= 1.0 in practice
        if scale < 0.5 or scale > 8.0:
            return 1.0
        return scale

    def _to_physical(self, bbox: Optional[BBox]) -> Optional[Tuple[int, int, int, int]]:
        if not bbox:
            return None
        x, y, w, h = bbox
        s = self._scale
        px = int(round(x * s))
        py = int(round(y * s))
        pw = int(round(w * s))
        ph = int(round(h * s))
        if pw <= 1 or ph <= 1:
            return None
        return (px, py, pw, ph)

    def _run(self):
        import cv2
        period = 1.0 / self.fps
        next_t = time.perf_counter()
        try:
            while self._running:
                phys = self._to_physical(self.bbox_fn())
                if phys is not None:
                    frame = self._grab(phys)
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
        if (frame.shape[1], frame.shape[0]) != (tw, th):
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
