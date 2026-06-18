"""Headless smoke test for the new high-performance GUI.

Builds the app, injects a synthetic recording as a loaded source, drives several
render ticks (incremental canvas drawing), forces a metrics update, exercises the
streaming logger round-trip and the spec evaluation — all without a touchpad and
without entering the Tk mainloop.

Run:  python tests/test_gui_smoke.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use("Agg")


def main():
    try:
        import tkinter  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: tkinter unavailable: {e}")
        return 0

    from ptp_metrics.synth import synth_recording
    from ptp_metrics import gui as gmod
    from ptp_metrics import spec as SPEC

    try:
        app = gmod.PTPMetricsApp()
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: cannot create Tk root: {e}")
        return 0

    app.withdraw()
    rec = synth_recording()
    app._loaded = rec

    # give the canvas a concrete size (no window is mapped in headless mode, so
    # the <Configure> event that normally sets this never fires)
    app.view._cw, app.view._ch = 800, 400
    app.update_idletasks()
    for _ in range(6):
        app._render_new_frames()
        app.update_idletasks()
    assert app._cursor >= len(rec.frames), f"cursor {app._cursor} < {len(rec.frames)}"
    assert app.view._all_line_ids, "no stroke lines drawn"
    print(f"PASS incremental render: {len(app.view._all_line_ids)} stroke(s), "
          f"cursor={app._cursor}")

    # metrics + spec
    app._update_metrics_and_charts()
    assert app._last_report is not None
    ev = SPEC.evaluate(rec, app._last_report)
    names = {c.name for c in ev.checks}
    assert "Stationary Jitter" in names and "Linearity" in names
    print(f"PASS spec eval: overall={ev.overall}, "
          + ", ".join(f"{c.name}={c.status}" for c in ev.checks))

    # streaming logger round-trip (csv + jsonl)
    from ptp_metrics.logger import StreamLogger
    from ptp_metrics.loaders import load_csv
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "stream.csv")
        lg = StreamLogger(p)
        lg.start()
        for f in rec.frames:
            lg.write(f)
        lg.stop()
        assert lg.frames_written == len(rec.frames), \
            f"{lg.frames_written} != {len(rec.frames)}"
        rec2 = load_csv(p, rec.device)
        assert len(rec2) == len(rec)
        print(f"PASS stream logger: {lg.frames_written} frames, "
              f"{lg.rows_written} rows, reload {len(rec2)}")

        pj = os.path.join(d, "stream.jsonl")
        lg2 = StreamLogger(pj)
        lg2.start()
        for f in rec.frames:
            lg2.write(f)
        lg2.stop()
        rec3 = app._load_jsonl(pj, rec.device)
        assert len(rec3) == len(rec)
        print(f"PASS jsonl logger round-trip: {len(rec3)} frames")

    # clear should empty the view
    app.clear_data()
    assert not app.view._all_line_ids and app._cursor == 0
    print("PASS clear empties view + cursor")

    app._on_close()
    print("\nGUI smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
