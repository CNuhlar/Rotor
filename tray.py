"""Rotor - system-tray app with a live custom panel.

The tray ICON shows the ACTIVE effect: its short code (VOL/FLT/DRV/CRU/DLY/RVB) over
a live bar of that effect's amount (Task-Manager-CPU style), colored per effect.
In bypass the bar is dimmed but still visible, so you can pre-set a value before
un-bypassing. Left- OR right-clicking opens a small always-on-top panel that
updates LIVE while open (a native tray menu can't): one row per effect with a
live bar + exact value (click a name to make it active), bypass, and device
pickers.

Run without a console window:
    .\\.venv\\Scripts\\pythonw.exe tray.py
"""

import ctypes
from ctypes import wintypes
import queue
import sys
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageColor, ImageDraw, ImageFont
from pystray import Icon
from pystray._win32 import win32

import autostart
import config
from engine import AudioEngine, list_devices, default_devices, resolve, device_name
from knob import KnobController

API = "DirectSound"          # shared-mode, won't lock devices (see README)

# --- panel palette ----------------------------------------------------
BG = "#1e1e24"
FG = "#e6e6eb"
MUTED = "#9a9aa5"
TRACK = "#33333d"
BYPASS = "#d25050"

# icon base colors (RGBA)
C_TRACK = (38, 38, 46, 210)
C_BORDER = (90, 90, 100, 255)
C_MID = (255, 255, 255, 120)
C_BYPASS = (210, 80, 80, 255)


# ======================================================================
# Monitor work area (so the panel never lands under the taskbar)
# ======================================================================
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _work_area_at(x, y):
    """Work area (l, t, r, b) of the monitor containing point (x, y): the
    monitor's rectangle minus its taskbar, whatever edge the taskbar is on.
    Multi-monitor aware, so the panel lands on the screen you clicked."""
    MONITOR_DEFAULTTONEAREST = 2
    pt = wintypes.POINT(int(x), int(y))
    hmon = ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if hmon and ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    rect = wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return (0, 0,
            ctypes.windll.user32.GetSystemMetrics(0),
            ctypes.windll.user32.GetSystemMetrics(1))


# ======================================================================
# Tray icon image
# ======================================================================
_FONT_CACHE = {}


def _font(size):
    if size not in _FONT_CACHE:
        f = None
        for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except Exception:
                continue
        _FONT_CACHE[size] = f or ImageFont.load_default()
    return _FONT_CACHE[size]


def render_icon(engine, effect):
    """Draw the tray icon at 128x128; Windows downscales it to tray size.
    Shows the active effect's short code + a live bar of its amount."""
    S = 128
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 12
    x0, y0, x1, y1 = pad, pad, S - pad, S - pad
    d.rounded_rectangle([x0, y0, x1, y1], radius=20, fill=C_TRACK, outline=C_BORDER, width=4)

    if engine.error is not None:
        d.line([x0 + 8, y0 + 8, x1 - 8, y1 - 8], fill=C_BYPASS, width=12)
        d.line([x0 + 8, y1 - 8, x1 - 8, y0 + 8], fill=C_BYPASS, width=12)
        return img

    dim = engine.bypass or getattr(effect, "muted", False)
    r, g, b = ImageColor.getrgb(effect.color)
    bar_alpha = 90 if dim else 255
    fill = (r, g, b, bar_alpha)
    ix0, ix1 = x0 + 8, x1 - 8

    if effect.bipolar:
        cy = (y0 + y1) / 2.0
        half = (y1 - y0) / 2.0 - 10
        h = abs(effect.amount) * half
        if effect.amount >= 0:                       # high-pass -> up
            d.rectangle([ix0, cy - h, ix1, cy], fill=fill)
        else:                                        # low-pass -> down
            d.rectangle([ix0, cy, ix1, cy + h], fill=fill)
        d.line([x0 + 4, cy, x1 - 4, cy], fill=C_MID, width=3)
    else:
        h = effect.amount * (y1 - y0 - 16)
        d.rectangle([ix0, (y1 - 8) - h, ix1, y1 - 8], fill=fill)

    # active effect short code, bold, top-centre, outlined for legibility
    text = effect.short
    f = _font(40)
    tb = d.textbbox((0, 0), text, font=f)
    tw = tb[2] - tb[0]
    tx = (S - tw) / 2 - tb[0]
    ty = pad + 4 - tb[1]
    for ox, oy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-3, -3), (3, 3), (-3, 3), (3, -3)):
        d.text((tx + ox, ty + oy), text, font=f, fill=(0, 0, 0, 230))
    d.text((tx, ty), text, font=f, fill=(255, 255, 255, 150 if dim else 255))

    if dim:                                          # bypass marker (bar stays visible)
        d.ellipse([S - 34, 8, S - 8, 34], fill=C_BYPASS)
    return img


