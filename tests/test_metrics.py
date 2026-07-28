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


def test_digiinfo_xml_loads():
    from ptp_metrics.loaders import load_digiinfo_xml, is_digiinfo_xml
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<inputmanager version="1.0" source="DigiInfo">\n'
        '  <digitizers>\n'
        '    <digitizer id="1" name="Test PTP" maxcsrs="5">\n'
        '      <properties>\n'
        '        <property name="x" logmin="0" logmax="32767" res="1192.83" unit="cm" />\n'
        '        <property name="y" logmin="0" logmax="32767" res="1789.57" unit="cm" />\n'
        '        <property name="contactid" logmin="0" logmax="1" res="0" unit="cm" />\n'
        '      </properties>\n'
        '    </digitizer>\n'
        '  </digitizers>\n'
        '  <events>\n'
        '    <packet x="1000" y="1200" down="true" confidence="true" contactid="0" '
        'scantime="100" time="1000" width="120" height="130" />\n'
        '    <packet x="1002" y="1201" down="true" confidence="true" contactid="0" '
        'scantime="170" time="1007" width="120" height="130" />\n'
        '    <packet x="1001" y="1203" down="true" confidence="true" contactid="0" '
        'scantime="240" time="1014" width="122" height="131" />\n'
        '  </events>\n'
        '</inputmanager>\n'
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sample.xml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(xml)
        assert is_digiinfo_xml(p)
        rec = load_digiinfo_xml(p)
        assert len(rec.frames) == 3
        assert rec.device.width_mm and rec.device.width_mm > 0
        assert rec.device.x_counts_per_mm is not None
        c0 = rec.frames[0].contacts[0]
        assert c0.width == 120 and c0.height == 130
        report = M.compute_all(rec)
        assert report.timing.report_rate_hz is not None


def test_gesture_recognition():
    from ptp_metrics import gestures as GEST
    from ptp_metrics.models import Contact, DeviceInfo, Frame, Recording

    dev = DeviceInfo(name="G", x_logical_min=0, x_logical_max=10000,
                     y_logical_min=0, y_logical_max=6000,
                     x_physical_mm=100.0, y_physical_mm=60.0)
    cpm = dev.x_counts_per_mm  # 100 counts/mm

    def make(frames_xy_by_cid, dt=0.007):
        frames = []
        for i, contacts in enumerate(frames_xy_by_cid):
            cs = [Contact(contact_id=cid, x=x, y=y, tip=True)
                  for cid, (x, y) in contacts.items()]
            frames.append(Frame(index=i, scan_time=None, contacts=cs,
                                contact_count=len(cs), host_timestamp=i * dt))
        return Recording(device=dev, frames=frames, source="synthetic")

    tfn = lambda f: f.host_timestamp

    # 1-finger swipe right: x grows by 40 mm
    seq = [{0: (1000 + int(k * 40 * cpm / 30), 3000)} for k in range(31)]
    g = GEST.recognize(make(seq).frames, dev, time_fn=tfn)
    assert g.name == "Swipe right", g.name
    assert g.n_fingers == 1

    # 1-finger swipe up: y decreases
    seq = [{0: (3000, 3000 - int(k * 40 * cpm / 30))} for k in range(31)]
    g = GEST.recognize(make(seq).frames, dev, time_fn=tfn)
    assert g.name == "Swipe up", g.name

    # two-finger pan down (geometry, count from HID Contact Count = 2)
    seq = [{0: (2000, 2000 + int(k * 30 * cpm / 30)),
            1: (2600, 2000 + int(k * 30 * cpm / 30))} for k in range(31)]
    g = GEST.recognize(make(seq).frames, dev, time_fn=tfn)
    assert g.name == "Two-finger pan down", g.name
    assert g.n_fingers == 2 and g.source == "HID"

    # OS-recognized scroll: a wheel event from Raw Input drives the label,
    # regardless of geometry (this is hidclass -> PTP driver -> wheel).
    seq = [{0: (3000, 3000), 1: (3600, 3000)} for _ in range(10)]
    frames = make(seq).frames
    wheel = [(frames[-1].host_timestamp - 0.05, 0.0, 3.0)]  # dy>0 -> scroll down
    g = GEST.recognize(frames, dev, time_fn=tfn, wheel_events=wheel)
    assert g.name == "Two-finger scroll down", g.name
    assert g.source == "OS wheel"

    # pinch zoom in: two fingers separate
    seq = [{0: (3000 - int(k * 20 * cpm / 30), 3000),
            1: (3000 + int(k * 20 * cpm / 30), 3000)} for k in range(31)]
    g = GEST.recognize(make(seq).frames, dev, time_fn=tfn)
    assert g.name == "Pinch zoom in", g.name

    # three-finger swipe left
    seq = [{0: (4000 - int(k * 30 * cpm / 30), 2000),
            1: (4000 - int(k * 30 * cpm / 30), 2600),
            2: (4000 - int(k * 30 * cpm / 30), 3200)} for k in range(31)]
    g = GEST.recognize(make(seq).frames, dev, time_fn=tfn)
    assert g.name == "3-finger swipe left", g.name

    # tap: single brief stationary contact
    seq = [{0: (3000, 3000)} for _ in range(3)]
    g = GEST.recognize(make(seq).frames, dev, time_fn=tfn)
    assert g.name == "Tap", g.name

    # press & hold: single stationary contact, sustained
    seq = [{0: (3000, 3000)} for _ in range(80)]
    g = GEST.recognize(make(seq).frames, dev, window_s=2.0, time_fn=tfn)
    assert g.name == "Press & hold", g.name


