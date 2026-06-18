"""Utility to export a :class:`Recording` to the canonical long-format CSV.

Useful for converting a live or synthetic capture into a file that
``ptp_metrics analyze`` (and other tools) can re-read, and for testing the
loader round-trip.
"""

from __future__ import annotations

import csv
from .models import Recording


CANONICAL_HEADER = [
    "Frame", "ScanTime", "ContactCount", "Button",
    "ContactId", "X", "Y", "TipSwitch", "Confidence",
    "Width", "Height", "Pressure", "HostTimestamp",
]


def export_csv(rec: Recording, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CANONICAL_HEADER)
        for f in rec.frames:
            contacts = f.contacts or [None]
            for c in contacts:
                if c is None:
                    w.writerow([f.index, _n(f.scan_time), f.contact_count,
                                int(f.button), "", "", "", "", "", "", "", "",
                                _n(f.host_timestamp)])
                else:
                    w.writerow([
                        f.index, _n(f.scan_time), f.contact_count, int(f.button),
                        c.contact_id, _n(c.x), _n(c.y), int(c.tip), int(c.confidence),
                        _n(c.width), _n(c.height), _n(c.pressure), _n(f.host_timestamp),
                    ])


def _n(v):
    return "" if v is None else v