# ======================================================================
# Tray icon that opens our panel on BOTH left and right click
# ======================================================================
class ClickIcon(Icon):
    on_click = None

    def _on_notify(self, wparam, lparam):
        if lparam in (win32.WM_LBUTTONUP, win32.WM_RBUTTONUP):
            if self.on_click is not None:
                self.on_click()


# ======================================================================
# Live panel (tkinter)
# ======================================================================
class Panel:
    W = 300

    def __init__(self, root, app):
        self.app = app
        self.engine = app.engine
        self.knob = app.knob
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=BG)
        self.top.bind("<Escape>", lambda e: self.hide())

        self.in_devs = list_devices(API, True)
        self.out_devs = list_devices(API, False)
        self.rows = []
        self._build()

    # --- build widgets -------------------------------------------------
    def _build(self):
        pad = 12
        wrap = tk.Frame(self.top, bg=BG)
        wrap.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x", padx=pad, pady=(pad, 6))
        tk.Label(head, text="Rotor", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        close = tk.Label(head, text="✕", bg=BG, fg=MUTED, font=("Segoe UI", 11), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.hide())

        # one row per effect: [name] [bar] [value]
        for e in self.engine.effects:
            self.rows.append(self._effect_row(wrap, e, pad))

        self._sep(wrap, pad)

        # bypass
        brow = tk.Frame(wrap, bg=BG)
        brow.pack(fill="x", padx=pad, pady=2)
        tk.Label(brow, text="output", bg=BG, fg=MUTED, width=7, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        self.btn_bypass = tk.Label(brow, text="BYPASS", bg=TRACK, fg=FG, padx=10, pady=2,
                                   font=("Segoe UI", 9), cursor="hand2")
        self.btn_bypass.pack(side="left", padx=(6, 0))
        self.btn_bypass.bind("<Button-1>", lambda e: self.app.toggle_bypass())

        # run at logon
        arow = tk.Frame(wrap, bg=BG)
        arow.pack(fill="x", padx=pad, pady=2)
        tk.Label(arow, text="startup", bg=BG, fg=MUTED, width=7, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        self.btn_autostart = tk.Label(arow, text="Start with Windows", bg=TRACK, fg=FG,
                                      padx=10, pady=2, font=("Segoe UI", 9), cursor="hand2")
        self.btn_autostart.pack(side="left", padx=(6, 0))
        self.btn_autostart.bind("<Button-1>", lambda e: self.app.toggle_autostart())

        self._sep(wrap, pad)

        self.in_combo = self._device_row(wrap, "Input", self.in_devs, pad, True)
        self.out_combo = self._device_row(wrap, "Output", self.out_devs, pad, False)
        self.err_label = tk.Label(wrap, text="", bg=BG, fg=BYPASS, font=("Segoe UI", 8),
                                  wraplength=self.W - 2 * pad, justify="left")
        self.err_label.pack(fill="x", padx=pad)

        self._sep(wrap, pad)

        foot = tk.Frame(wrap, bg=BG)
        foot.pack(fill="x", padx=pad, pady=(0, pad))
        tk.Label(foot, text="turn: adjust   ·   shift+turn: switch fx\npress: bypass",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), justify="left").pack(side="left")
        qbtn = tk.Label(foot, text="Quit", bg=BG, fg=MUTED, font=("Segoe UI", 9), cursor="hand2")
        qbtn.pack(side="right", anchor="s")
        qbtn.bind("<Button-1>", lambda e: self.app.quit())

    def _effect_row(self, parent, effect, pad):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=pad, pady=2)
        name = tk.Label(row, text=effect.name, bg=TRACK, fg=FG, width=7,
                        font=("Segoe UI", 9), cursor="hand2")
        name.pack(side="left")
        name.bind("<Button-1>", lambda e, n=effect.name: self.app.set_mode(n))
        val = tk.Label(row, text="", bg=BG, fg=FG, width=8, anchor="e",
                       font=("Consolas", 9))
        val.pack(side="right")
        canvas = tk.Canvas(row, height=16, bg=TRACK, highlightthickness=0)
        canvas.pack(side="left", fill="x", expand=True, padx=(6, 6))
        return effect, name, canvas, val

    def _device_row(self, parent, label, devs, pad, want_input):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=pad, pady=3)
        tk.Label(row, text=label, bg=BG, fg=MUTED, width=6, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        names = [n for _i, n, _a in devs] or ["(none)"]
        combo = ttk.Combobox(row, values=names, state="readonly", font=("Segoe UI", 8),
                             width=8)
        combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        combo.bind("<<ComboboxSelected>>",
                   lambda e: self.app.set_device(want_input, devs, combo.current()))
        return combo

    def _sep(self, parent, pad):
        tk.Frame(parent, bg=TRACK, height=1).pack(fill="x", padx=pad, pady=5)

    # --- show / hide ---------------------------------------------------
    def is_visible(self):
        return self.top.state() != "withdrawn"

    def toggle(self):
        self.hide() if self.is_visible() else self.show()

    def show(self):
        self.app.refresh_autostart()
        self._sync_combos()
        self.update()
        self.top.update_idletasks()
        w = self.W
        h = self.top.winfo_reqheight()
        px, py = self.top.winfo_pointerxy()
        left, top, right, bottom = _work_area_at(px, py)
        margin = 8
        x = min(max(left + margin, px - w), right - w - margin)
        y = min(max(top + margin, py - h), bottom - h - margin)
        self.top.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self.top.deiconify()
        self.top.lift()
        self.top.attributes("-topmost", True)
        self.top.focus_force()

    def hide(self):
        self.top.withdraw()

    def _sync_combos(self):
        for combo, devs, cur in (
            (self.in_combo, self.in_devs, self.engine.input),
            (self.out_combo, self.out_devs, self.engine.output),
        ):
            for i, (idx, _n, _a) in enumerate(devs):
                if idx == cur:
                    combo.current(i)
                    break

    # --- live refresh --------------------------------------------------
    def update(self):
        active = self.knob.mode
        for effect, name, canvas, val in self.rows:
            self._draw_bar(canvas, effect)
            val.config(text=effect.desc())
            if effect.name == active:
                name.config(bg=effect.color, fg="#10151c")
            else:
                name.config(bg=TRACK, fg=FG)
        self.btn_bypass.config(bg=BYPASS if self.engine.bypass else TRACK)
        on = self.app.autostart_on
        self.btn_autostart.config(
            text="Start with Windows: ON" if on else "Start with Windows",
            bg="#4a7dd6" if on else TRACK)
        self.err_label.config(text=(f"audio error: {self.engine.error}"
                                    if self.engine.error else ""))

    def _draw_bar(self, canvas, effect):
        canvas.delete("all")
        w = canvas.winfo_width() or (self.W - 120)
        h = 16
        col = effect.color
        if effect.bipolar:
            cx = w / 2
            canvas.create_line(cx, 0, cx, h, fill=MUTED)
            v = effect.amount
            if abs(v) >= 0.02:
                if v >= 0:
                    canvas.create_rectangle(cx, 2, cx + v * (w / 2), h - 2, fill=col, width=0)
                else:
                    canvas.create_rectangle(cx + v * (w / 2), 2, cx, h - 2, fill=col, width=0)
        else:
            v = effect.amount
            if v > 0.001:
                canvas.create_rectangle(0, 2, v * w, h - 2, fill=col, width=0)


# ======================================================================
# App orchestration
# ======================================================================
class TrayApp:
    def __init__(self):
        self.engine = AudioEngine()
        self.knob = KnobController(self.engine)
        self.cfg = config.load()
        self.autostart_on = False
        self.icon = None
        self.root = None
        self.panel = None
        self._events = queue.Queue()
        self._last_sig = None
        self._running = True

    def set_mode(self, name):
        self.knob.set_active(name)
        self._refresh_soon()

    def toggle_bypass(self):
        self.engine.bypass = not self.engine.bypass
        self._refresh_soon()

    def set_device(self, want_input, devs, combo_index):
        if combo_index is None or combo_index < 0 or combo_index >= len(devs):
            return
        idx = devs[combo_index][0]
        if want_input:
            self.engine.start(idx, self.engine.output)
        else:
            self.engine.start(self.engine.input, idx)
        self._save_devices()
        self._refresh_soon()

    def _save_devices(self):
        self.cfg["in_name"] = device_name(self.engine.input)
        self.cfg["out_name"] = device_name(self.engine.output)
        config.save(self.cfg)

    # --- run at logon --------------------------------------------------
    def refresh_autostart(self):
        self.autostart_on = autostart.is_enabled()

    def toggle_autostart(self):
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()
        self.refresh_autostart()
        self._refresh_soon()

    def quit(self):
        self._running = False
        try:
            if self.icon is not None:
                self.icon.stop()
        finally:
            if self.root is not None:
                self.root.quit()

    def _start_devices(self):
        # Prefer saved devices (by name), then the host API defaults, then the
        # first device found. Names survive reboots/reordering; indices don't.
        in_idx = resolve(self.cfg["in_name"], True, API) if self.cfg.get("in_name") else None
        out_idx = resolve(self.cfg["out_name"], False, API) if self.cfg.get("out_name") else None
        d_in, d_out = default_devices(API)
        if in_idx is None:
            in_idx = d_in
        if out_idx is None:
            out_idx = d_out
        if in_idx is None:
            ins = list_devices(API, True)
            in_idx = ins[0][0] if ins else None
        if out_idx is None:
            outs = list_devices(API, False)
            out_idx = outs[0][0] if outs else None
        if in_idx is not None and out_idx is not None:
            self.engine.start(in_idx, out_idx)
            # Note: we do NOT save here -- only explicit panel choices persist,
            # so a temporarily-absent saved device isn't overwritten by a default.

    def _sig(self):
        e = self.knob.current()
        return (e.name, round(e.amount, 3), getattr(e, "muted", False),
                self.engine.bypass, self.engine.error is not None)

    def _tooltip(self):
        if self.engine.error:
            return "Rotor - audio error (open panel, pick devices)"
        e = self.knob.current()
        b = " [BYPASS]" if self.engine.bypass else ""
        return f"Rotor{b}  active: {e.name} {e.desc()}"

    def _refresh_soon(self):
        self._last_sig = None

    def _poll(self):
        if not self._running:
            return
        try:
            while True:
                self._events.get_nowait()
                self.panel.toggle()
        except queue.Empty:
            pass
        sig = self._sig()
        if sig != self._last_sig:
            self._last_sig = sig
            if self.icon is not None:
                self.icon.icon = render_icon(self.engine, self.knob.current())
                self.icon.title = self._tooltip()
        if self.panel.is_visible():
            self.panel.update()
        self.root.after(60, self._poll)

    def _setup(self, icon):
        icon.visible = True

    def run(self):
        # Build the UI and tray icon FIRST. Widget/window creation is heavy and
        # holds the GIL; doing it before the audio stream opens keeps those
        # one-time costs from starving the audio thread (startup crackle).
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = Panel(self.root, self)

        self.icon = ClickIcon(
            "rotor",
            icon=render_icon(self.engine, self.knob.current()),
            title=self._tooltip(),
        )
        self.icon.on_click = lambda: self._events.put("click")
        self.icon.run_detached(setup=self._setup)

        # Now start audio into a calm process: prime the DSP, then open the stream.
        self.knob.start()
        self.engine.warmup()
        self._start_devices()
        self.engine.start_volume_sync()

        self.root.after(60, self._poll)
        try:
            self.root.mainloop()
        finally:
            self._running = False
            try:
                self.icon.stop()
            except Exception:
                pass
            self.knob.stop()
            self.engine.stop_volume_sync()
            self.engine.stop()


if __name__ == "__main__":
    try:
        TrayApp().run()
    except Exception:
        import traceback
        try:
            with open("tray_error.log", "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        print("Rotor tray failed:", file=sys.stderr)
        traceback.print_exc()
        raise
