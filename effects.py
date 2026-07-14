"""Real-time DJ effects with click-free (de-zippered) parameter changes.

Every effect processes audio block by block (numpy array, shape = (frames,
channels)) and keeps its state across calls. A single knob-controlled parameter
`amount` drives each effect; the knob writes `amount` (the target) and the audio
thread smooths toward it inside process() so turning the knob never zippers:

    - gain-style params (delay/reverb/crush/drive mix) ramp per-sample across
      the block via Effect._ramp(),
    - the filter smooths its cutoff one-pole per block and always runs the
      biquad (no hard bypass), so sweeping through the centre is seamless.

`amount` range is 0..1, except effects with `bipolar = True` (the filter) which
use -1..1. The knob clips to the right range.
"""

import numpy as np
from scipy.signal import lfilter


class Effect:
    name = "effect"
    short = "FX"
    bipolar = False
    color = "#8888aa"

    def __init__(self, fs):
        self.fs = fs
        self.amount = 0.0          # target, knob writes here
        self._cur = 0.0            # smoothed current value

    def _ramp(self, n):
        """Per-sample linear ramp (n,1) from _cur to amount; advances _cur."""
        r = np.linspace(self._cur, self.amount, n, dtype=np.float32)
        self._cur = self.amount
        return r.reshape(n, 1)

    def desc(self):
        return f"{int(round(self.amount * 100))}%"

    def process(self, x):
        return x


class Volume(Effect):
    """The Windows master volume, mirrored. When 'volume' is the active effect the
    knob's media keys pass straight through to Windows (native OSD, real level
    change) and press = Windows mute; the engine reads the resulting level/mute
    back into `amount`/`muted` so the app shows the same value. Audio is passed
    through untouched here -- the OS endpoint does the actual attenuation, so
    there's no double-scaling.
    """

    name = "volume"
    short = "VOL"
    color = "#ffd24a"

    def __init__(self, fs):
        super().__init__(fs)
        self.amount = 1.0                     # mirrors Windows level (0..1)
        self._cur = 1.0
        self.muted = False                    # mirrors Windows mute

    def desc(self):
        if self.muted:
            return "muted"
        return f"{int(round(self.amount * 100))}%"

    def process(self, x):
        return x                              # OS endpoint controls the level


class DJFilter(Effect):
    """Single-knob low-pass <-> (open in the middle) <-> high-pass sweep.

    amount:  0 -> open, -1..0 -> low-pass (darker), 0..1 -> high-pass (thinner).
    Cutoff is smoothed per block and the biquad always runs, so the sweep is
    click-free even through the centre.
    """

    name = "filter"
    short = "FLT"
    bipolar = True
    color = "#50aaff"
    F_MIN = 30.0
    F_MAX = 18000.0
    SMOOTH = 0.35                  # per-block one-pole toward target

    def __init__(self, fs, q=0.9):
        super().__init__(fs)
        self.q = q
        self._zi = None

    def desc(self):
        v = self.amount
        if abs(v) < 0.02:
            return "open"
        return f"LP {int(-v * 100)}%" if v < 0 else f"HP {int(v * 100)}%"

    def _map_freq(self, x):
        return self.F_MIN * (self.F_MAX / self.F_MIN) ** float(np.clip(x, 0.0, 1.0))

    def _coeffs(self, v):
        if v < 0.0:                               # low-pass
            f0 = self._map_freq(1.0 + v)          # v=-1 -> 30Hz, v~0 -> 18k (open)
            lp = True
        else:                                     # high-pass
            f0 = self._map_freq(v)                # v~0 -> 30Hz (open), v=1 -> 18k
            lp = False
        w0 = 2.0 * np.pi * f0 / self.fs
        c = np.cos(w0)
        alpha = np.sin(w0) / (2.0 * self.q)
        if lp:
            b0, b1, b2 = (1 - c) / 2, 1 - c, (1 - c) / 2
        else:
            b0, b1, b2 = (1 + c) / 2, -(1 + c), (1 + c) / 2
        a0, a1, a2 = 1 + alpha, -2 * c, 1 - alpha
        return np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0])

    def process(self, x):
        self._cur += (self.amount - self._cur) * self.SMOOTH
        n, ch = x.shape
        b, a = self._coeffs(self._cur)
        if self._zi is None or self._zi.shape[1] != ch:
            self._zi = np.zeros((2, ch), dtype=np.float64)
        y = np.empty_like(x)
        for c in range(ch):
            y[:, c], self._zi[:, c] = lfilter(b, a, x[:, c], zi=self._zi[:, c])
        return y.astype(np.float32)


class Distortion(Effect):
    """Soft-clip drive. amount 0 = clean, up = more saturation (tanh)."""

    name = "drive"
    short = "DRV"
    color = "#59d98e"

    def process(self, x):
        g = 1.0 + self.amount * 9.0
        wet = np.tanh(x * g)
        if g > 1.0:
            wet = wet / np.tanh(g)                # makeup so hot region ~ unity
        a = self._ramp(x.shape[0])
        return (x * (1.0 - a) + wet * a).astype(np.float32)