def test_palm_rejection():
    from ptp_metrics import gestures as GEST
    from ptp_metrics.models import Contact, DeviceInfo, Frame

    dev = DeviceInfo(name="P", x_logical_min=0, x_logical_max=10000,
                     y_logical_min=0, y_logical_max=6000,
                     x_physical_mm=100.0, y_physical_mm=60.0)
    tfn = lambda f: f.host_timestamp

    def palm_frames(n=15):
        # low-confidence (device-flagged palm) contact present each frame
        return [Frame(index=i, scan_time=None,
                      contacts=[Contact(0, 3000 + i, 3000, tip=True,
                                        confidence=False)],
                      contact_count=1, host_timestamp=i * 0.007)
                for i in range(n)]

    frames = palm_frames()
    t_last = frames[-1].host_timestamp

    # successful rejection: palm present, OS cursor did NOT move (empty list)
    g = GEST.recognize(frames, dev, time_fn=tfn, cursor_events=[])
    assert g.name == "Palm rejected", g.name
    assert g.source == "HID+OS"

    # failed rejection: palm present AND cursor moved
    g = GEST.recognize(frames, dev, time_fn=tfn,
                       cursor_events=[(t_last - 0.05, 12.0, 4.0)])
    assert g.name == "Palm NOT rejected", g.name

    # offline (no OS cursor data): report detection only
    g = GEST.recognize(frames, dev, time_fn=tfn, cursor_events=None)
    assert g.name == "Palm detected", g.name

    # large contact footprint also counts as a palm (confident but big W/H)
    big = [Frame(index=i, scan_time=None,
                 contacts=[Contact(0, 3000, 3000, tip=True, confidence=True,
                                   width=2000, height=2000)],
                 contact_count=1, host_timestamp=i * 0.007) for i in range(12)]
    g = GEST.recognize(big, dev, time_fn=tfn, cursor_events=[])
    assert g.name == "Palm rejected", g.name

    # a normal confident finger is NOT a palm even if the cursor moves
    fing = [Frame(index=i, scan_time=None,
                  contacts=[Contact(0, 3000 + i * 40, 3000, tip=True,
                                    confidence=True, width=200, height=200)],
                  contact_count=1, host_timestamp=i * 0.007) for i in range(20)]
    g = GEST.recognize(fing, dev, time_fn=tfn,
                       cursor_events=[(fing[-1].host_timestamp, 40.0, 0.0)])
    assert g.name.startswith("Swipe"), g.name


def test_logger_includes_gesture():
    from ptp_metrics.logger import StreamLogger
    from ptp_metrics.models import Contact, DeviceInfo, Frame, Recording
    import json as _json

    dev = DeviceInfo(name="L")
    frames = [
        Frame(index=0, scan_time=100, contacts=[Contact(0, 500, 300)],
              contact_count=1, host_timestamp=0.0, gesture="Swipe right"),
        Frame(index=1, scan_time=170, contacts=[Contact(0, 520, 300)],
              contact_count=1, host_timestamp=0.007, gesture="Swipe right"),
    ]
    rec = Recording(device=dev, frames=frames, source="synthetic")

    with tempfile.TemporaryDirectory() as d:
        pcsv = os.path.join(d, "g.csv")
        lg = StreamLogger(pcsv)
        lg.start()
        for f in rec.frames:
            lg.write(f)
        lg.stop()
        with open(pcsv, encoding="utf-8") as fh:
            head = fh.readline().strip()
            first = fh.readline().strip()
        assert head.endswith("Gesture"), head
        assert first.endswith("Swipe right"), first

        pj = os.path.join(d, "g.jsonl")
        lg2 = StreamLogger(pj)
        lg2.start()
        for f in rec.frames:
            lg2.write(f)
        lg2.stop()
        with open(pj, encoding="utf-8") as fh:
            obj = _json.loads(fh.readline())
        assert obj.get("gesture") == "Swipe right", obj


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
