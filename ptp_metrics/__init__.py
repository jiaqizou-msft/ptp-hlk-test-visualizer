"""PTP Metrics - real-time numerical Precision Touchpad (PTP) quality metrics.

Computes linearity, jitter and resolution (as defined by the Windows
Precision Touchpad / HID spec) from data captured by Microsoft's ptrecorder,
from a live HID capture, or from synthetic test data, and renders rich
visualizations of scan-time (frame timing), position, pressure and contact
timing structure.
"""

from .models import Contact, Frame, DeviceInfo, Recording

__all__ = ["Contact", "Frame", "DeviceInfo", "Recording"]
__version__ = "0.1.0"
