"""Test the screen-recording encoder with a synthetic frame source.

Feeds deterministic numpy frames through :class:`ScreenRecorder` (bypassing the
real screen grab) and verifies a valid, non-empty MP4 is produced and can be
re-opened. Skips cleanly if OpenCV is not installed.

Run:  python tests/test_screencap.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    try:
        import cv2  # noqa: F401
        import numpy as np
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: OpenCV/numpy unavailable: {e}")
        return 0

    from ptp_metrics.screencap import ScreenRecorder

    W, H = 320, 240
    counter = {"n": 0}
    grabbed = {"bbox": None}

    def fake_grab(phys_bbox):
        # record the physical bbox the recorder asked for
        grabbed["bbox"] = phys_bbox
        _, _, w, h = phys_bbox
        i = counter["n"]
        counter["n"] += 1
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        frame[:, :, 1] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
        frame[:, :, 2] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
        return frame

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.mp4")
        # no screen_size_fn -> scale 1.0, physical bbox == logical bbox
        rec = ScreenRecorder(path, bbox_fn=lambda: (0, 0, W, H), fps=15,
                             grab_fn=fake_grab)
        rec.start()
        # let the background thread grab/encode a handful of frames
        import time
        time.sleep(0.8)
        rec.stop()

        assert rec.error is None, f"recorder error: {rec.error}"
        assert rec.frames_written > 0, "no frames written"
        assert os.path.exists(path) and os.path.getsize(path) > 0, "empty mp4"
        assert grabbed["bbox"] == (0, 0, W, H), \
            f"unexpected physical bbox {grabbed['bbox']}"

        cap = cv2.VideoCapture(path)
        ok, frame = cap.read()
        cap.release()
        assert ok and frame is not None, "encoded mp4 not readable"
        assert frame.shape[0] == H and frame.shape[1] == W, \
            f"unexpected frame size {frame.shape}"
        print(f"PASS screencap: wrote {rec.frames_written} frames "
              f"({rec.duration_s:.2f}s), mp4 {os.path.getsize(path)} bytes, "
              f"readback {frame.shape[1]}x{frame.shape[0]}")

    # DPI-scaling: a logical 400x300 window on a 150%-scaled 1280-logical screen
    # (physical 1920) must be grabbed at 600x450 physical pixels.
    import ptp_metrics.screencap as sc
    orig = sc._full_screen_physical_size
    sc._full_screen_physical_size = lambda: (1920, 1080)
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "scaled.mp4")
            grabbed["bbox"] = None
            rec = ScreenRecorder(path, bbox_fn=lambda: (100, 50, 400, 300),
                                 fps=15, screen_size_fn=lambda: (1280, 720),
                                 grab_fn=fake_grab)
            rec.start()
            time.sleep(0.4)
            rec.stop()
            assert abs(rec.scale - 1.5) < 1e-6, f"scale {rec.scale} != 1.5"
            assert grabbed["bbox"] == (150, 75, 600, 450), \
                f"physical bbox not scaled: {grabbed['bbox']}"
            print(f"PASS dpi scaling: scale={rec.scale}, "
                  f"logical (100,50,400,300) -> physical {grabbed['bbox']}")
    finally:
        sc._full_screen_physical_size = orig

    print("\nScreen-capture test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
