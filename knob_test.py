"""Diagnostic: log every key event so we can see what your volume knob emits.

Run as Administrator, then turn the knob left/right and press it (mute).
Each event prints its name + scan code. We use those names in knob.py.

    python knob_test.py      # Ctrl+C to stop
"""

import sys
import keyboard

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("Knob diagnostic running. Turn the knob and press it. Ctrl+C to stop.\n")
print(f"{'event':<8} {'name':<20} {'scan_code':<10} is_keypad")
print("-" * 50)


def on_event(e):
    print(f"{e.event_type:<8} {str(e.name):<20} {str(e.scan_code):<10} {e.is_keypad}")


keyboard.hook(on_event)

try:
    keyboard.wait()  # blocks until Ctrl+C
except KeyboardInterrupt:
    print("\nstopped.")