class Bitcrush(Effect):
    """Lo-fi: bit-depth reduction + sample-and-hold downsampling.
    amount 0 = off, up = fewer bits and lower rate."""

    name = "crush"
    short = "CRU"
    color = "#ff5da2"

    def process(self, x):
        amt = self.amount
        if amt < 0.001 and self._cur < 0.001:
            return x
        n = x.shape[0]
        k = int(round(1 + amt * 15))              # sample-hold factor
        held = x[(np.arange(n) // k) * k]
        bits = 16.0 - amt * 13.0                  # 16 -> 3 bits
        step = 2.0 / (2.0 ** bits)
        q = np.round(held / step) * step
        a = self._ramp(n)
        return (x * (1.0 - a) + q * a).astype(np.float32)


class Delay(Effect):
    """Stereo echo. amount = wet mix (0..1); the buffer is always fed so echo
    starts the instant you turn it up. Wet mix ramps per-sample (click-free)."""

    name = "delay"
    short = "DLY"
    color = "#ff9640"

    def __init__(self, fs, time_s=0.375, max_s=2.0, feedback=0.4):
        super().__init__(fs)
        self.delay = int(time_s * fs)
        self.n = int(max_s * fs)
        self.feedback = feedback
        self._buf = None
        self._w = 0

    def desc(self):
        return f"{int(self.amount * 100)}% wet"

    def process(self, x):
        n, ch = x.shape
        if self._buf is None or self._buf.shape[1] != ch:
            self._buf = np.zeros((self.n, ch), dtype=np.float32)
            self._w = 0
        w = self._w
        write_idx = (w + np.arange(n)) % self.n
        read_idx = (w - self.delay + np.arange(n)) % self.n
        delayed = self._buf[read_idx]
        a = self._ramp(n)
        out = x + a * delayed
        self._buf[write_idx] = x + self.feedback * delayed
        self._w = (w + n) % self.n
        return np.clip(out, -1.0, 1.0).astype(np.float32)


class Reverb(Effect):
    """Freeverb-lite: parallel damped comb filters + series allpass.

    All delay lengths are > a typical block (512), so each block is a plain
    circular-buffer read/write with no intra-block recursion (vectorized).
    amount = wet mix (0..1), ramped per-sample."""

    name = "reverb"
    short = "RVB"
    color = "#7d7bff"
    COMBS = [1557, 1617, 1491, 1422, 1277, 1356, 1188, 1116]
    ALLPASS = [556, 673]                          # kept > block size on purpose
    WET_GAIN = 0.6

    def __init__(self, fs, feedback=0.84, damp=0.2):
        super().__init__(fs)
        s = fs / 44100.0
        self._cd = [max(1, int(d * s)) for d in self.COMBS]
        self._ad = [max(1, int(d * s)) for d in self.ALLPASS]
        self.feedback = feedback
        self.damp = damp
        self._ch = 0

    def desc(self):
        return f"{int(self.amount * 100)}% wet"

    def _setup(self, ch):
        self._cbuf = [np.zeros((d, ch), dtype=np.float32) for d in self._cd]
        self._cw = [0] * len(self._cd)
        self._czi = [np.zeros((1, ch), dtype=np.float64) for _ in self._cd]
        self._abuf = [np.zeros((d, ch), dtype=np.float32) for d in self._ad]
        self._aw = [0] * len(self._ad)
        self._ch = ch

    def process(self, x):
        n, ch = x.shape
        if self._ch != ch:
            self._setup(ch)
        acc = np.zeros((n, ch), dtype=np.float32)
        blp, alp = [1.0 - self.damp], [1.0, -self.damp]
        for i, D in enumerate(self._cd):
            buf, w = self._cbuf[i], self._cw[i]
            idx = (w + np.arange(n)) % D
            out = buf[idx]                        # values from D samples ago
            filt = np.empty_like(out)
            for c in range(ch):
                filt[:, c], self._czi[i][:, c] = lfilter(
                    blp, alp, out[:, c], zi=self._czi[i][:, c])
            buf[idx] = x + filt * self.feedback
            self._cw[i] = (w + n) % D
            acc += out
        y = acc * (1.0 / len(self._cd))
        for i, D in enumerate(self._ad):
            buf, w = self._abuf[i], self._aw[i]
            idx = (w + np.arange(n)) % D
            bufout = buf[idx]
            outp = -y + bufout
            buf[idx] = y + bufout * 0.5
            self._aw[i] = (w + n) % D
            y = outp
        wet = y * self.WET_GAIN
        a = self._ramp(n)
        return (x * (1.0 - a) + wet * a).astype(np.float32)
