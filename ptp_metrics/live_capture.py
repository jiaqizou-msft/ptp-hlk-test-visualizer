"""Live Precision Touchpad capture on Windows via Raw Input + HidP (ctypes).

This is the genuine real-time path: it registers for the digitizer "touch pad"
top-level collection (Usage Page 0x0D, Usage 0x05) using the Raw Input API in
*sink* mode, then parses each incoming HID report with the ``HidP_*`` helpers
from ``hid.dll`` (the same APIs ptrecorder uses) to extract X, Y, Tip Switch,
Contact Id, Contact Count and Scan Time.

It also derives a :class:`DeviceInfo` (logical/physical ranges -> resolution)
directly from the device's preparsed data, so resolution is available even
before any finger touches the pad.

Notes / caveats:
  * Windows only. On other platforms importing still works but :func:`is_supported`
    returns ``False``.
  * Raw Input sink mode receives a copy of the touch reports while Windows keeps
    using them, so normal cursor behaviour is unaffected and no elevation is
    required for the common case.
  * Per-contact extraction iterates the report's logical link collections; the
    layout is standard PTP. Exotic descriptors may need tweaks.
"""

from __future__ import annotations

import ctypes as C
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, List, Optional

from .models import Contact, DeviceInfo, Frame


def is_supported() -> bool:
    return sys.platform == "win32"


# --------------------------------------------------------------------------- #
# HID usage constants
# --------------------------------------------------------------------------- #
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_PAGE_DIGITIZER = 0x0D
HID_USAGE_GENERIC_X = 0x30
HID_USAGE_GENERIC_Y = 0x31
HID_USAGE_DIGITIZER_TOUCHPAD = 0x05
HID_USAGE_DIGITIZER_TIP_SWITCH = 0x42
HID_USAGE_DIGITIZER_CONFIDENCE = 0x47
HID_USAGE_DIGITIZER_CONTACT_ID = 0x51
HID_USAGE_DIGITIZER_CONTACT_COUNT = 0x54
HID_USAGE_DIGITIZER_SCAN_TIME = 0x56
HID_USAGE_DIGITIZER_WIDTH = 0x48
HID_USAGE_DIGITIZER_HEIGHT = 0x49

HIDP_INPUT = 0

RIM_TYPEHID = 2
RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICEINFO = 0x2000000B
RIDEV_INPUTSINK = 0x00000100
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
HWND_MESSAGE = wintypes.HWND(-3)
HIDP_STATUS_SUCCESS = 0x00110000


