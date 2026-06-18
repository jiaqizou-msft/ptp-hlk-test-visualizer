"""Core data models for PTP recordings.

Units convention used throughout the package:
  * Raw position values (``x``/``y``) are HID *logical* counts as reported by
    the device.
  * ``scan_time`` is the raw HID Scan Time value (Usage 0x0D:0x56). By the HID
    Usage Tables the unit is 100 microseconds, so microseconds = value * 100.
  * ``DeviceInfo`` carries the logical/physical ranges needed to convert counts
    to millimetres and to derive resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# HID Scan Time unit is 100 microseconds per count (HID Usage Tables, Digitizers).
SCAN_TIME_UNIT_US = 100.0


@dataclass
class Contact:
    """A single finger/contact reported within one frame."""

    contact_id: int
    x: float
    y: float
    tip: bool = True              # Tip Switch (contact touching the surface)
    confidence: bool = True       # Confidence (device believes it is a finger)
    width: Optional[float] = None     # contact width in logical counts (if reported)
    height: Optional[float] = None    # contact height in logical counts (if reported)
    pressure: Optional[float] = None  # true pressure if device reports it (rare on PTP)


@dataclass
class Frame:
    """One PTP input report (a "scan frame")."""

    index: int
    scan_time: Optional[float]        # raw HID scan-time counts (100us units)
    contacts: List[Contact] = field(default_factory=list)
    contact_count: Optional[int] = None   # reported contact count for the frame
    button: bool = False                  # clickpad button state
    host_timestamp: Optional[float] = None  # seconds, wall-clock at capture (live mode)

    @property
    def scan_time_us(self) -> Optional[float]:
        if self.scan_time is None:
            return None
        return self.scan_time * SCAN_TIME_UNIT_US

    @property
    def active_contacts(self) -> List[Contact]:
        return [c for c in self.contacts if c.tip]


@dataclass
class DeviceInfo:
    """Static device parameters parsed from the HID report descriptor / DigiInfo.

    Resolution is derived from the HID formula:

        Resolution = (LogicalMax - LogicalMin)
                     / ((PhysicalMax - PhysicalMin) * 10**UnitExponent)

    which yields counts per physical unit. The HID length unit for PTP is
    centimetres, so :meth:`x_counts_per_mm` divides that by 10.
    """

    name: str = "Unknown PTP device"

    x_logical_min: float = 0.0
    x_logical_max: float = 1.0
    y_logical_min: float = 0.0
    y_logical_max: float = 1.0

    # Physical extent of the sensor in millimetres (preferred, if known).
    x_physical_mm: Optional[float] = None
    y_physical_mm: Optional[float] = None

    # Raw HID physical range + unit exponent (used if *_physical_mm not supplied).
    x_physical_min: Optional[float] = None
    x_physical_max: Optional[float] = None
    y_physical_min: Optional[float] = None
    y_physical_max: Optional[float] = None
    unit_exponent: int = 0
    unit_is_cm: bool = True   # HID SI-Linear length default unit is centimetre

    max_contacts: Optional[int] = None
    scan_time_unit_us: float = SCAN_TIME_UNIT_US

    # -- physical size helpers -------------------------------------------------
    def _phys_mm(self, pmin, pmax) -> Optional[float]:
        if pmin is None or pmax is None:
            return None
        span = (pmax - pmin) * (10.0 ** self.unit_exponent)
        if self.unit_is_cm:
            span *= 10.0  # cm -> mm
        return abs(span)

    @property
    def width_mm(self) -> Optional[float]:
        if self.x_physical_mm is not None:
            return self.x_physical_mm
        return self._phys_mm(self.x_physical_min, self.x_physical_max)

    @property
    def height_mm(self) -> Optional[float]:
        if self.y_physical_mm is not None:
            return self.y_physical_mm
        return self._phys_mm(self.y_physical_min, self.y_physical_max)

    # -- resolution ------------------------------------------------------------
    @property
    def x_counts_per_mm(self) -> Optional[float]:
        w = self.width_mm
        if not w:
            return None
        return abs(self.x_logical_max - self.x_logical_min) / w

    @property
    def y_counts_per_mm(self) -> Optional[float]:
        h = self.height_mm
        if not h:
            return None
        return abs(self.y_logical_max - self.y_logical_min) / h

    @property
    def x_dpi(self) -> Optional[float]:
        cpm = self.x_counts_per_mm
        return cpm * 25.4 if cpm else None

    @property
    def y_dpi(self) -> Optional[float]:
        cpm = self.y_counts_per_mm
        return cpm * 25.4 if cpm else None

    def x_to_mm(self, x: float) -> Optional[float]:
        cpm = self.x_counts_per_mm
        return (x - self.x_logical_min) / cpm if cpm else None

    def y_to_mm(self, y: float) -> Optional[float]:
        cpm = self.y_counts_per_mm
        return (y - self.y_logical_min) / cpm if cpm else None


@dataclass
class Recording:
    """A full capture: device metadata plus an ordered list of frames."""

    device: DeviceInfo = field(default_factory=DeviceInfo)
    frames: List[Frame] = field(default_factory=list)
    source: str = ""

    def __len__(self) -> int:
        return len(self.frames)

    @property
    def contact_ids(self) -> List[int]:
        ids = set()
        for f in self.frames:
            for c in f.contacts:
                ids.add(c.contact_id)
        return sorted(ids)
