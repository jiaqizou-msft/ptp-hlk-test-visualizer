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

    def fake_grab(bbox):
        # moving gradient so successive frames differ (BGR uint8)
        i = counter["n"]
        counter["n"] += 1
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        frame[:, :, 1] = np.linspace(0, 255, W, dtype=np.uint8)[None, :]
        frame[:, :, 2] = np.linspace(0, 255, H, dtype=np.uint8)[:, None]
        return frame

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.mp4")
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

        cap = cv2.VideoCapture(path)
        ok, frame = cap.read()
        cap.release()
        assert ok and frame is not None, "encoded mp4 not readable"
        assert frame.shape[0] == H and frame.shape[1] == W, \
            f"unexpected frame size {frame.shape}"
        print(f"PASS screencap: wrote {rec.frames_written} frames "
              f"({rec.duration_s:.2f}s), mp4 {os.path.getsize(path)} bytes, "
              f"readback {frame.shape[1]}x{frame.shape[0]}")

    print("\nScreen-capture test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
