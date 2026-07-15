"""Audio engine: owns the stream, the effects and device switching.

The stream runs on a separate PortAudio thread and calls _callback per block.
Effect parameters (filt.value, delay.mix) and `bypass` are plain attributes the
knob thread writes and the audio thread reads (single assignments are atomic
under the GIL). Devices can be swapped live via start(); the old stream is
closed first.
"""

import threading

import numpy as np
import sounddevice as sd

from effects import Volume, DJFilter, Distortion, Bitcrush, Delay, Reverb


def list_devices(api=None, want_input=None):
    """Return [(index, name, api_name)] filtered by host API and direction.

    want_input=True  -> only devices with input channels
    want_input=False -> only devices with output channels
    """
    out = []
    for i, d in enumerate(sd.query_devices()):
        api_name = sd.query_hostapis(d["hostapi"])["name"]
        if api and api.lower() not in api_name.lower():
            continue
        if want_input is True and d["max_input_channels"] <= 0:
            continue
        if want_input is False and d["max_output_channels"] <= 0:
            continue
        out.append((i, d["name"], api_name))
    return out


def resolve(name_sub, want_input, api=None):
    """First device index whose name contains name_sub (or None)."""
    for i, name, _api in list_devices(api, want_input):
        if name_sub.lower() in name.lower():
            return i
    return None


def device_name(idx):
    """Name of device `idx`, or None."""
    if idx is None:
        return None
    try:
        return sd.query_devices(idx)["name"]
    except Exception:
        return None


def default_devices(api=None):
    """(input_index, output_index) for the system's default devices.

    Machine-agnostic: if `api` is given, use that host API's own default in/out
    devices; otherwise fall back to sounddevice's global defaults. Either part
    is None if unavailable, so callers can fall back to the first device found.
    """
    if api:
        for ha in sd.query_hostapis():
            if api.lower() in ha["name"].lower():
                di, do = ha["default_input_device"], ha["default_output_device"]
                return (di if di is not None and di >= 0 else None,
                        do if do is not None and do >= 0 else None)
    try:
        di, do = sd.default.device
    except Exception:
        di, do = None, None
    norm = lambda x: x if isinstance(x, int) and x >= 0 else None
    return norm(di), norm(do)


class AudioEngine:
    def __init__(self, fs=48000, block=512):
        self.fs = fs
        self.block = block
        # Signal chain order: gain -> tone-shaping -> saturation -> lo-fi -> time fx.
        # Volume is first so it's the default selected effect (the knob starts here).
        self.effects = [
            Volume(fs),
            DJFilter(fs),
            Distortion(fs),
            Bitcrush(fs),
            Delay(fs),
            Reverb(fs),
        ]
        self.level = {"in": 0.0, "out": 0.0}
        self.bypass = False
        self.input = None
        self.output = None
        self.out_name = None                    # friendly name of the output device
        self.error = None
        self._stream = None
        # Background applier: the Volume effect mirrors the OUTPUT device's real
        # Windows volume. The knob (key-hook thread) only steps the target and
        # sets this event; the COM read/write happens here, off the hook thread.
        self._vol_event = threading.Event()
        self._vol_stop = threading.Event()
        self._vol_thread = None

    def effect(self, name):
        for e in self.effects:
            if e.name == name:
                return e
        return None

    # --- system volume mirror -----------------------------------------
    def request_volume_apply(self):
        """Ask the applier thread to push the Volume effect's target to the OS."""
        self._vol_event.set()

    def start_volume_sync(self):
        if self._vol_thread and self._vol_thread.is_alive():
            return
        self._vol_stop.clear()
        self._vol_thread = threading.Thread(
            target=self._volume_worker, name="rotor-volume", daemon=True)
        self._vol_thread.start()

    def stop_volume_sync(self):
        self._vol_stop.set()
        self._vol_event.set()                   # wake it so it can exit
        if self._vol_thread and self._vol_thread.is_alive():
            self._vol_thread.join(timeout=1.0)
        self._vol_thread = None

    def _volume_worker(self):
        """Push the Volume target to the output device when the knob changes it,
        and otherwise poll the device so external changes show in the app."""
        vol = self.effect("volume")
        if vol is None:
            return
        import winvol
        while not self._vol_stop.is_set():
            pushed = self._vol_event.wait(0.4)
            self._vol_event.clear()
            if self._vol_stop.is_set():
                break
            name = self.out_name
            if not name:
                continue
            if pushed:                          # knob moved -> write to the device
                winvol.set_device_mute(name, vol.muted)
                winvol.set_device_volume(name, vol.amount)
            else:                               # idle -> mirror the device's state
                level, muted = winvol.read_device(name)
                if level is not None:
                    vol.amount = level
                    vol._cur = level
                if muted is not None:
                    vol.muted = muted

    def warmup(self, blocks=10):
        """Prime the DSP off the audio thread: allocate every effect's buffers
        and run scipy/numpy code paths once, so the first seconds of real audio
        don't underrun (which sounds like crackle) while things JIT/allocate.
        Runs on silence with amounts at 0, so it changes no state."""
        x = np.zeros((self.block, 2), dtype=np.float32)
        for _ in range(blocks):
            y = x
            for e in self.effects:
                y = e.process(y)

    # --- audio thread --------------------------------------------------
    def _callback(self, indata, outdata, frames, time_info, status):
        self.level["in"] = float(np.sqrt(np.mean(indata**2)))
        if self.bypass:
            outdata[:] = indata
        else:
            y = indata
            for e in self.effects:
                y = e.process(y)
            outdata[:] = y
        self.level["out"] = float(np.sqrt(np.mean(outdata**2)))

    # --- lifecycle -----------------------------------------------------
    @property
    def running(self):
        return self._stream is not None

    def start(self, input, output):
        """(Re)open the stream on the given device indices. Returns True on
        success; on failure stores the message in self.error and returns False."""
        self.stop()
        try:
            self._stream = sd.Stream(
                samplerate=self.fs,
                blocksize=self.block,
                dtype="float32",
                channels=2,
                device=(input, output),
                callback=self._callback,
            )
            self._stream.start()
            self.input = input
            self.output = output
            self.out_name = device_name(output)
            self.error = None
            return True
        except Exception as e:
            self.error = str(e)
            self._stream = None
            return False

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
