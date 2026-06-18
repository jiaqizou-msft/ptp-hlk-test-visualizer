"""Entry point for the standalone GUI executable (used by PyInstaller).

Building:  python build_exe.py
Running:   dist/PTPMetrics.exe
"""
from ptp_metrics.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
