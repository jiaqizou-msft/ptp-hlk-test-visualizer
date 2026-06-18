"""Real-time streaming data logger.

Writes PTP frames to disk *as they arrive* (not accumulate-then-dump), using a
background writer thread fed by a queue so the high-rate capture callback never
blocks on disk I/O.

Two formats:
  * ``csv``   - the canonical long-format schema (one row per contact), matching
    :func:`ptp_metrics.export.export_csv` so the result re-loads cleanly.
  * ``jsonl`` - one JSON object per frame (lossless, easy to stream/parse).

Usage::

    log = StreamLogger("session.csv")
    log.start()
    # in capture callback:
    log.write(frame)
    ...
    log.stop()    # flushes and closes
"""

from __future__ import annotations

import json
import os
import queue
import threading
from typing import Optional

from .models import Frame

CSV_HEADER = ("Frame,ScanTime,ContactCount,Button,ContactId,X,Y,"
              "TipSwitch,Confidence,Width,Height,Pressure,HostTimestamp\n")


class StreamLogger:
    def __init__(self, path: str, fmt: Optional[str] = None):
        self.path = path
        self.fmt = (fmt or ("jsonl" if path.lower().endswith(".jsonl") else "csv")).lower()
        self._q: "queue.Queue" = queue.Queue(maxsize=100000)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._fh = None
        self.frames_written = 0
        self.rows_written = 0
        self._err: Optional[Exception] = None

    def start(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8", newline="")
        if self.fmt == "csv":
            self._fh.write(CSV_HEADER)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="PTPLogger")
        self._thread.start()

    def write(self, frame: Frame):
        if self._running:
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass  # drop rather than stall capture; rare

    def stop(self):
        self._running = False
        if self._thread:
            self._q.put(None)  # sentinel
            self._thread.join(timeout=3.0)
        if self._fh:
            try:
                self._fh.flush()
                self._fh.close()
            finally:
                self._fh = None

    # -- internals ---------------------------------------------------------- #
    def _run(self):
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                self._write_one(item)
        except Exception as e:  # noqa: BLE001
            self._err = e

    def _write_one(self, f: Frame):
        if self.fmt == "jsonl":
            obj = {
                "i": f.index, "scan": f.scan_time, "cc": f.contact_count,
                "btn": int(f.button), "t": f.host_timestamp,
                "contacts": [
                    {"id": c.contact_id, "x": c.x, "y": c.y, "tip": int(c.tip),
                     "conf": int(c.confidence), "w": c.width, "h": c.height,
                     "p": c.pressure}
                    for c in f.contacts
                ],
            }
            self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        else:
            contacts = f.contacts or [None]
            for c in contacts:
                if c is None:
                    row = (f.index, _n(f.scan_time), _n(f.contact_count),
                           int(f.button), "", "", "", "", "", "", "", "", _n(f.host_timestamp))
                else:
                    row = (f.index, _n(f.scan_time), _n(f.contact_count), int(f.button),
                           c.contact_id, _n(c.x), _n(c.y), int(c.tip), int(c.confidence),
                           _n(c.width), _n(c.height), _n(c.pressure), _n(f.host_timestamp))
                self._fh.write(",".join(str(v) for v in row) + "\n")
                self.rows_written += 1
        self.frames_written += 1


def _n(v):
    return "" if v is None else v
