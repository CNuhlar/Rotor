"""Read the Windows default-output volume/mute via Core Audio (pure ctypes COM).

Used to MIRROR the system volume into Rotor's display while the 'volume' effect
is active: the knob passes its media keys straight to Windows (native volume OSD,
real level change) and we read the resulting level/mute back here so the app
shows the same value. No external dependency (no pycaw/comtypes), so it bundles
cleanly into the single-file exe.
"""

import ctypes
from ctypes import (POINTER, byref, c_float, c_int, c_ubyte, c_ulong, c_ushort,
                    c_void_p, c_wchar_p)

ole32 = ctypes.windll.ole32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ole32.CLSIDFromString(c_wchar_p(s), byref(self))


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

CLSCTX_ALL = 0x17
eRender = 0                          # data flow: render (output)
eConsole = 0                         # role: console/games/system sounds
S_OK = 0


def _method(ptr, index, restype, *argtypes):
    """Bind vtable slot `index` of COM object `ptr` as a callable (this-first)."""
    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    fn_addr = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(fn_addr)


def _release(ptr):
    if ptr:
        _method(ptr, 2, c_int)(ptr)          # IUnknown::Release (vtbl 2)


def _endpoint_volume():
    """(IAudioEndpointVolume, IMMDevice, IMMDeviceEnumerator) for the default
    render device. All three must be _release()d by the caller. (None,)*3 on fail."""
    enum = c_void_p()
    hr = ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                byref(IID_IMMDeviceEnumerator), byref(enum))
    if hr != S_OK or not enum:
        return None, None, None
    dev = c_void_p()
    get_default = _method(enum, 4, c_int, c_int, c_int, POINTER(c_void_p))
    if get_default(enum, eRender, eConsole, byref(dev)) != S_OK or not dev:
        _release(enum)
        return None, None, None
    vol = c_void_p()
    activate = _method(dev, 3, c_int, POINTER(GUID), c_int, c_void_p, POINTER(c_void_p))
    if activate(dev, byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None,
                byref(vol)) != S_OK or not vol:
        _release(dev)
        _release(enum)
        return None, None, None
    return vol, dev, enum


def read():
    """(volume 0..1, muted bool) for the default output; (None, None) on failure.

    Safe to call from any thread and never raises -- returns (None, None) if
    Core Audio is unavailable so callers can just keep their last value.
    """
    try:
        ole32.CoInitialize(None)             # idempotent; ignore mode/return
        vol, dev, enum = _endpoint_volume()
        try:
            if not vol:
                return None, None
            level, muted = c_float(), c_int()
            get_scalar = _method(vol, 9, c_int, POINTER(c_float))   # GetMasterVolumeLevelScalar
            get_mute = _method(vol, 15, c_int, POINTER(c_int))      # GetMute
            lv = float(level.value) if get_scalar(vol, byref(level)) == S_OK else None
            mv = bool(muted.value) if get_mute(vol, byref(muted)) == S_OK else None
            return lv, mv
        finally:
            _release(vol)
            _release(dev)
            _release(enum)
    except Exception:
        return None, None
