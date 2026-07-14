"""Run Rotor at logon via a Scheduled Task with highest privileges.

Why a scheduled task and not the Startup folder / Run key: the knob needs a
global keyboard hook with suppression, which requires elevation. A logon task
with "run with highest privileges" launches the app elevated without a UAC
prompt every time. Creating/removing the task itself needs admin -- which the
app already has when it's running (that's why the hook works).
"""

import os
import subprocess
import sys

TASK = "Rotor"
_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW, so schtasks doesn't flash


def _target():
    """Command line the task should run."""
    if getattr(sys, "frozen", False):            # packaged .exe
        return f'"{sys.executable}"'
    # running from source: prefer pythonw.exe (no console) + tray.py
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "tray.py"))
    return f'"{pyw}" "{script}"'


def _run(args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=_NO_WINDOW)


def is_enabled():
    return _run(["/Query", "/TN", TASK]).returncode == 0


def enable():
    """Create/replace the logon task. Returns True on success."""
    r = _run(["/Create", "/TN", TASK, "/TR", _target(),
              "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"])
    return r.returncode == 0


def disable():
    """Remove the task. Returns True on success (or if it didn't exist)."""
    return _run(["/Delete", "/TN", TASK, "/F"]).returncode == 0
