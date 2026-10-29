r"""ClawTweaks client: the helper's quick-settings pipe \\.\pipe\ClawTweaksQuickSettings (XboxGamingBarHelper.exe).

Protocol (from the helper's own code, ClawTweaks 1.0.1.3):
  - one JSON object per line: {"RequestId": n, "Command": 0 get | 1 set, "Function": <Shared.Enums.Function>,
    "Content": "..."}; every other top-level key is an "Extra" command such as {"MsiFanFullBlast": "on"}.
    A plain text line (not JSON) is ignored by the helper.
  - a property reply carries the same RequestId with Command 3; the helper also pushes live metrics
    (Function 260) to every client, so replies are matched by RequestId, never "the first line".
  - Extra commands answer on the widget's own pipe, so here they are sent without waiting for a reply.
  - the pipe has a single instance: connect, talk, close quickly.
"""
import ctypes, json, msvcrt, os, re, time
from ctypes import wintypes

PIPE = r"\\.\pipe\ClawTweaksQuickSettings"
PACKAGE_STATE = os.path.expandvars(r"%LOCALAPPDATA%\Packages\MSIClaw.ClawTweaks_7eszav2039cvc\LocalState")
SETTINGS = os.path.join(PACKAGE_STATE, "settings.json")

GET, SET, RESPONSE = 0, 1, 3
# Shared.Enums.Function
OSD, TDP, CURRENT_TDP, RUNNING_GAME, CPU_BOOST = 1, 2, 3, 4, 6
REFRESH_RATES, REFRESH_RATE = 13, 14
LS_INSTALLED, LS_ENABLED = 53, 55
FPS_LIMIT, OS_POWER_MODE, METRICS = 212, 223, 260
TDP_BOOST, TDP_BOOST_PL2 = 216, 218  # separate PL2 mode on/off, its PL2 watts (PL1 + 1 .. 37)
EMULATION = 272  # ClawTweaks' virtual controller is on
CONTROLLER_MODE = 343  # with the virtual controller: True = gamepad, False = mouse (the desktop mode tile)
HW_MOUSE = 366  # hardware controller mode: the firmware mouse, True = mouse
OSD_POSITION, AI_NOISE, FSE = 400, 443, 445
# tile actions of ExecuteControllerAction (the helper's ExecuteLeftClawAction)
ACT_FSE, ACT_SUSPEND, ACT_OVERLAY_ONOFF, ACT_OSD_CYCLE = 16, 17, 18, 20
ACT_STEAM_BIG_PICTURE, ACT_PLAYNITE, ACT_XBOX_APP, ACT_CLAWTWEAKS, ACT_LIBRARY = 40, 41, 42, 43, 44

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_rid = [int(time.time()) % 100000 * 10]


def installed():
    return os.path.isdir(PACKAGE_STATE)


def _open(timeout):
    t0 = time.time()
    while True:
        try:
            return open(PIPE, "r+b", buffering=0)
        except OSError:  # busy (one instance) or the helper is between clients
            if time.time() - t0 > timeout:
                raise
            time.sleep(0.04)


def _talk(msg, want=None, timeout=1.5):
    """Send one JSON line. want(reply dict) -> True picks the reply to return; None = do not wait."""
    try:
        f = _open(timeout)
    except OSError:
        return None
    with f:
        f.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        if want is None:
            time.sleep(0.15)  # the helper reads the line before it notices the disconnect
            return {}
        h, buf, end = msvcrt.get_osfhandle(f.fileno()), b"", time.time() + timeout
        avail = wintypes.DWORD()
        while time.time() < end:
            if not kernel32.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
                return None
            if avail.value:
                buf += f.read(avail.value)
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        r = json.loads(line.decode("utf-8-sig"))
                    except ValueError:
                        continue
                    if want(r):
                        return r
            else:
                time.sleep(0.02)
    return None


def _next():
    _rid[0] += 1
    return _rid[0]


def get(fn, timeout=1.5):
    """Property value as a string, None when the helper did not answer."""
    rid = _next()
    r = _talk({"RequestId": rid, "Command": GET, "Function": fn, "Content": ""},
              lambda r: r.get("RequestId") == rid and r.get("Function") == fn)
    return None if r is None else str(r.get("Content", ""))


def set_(fn, value, timeout=1.5):
    """Set a property; returns the value the helper reports afterwards (None = no answer)."""
    value = ("True" if value else "False") if isinstance(value, bool) else str(value)
    _talk({"RequestId": _next(), "Command": SET, "Function": fn, "Content": value}, None)
    time.sleep(0.1)
    return get(fn, timeout)


def extra(key, value=""):
    """An Extra command (fire and forget). False when the pipe could not be opened."""
    return _talk({"RequestId": _next(), key: value}, None) is not None


def action(n):
    return extra("ExecuteControllerAction", n)


def get_int(fn, default=None):
    v = get(fn)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def get_bool(fn):
    v = get(fn)
    return None if v in (None, "") else v.strip().lower() in ("true", "1")


def metrics(timeout=2.5):
    """Live sensors the helper pushes: cpuTemp, cpuWattage, cpuUsage, gpuUsage, batteryLevel, fanRpm, fanRpm2,
    fanDuty1, chargeLimit, isCharging... ({} when nothing came)."""
    # any client gets the pushes (RequestId 0); asking Function 260 directly only returns an error line
    r = _talk({"RequestId": _next(), "Command": GET, "Function": 0, "Content": ""},
              lambda r: r.get("Function") == METRICS and r.get("Content"), timeout)
    if not r:
        return {}
    try:  # the helper formats decimals with the Russian comma: "cpuWattage":9,2
        return json.loads(re.sub(r"(\d),(\d)", r"\1.\2", r.get("Content", "{}")))
    except ValueError:
        return {}


def running_game():
    """{'pid', 'name', 'fps'} of the game ClawTweaks detected ({} when none)."""
    c = get(RUNNING_GAME) or ""
    pid = re.search(r"<ProcessId>(\d+)</ProcessId>", c)
    if not pid or pid.group(1) == "0":
        return {}
    name = re.search(r"<Name>(.*?)</Name>", c)
    fps = re.search(r"<FPS>(\d+)", c)
    return {"pid": int(pid.group(1)), "name": name.group(1) if name else "", "fps": int(fps.group(1)) if fps else 0}


def settings():
    """ClawTweaks settings.json (read only: the helper owns it)."""
    try:
        with open(SETTINGS, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- is a process suspended (to pick the direction of the helper's suspend toggle) ---
TH32CS_SNAPTHREAD, THREAD_SUSPEND_RESUME = 0x4, 0x2


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.Thread32First.argtypes = kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.SuspendThread.argtypes = kernel32.ResumeThread.argtypes = kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.SuspendThread.restype = wintypes.DWORD


def suspended(pid):
    """True when the process' first thread has a non-zero suspend count (suspend + resume it once to read it)."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        ok = kernel32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                h = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                if not h:
                    return None
                try:
                    prev = kernel32.SuspendThread(h)
                    if prev == 0xFFFFFFFF:
                        return None
                    kernel32.ResumeThread(h)
                    return prev > 0
                finally:
                    kernel32.CloseHandle(h)
            ok = kernel32.Thread32Next(snap, ctypes.byref(te))
    finally:
        kernel32.CloseHandle(snap)
    return None
