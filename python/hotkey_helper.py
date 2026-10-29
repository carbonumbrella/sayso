# Elevated hotkey listener for dictate.py. Runs as a "highest privileges" scheduled task
# (ClawDictateHotkeys) so the hold-to-talk buttons keep working while an elevated window
# (Task Manager, admin terminal) is in the foreground: a normal-integrity process does not get
# GetAsyncKeyState for keys aimed at higher-integrity windows (UIPI).
# It does NOTHING else: it only forwards "down <mode>" / "up" over UDP 127.0.0.1:47321, so all
# actions (paste, apps, Gemma) stay in the non-elevated dictate.py and never run as admin.
# dictate.py pings 127.0.0.1:47320; if this helper answers "pong", dictate skips its own hotkeys.
# The combinations come from voice_settings.json (ClawVoice Settings) and are re-registered on change.
import ctypes, ctypes.wintypes as wt, socket, threading, time, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_settings

MOD_NOREPEAT = 0x4000
EVENTS, PING = ("127.0.0.1", 47321), ("127.0.0.1", 47320)
POLL, RELEASE_POLLS = 0.01, 3

user32 = ctypes.windll.user32
if sys.stdout is None:
    sys.stdout = sys.stderr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotkey_helper.log"),
                                   "w", encoding="utf-8", buffering=1)
out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def answer_pings():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(PING)
    while True:
        data, addr = s.recvfrom(64)
        if data == b"ping":
            s.sendto(b"pong", addr)


def register(keys, old=None):
    """Register `keys`; if one is taken, keep the old set (first start: exit, another copy is running)."""
    for hid in (old or {}):
        user32.UnregisterHotKey(None, hid)
    for hid, (mods, vk, mode, label) in keys.items():
        if not user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
            print(f"hotkey {label} ({mode}) is taken", flush=True)
            for h in keys:
                user32.UnregisterHotKey(None, h)
            if old is None:
                sys.exit(1)
            return register(old)
    print("hotkeys: " + ", ".join(f"{k[3]} = {k[2]}" for k in keys.values()), flush=True)
    return keys


HOTKEYS = register(voice_settings.hotkeys())
threading.Thread(target=answer_pings, daemon=True).start()
print("hotkey helper ready", flush=True)

msg = wt.MSG()
held_vk, gone, checked = None, 0, time.time()
while True:
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
        if msg.message == 0x0312 and msg.wParam in HOTKEYS and held_vk is None:  # WM_HOTKEY
            _, held_vk, mode, _ = HOTKEYS[msg.wParam]
            gone = 0
            out.sendto(f"down {mode}".encode(), EVENTS)
    if held_vk is not None:
        gone = 0 if user32.GetAsyncKeyState(held_vk) & 0x8000 else gone + 1
        if gone >= RELEASE_POLLS:
            held_vk = None
            out.sendto(b"up", EVENTS)
    elif time.time() - checked > 1:  # new combinations from the Settings window (not while one is held)
        checked = time.time()
        keys = voice_settings.hotkeys()
        if keys != HOTKEYS:
            HOTKEYS = register(keys, HOTKEYS)
    time.sleep(POLL)
