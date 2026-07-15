"""Read/set a Windows output device's volume & mute via Core Audio (ctypes COM).

Rotor's 'volume' effect controls the REAL Windows volume of the device Rotor
plays to (e.g. your speakers) -- not the default endpoint. With Rotor's routing
the default endpoint is the virtual cable, whose slider doesn't attenuate the
looped-back audio; the device you actually hear is Rotor's output. So we find
that device by name and drive its endpoint volume/mute here. The knob steps a
target and a background thread pushes it, so this never runs on the key hook.

No external dependency (no pycaw/comtypes), so it bundles cleanly into the exe.
"""

import ctypes
from ctypes import (POINTER, byref, c_float, c_int, c_ubyte, c_uint, c_ulong,
                    c_ushort, c_void_p, c_wchar_p)

ole32 = ctypes.windll.ole32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ole32.CLSIDFromString(c_wchar_p(s), byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    # Big enough to hold the LPWSTR case; PropVariantClear frees it.
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort),
                ("r3", c_ushort), ("val", c_void_p), ("val2", c_void_p)]


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
PKEY_Device_FriendlyName_FMTID = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")

CLSCTX_ALL = 0x17
eRender = 0
DEVICE_STATE_ACTIVE = 0x00000001
STGM_READ = 0x0
VT_LPWSTR = 31
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


def _friendly_name(dev):
    store = c_void_p()
    open_ps = _method(dev, 4, c_int, c_ulong, POINTER(c_void_p))      # OpenPropertyStore
    if open_ps(dev, STGM_READ, byref(store)) != S_OK or not store:
        return None
    try:
        key = PROPERTYKEY(PKEY_Device_FriendlyName_FMTID, 14)
        pv = PROPVARIANT()
        get_val = _method(store, 5, c_int, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))
        if get_val(store, byref(key), byref(pv)) != S_OK:
            return None
        try:
            if pv.vt == VT_LPWSTR and pv.val:
                return ctypes.cast(pv.val, c_wchar_p).value
            return None
        finally:
            ole32.PropVariantClear(byref(pv))
    finally:
        _release(store)


def _match(friendly, needle):
    a, b = friendly.lower(), needle.lower()
    return b in a or a in b


def _endpoint_by_name(enum, name_sub):
    """IAudioEndpointVolume for the active render device whose friendly name
    matches name_sub (caller releases it), or None."""
    coll = c_void_p()
    enum_ep = _method(enum, 3, c_int, c_int, c_ulong, POINTER(c_void_p))  # EnumAudioEndpoints
    if enum_ep(enum, eRender, DEVICE_STATE_ACTIVE, byref(coll)) != S_OK or not coll:
        return None
    try:
        count = c_uint()
        get_count = _method(coll, 3, c_int, POINTER(c_uint))
        if get_count(coll, byref(count)) != S_OK:
            return None
        item = _method(coll, 4, c_int, c_uint, POINTER(c_void_p))
        for i in range(count.value):
            dev = c_void_p()
            if item(coll, i, byref(dev)) != S_OK or not dev:
                continue
            name = _friendly_name(dev)
            if name and _match(name, name_sub):
                vol = c_void_p()
                activate = _method(dev, 3, c_int, POINTER(GUID), c_int,
                                   c_void_p, POINTER(c_void_p))
                ok = (activate(dev, byref(IID_IAudioEndpointVolume), CLSCTX_ALL,
                               None, byref(vol)) == S_OK and vol)
                _release(dev)
                if ok:
                    return vol
            else:
                _release(dev)
        return None
    finally:
        _release(coll)


def _with_device(name_sub, fn, default=None):
    """Run fn(endpoint_volume) for the render device named name_sub. Never raises;
    returns `default` if Core Audio or the device is unavailable."""
    if not name_sub:
        return default
    try:
        ole32.CoInitialize(None)
        enum = c_void_p()
        if ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                  byref(IID_IMMDeviceEnumerator), byref(enum)) != S_OK or not enum:
            return default
        try:
            vol = _endpoint_by_name(enum, name_sub)
            if not vol:
                return default
            try:
                return fn(vol)
            finally:
                _release(vol)
        finally:
            _release(enum)
    except Exception:
        return default


def read_device(name_sub):
    """(volume 0..1, muted bool) for the named output device; (None, None) on fail."""
    def fn(vol):
        level, muted = c_float(), c_int()
        gs = _method(vol, 9, c_int, POINTER(c_float))       # GetMasterVolumeLevelScalar
        gm = _method(vol, 15, c_int, POINTER(c_int))        # GetMute
        lv = float(level.value) if gs(vol, byref(level)) == S_OK else None
        mv = bool(muted.value) if gm(vol, byref(muted)) == S_OK else None
        return (lv, mv)
    return _with_device(name_sub, fn, (None, None))


def set_device_volume(name_sub, level):
    """Set the named output device's master volume (0..1). True on success."""
    level = max(0.0, min(1.0, float(level)))

    def fn(vol):
        ss = _method(vol, 7, c_int, c_float, c_void_p)      # SetMasterVolumeLevelScalar
        return ss(vol, level, None) == S_OK
    return bool(_with_device(name_sub, fn, False))


def set_device_mute(name_sub, mute):
    """Mute/unmute the named output device. True on success."""
    def fn(vol):
        sm = _method(vol, 14, c_int, c_int, c_void_p)       # SetMute
        return sm(vol, 1 if mute else 0, None) == S_OK
    return bool(_with_device(name_sub, fn, False))