if is_supported():
    user32 = C.WinDLL("user32", use_last_error=True)
    hid = C.WinDLL("hid", use_last_error=True)
    kernel32 = C.WinDLL("kernel32", use_last_error=True)

    ULONG = wintypes.ULONG
    USHORT = wintypes.USHORT
    LONG = wintypes.LONG
    UCHAR = C.c_ubyte
    BOOLEAN = C.c_ubyte

    class RAWINPUTDEVICE(C.Structure):
        _fields_ = [("usUsagePage", USHORT), ("usUsage", USHORT),
                    ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

    class RAWINPUTHEADER(C.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                    ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

    class RAWHID(C.Structure):
        _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD),
                    ("bRawData", UCHAR * 1)]

    class RAWINPUT(C.Structure):
        _fields_ = [("header", RAWINPUTHEADER), ("hid", RAWHID)]

    class HIDP_CAPS(C.Structure):
        _fields_ = [
            ("Usage", USHORT), ("UsagePage", USHORT),
            ("InputReportByteLength", USHORT),
            ("OutputReportByteLength", USHORT),
            ("FeatureReportByteLength", USHORT),
            ("Reserved", USHORT * 17),
            ("NumberLinkCollectionNodes", USHORT),
            ("NumberInputButtonCaps", USHORT),
            ("NumberInputValueCaps", USHORT),
            ("NumberInputDataIndices", USHORT),
            ("NumberOutputButtonCaps", USHORT),
            ("NumberOutputValueCaps", USHORT),
            ("NumberOutputDataIndices", USHORT),
            ("NumberFeatureButtonCaps", USHORT),
            ("NumberFeatureValueCaps", USHORT),
            ("NumberFeatureDataIndices", USHORT),
        ]

    class _VC_Range(C.Structure):
        _fields_ = [("UsageMin", USHORT), ("UsageMax", USHORT),
                    ("StringMin", USHORT), ("StringMax", USHORT),
                    ("DesignatorMin", USHORT), ("DesignatorMax", USHORT),
                    ("DataIndexMin", USHORT), ("DataIndexMax", USHORT)]

    class _VC_NotRange(C.Structure):
        _fields_ = [("Usage", USHORT), ("Reserved1", USHORT),
                    ("StringIndex", USHORT), ("Reserved2", USHORT),
                    ("DesignatorIndex", USHORT), ("Reserved3", USHORT),
                    ("DataIndex", USHORT), ("Reserved4", USHORT)]

    class _VC_Union(C.Union):
        _fields_ = [("Range", _VC_Range), ("NotRange", _VC_NotRange)]

    class HIDP_VALUE_CAPS(C.Structure):
        _fields_ = [
            ("UsagePage", USHORT), ("ReportID", UCHAR), ("IsAlias", BOOLEAN),
            ("BitField", USHORT), ("LinkCollection", USHORT),
            ("LinkUsage", USHORT), ("LinkUsagePage", USHORT),
            ("IsRange", BOOLEAN), ("IsStringRange", BOOLEAN),
            ("IsDesignatorRange", BOOLEAN), ("IsAbsolute", BOOLEAN),
            ("HasNull", BOOLEAN), ("Reserved", UCHAR),
            ("BitSize", USHORT), ("ReportCount", USHORT),
            ("Reserved2", USHORT * 5),
            ("UnitsExp", ULONG), ("Units", ULONG),
            ("LogicalMin", LONG), ("LogicalMax", LONG),
            ("PhysicalMin", LONG), ("PhysicalMax", LONG),
            ("u", _VC_Union),
        ]

    # function prototypes
    hid.HidP_GetCaps.argtypes = [C.c_void_p, C.POINTER(HIDP_CAPS)]
    hid.HidP_GetCaps.restype = LONG
    hid.HidP_GetValueCaps.argtypes = [C.c_int, C.POINTER(HIDP_VALUE_CAPS),
                                      C.POINTER(USHORT), C.c_void_p]
    hid.HidP_GetValueCaps.restype = LONG
    hid.HidP_GetUsageValue.argtypes = [C.c_int, USHORT, USHORT, USHORT,
                                       C.POINTER(ULONG), C.c_void_p,
                                       C.c_char_p, ULONG]
    hid.HidP_GetUsageValue.restype = LONG
    # Buttons (e.g. Tip Switch, Confidence) are reported as *usages*, not values.
    hid.HidP_GetUsages.argtypes = [C.c_int, USHORT, USHORT,
                                   C.POINTER(USHORT), C.POINTER(ULONG),
                                   C.c_void_p, C.c_char_p, ULONG]
    hid.HidP_GetUsages.restype = LONG

    # user32 raw-input prototypes (explicit so 64-bit HANDLEs are not truncated)
    user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, C.c_void_p,
                                       C.POINTER(wintypes.UINT), wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                              C.c_void_p, C.POINTER(wintypes.UINT)]
    user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    user32.RegisterRawInputDevices.argtypes = [C.c_void_p, wintypes.UINT, wintypes.UINT]
    user32.RegisterRawInputDevices.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = C.c_longlong

    WNDPROCTYPE = C.WINFUNCTYPE(C.c_longlong, wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASS(C.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROCTYPE),
                    ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class MSG(C.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


FrameCallback = Callable[[Frame], None]


class LiveCapture:
    """Capture live PTP frames and invoke a callback for each report.

    Usage::

        cap = LiveCapture(on_frame=lambda f: ...)
        cap.start()    # spawns a background thread with a message pump
        ...            # device info available via cap.device once a report arrives
        cap.stop()
    """

    def __init__(self, on_frame: Optional[FrameCallback] = None):
        if not is_supported():
            raise RuntimeError("Live capture requires Windows.")
        self.on_frame = on_frame
        self.device = DeviceInfo(name="Live PTP device")
        self.frames: List[Frame] = []
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._tid = None
        self._running = False
        self._frame_index = 0
        self._caps_by_device = {}
        self._t0 = None

    # -- public API ---------------------------------------------------------- #
    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="PTPLiveCapture")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- internals ----------------------------------------------------------- #
    def _run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        hInstance = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROCTYPE(self._on_message)  # keep ref alive
        cls = WNDCLASS()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hInstance
        cls.lpszClassName = "PTPMetricsRawInputWnd"
        atom = user32.RegisterClassW(C.byref(cls))
        if not atom:
            raise C.WinError(C.get_last_error())
        self._hwnd = user32.CreateWindowExW(
            0, cls.lpszClassName, "PTPMetrics", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hInstance, None)
        if not self._hwnd:
            raise C.WinError(C.get_last_error())

        rid = RAWINPUTDEVICE(HID_USAGE_PAGE_DIGITIZER, HID_USAGE_DIGITIZER_TOUCHPAD,
                             RIDEV_INPUTSINK, self._hwnd)
        if not user32.RegisterRawInputDevices(C.byref(rid), 1, C.sizeof(RAWINPUTDEVICE)):
            raise C.WinError(C.get_last_error())

        msg = MSG()
        while self._running:
            r = user32.GetMessageW(C.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(C.byref(msg))
            user32.DispatchMessageW(C.byref(msg))

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == WM_INPUT:
            try:
                self._handle_raw_input(lparam)
            except Exception:
                pass  # never let a parse error kill the pump
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _get_preparsed(self, hDevice):
        if hDevice in self._caps_by_device:
            return self._caps_by_device[hDevice]
        size = wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(hDevice, RIDI_PREPARSEDDATA, None, C.byref(size))
        if size.value == 0:
            return None
        buf = (C.c_byte * size.value)()
        if user32.GetRawInputDeviceInfoW(hDevice, RIDI_PREPARSEDDATA, buf, C.byref(size)) <= 0:
            return None
        pp = C.cast(buf, C.c_void_p)
        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(pp, C.byref(caps)) != HIDP_STATUS_SUCCESS:
            return None
        n = USHORT(caps.NumberInputValueCaps)
        vcaps = (HIDP_VALUE_CAPS * n.value)()
        hid.HidP_GetValueCaps(HIDP_INPUT, vcaps, C.byref(n), pp)
        info = {"buf": buf, "pp": pp, "caps": caps, "vcaps": vcaps[:n.value]}
        self._caps_by_device[hDevice] = info
        self._update_device_info(vcaps[:n.value])
        return info

    def _update_device_info(self, vcaps) -> None:
        for v in vcaps:
            if v.LinkUsagePage == 0 and v.UsagePage not in (HID_USAGE_PAGE_GENERIC,):
                pass
            usage = v.u.NotRange.Usage if not v.IsRange else v.u.Range.UsageMin
            exp = v.UnitsExp if v.UnitsExp < 8 else v.UnitsExp - 16
            if v.UsagePage == HID_USAGE_PAGE_GENERIC and usage == HID_USAGE_GENERIC_X:
                self.device.x_logical_min = v.LogicalMin
                self.device.x_logical_max = v.LogicalMax
                if v.PhysicalMax:
                    self.device.x_physical_min = v.PhysicalMin
                    self.device.x_physical_max = v.PhysicalMax
                    self.device.unit_exponent = exp
            elif v.UsagePage == HID_USAGE_PAGE_GENERIC and usage == HID_USAGE_GENERIC_Y:
                self.device.y_logical_min = v.LogicalMin
                self.device.y_logical_max = v.LogicalMax
                if v.PhysicalMax:
                    self.device.y_physical_min = v.PhysicalMin
                    self.device.y_physical_max = v.PhysicalMax

    def _handle_raw_input(self, lparam) -> None:
        size = wintypes.UINT(0)
        user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, None,
                               C.byref(size), C.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            return
        buf = (C.c_byte * size.value)()
        if user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, buf,
                                  C.byref(size), C.sizeof(RAWINPUTHEADER)) != size.value:
            return
        ri = C.cast(buf, C.POINTER(RAWINPUT)).contents
        if ri.header.dwType != RIM_TYPEHID:
            return
        info = self._get_preparsed(ri.header.hDevice)
        if not info:
            return
        size_hid = ri.hid.dwSizeHid
        count = ri.hid.dwCount
        base = C.addressof(ri.hid) + RAWHID.bRawData.offset
        for k in range(count):
            report = C.string_at(base + k * size_hid, size_hid)
            frame = self._parse_report(info, report)
            if frame is not None:
                self._emit(frame)

    def _value(self, info, usage_page, link_collection, usage, report) -> Optional[int]:
        val = wintypes.ULONG(0)
        status = hid.HidP_GetUsageValue(
            HIDP_INPUT, usage_page, link_collection, usage,
            C.byref(val), info["pp"], report, len(report))
        if status != HIDP_STATUS_SUCCESS:
            return None
        return val.value

    def _usages(self, info, usage_page, link_collection, report) -> set:
        """Return the set of active button usages on a page within a link collection."""
        length = wintypes.ULONG(64)
        arr = (wintypes.USHORT * 64)()
        status = hid.HidP_GetUsages(
            HIDP_INPUT, usage_page, link_collection, arr, C.byref(length),
            info["pp"], report, len(report))
        if status != HIDP_STATUS_SUCCESS:
            return set()
        return {arr[i] for i in range(length.value)}

    def _parse_report(self, info, report) -> Optional[Frame]:
        # Contact count (top-level digitizer collection -> link 0 works on most PTP)
        contact_count = self._value(info, HID_USAGE_PAGE_DIGITIZER, 0,
                                    HID_USAGE_DIGITIZER_CONTACT_COUNT, report)
        scan_time = self._value(info, HID_USAGE_PAGE_DIGITIZER, 0,
                                HID_USAGE_DIGITIZER_SCAN_TIME, report)
        # iterate per-contact logical collections by their LinkCollection index,
        # discovered from the X value caps
        contacts: List[Contact] = []
        link_collections = sorted({v.LinkCollection for v in info["vcaps"]
                                   if v.UsagePage == HID_USAGE_PAGE_GENERIC and
                                   (v.u.NotRange.Usage in (HID_USAGE_GENERIC_X, HID_USAGE_GENERIC_Y))})
        tip_button_seen = False
        for lc in link_collections:
            x = self._value(info, HID_USAGE_PAGE_GENERIC, lc, HID_USAGE_GENERIC_X, report)
            y = self._value(info, HID_USAGE_PAGE_GENERIC, lc, HID_USAGE_GENERIC_Y, report)
            if x is None or y is None:
                continue
            # Tip Switch / Confidence are buttons -> read them as usages.
            buttons = self._usages(info, HID_USAGE_PAGE_DIGITIZER, lc, report)
            if buttons:
                tip_button_seen = True
            tip_down = HID_USAGE_DIGITIZER_TIP_SWITCH in buttons
            confident = HID_USAGE_DIGITIZER_CONFIDENCE in buttons
            # Skip inactive/empty contact slots: a lifted slot reports tip up and
            # often stale (0,0) coordinates, which would create phantom traces.
            if not tip_down:
                continue
            cid = self._value(info, HID_USAGE_PAGE_DIGITIZER, lc, HID_USAGE_DIGITIZER_CONTACT_ID, report)
            width = self._value(info, HID_USAGE_PAGE_DIGITIZER, lc, HID_USAGE_DIGITIZER_WIDTH, report)
            height = self._value(info, HID_USAGE_PAGE_DIGITIZER, lc, HID_USAGE_DIGITIZER_HEIGHT, report)
            contacts.append(Contact(
                contact_id=cid if cid is not None else lc,
                x=float(x), y=float(y),
                tip=True,
                confidence=confident,
                width=float(width) if width is not None else None,
                height=float(height) if height is not None else None,
            ))
        # Fallback: if the button (Tip Switch) read is unsupported on this device,
        # gate on contact_count and non-zero coordinates instead so we still work.
        if not tip_button_seen and contact_count:
            contacts = self._fallback_contacts(info, link_collections, report, contact_count)
        if not contacts and not contact_count:
            return None
        now = time.perf_counter()
        if self._t0 is None:
            self._t0 = now
        frame = Frame(
            index=self._frame_index,
            scan_time=float(scan_time) if scan_time is not None else None,
            contacts=contacts,
            contact_count=contact_count,
            host_timestamp=now,
        )
        self._frame_index += 1
        return frame

    def _fallback_contacts(self, info, link_collections, report, contact_count) -> List[Contact]:
        """Used when Tip Switch can't be read as a button: keep the first
        ``contact_count`` slots that have non-zero coordinates."""
        out: List[Contact] = []
        for lc in link_collections:
            if len(out) >= contact_count:
                break
            x = self._value(info, HID_USAGE_PAGE_GENERIC, lc, HID_USAGE_GENERIC_X, report)
            y = self._value(info, HID_USAGE_PAGE_GENERIC, lc, HID_USAGE_GENERIC_Y, report)
            if x is None or y is None or (x == 0 and y == 0):
                continue
            cid = self._value(info, HID_USAGE_PAGE_DIGITIZER, lc, HID_USAGE_DIGITIZER_CONTACT_ID, report)
            out.append(Contact(contact_id=cid if cid is not None else lc,
                               x=float(x), y=float(y), tip=True, confidence=True))
        return out

    def _emit(self, frame: Frame) -> None:
        self.frames.append(frame)
        if self.on_frame:
            self.on_frame(frame)
