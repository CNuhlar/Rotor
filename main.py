"""Rotor - console runner (debug / headless).

For the everyday app use tray.py (system-tray UI). This console version is
handy for debugging device/routing issues because it prints a live input meter.

    python main.py --list                 # list audio devices
    python main.py                         # default input/output
    python main.py --in 3 --out 5          # by device index
"""

import argparse
import sys

# Keep Unicode from crashing on a cp1252 Windows console:
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import sounddevice as sd

from engine import AudioEngine, resolve, default_devices
from knob import KnobController


def _meter(rms, width=12):
    """rms (0..~1) -> small ASCII bar so you can see input level at a glance."""
    import math
    if rms <= 1e-5:
        n = 0
    else:
        db = 20.0 * math.log10(rms)          # ~-60dB..0dB window
        n = max(0, min(width, int(round((db + 60.0) / 60.0 * width))))
    return "#" * n + "-" * (width - n)


def print_status(engine, knob):
    if knob.mode == "volume":
        engine.sync_system_volume()
    if engine.bypass:
        line = "  [BYPASS] "
    else:
        line = " "
    parts = []
    for e in engine.effects:
        star = "*" if e.name == knob.mode else " "
        parts.append(f"{star}{e.name}:{e.desc()}")
    line += " ".join(parts)
    line += f" | in[{_meter(engine.level['in'])}]"
    sys.stdout.write("\r" + line.ljust(90))
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list audio devices")
    ap.add_argument("--in", dest="input", type=int, default=None)
    ap.add_argument("--out", dest="output", type=int, default=None)
    ap.add_argument("--in-name", dest="in_name", default=None,
                    help="pick input device by name substring (optional override)")
    ap.add_argument("--out-name", dest="out_name", default=None,
                    help="pick output device by name substring (optional override)")
    ap.add_argument("--api", default="DirectSound",
                    help="host API filter; DirectSound = shared mode, won't lock devices")
    ap.add_argument("--sr", type=int, default=48000)
    ap.add_argument("--block", type=int, default=512)
    args = ap.parse_args()

    if args.list:
        print(sd.query_devices())
        return

    # Resolve devices: explicit index > name substring > system default.
    def_in, def_out = default_devices(args.api)
    if args.input is None:
        args.input = resolve(args.in_name, True, args.api) if args.in_name else def_in
    if args.output is None:
        args.output = resolve(args.out_name, False, args.api) if args.out_name else def_out
    if args.input is None or args.output is None:
        raise SystemExit("Could not resolve input/output device. Use --list then --in/--out.")

    engine = AudioEngine(fs=args.sr, block=args.block)
    knob = KnobController(engine, on_change=lambda k: print_status(engine, k))

    knob.start()
    if not engine.start(args.input, args.output):
        print(f"Error opening stream: {engine.error}")
        print("Hint: run 'python main.py --list' to see devices, then pass --in/--out.")
        return

    print("Rotor running.  Press Ctrl+C to quit.")
    print("  turn = selected effect | Shift+turn = switch effect | press = bypass")
    print("  if the in[####----] bar never moves, no audio is reaching the program")
    print("  -> set Windows > Sound > Output = 'CABLE Input'.\n")
    print_status(engine, knob)

    try:
        while True:
            sd.sleep(120)
            print_status(engine, knob)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        knob.stop()
        engine.stop()


if __name__ == "__main__":
    main()
