"""Parse a HID report descriptor to recover the parameters needed for
resolution and counts-to-millimetre conversion.

This is a compact HID *report descriptor* walker (not a full parser). It tracks
the global item state (logical/physical min-max, unit, unit exponent) and, for
each ``Input`` main item, associates it with the pending local Usage so we can
pull out the X (Generic Desktop 0x30) and Y (0x31) field definitions.

It also offers :func:`device_from_digiinfo_text` to build a :class:`DeviceInfo`
from the human-readable output of Microsoft's ``DigiInfo.exe``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .models import DeviceInfo

# HID usage pages / usages we care about.
PAGE_GENERIC_DESKTOP = 0x01
PAGE_DIGITIZER = 0x0D
USAGE_X = 0x30
USAGE_Y = 0x31

# Main item tags
TAG_INPUT = 0x8
TAG_COLLECTION = 0xA
TAG_END_COLLECTION = 0xC

# Item type codes
TYPE_MAIN = 0
TYPE_GLOBAL = 1
TYPE_LOCAL = 2


def _signed(value: int, size_bytes: int) -> int:
    if size_bytes == 0:
        return value
    sign_bit = 1 << (size_bytes * 8 - 1)
    if value & sign_bit:
        value -= 1 << (size_bytes * 8)
    return value


class _FieldDef:
    __slots__ = (
        "usage_page", "usage", "logical_min", "logical_max",
        "physical_min", "physical_max", "unit", "unit_exponent",
    )

    def __init__(self, usage_page, usage, g):
        self.usage_page = usage_page
        self.usage = usage
        self.logical_min = g.get("logical_min")
        self.logical_max = g.get("logical_max")
        self.physical_min = g.get("physical_min")
        self.physical_max = g.get("physical_max")
        self.unit = g.get("unit", 0)
        self.unit_exponent = g.get("unit_exponent", 0)


def parse_report_descriptor(data: bytes) -> List[_FieldDef]:
    """Walk a raw HID report descriptor and return the X/Y input field defs."""
    fields: List[_FieldDef] = []
    g: Dict[str, int] = {}
    usages: List[int] = []
    usage_page = 0
    i = 0
    n = len(data)
    while i < n:
        prefix = data[i]
        i += 1
        size_code = prefix & 0x03
        size = 4 if size_code == 3 else size_code
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        raw = data[i:i + size]
        i += size
        val = int.from_bytes(raw, "little") if raw else 0

        if item_type == TYPE_GLOBAL:
            if tag == 0x0:
                usage_page = val
            elif tag == 0x1:
                g["logical_min"] = _signed(val, size)
            elif tag == 0x2:
                g["logical_max"] = _signed(val, size)
            elif tag == 0x3:
                g["physical_min"] = _signed(val, size)
            elif tag == 0x4:
                g["physical_max"] = _signed(val, size)
            elif tag == 0x5:
                g["unit_exponent"] = _nibble_exponent(val)
            elif tag == 0x6:
                g["unit"] = val
        elif item_type == TYPE_LOCAL:
            if tag == 0x0:  # Usage
                usages.append(val)
        elif item_type == TYPE_MAIN:
            if tag == TAG_INPUT:
                for u in (usages or [None]):
                    page = usage_page
                    fields.append(_FieldDef(page, u, g))
            # main items clear the local state (usages)
            usages = []
    return fields


def _nibble_exponent(val: int) -> int:
    """HID unit-exponent is a signed 4-bit nibble (0..7 -> 0..7, 8..15 -> -8..-1)."""
    val &= 0x0F
    return val - 16 if val > 7 else val


def device_from_report_descriptor(data: bytes, name: str = "PTP device") -> DeviceInfo:
    """Build a :class:`DeviceInfo` from a raw HID report descriptor."""
    fields = parse_report_descriptor(data)
    dev = DeviceInfo(name=name)
    for f in fields:
        if f.usage_page == PAGE_GENERIC_DESKTOP and f.usage == USAGE_X:
            _apply_axis(dev, f, axis="x")
        elif f.usage_page == PAGE_GENERIC_DESKTOP and f.usage == USAGE_Y:
            _apply_axis(dev, f, axis="y")
    return dev


def _apply_axis(dev: DeviceInfo, f: _FieldDef, axis: str) -> None:
    if f.logical_min is not None:
        setattr(dev, f"{axis}_logical_min", float(f.logical_min))
    if f.logical_max is not None:
        setattr(dev, f"{axis}_logical_max", float(f.logical_max))
    if f.physical_min is not None:
        setattr(dev, f"{axis}_physical_min", float(f.physical_min))
    if f.physical_max is not None:
        setattr(dev, f"{axis}_physical_max", float(f.physical_max))
    if f.unit_exponent is not None:
        dev.unit_exponent = f.unit_exponent


_NUM = r"(-?\d+(?:\.\d+)?)"


def device_from_digiinfo_text(text: str, name: str = "PTP device") -> DeviceInfo:
    """Best-effort parse of DigiInfo-style textual output.

    DigiInfo prints per-axis logical/physical ranges and units. The exact
    wording varies between versions, so we match on keyword fragments and only
    fill what we can find. Anything missing falls back to DeviceInfo defaults.
    """
    dev = DeviceInfo(name=name)
    low = text.lower()

    def grab(patterns) -> Optional[float]:
        for p in patterns:
            m = re.search(p, low)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
        return None

    mapping = {
        "x_logical_min": [rf"x[^\n]*logical[^\n]*min[^\n-]*{_NUM}",
                          rf"x[^\n]*min[^\n]*logical[^\n-]*{_NUM}"],
        "x_logical_max": [rf"x[^\n]*logical[^\n]*max[^\n-]*{_NUM}",
                          rf"x[^\n]*max[^\n]*logical[^\n-]*{_NUM}"],
        "y_logical_min": [rf"y[^\n]*logical[^\n]*min[^\n-]*{_NUM}"],
        "y_logical_max": [rf"y[^\n]*logical[^\n]*max[^\n-]*{_NUM}"],
        "x_physical_min": [rf"x[^\n]*physical[^\n]*min[^\n-]*{_NUM}"],
        "x_physical_max": [rf"x[^\n]*physical[^\n]*max[^\n-]*{_NUM}"],
        "y_physical_min": [rf"y[^\n]*physical[^\n]*min[^\n-]*{_NUM}"],
        "y_physical_max": [rf"y[^\n]*physical[^\n]*max[^\n-]*{_NUM}"],
    }
    for attr, pats in mapping.items():
        v = grab(pats)
        if v is not None:
            setattr(dev, attr, v)

    me = grab([rf"max[^\n]*contact[^\n-]*{_NUM}", rf"contact[^\n]*count[^\n]*max[^\n-]*{_NUM}"])
    if me is not None:
        dev.max_contacts = int(me)

    ue = grab([rf"unit[^\n]*exponent[^\n-]*(-?\d+)"])
    if ue is not None:
        dev.unit_exponent = int(ue)

    # explicit physical size in mm overrides raw range, if DigiInfo reports it
    wmm = grab([rf"width[^\n]*{_NUM}\s*mm", rf"{_NUM}\s*mm[^\n]*wide"])
    hmm = grab([rf"height[^\n]*{_NUM}\s*mm", rf"{_NUM}\s*mm[^\n]*(?:tall|high)"])
    if wmm is not None:
        dev.x_physical_mm = wmm
    if hmm is not None:
        dev.y_physical_mm = hmm
    return dev
