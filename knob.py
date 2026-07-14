"""Captures a keyboard's volume knob / media keys and drives an AudioEngine.

Works with any device that emits the standard Volume Up / Volume Down / Mute
media keys -- most keyboards with a volume knob (Keychron and others), standalone
USB volume dials, or plain keyboard media keys. Nothing here is brand-specific.

Why a raw global hook instead of on_press_key: the knob's media keys arrive
with negative scan codes (e.g. -175 for "volume up"), while keyboard's
key_to_scan_codes("volume up") returns 57392. on_press_key/hook_key filter by
scan code, so they NEVER match -> the handler never fires and Windows changes
its own volume. keyboard.hook() sees every event and lets us match by name and
suppress it (return False) so Windows stays out of the way.

    turn             -> adjust the selected effect's amount
    Shift + turn     -> switch effect (cycles engine.effects)
    single press     -> toggle bypass of all effects
"""

import keyboard
import numpy as np

STEP = 0.05

# Media-key names the knob emits (see knob_test.py output).
_VOLUME_KEYS = {"volume up", "volume down", "volume mute"}


class KnobController:
    def __init__(self, engine, on_change=None):
        self.engine = engine
        self.on_change = on_change or (lambda c: None)
        self.index = 0
        self._unhook = None

    # --- selected effect ----------------------------------------------
    def current(self):
        return self.engine.effects[self.index]

    @property
    def mode(self):
        return self.current().name

    def set_active(self, name):
        for i, e in enumerate(self.engine.effects):
            if e.name == name:
                self.index = i
                self.on_change(self)
                return

    # --- knob events ---------------------------------------------------
    def _turn(self, sign):
        if keyboard.is_pressed("shift"):
            self._switch_effect(sign)
        else:
            self._adjust(sign)

    def _adjust(self, sign):
        e = self.current()
        lo = -1.0 if e.bipolar else 0.0
        e.amount = float(np.clip(e.amount + sign * STEP, lo, 1.0))
        self.on_change(self)

    def _switch_effect(self, sign):
        step = 1 if sign > 0 else -1
        self.index = (self.index + step) % len(self.engine.effects)
        self.on_change(self)

    def _toggle_bypass(self):
        self.engine.bypass = not self.engine.bypass
        self.on_change(self)

    # --- global hook ---------------------------------------------------
    def _handle(self, e):
        """Return False to suppress (block Windows), True to let the key pass.

        Runs in the OS hook thread, so it must never raise: returning None/False
        for a non-volume key would block normal typing. We guard accordingly.
        """
        try:
            name = e.name
            if name not in _VOLUME_KEYS:
                return True                     # not ours -> pass through
            # In volume mode the knob IS the Windows volume: let the media key
            # reach Windows (native OSD + real level/mute) instead of driving an
            # effect. Shift still switches effect, so it stays suppressed then.
            if self.mode == "volume" and not keyboard.is_pressed("shift"):
                return True                     # pass through to Windows
            if e.event_type == keyboard.KEY_DOWN:
                if name == "volume up":
                    self._turn(+1)
                elif name == "volume down":
                    self._turn(-1)
                else:                           # volume mute
                    self._toggle_bypass()
        except Exception:
            return True                         # never block typing on an error
        return False                            # suppress the volume key

    def start(self):
        try:
            self._unhook = keyboard.hook(self._handle, suppress=True)
        except Exception as e:
            raise RuntimeError(
                "Could not install the keyboard hook. Try running as "
                "Administrator; also make sure the 'keyboard' package is installed."
            ) from e

    def stop(self):
        if self._unhook is not None:
            try:
                self._unhook()
            except Exception:
                pass
            self._unhook = None
