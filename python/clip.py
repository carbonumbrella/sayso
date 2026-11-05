"""Clipboard for pasting voice text without touching the user's clipboard history (Win+V).

Dictation pastes by Ctrl+V: the text goes to the clipboard for a moment and the previous content comes back. Both
writes carry the Windows "do not keep" formats (CanIncludeInClipboardHistory = 0, CanUploadToCloudClipboard = 0,
ExcludeClipboardContentFromMonitorProcessing), so Win+V keeps only what the user copied. The previous content is
saved with every memory format it has (text, images, copied files), not only as text: a copied picture used to be
lost after a dictation."""
import ctypes, time
from ctypes import wintypes

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = wintypes.HANDLE
u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
u32.SetClipboardData.restype = wintypes.HANDLE
u32.EnumClipboardFormats.argtypes = [wintypes.UINT]
u32.EnumClipboardFormats.restype = wintypes.UINT
u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
u32.RegisterClipboardFormatW.restype = wintypes.UINT
k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalAlloc.restype = wintypes.HGLOBAL
k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
k32.GlobalSize.argtypes = [wintypes.HGLOBAL]
k32.GlobalSize.restype = ctypes.c_size_t

CF_UNICODETEXT = 13
GHND = 0x0042  # moveable, zeroed
# GDI handles, not memory: they cannot be copied as bytes (the memory twins CF_DIB and others carry the same data)
GDI_FORMATS = {2, 3, 9, 14, 0x82, 0x83, 0x8E} | set(range(0x0300, 0x0400))
NO_HISTORY = [("ExcludeClipboardContentFromMonitorProcessing", b"\0\0\0\0"),
              ("CanIncludeInClipboardHistory", b"\0\0\0\0"), ("CanUploadToCloudClipboard", b"\0\0\0\0")]


def _open():
    for _ in range(20):  # another program may hold it for a moment
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _put(fmt, data):
    h = k32.GlobalAlloc(GHND, max(1, len(data)))
    p = k32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)
    u32.SetClipboardData(fmt, h)  # the clipboard owns the memory from here


def _no_history():
    for name, data in NO_HISTORY:
        _put(u32.RegisterClipboardFormatW(name), data)


def save():
    """Every memory format on the clipboard now, as [(format, bytes)]; [] when it is empty or busy."""
    if not _open():
        return []
    out = []
    try:
        fmt = u32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in GDI_FORMATS:
                h = u32.GetClipboardData(fmt)
                size = k32.GlobalSize(h) if h else 0
                p = k32.GlobalLock(h) if size else None
                if p:
                    out.append((fmt, ctypes.string_at(p, size)))
                    k32.GlobalUnlock(h)
            fmt = u32.EnumClipboardFormats(fmt)
    finally:
        u32.CloseClipboard()
    return out


def set_text(text):
    """The text for one Ctrl+V, kept out of the clipboard history."""
    if not _open():
        return False
    try:
        u32.EmptyClipboard()
        _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        _no_history()
    finally:
        u32.CloseClipboard()
    return True


def restore(saved):
    """Put back what save() returned (also kept out of the history: the user's own copy is in it already)."""
    if not _open():
        return
    try:
        u32.EmptyClipboard()
        for fmt, data in saved:
            _put(fmt, data)
        if saved:
            _no_history()
    finally:
        u32.CloseClipboard()
