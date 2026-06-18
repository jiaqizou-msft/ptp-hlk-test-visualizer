"""Loaders that turn captured data into a :class:`Recording`.

Three entry points:

* :func:`load_csv` - flexible CSV reader with header auto-detection. Handles both
  "long" layout (one row per contact, grouped by a frame/report index) and
  "wide" layout (per-contact indexed columns such as ``X0,Y0,X1,Y1``).
* :func:`load_ptrecorder_dir` - point it at the directory that
  ``ptrecorder.exe /dir <dir>`` produced; it locates the data file(s) and parses
  them with :func:`load_csv`.
* :func:`run_digiinfo` - launch ``DigiInfo.exe`` and parse its output into a
  :class:`DeviceInfo`.

The exact ptrecorder column layout differs between builds, so parsing is driven
by keyword matching rather than fixed positions. See ``CANONICAL_SCHEMA`` for the
column names that are recognised.
"""

from __future__ import annotations

import csv
import glob
import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from .models import Contact, DeviceInfo, Frame, Recording
from .hid_descriptor import device_from_digiinfo_text

CANONICAL_SCHEMA = {
    "frame": ["frame", "report", "reportindex", "index", "seq", "sample", "packet"],
    "scan_time": ["scantime", "scan_time", "scan time", "ulscantime", "scantimems", "tscan"],
    "contact_id": ["contactid", "contact_id", "contact id", "cid", "contactidentifier", "id"],
    "contact_count": ["contactcount", "contact_count", "contact count", "ncontacts", "count"],
    "x": ["x", "xpos", "x_pos", "positionx", "x position", "logicalx"],
    "y": ["y", "ypos", "y_pos", "positiony", "y position", "logicaly"],
    "tip": ["tip", "tipswitch", "tip_switch", "tip switch", "touch", "down"],
    "confidence": ["confidence", "conf"],
    "width": ["width", "w", "contactwidth"],
    "height": ["height", "h", "contactheight"],
    "pressure": ["pressure", "force", "tippressure"],
    "button": ["button", "btn", "click", "buttonstate"],
    "timestamp": ["timestamp", "hosttime", "host_time", "host timestamp", "qpc", "systime", "time"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match_columns(headers: List[str]) -> Dict[str, int]:
    """Map canonical field -> column index using exact-then-fuzzy matching."""
    norm = [_norm(h) for h in headers]
    result: Dict[str, int] = {}
    used = set()
    # exact match first (longest alias wins to avoid 'x' grabbing 'maxx')
    for field, aliases in CANONICAL_SCHEMA.items():
        for alias in sorted(aliases, key=len, reverse=True):
            a = _norm(alias)
            for idx, h in enumerate(norm):
                if idx in used:
                    continue
                if h == a:
                    result[field] = idx
                    used.add(idx)
                    break
            if field in result:
                break
    return result


def _detect_wide_contacts(headers: List[str]) -> Dict[int, Dict[str, int]]:
    """Detect per-contact indexed columns, e.g. X0,Y0,Tip0,X1,Y1,...

    Returns {contact_index: {field: column_index}}.
    """
    norm = [_norm(h) for h in headers]
    pattern_fields = {
        "x": r"^x(\d+)$|^contact(\d+)x$|^x_(\d+)$",
        "y": r"^y(\d+)$|^contact(\d+)y$|^y_(\d+)$",
        "tip": r"^tip(?:switch)?(\d+)$",
        "contact_id": r"^contactid(\d+)$|^cid(\d+)$|^id(\d+)$",
        "confidence": r"^confidence(\d+)$|^conf(\d+)$",
        "width": r"^width(\d+)$|^w(\d+)$",
        "height": r"^height(\d+)$|^h(\d+)$",
        "pressure": r"^pressure(\d+)$",
    }
    out: Dict[int, Dict[str, int]] = {}
    for col, h in enumerate(norm):
        for field, pat in pattern_fields.items():
            m = re.match(pat, h)
            if m:
                ci = next((int(g) for g in m.groups() if g is not None), None)
                if ci is None:
                    continue
                out.setdefault(ci, {})[field] = col
    # only keep contacts that at least have an x and y column
    return {ci: cols for ci, cols in out.items() if "x" in cols and "y" in cols}


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    v = str(v).strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        # hex like 0x1A
        try:
            return float(int(v, 16))
        except ValueError:
            return None


def _to_bool(v) -> bool:
    f = _to_float(v)
    if f is not None:
        return f != 0
    return str(v).strip().lower() in ("true", "yes", "on", "down", "t")


def _sniff(path: str) -> str:
    with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(4096)
    for delim in [",", "\t", ";", "|"]:
        if delim in sample:
            return delim
    return ","


def load_csv(path: str, device: Optional[DeviceInfo] = None) -> Recording:
    """Load a CSV/TSV recording with automatic header and layout detection."""
    device = device or DeviceInfo()
    delim = _sniff(path)
    with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delim)
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return Recording(device=device, frames=[], source=path)

    headers = rows[0]
    has_header = any(re.search(r"[a-zA-Z]", c) for c in headers)
    if not has_header:
        raise ValueError(f"{path}: no header row detected; cannot map columns")

    wide = _detect_wide_contacts(headers)
    cols = _match_columns(headers)
    data_rows = rows[1:]

    if wide:
        rec = _parse_wide(data_rows, headers, wide, cols, device, path)
    else:
        rec = _parse_long(data_rows, cols, device, path)
    return rec


def _parse_long(data_rows, cols, device, path) -> Recording:
    frames: List[Frame] = []
    by_frame: Dict[object, Frame] = {}
    order: List[object] = []
    auto = 0
    for row in data_rows:
        def get(field):
            idx = cols.get(field)
            return row[idx] if idx is not None and idx < len(row) else None

        fkey = get("frame")
        st = get("scan_time")
        if fkey is None or str(fkey).strip() == "":
            # no explicit frame id; group by scan_time, else one frame per row
            fkey = st if st not in (None, "") else f"__auto_{auto}"
            auto += 1

        if fkey not in by_frame:
            frame = Frame(
                index=len(order),
                scan_time=_to_float(st),
                contact_count=int(_to_float(get("contact_count")) or 0) or None,
                button=_to_bool(get("button")),
                host_timestamp=_to_float(get("timestamp")),
            )
            by_frame[fkey] = frame
            order.append(fkey)
        frame = by_frame[fkey]

        x = _to_float(get("x"))
        y = _to_float(get("y"))
        if x is not None and y is not None:
            cid = get("contact_id")
            frame.contacts.append(Contact(
                contact_id=int(_to_float(cid) or 0),
                x=x, y=y,
                tip=_to_bool(get("tip")) if cols.get("tip") is not None else True,
                confidence=_to_bool(get("confidence")) if cols.get("confidence") is not None else True,
                width=_to_float(get("width")),
                height=_to_float(get("height")),
                pressure=_to_float(get("pressure")),
            ))
    frames = [by_frame[k] for k in order]
    for i, f in enumerate(frames):
        f.index = i
        if f.contact_count is None:
            f.contact_count = len(f.active_contacts)
    return Recording(device=device, frames=frames, source=path)


def _parse_wide(data_rows, headers, wide, cols, device, path) -> Recording:
    frames: List[Frame] = []
    for i, row in enumerate(data_rows):
        def get(field):
            idx = cols.get(field)
            return row[idx] if idx is not None and idx < len(row) else None

        frame = Frame(
            index=i,
            scan_time=_to_float(get("scan_time")),
            button=_to_bool(get("button")),
            host_timestamp=_to_float(get("timestamp")),
        )
        for ci, fcols in sorted(wide.items()):
            def gc(field):
                idx = fcols.get(field)
                return row[idx] if idx is not None and idx < len(row) else None
            x = _to_float(gc("x"))
            y = _to_float(gc("y"))
            if x is None or y is None:
                continue
            tip = _to_bool(gc("tip")) if "tip" in fcols else True
            cid = gc("contact_id")
            frame.contacts.append(Contact(
                contact_id=int(_to_float(cid)) if cid not in (None, "") else ci,
                x=x, y=y, tip=tip,
                confidence=_to_bool(gc("confidence")) if "confidence" in fcols else True,
                width=_to_float(gc("width")),
                height=_to_float(gc("height")),
                pressure=_to_float(gc("pressure")),
            ))
        cc = _to_float(get("contact_count"))
        frame.contact_count = int(cc) if cc is not None else len(frame.active_contacts)
        frames.append(frame)
    return Recording(device=device, frames=frames, source=path)


def load_ptrecorder_dir(path: str, device: Optional[DeviceInfo] = None) -> Recording:
    """Load the recording produced by ``ptrecorder.exe /dir <path>``.

    ptrecorder writes its parsed report data into the target directory. We pick
    the largest CSV/TXT/LOG file that has a recognisable header and parse it.
    """
    if os.path.isfile(path):
        return load_csv(path, device)
    candidates: List[str] = []
    for ext in ("*.csv", "*.tsv", "*.txt", "*.log"):
        candidates.extend(glob.glob(os.path.join(path, "**", ext), recursive=True))
    if not candidates:
        raise FileNotFoundError(
            f"No .csv/.tsv/.txt/.log data files found under {path!r}. "
            "Run ptrecorder.exe /dir <path> first (elevated)."
        )
    # prefer files that parse to non-empty recordings, largest first
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    last_err: Optional[Exception] = None
    for c in candidates:
        try:
            rec = load_csv(c, device)
            if rec.frames:
                return rec
        except Exception as e:  # noqa: BLE001 - try next candidate
            last_err = e
    if last_err:
        raise last_err
    raise ValueError(f"Found data files under {path!r} but none contained frames.")


def infer_logical_range(rec: Recording, force: bool = False) -> bool:
    """Fill in the device logical X/Y range from observed data when unknown.

    The exact logical range belongs in the HID descriptor (use DigiInfo or live
    capture for precise values). When it is at the default (span <= 1) and the
    data clearly exceeds it, we estimate it from the min/max of reported
    positions so that resolution / jitter / linearity can still be expressed in
    millimetres (approximate, since a finger may not reach the sensor edges).

    Returns True if it modified the device.
    """
    dev = rec.device
    xs = [c.x for f in rec.frames for c in f.contacts]
    ys = [c.y for f in rec.frames for c in f.contacts]
    if not xs or not ys:
        return False
    changed = False
    if force or abs(dev.x_logical_max - dev.x_logical_min) <= 1:
        dev.x_logical_min = min(xs)
        dev.x_logical_max = max(xs)
        changed = True
    if force or abs(dev.y_logical_max - dev.y_logical_min) <= 1:
        dev.y_logical_min = min(ys)
        dev.y_logical_max = max(ys)
        changed = True
    return changed


def run_digiinfo(digiinfo_exe: str, timeout: float = 8.0) -> Tuple[str, DeviceInfo]:
    """Run DigiInfo.exe, capture stdout, and parse it into a :class:`DeviceInfo`.

    Returns ``(raw_text, device_info)``. DigiInfo may require a connected PTP
    device; on failure the raw text still carries whatever was emitted.
    """
    try:
        proc = subprocess.run(
            [digiinfo_exe],
            capture_output=True, text=True, timeout=timeout,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or "") if isinstance(e.stdout, str) else ""
    except OSError as e:
        return f"<failed to launch DigiInfo: {e}>", DeviceInfo()
    return text, device_from_digiinfo_text(text, name="DigiInfo PTP device")
