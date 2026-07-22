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
    # mean L2 distance from the initial contact point: same order as the noise,
    # positive, and below the peak-to-peak spread.
    assert jit.worst_mean_dist_from_init_mm is not None
    assert jit.worst_mean_dist_from_init_mm > 0
    assert jit.worst_mean_dist_from_init_mm <= (jit.worst_p2p_mm or 1e9)
    for seg in jit.per_segment:
        assert seg.mean_dist_from_init_mm is not None
        assert seg.mean_dist_from_init_counts >= 0


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


def test_continuity_clean_swipe_has_no_dropouts():
    # a normal continuous drag should report zero break-ups
    rec = synth_recording(seed=5)
    cont = M.continuity_metrics(rec)
    assert cont.dropout_count == 0
    assert cont.contacts_analyzed >= 1


def test_continuity_detects_swipe_dropouts():
    # fast swipe with three omitted reports mid-drag -> three break-ups
    rec = synth_recording(drag_ms=300.0, drag_len_mm=90.0, jitter_mm=0.01,
                          seed=6, drop_drag_frames=(10, 20, 21))
    cont = M.continuity_metrics(rec)
    # frames 20 & 21 are adjacent -> a single 2-frame gap; frame 10 -> another.
    assert cont.dropout_count == 2, f"expected 2 events, got {cont.dropout_count}"
    assert cont.max_missing_frames >= 2
    for ev in cont.events:
        assert ev.missing_frames >= 1
        assert ev.step_before_mm is not None and ev.step_before_mm > 0
    # exposed through the full report + dict
    report = M.compute_all(rec)
    assert report.continuity.dropout_count == 2
    assert report.to_dict()["continuity"]["dropout_count"] == 2


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
