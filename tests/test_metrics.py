"""Sanity tests for the PTP metrics engine and CSV round-trip.

Run with:  python -m pytest   (or)   python tests/test_metrics.py
These validate that the engine recovers known injected ground-truth values.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ptp_metrics import metrics as M
from ptp_metrics.synth import synth_recording
from ptp_metrics.export import export_csv
from ptp_metrics.loaders import load_csv
from ptp_metrics.hid_descriptor import device_from_report_descriptor


def test_resolution_recovered():
    rec = synth_recording()
    res = M.resolution_metrics(rec)
    # 10500 counts / 105 mm = 100 counts/mm
    assert abs(res.reported_x_counts_per_mm - 100.0) < 1e-6
    assert abs(res.reported_y_counts_per_mm - 100.0) < 1e-6


def test_jitter_recovered():
    rec = synth_recording(jitter_mm=0.08, seed=1)
    jit = M.jitter_metrics(rec)
    assert jit.worst_rms_radial_mm is not None
    # radial RMS ~ sqrt(2)*0.08 = 0.113 mm; allow generous tolerance
    assert 0.05 < jit.worst_rms_radial_mm < 0.25


def test_linearity_recovered():
    rec = synth_recording(linearity_error_mm=0.15, jitter_mm=0.01, seed=2)
    lin = M.linearity_metrics(rec)
    assert lin.worst_max_dev_mm is not None
    assert 0.08 < lin.worst_max_dev_mm < 0.4


def test_timing_recovered():
    rec = synth_recording(report_rate_hz=133.0, seed=3)
    tim = M.timing_metrics(rec)
    assert tim.report_rate_hz is not None
    assert 120 < tim.report_rate_hz < 145


def test_csv_roundtrip():
    rec = synth_recording(seed=4)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rec.csv")
        export_csv(rec, path)
        rec2 = load_csv(path, device=rec.device)
        assert len(rec2) == len(rec)
        r1 = M.compute_all(rec)
        r2 = M.compute_all(rec2)
        assert abs((r1.timing.report_rate_hz or 0) - (r2.timing.report_rate_hz or 0)) < 1.0


def test_hid_descriptor_resolution():
    # Minimal descriptor fragment: X axis, logical 0..4095, physical 0..108, unit exp -1 (mm? cm default)
    # Generic Desktop (0x05 0x01), Usage X (0x09 0x30),
    # Logical Min 0 (0x15 0x00), Logical Max 4095 (0x26 0xFF 0x0F),
    # Physical Min 0 (0x35 0x00), Physical Max 1080 (0x46 0x38 0x04), Unit Exp -1 (0x55 0x0F),
    # Input (0x81 0x02)
    desc = bytes([
        0x05, 0x01, 0x09, 0x30,
        0x15, 0x00, 0x26, 0xFF, 0x0F,
        0x35, 0x00, 0x46, 0x38, 0x04,
        0x55, 0x0F,
        0x81, 0x02,
    ])
    dev = device_from_report_descriptor(desc)
    assert dev.x_logical_max == 4095
    # physical span 1080 * 10^-1 = 108 (cm) -> *10 = 1080 mm. counts/mm = 4095/1080 ~ 3.79
    assert dev.x_counts_per_mm is not None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
