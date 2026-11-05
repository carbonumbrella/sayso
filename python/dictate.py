# Push-to-talk dictation: Whisper (OpenVINO), text pasted into the focused window.
# Device: NPU default (iGPU stays free for games). Model: setting stt_model, small or large-v3-turbo
# (voice_settings.WHISPER has the catalog and the measurements); the NPU runs turbo only as its fp16 build.
# Hold to talk on Ctrl+Alt+Win+W: the rear M1 button sends it from ClawTweaks keyboard
# mode, held while pressed (keyprobe 2026-10-26). Registered with RegisterHotKey, so the
# keystroke is swallowed and games never see it. No gamepad chord (games would see it).
# Records while held, transcribes on release. Beep high = recording, low = released.
# Second hotkey Ctrl+Win+Shift+G (hold): same recording, but the phrase goes to the Gemma
# voice-command agent (gemma_agent.py, Gemma on the NPU server :8081) instead of being pasted.
# Hotkeys normally come from hotkey_helper.py (elevated scheduled task, UDP events) so they also
# work over admin windows; without the helper this process registers them itself.
# Latency: on AC power the mic stays open (warm) with a 0.4 s pre-roll, because waking the mic from
# idle took 0.8-2.2 s on this Claw. On battery it is closed and opened per press (user choice: save
# power, accept the delay). DICTATE_WARM_MIC=0 never keeps it warm. Beeps are async WAVs.
# Wake phrase "<name>, ..." (wake.py: Silero VAD + this Whisper) works on AC power while
# listening is on; "выключи/включи слушание" toggles it (by voice or via the Gemma button), the
# state survives restarts in listen_state.txt.
import os, re, sys, json, time, queue, socket, subprocess, threading, winsound, ctypes, collections, wave
import ctypes.wintypes as wt
import numpy as np
import sounddevice as sd
import pyperclip
import openvino_genai as ov_genai
import voice_settings  # follow_sec, warm_mic (written by the ClawVoice tray Settings window)

HERE = os.path.dirname(os.path.abspath(__file__))
import voice_settings as _vs  # stt_device / language from SaySo Settings (env vars still override)
if sys.stdout is None:  # pythonw (autostart): no console, log to a file instead
    sys.stdout = sys.stderr = open(os.path.join(HERE, "dictate.log"), "w", encoding="utf-8", buffering=1)
    if _vs.get("dictation_log") is False:
        # Settings "Журнал диктовки" off: no recognized text or events on disk (every print goes to stdout);
        # errors and tracebacks (stderr) still land in the file
        sys.stdout = open(os.devnull, "w", encoding="utf-8")


def _pick_device():
    """Settings "stt_device": auto = NPU, else GPU, else CPU (whatever OpenVINO sees on this PC)."""
    want = (os.environ.get("DICTATE_DEVICE") or _vs.get("stt_device") or "auto").upper()
    try:
        import openvino as _ov
        core = _ov.Core()
        # Intel only: OpenVINO also lists an AMD Radeon iGPU as "GPU" (MSI Claw A8), and loading Whisper on it
        # killed the process with no Python error at all, so it restarted in a loop
        have = [d for d in core.available_devices
                if d.startswith("CPU") or "intel" in str(core.get_property(d, "FULL_DEVICE_NAME")).lower()]
    except Exception:
        have = ["CPU"]
    crashed = _crashed_on()
    if crashed and want == "AUTO":  # (a device chosen in Settings is tried again)
        print(f"the last start died while loading Whisper on {crashed}: skipping it", flush=True)
        have = [d for d in have if not d.startswith(crashed)] or ["CPU"]
    if want != "AUTO" and any(d.startswith(want) for d in have):
        return want
    return next((d for d in ("NPU", "GPU", "CPU") if any(h.startswith(d) for h in have)), "CPU")


# A native crash inside WhisperPipeline() cannot be caught: the device is noted before loading and cleared
# after it, so a note left behind means the previous start died there and that device is skipped.
LOADING_FILE = os.path.join(HERE, "stt_loading.txt")


def _crashed_on():
    try:
        return open(LOADING_FILE).read().strip() or None
    except OSError:
        return None


def _model_for(device):
    """Setting stt_model (voice_settings.WHISPER): the chosen model when it is downloaded, else another one that is,
    small first. The NPU build of turbo is used only once compiled (NPU_READY): until then small runs and
    _prepare_npu() compiles it in the background (~6 min the first time)."""
    if os.environ.get("DICTATE_MODEL"):
        return os.path.join(HERE, os.environ["DICTATE_MODEL"])
    want = _vs.whisper_key(_vs.get("stt_model"), device)
    order = [want] + [k for k in ("small", "turbo") if k != want and device in _vs.WHISPER[k]["devices"]]
    for key in order:
        if _vs.whisper_complete(key) and (key != "turbo-npu" or os.path.exists(_npu_ready(key))):
            return _vs.whisper_dir(key)
    if _vs.whisper_complete("turbo-npu") and device == "NPU":  # no small to dictate meanwhile: compile while loading
        return _vs.whisper_dir("turbo-npu")
    return None  # nothing downloaded for this device (it used to load the missing small and restart in a loop)


def _cache_dir(model):
    # small keeps the old shared folder; other models one of their own (removed together with the model)
    name = os.path.basename(model)
    return os.path.join(HERE, "npu_cache") if "small" in name else os.path.join(HERE, "npu_cache", name)


def _npu_ready(key):
    return os.path.join(_cache_dir(_vs.whisper_dir(key)), "ready")


DEVICE = _pick_device()
MODEL = _model_for(DEVICE)
if MODEL is None:  # Settings shows "no model for this device" from this state instead of "долго не запускается"
    with open(os.path.join(HERE, "stt_state.json"), "w", encoding="utf-8") as _f:
        json.dump({"model": "", "device": DEVICE, "preparing": "", "error": "no_model"}, _f)
    print(f"no downloaded speech model for {DEVICE}: download one in Settings, Распознавание", flush=True)
    sys.exit(3)
_lang = os.environ.get("DICTATE_LANG")
if _lang is None:
    _l = _vs.get("language") or "ru"
    _lang = "" if _l == "auto" else f"<|{_l}|>"
LANG = _lang  # empty = Whisper detects the language
RATE = 16000
MAX_SEC = 120
POLL = 0.02
RELEASE_POLLS = 3  # trigger must be gone this many polls in a row (macro jitter)
WARM_MIC = os.environ.get("DICTATE_WARM_MIC", "1") != "0"
PREROLL_SEC = 0.4
EVENTS, PING = ("127.0.0.1", 47321), ("127.0.0.1", 47320)
MOD_ALT, MOD_CONTROL, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x8, 0x4000
MOD_SHIFT = 0x4
VK_W, VK_G = 0x57, 0x47
# hotkeys come from voice_settings.json (ClawVoice Settings; defaults Ctrl+Alt+Win+W = rear M1,
# Ctrl+Win+Shift+G = Gemma); used here only without the elevated hotkey_helper.py
HOTKEYS = voice_settings.hotkeys()

user32 = ctypes.windll.user32

print(f"loading Whisper on {DEVICE} ...", flush=True)
_compiling = os.path.basename(MODEL) == os.path.basename(_vs.whisper_dir("turbo-npu")) and \
    not os.path.exists(_npu_ready("turbo-npu"))
if _compiling:  # the first load compiles it (~6 min): Settings says so meanwhile
    with open(os.path.join(HERE, "stt_state.json"), "w", encoding="utf-8") as _f:
        json.dump({"model": os.path.basename(MODEL), "device": DEVICE, "preparing": "turbo-npu"}, _f)
_noted = DEVICE != "CPU" and not _crashed_on()
if _noted:
    with open(LOADING_FILE, "w") as _f:
        _f.write(DEVICE)
try:
    pipe = ov_genai.WhisperPipeline(MODEL, DEVICE, CACHE_DIR=_cache_dir(MODEL))
except Exception as e:  # an old NPU/GPU driver: fall back instead of leaving the user without dictation
    print(f"Whisper on {DEVICE} failed ({e!r}), falling back", flush=True)
    for DEVICE in [d for d in ("GPU", "CPU") if d != DEVICE]:
        try:
            MODEL = _model_for(DEVICE)
            if MODEL is None:
                raise RuntimeError(f"no downloaded model for {DEVICE}")
            pipe = ov_genai.WhisperPipeline(MODEL, DEVICE, CACHE_DIR=_cache_dir(MODEL))
            break
        except Exception as e2:
            print(f"Whisper on {DEVICE} failed too ({e2!r})", flush=True)
    else:
        raise
if _noted:  # loaded: not a crash. A note we skipped (previous crash) stays, the uninstaller removes it
    os.remove(LOADING_FILE)
if _compiling and DEVICE == "NPU":  # compiled at this load: _prepare_npu must not do it again
    open(_npu_ready("turbo-npu"), "w").write("ok")
print(f"Whisper {os.path.basename(MODEL)} on {DEVICE}, language {LANG or 'auto'}", flush=True)
whisper_lock = threading.Lock()  # hotkey recordings and the wake listener share one pipeline


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


_k32, _psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
_trim_timer = None


def _working_set_mb():
    c = _PMC(cb=ctypes.sizeof(_PMC))
    _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize >> 20


def _trim():
    if _working_set_mb() > 400:
        _psapi.EmptyWorkingSet(_k32.GetCurrentProcess())


def trim_memory(delay=3.0):
    """NPU only. After its first inference this process holds ~2.5 GB of working set (the cache blobs mapped in and
    a host copy), but later inferences touch ~50 MB of it: the model runs from the NPU driver's own memory (~2.2 GB
    for turbo fp16, the size of its blobs, not counted here). Handing the set back frees ~0.3 GB of RAM in practice
    (the rest is compressed by Windows) at no measurable cost per phrase (0.46 s either way, Claw 8 AI+); it runs
    only while the set is big again: after the load and the rare phrase that grows it."""
    global _trim_timer
    if DEVICE != "NPU":
        return
    if _trim_timer is not None:
        _trim_timer.cancel()
    _trim_timer = threading.Timer(delay, _trim)
    _trim_timer.daemon = True
    _trim_timer.start()


def warm_up(p):
    """One inference on silence: the first one maps the model in (0.9 s instead of 0.45 s), better now than on the
    user's first phrase, and the working set it leaves behind is trimmed right after."""
    try:
        p.generate([0.0] * RATE, task="transcribe")
    except Exception as e:
        print(f"warm-up failed: {e!r}", flush=True)
    trim_memory(0.5)


warm_up(pipe)
STATE_FILE = os.path.join(HERE, "stt_state.json")  # what is loaded, for the settings (model in use, one being prepared)


def write_state(preparing=""):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"model": os.path.basename(MODEL), "device": DEVICE, "preparing": preparing}, f)
    except OSError:
        pass


def _prepare_npu():
    """turbo chosen on the NPU but not compiled yet: compile it in a separate low-priority process while small
    dictates, then switch to it here without a restart."""
    global pipe, MODEL
    key = _vs.whisper_key(_vs.get("stt_model"), DEVICE)
    if DEVICE != "NPU" or key != "turbo-npu" or not _vs.whisper_complete(key) or os.path.exists(_npu_ready(key)):
        return
    model = _vs.whisper_dir(key)
    print(f"compiling {os.path.basename(model)} for the NPU in the background (~6 min)", flush=True)
    write_state(key)
    code = ("import sys, openvino_genai as g; g.WhisperPipeline(sys.argv[1], 'NPU', CACHE_DIR=sys.argv[2]); "
            "open(sys.argv[3], 'w').write('ok')")
    r = subprocess.run([sys.executable, "-c", code, model, _cache_dir(model), _npu_ready(key)],
                       creationflags=0x08000000 | 0x00004000, capture_output=True, text=True)  # no window, below normal
    if r.returncode or not os.path.exists(_npu_ready(key)):
        # (an update kills it midway too: every process of the app folder is ended; the next start tries again)
        print(f"NPU compile failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:] or 'no output'}", flush=True)
        write_state()
        return
    try:
        new = ov_genai.WhisperPipeline(model, "NPU", CACHE_DIR=_cache_dir(model))  # from the cache: seconds
    except Exception as e:
        print(f"switching to {os.path.basename(model)} failed: {e!r}", flush=True)
        write_state()
        return
    warm_up(new)
    with whisper_lock:
        pipe, MODEL = new, model
    write_state()
    print(f"Whisper {os.path.basename(MODEL)} on {DEVICE} (compiled in the background)", flush=True)


write_state()
threading.Thread(target=_prepare_npu, daemon=True).start()
LISTEN_FILE = os.path.join(HERE, "listen_state.txt")
listening = not os.path.exists(LISTEN_FILE) or open(LISTEN_FILE).read().strip() != "0"
wake_listener = None

lock = threading.Lock()
recording = False
session = 0
chunks = []
stream = None
mode = "paste"
preroll = collections.deque(maxlen=int(PREROLL_SEC * RATE / 800))  # 800-frame blocks


FAR_MIC = 0  # tts_one_speaker: the voice is on the left speaker, mic 0 is the far one (MSI Claw 8 AI+)


def on_audio(data, frames, t, status):
    block = data.mean(1) if data.shape[1] > 1 else data[:, 0].copy()
    if barge and data.shape[1] > 1 and wake_listener is not None:
        wake_listener.feed(data[:, FAR_MIC].copy())  # while the voice talks: the mic away from its speaker
        if recording:
            chunks.append(block)
        else:
            preroll.append(block)
        return
    if TRAIN["buf"] is not None:  # name trainer: everything goes to it, nothing to the wake listener
        TRAIN["buf"].append(block)
        return
    if recording:
        chunks.append(block)
    else:
        preroll.append(block)
        if wake_listener is not None:
            wake_listener.feed(block)


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_ac():
    ps = _PowerStatus()
    return bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(ps))) and ps.ACLineStatus == 1


def want_warm():
    return WARM_MIC and voice_settings.get("warm_mic") and on_ac() and listening  # listening off: the mic (tray icon) sleeps like on battery


MIC_LINGER_SEC = 300  # after a press the mic stays open this long (battery too): waking it took ~1 s, the first word was lost


def recently_used():
    return time.time() - last_stop_time < MIC_LINGER_SEC


def close_mic():
    global stream
    if stream is not None:
        try:
            stream.stop(); stream.close()
        except Exception:
            pass
        stream = None


def mic_index():
    """Input device chosen in Settings (by name, any host API that has it), None = system default."""
    want = (voice_settings.get("mic_device") or "").strip().lower()
    if not want:
        return None
    default_api = sd.default.hostapi  # MME: takes 16 kHz mono from any device; its names are cut to 31 chars
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].strip().lower()
        if d["max_input_channels"] > 0 and d["hostapi"] == default_api and (name == want or want.startswith(name)):
            return i
    return None


def mic_users():
    """Apps that hold the microphone now, from Windows' privacy records (LastUsedTimeStop 0 = still in use)."""
    import winreg
    found = []
    root = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    for sub in ("", "\\NonPackaged"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root + sub) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    name = winreg.EnumKey(k, i)
                    try:
                        with winreg.OpenKey(k, name) as app:
                            if winreg.QueryValueEx(app, "LastUsedTimeStop")[0] == 0 and \
                                    winreg.QueryValueEx(app, "LastUsedTimeStart")[0] != 0:
                                found.append(name.split("#")[-1])
                    except OSError:
                        pass
        except OSError:
            pass
    return [n for n in found if n.lower() not in ("mic_capture.exe", "nonpackaged")]


def mic_denied():
    """Windows privacy switch off: mic access for this device (HKLM), for apps, or for desktop apps."""
    import winreg
    root = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, ""), (winreg.HKEY_CURRENT_USER, ""),
                      (winreg.HKEY_CURRENT_USER, "\\NonPackaged")):
        try:
            with winreg.OpenKey(hive, root + sub) as k:
                if winreg.QueryValueEx(k, "Value")[0] == "Deny":
                    return True
        except OSError:
            pass
    return False


def mic_failed(e, where="mic"):
    """A mic that does not open must not kill dictation: log it, show why on the card, the caller gives up."""
    print(f"{where} unavailable: {e}", flush=True)
    ov("info", "Доступ к микрофону выключен: Параметры Windows, Конфиденциальность, Микрофон." if mic_denied()
       else "Микрофон не открылся: он подключён и выбран в настройках SaySo (вкладка «Голос»)?",
       "Микрофон", 8000)


MIC_EXE = os.path.join(HERE, "mic_capture", "mic_capture.exe")
MIC_MODE = "normal"  # "speech" while the driver's own processing cleans the mic (mic_capture.exe)


class HelperMic:
    """The mic through mic_capture.exe (WASAPI with an audio category): same callback and blocks as the
    sounddevice stream, 16 kHz float32. Setting mic_processing: auto (default) / normal."""

    def __init__(self, mode, device):
        self.p = subprocess.Popen([MIC_EXE, "--mode", mode, "--device", device], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=0x08000000)
        head = self.p.stdout.readline().decode("utf-8", "replace").strip()
        if not head or head.startswith("ERR"):
            self.p.kill()
            raise RuntimeError(head or "mic_capture.exe gave no answer")
        rate, ch, self.mode, self.name = (head.split(" ", 3) + ["", ""])[:4]
        self.rate, self.ch = int(rate), int(ch)
        self.active = True
        threading.Thread(target=self._run, daemon=True, name="mic-helper").start()

    def _run(self):
        frame = self.rate // 100 * self.ch * 4  # 10 ms
        pending = np.zeros((0, self.ch), np.float32)
        try:
            while self.active:
                b = self.p.stdout.read(frame)
                if not b:
                    break
                a = np.frombuffer(b[:len(b) // (4 * self.ch) * 4 * self.ch], np.float32).reshape(-1, self.ch)
                if self.rate != RATE:
                    k = self.rate // RATE
                    if self.rate % RATE == 0:  # 48 kHz: average of 3, enough for Whisper's band
                        a = a[:len(a) // k * k].reshape(-1, k, self.ch).mean(1)
                    else:
                        idx = np.arange(0, len(a), self.rate / RATE)
                        a = np.stack([np.interp(idx, np.arange(len(a)), a[:, c]) for c in range(self.ch)], 1)
                pending = np.concatenate([pending, a.astype(np.float32)])
                while len(pending) >= 800:  # the blocksize of the sounddevice stream
                    block, pending = pending[:800], pending[800:]
                    on_audio(block, 800, None, None)
        except Exception as e:
            print(f"mic helper stopped: {e!r}", flush=True)
        self.active = False

    def stop(self):
        self.active = False
        try:
            self.p.stdin.close()
            self.p.wait(2)
        except Exception:
            self.p.kill()

    def close(self):
        if self.p.poll() is None:
            self.p.kill()


def sysfx_off(name):
    """True when Windows "Audio enhancements" are off for the capture endpoint called `name`. Speech mode then
    gets no driver gain (the voice is ~3-4x quieter) and its silence gate drops real phrases."""
    import winreg
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                ep = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, ep + r"\Properties") as k:
                        short = winreg.QueryValueEx(k, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")[0]
                        desc = winreg.QueryValueEx(k, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")[0]
                    if name != f"{short} ({desc})":
                        continue
                    with winreg.OpenKey(root, ep + r"\FxProperties") as k:
                        return winreg.QueryValueEx(k, "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5")[0] == 1
                except OSError:
                    continue
    except OSError:
        pass
    return False


def open_mic():
    global stream, MIC_MODE
    if (voice_settings.get("mic_processing") or "auto") != "normal" and os.path.exists(MIC_EXE):
        try:
            stream = HelperMic("auto", voice_settings.get("mic_device") or "")
            MIC_MODE = stream.mode
            print(f"mic: {stream.name}, {stream.mode} mode ({stream.rate} Hz)", flush=True)
            if MIC_MODE == "speech" and sysfx_off(stream.name):
                MIC_MODE = "normal"  # no driver gain: the normal silence gate, or quiet speech is dropped
                print("audio enhancements are off for this mic: normal silence gate", flush=True)
            others = mic_users()
            if stream.mode == "speech" and others:
                # a second capture in speech mode broke the driver's processing (music -47 -> -28 dBFS)
                print(f"other apps hold the mic, the hardware processing may not work: {', '.join(others)}", flush=True)
            return
        except Exception as e:
            print(f"mic helper failed, plain capture: {e}", flush=True)
    MIC_MODE = "normal"
    dev = mic_index()
    stereo = bool(voice_settings.get("tts_one_speaker"))
    try:
        stereo = stereo and sd.query_devices(dev if dev is not None else sd.default.device[0])["max_input_channels"] >= 2
    except Exception:
        stereo = False
    stream = sd.InputStream(samplerate=RATE, channels=2 if stereo else 1, dtype="float32", blocksize=800,
                            callback=on_audio, device=dev)
    stream.start()


def make_beep(name, freq, ms=70, volume=0.35):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        t = np.arange(int(44100 * ms / 1000)) / 44100
        env = np.minimum(1, np.minimum(t, t[-1] - t) / 0.008)  # 8 ms fade in/out, no clicks
        pcm = (np.sin(2 * np.pi * freq * t) * env * volume * 32767).astype(np.int16)
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(pcm.tobytes())
    return path


BEEP_START, BEEP_STOP = make_beep("beep_start.wav", 1200), make_beep("beep_stop.wav", 700)
BEEP_FOLLOW = make_beep("beep_follow.wav", 990, 55, 0.18)  # soft "still listening" after an answer


def beep(path):
    if voice_settings.get("beeps") is False:  # Settings -> Голос -> "Звуковой сигнал": the tones can be turned off
        return
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


def ctrl_v():
    # Virtual-key codes, independent of the active keyboard layout.
    kb = user32.keybd_event
    kb(0x11, 0, 0, 0); kb(0x56, 0, 0, 0)
    kb(0x56, 0, 2, 0); kb(0x11, 0, 2, 0)


def paste(text):
    """Ctrl+V through the clipboard, out of the Win+V history; the previous content (pictures and files too) comes
    back after (clip.py)."""
    import clip
    old = clip.save()
    if not clip.set_text(text):
        pyperclip.copy(text)  # the clipboard was busy: the old way rather than losing the phrase
    time.sleep(0.05)
    ctrl_v()
    try:
        agent().note_typed(text)  # "отправь" and the editing commands work in this window afterwards
    except Exception:
        pass
    time.sleep(0.3)
    clip.restore(old)


# On trailing silence Whisper appends subtitle credits from its training data ("Субтитры субтитров О.Мастеров",
# "Редактор субтитров А.Синецкая", "Продолжение следует..."): cut them off the end of the text
# (capital "Субтитры" only: "включи субтитры" at the end of a real phrase stays)
TAIL_JUNK = re.compile(r"\s*(?:(?:Редактор|Корректор|Автор)\w*\s+субтитр\w*|Субтитр\w*)"
                       r"(?i:\s+(?:субтитр\w*|сделал\w*|создавал\w*|подогнал\w*|редактор\w*|корректор\w*|"
                       r"\w\.\s?\w+|[a-z]\w*|\w{1,2}))*\W*$|"  # "\w{1,2}": the cut-off "Субтитры в"
                       r"\s*(?i:продолжение следует|спасибо за просмотр|подписывайтесь на канал)\W*$")


def is_silence_or_noise(text, audio=None, wake_gate=False):
    if audio is not None and len(audio) > 0:
        rms = float(np.sqrt(np.mean(audio**2)))
        # speech mode leaves a faint hiss around -46 dBFS on which Whisper invents words ("Смешно", "Счастье");
        # the user's speech there is about -27 dBFS
        if rms < (0.008 if MIC_MODE == "speech" else 0.003):
            return True
        # what is left of the assistant's own answer in speech mode is about 0.011 rms, the user ~0.04-0.1:
        # a stricter gate for the wake listener only (dictation clips carry silence and average lower)
        if wake_gate and MIC_MODE == "speech" and rms < 0.02:
            return True
    t = text.strip().lower()
    if not t:
        return True
    # digits count as content: counting aloud comes back as "1, 2, 3, 4, 5." and was dropped as noise
    # (the wizard's mic test "heard nothing", dictated numbers vanished)
    letters = re.sub(r'[\W_]+', '', t)
    if len(letters) <= 1:
        return True
    hallucinations = [
        r'^продолжение\s+следует',
        r'^спасибо\s+за\s+просмотр',
        r'^трансляция\s*$',
        r'^подпишись\s*$',
        r'^[\[(]?(музыка|аплодисменты|смех|тишина)[\])]?\W*$',
        r'^[\[(]?(\w+(ая|яя)\s+){1,2}музыка[\])]?\W*$',  # "БОДРАЯ МУЗЫКА", "[спокойная музыка]"
        r'^(\w)\1{4,}\W*$',                     # "Ммммм...", "Ооооо"
    ]
    if any(re.search(p, t) for p in hallucinations):
        return True
    # subtitle credits only when they are the whole text: "Субтитры в пришли как артефакт" is real speech
    if not TAIL_JUNK.sub("", text.strip()).strip():
        return True
    # Whisper small invents a lone word ("Счастье.") on a short, nearly empty press: drop those words
    # only for short clips, so a real word in longer dictation is kept
    short = audio is not None and len(audio) < RATE * 1.5
    # ("спасибо", "пока", "всё" are real short commands: they end the conversation, never filter them)
    return short and bool(re.fullmatch(r'(сч[ае]стье|угу|ага|ну|хм+)\W*', t))


def speech_to_text(audio, quiet=False, wake_gate=None):
    """Whisper on the NPU. quiet=True (wake listener): background speech is never logged. wake_gate (default:
    quiet) adds the stricter level gate meant for short wake segments; a fixed-length test clip is mostly
    silence and must skip it, or the wizard's mic test and the name trainer hear nothing."""
    if len(audio) < RATE * 0.3:
        return ""
    kw = {"task": "transcribe"}
    if LANG:
        kw["language"] = LANG
    t = time.time()
    with whisper_lock:
        text = pipe.generate(audio.tolist(), **kw).texts[0].strip()
    trim_memory()
    # Whisper loops on a short repeated sound: a lone "Мурмур" came back as "Мурмурмурмур…" (hundreds of
    # times). Three or more repeats of one 2-5 letter unit collapse to two ("мурмур").
    text = re.sub(r"(?i)(\w{2,5}?)\1{2,}", r"\1\1", text)
    text = TAIL_JUNK.sub("", text).strip() or text  # junk alone is dropped by is_silence_or_noise
    if is_silence_or_noise(text, audio, wake_gate=quiet if wake_gate is None else wake_gate):
        if not quiet:
            print(f"[{time.time() - t:.1f}s] (ignored silence/noise: {text!r})", flush=True)
        return ""
    if not quiet:
        print(f"[{time.time() - t:.1f}s] {text}", flush=True)
    return text


OVERLAY_EXE = os.path.join(HERE, "overlay", "bin", "publish", "ClawOverlay.exe")
_ov_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def assistant_title():
    return voice_settings.get("assistant_name") or "Сэйсо"


def ov(kind, text="", title=None, ms=0):
    """State pill in ClawOverlay ("Слушаю…" while the button is held). Fire-and-forget UDP."""
    msg = json.dumps({"kind": kind, "title": title or assistant_title(), "text": text, "ms": ms}, ensure_ascii=False)
    try:
        _ov_sock.sendto(msg.encode("utf-8"), ("127.0.0.1", 47330))
    except OSError:
        pass


def start_overlay():
    h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\ClawOverlay")
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
    elif os.path.exists(OVERLAY_EXE):
        subprocess.Popen([OVERLAY_EXE], cwd=os.path.dirname(OVERLAY_EXE))


def transcribe(audio, m):
    text = speech_to_text(audio)
    if not text:
        ov("hide")
        return
    if m == "agent":
        command(text)  # shows «text» while Gemma works
    else:
        a = agent()
        # a whole utterance that is an editing command ("удали слово", "новая строка", "отправить") is done,
        # not typed; "напиши удали" types the word itself
        m = re.match(r"(?i)^\W*(?:напиши|пиши)\s+(.+)$", text)
        if m:
            text = m.group(1)
        else:
            edited = a.edit_route(text, dictation=True)
            if edited:
                ov("ok", edited, "Диктовка", 1200)
                print(f"[edit] {edited}", flush=True)
                return
        text = a.shape_dictation(text)
        ov("info", text, "Диктовка", 1800)
        paste(text + " ")


LISTEN_OFF = re.compile(r"(?:выключи|отключи|останови|прекрати|выруби|отмени)\w*\s+(?:режим\s+)?"
                        r"(?:прослуш|слушан|слушать|подслуш)|не\s+слушай|хватит\s+слушать", re.I)
LISTEN_ON = re.compile(r"(?:включи|запусти|начни|вруби|верни)\w*\s+(?:режим\s+)?(?:прослуш|слушан|слушать)"
                       r"|слушай\s+(?:меня|постоянно|всегда)", re.I)


def set_listening(on):
    global listening
    listening = on
    with open(LISTEN_FILE, "w") as f:
        f.write("1" if on else "0")
    if not on:
        msg = "Слушание выключено. Зови кнопкой."
    elif on_ac():
        msg = f"Слушаю. Зови: {assistant_title()}."
    else:
        msg = "Слушание включено, заработает от зарядки."
    print(f"listening {'on' if on else 'off'}", flush=True)
    agent().report(msg, vocal=True)


# ---------------- quieter speakers while a button listens. One worker keeps the on/off order; the old level is also
# in a file, so a crash in between cannot leave the volume low after the next start.
DUCK_FILE = os.path.join(voice_settings.USER_DIR, "duck_volume.txt")
_duck_q = queue.Queue()
_duck_id = 0


def duck(sec):
    """On for at most sec: a phrase that never reaches a command must not leave the speakers quiet."""
    global _duck_id
    _duck_id += 1
    sid = _duck_id
    _duck_q.put(True)
    threading.Timer(sec, lambda: sid == _duck_id and unduck()).start()


def unduck():
    global _duck_id
    _duck_id += 1
    _duck_q.put(False)


def duck_worker():
    was = None
    try:
        with open(DUCK_FILE, encoding="utf-8") as f:
            state = f.read()
        os.remove(DUCK_FILE)
        if not state.strip().isdigit():  # (a lone number is a master volume from an older version: skip it)
            agent().duck_off(state)
    except (OSError, ValueError):
        pass
    except Exception as e:
        print(f"duck restore failed: {e!r}", flush=True)
    while True:
        want = _duck_q.get()
        try:
            if want and was is None:
                time.sleep(0.2)  # the start beep plays at the normal volume
                was = agent().duck_on()
                if was is not None:
                    with open(DUCK_FILE, "w", encoding="utf-8") as f:
                        f.write(was)
            elif not want and was is not None:
                agent().duck_off(was)
                was = None
                os.remove(DUCK_FILE)
        except Exception as e:
            print(f"duck failed: {e!r}", flush=True)


def command(text):
    """A spoken command (Gemma button or wake phrase). Listening on/off is handled here, the
    rest goes to the agent. The speakers stay quieter: the answer and the follow-up window come next."""
    ov("think", f"«{text}»")
    if LISTEN_OFF.search(text):
        set_listening(False)
    elif LISTEN_ON.search(text):
        set_listening(True)
    else:
        agent().handle_async(text)


FOLLOW_SEC = 6          # Alice-style: after any answer the assistant listens this long without the wake word
followup_until = 0.0


def in_followup(extra=0.0):
    return time.time() < followup_until + extra


# ---------------- name trainer (SaySo tray "Тренировать имя"): UDP 127.0.0.1:47323
# {"cmd": "record", "sec": 3} -> records that long from the mic, answers {"text": "<Whisper transcript>"}.
# While it records, the wake listener gets no audio, so saying the name does not run commands.
TRAIN = {"buf": None}
TRAIN_PORT = 47323


def run_trainer_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", TRAIN_PORT))
    while True:
        data, addr = s.recvfrom(1024)
        try:
            req = json.loads(data)
            if req.get("cmd") == "ping":  # the wizard asks first, so it says "Говори" only when this is ready
                s.sendto(b'{"ok": true}', addr)
                continue
            sec = max(1.0, min(8.0, float(req.get("sec", 3))))
            with lock:
                if stream is None or not stream.active:
                    open_mic()
                    time.sleep(0.5)  # a cold mic (and the driver's processing) needs a moment before audio flows
            TRAIN["buf"] = []
            time.sleep(sec)
            audio = np.concatenate(TRAIN["buf"]) if TRAIN["buf"] else np.zeros(0, np.float32)
            TRAIN["buf"] = None
            rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
            print(f"[mic test] {len(audio) / RATE:.1f} s, rms {rms:.4f}, peak {float(np.abs(audio).max()) if len(audio) else 0:.3f},"
                  f" {MIC_MODE} mode", flush=True)
            # logged like dictation (the user speaks on purpose here): the next line shows what Whisper returned
            # and whether the noise filter dropped it
            text = speech_to_text(audio, wake_gate=False) if len(audio) else ""
            s.sendto(json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"), addr)
        except Exception as e:
            TRAIN["buf"] = None
            s.sendto(json.dumps({"error": str(e)}).encode("utf-8"), addr)


def wake_active():
    # the VAD runs for the wake phrase (AC + listening on) and, on any power, during a follow-up window
    # (+15 s so a phrase that started inside the window can finish) or while the voice talks (barge-in)
    return not recording and stream is not None and _vs.assistant_on() and \
        ((listening and on_ac()) or in_followup(15) or barge)


barge = False           # the TTS voice is talking: "стоп" said over it cuts it
STOP_WORDS = re.compile(r"\b(стоп|ступ|хватит|замолчи|молчи|умолкни|помолчи|достаточно|тихо)\b")


def on_speaking(on):
    """Called by gemma_agent around the spoken answer: keep the mic open (also on battery) to hear "стоп"."""
    global barge
    with lock:
        if on and not recording and (stream is None or not stream.active):
            try:
                open_mic()
            except Exception as e:
                print(f"barge-in mic failed: {e}", flush=True)
        barge = on
    if wake_listener is not None:
        wake_listener.barge = on
    if on:
        duck(180)  # quieter speakers under the answer, like a smart speaker
    else:  # back to the old volume, unless a follow-up window opens now (it restores the volume when it ends)
        threading.Timer(1.0, lambda: not recording and not in_followup() and unduck()).start()
    print(f"[barge] {time.strftime('%H:%M:%S')} listening for a stop word: {'on' if on else 'off'}, "
          f"mic open: {stream is not None and stream.active}", flush=True)


def barge_text(text):
    """A rolling transcript of the last ~1.6 s while the voice talks. Mostly it is the voice itself
    (speakers -> mic), so only a stop word that is NOT in the answer being read counts."""
    m = STOP_WORDS.search(text.lower()) or re.fullmatch(r"\W*(сто|100|топ)\W*", text.lower())  # Whisper: "Сто.", "100", "Топ." over the voice
    if not m or re.search(rf"\b{m.group(1)}\b",agent().SPOKEN["text"]):
        return False
    print(f"[barge-in] {text}", flush=True)
    # "стоп" ends the dialog: a follow-up window here only caught the echo tail ("МАК", "И по") as commands
    agent().CTX["end"] = True
    agent().stop_speaking()
    return True


def open_followup(sound=None):
    """Called by gemma_agent after an answer has been spoken (and by a Gemma button tap): listen
    FOLLOW_SEC more seconds."""
    global followup_until
    with lock:
        if recording:
            return
        if stream is None or not stream.active:
            try:
                open_mic()  # battery: the mic sleeps between commands, wake it for the window
            except Exception as e:
                return mic_failed(e)
    sec = voice_settings.get("follow_sec")
    sec = FOLLOW_SEC if sec is None else sec
    try:  # conversation mode: a longer pause to think of the next phrase
        sec = max(sec, _agent.chat_followup()) if sec > 0 else sec
    except Exception:
        pass
    if sec <= 0:  # follow-up after answers turned off in the tray Settings; a button tap still listens
        if sound is None:
            return
        sec = FOLLOW_SEC
    followup_until = time.time() + sec
    if wake_listener is not None:
        wake_listener.awaiting_until = followup_until
    beep(sound or BEEP_FOLLOW)
    duck(sec + 20)  # the follow-up window listens too: quieter speakers until it ends (end_followup)
    ov("mic", ms=int(sec * 1000))
    threading.Timer(sec + 0.3, end_followup).start()


def quiet_after_command():
    """A command that needs no next phrase is done: close the listening window the command button opened and bring
    the sound back now, not when that window (the "mic" pill) runs out."""
    global followup_until
    if recording or barge:
        return
    followup_until = 0.0
    if wake_listener is not None:
        wake_listener.awaiting_until = 0.0
    unduck()
    ov("mic_off")


def end_followup():
    if in_followup() or (wake_listener is not None and wake_listener.speech):
        return  # renewed, or a phrase is still being spoken: its command will open a new window
    if not recording and not barge:  # (a window that ends while the answer still plays keeps the speakers quiet)
        unduck()
    ov("mic_off")


_agent, _agent_mtime = None, 0


def _mtime(mod):
    files = mod.watched_files()
    return (len(files), max(os.path.getmtime(f) for f in files if os.path.exists(f)))


def agent():
    # Reload gemma_agent only when the file changed: keeps its timers and app cache between calls.
    global _agent, _agent_mtime
    import importlib
    if _agent is None:
        import gemma_agent as _agent
        _agent_mtime = _mtime(_agent)
    else:
        mt = _mtime(_agent)
        if mt != _agent_mtime:  # agent, net_skills, plugin system or a plugin file changed (or a plugin added)
            _agent = importlib.reload(_agent)
            _agent_mtime = _mtime(_agent)
    _agent.after_reply = open_followup  # set on every call: a reload creates a fresh module
    _agent.after_quiet = quiet_after_command
    _agent.on_speaking = on_speaking
    return _agent


last_stop_time = 0


ONE_TAP_SEC = 0.25  # one-button mode: a release before this is a tap (the command; a clicky key taps in ~0.1 s)
tap_pending = False  # one-button press not yet known to be a hold: no beep so far ("Слушаю…" shows at once)


def one_button():
    """Setting one_button: the dictation key alone does both (tap = command, hold = dictation). Needs the assistant."""
    return bool(_vs.get("one_button")) and _vs.assistant_on()


def _hold_confirmed(sid):
    """One-button mode: still held after ONE_TAP_SEC, so it is dictation: now the "speak" beep."""
    global tap_pending
    with lock:
        if not recording or session != sid or not tap_pending:
            return
        tap_pending = False
    beep(BEEP_START)
    ov("listen", "Слушаю…", "Диктовка")


def start(m="paste"):
    global recording, chunks, session, mode, tap_pending
    if m == "agent" and not _vs.assistant_on():  # dictation-only install: the command button has nothing to run
        ov("info", "Установлена только диктовка. Помощника включают в настройках SaySo", None, 4000)
        return
    if m == "agent":  # Gemma button is a tap: open the 6 s listening window, no hold needed
        threading.Thread(target=lambda: agent().stop_speaking(), daemon=True).start()
        open_followup(BEEP_START)
        duck(25)  # the window, a phrase started at its end (up to 15 s) and Whisper
        return
    with lock:
        # Debounce macro jitter here (not in stop): a bounce right after a release must not
        # start a new recording; ignoring a stop instead would leave the recording running.
        if recording or time.time() - last_stop_time < 0.3:
            return
        mode = m
        if stream is not None and stream.active:
            chunks = list(preroll)  # warm mic: include the moment just before the press
        else:
            chunks = []
            t_open = time.time()
            try:
                open_mic()  # cold mic (battery): 0.1-2 s to wake
            except Exception as e:
                return mic_failed(e)
            print(f"cold mic opened in {time.time() - t_open:.2f}s", flush=True)  # the start of a phrase lost meanwhile
        # one-button mode: recording starts now (nothing of the phrase is lost), but tap or hold is known only after
        # ONE_TAP_SEC; the beep and the card wait for that (_hold_confirmed)
        tap_pending = m == "paste" and one_button()
        if not tap_pending:
            beep(BEEP_START)  # after the mic is live, so the beep means "speak now"
        # the card at once, also in the one-button mode: recording runs from the press, waiting for it lost words
        # one-button press: the assistant's name until it is known (a tap turns into the command window, a hold into "Диктовка")
        ov("listen", "Слушаю…", None if tap_pending or m == "agent" else "Диктовка")  # None: the assistant's name
        duck(MAX_SEC + 5)
        threading.Thread(target=lambda: agent().stop_speaking(), daemon=True).start()  # pressing talks over the voice
        recording = True
        session += 1
        sid = session
    if tap_pending:
        threading.Timer(ONE_TAP_SEC, _hold_confirmed, args=(sid,)).start()
    threading.Timer(MAX_SEC, lambda: session == sid and stop()).start()


def stop():
    global recording, last_stop_time, tap_pending
    with lock:
        if not recording:
            return
        last_stop_time = time.time()
        recording = False
        tap = tap_pending  # released before ONE_TAP_SEC: the command, the bit of audio is dropped
        tap_pending = False
        if not want_warm() and not tap and not recently_used():  # (open for MIC_LINGER_SEC: manage_mic closes it)
            close_mic()
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if tap:
        start("agent")
        return
    unduck()
    beep(BEEP_STOP)
    ov("think", "Распознаю…", None if mode == "agent" else "Диктовка")
    threading.Thread(target=transcribe, args=(audio, mode), daemon=True).start()


def helper_alive(tries=6):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    for _ in range(tries):
        try:
            s.sendto(b"ping", PING)
            if s.recvfrom(64)[0] == b"pong":
                return True
        except OSError:
            time.sleep(0.5)
    return False


def manage_mic():
    """Warm mic on AC, closed on battery; also reopens it after a device change or sleep."""
    last_err, err_at = "", 0.0
    while True:
        time.sleep(5)
        try:  # listen_state.txt changed from outside (Desktop bat): apply it
            want = open(LISTEN_FILE).read().strip() != "0"
            if want != listening:
                set_listening(want)
        except OSError:
            pass
        with lock:
            if recording:
                continue
            warm, active = want_warm() or in_followup(15) or barge or recently_used(), stream is not None and stream.active
            if last_err and time.time() - err_at < 60:  # no mic: try again once a minute, not every 5 s
                continue
            try:
                if warm and not active:
                    close_mic()
                    open_mic()
                    print("mic warm (AC power)", flush=True)
                elif not warm and stream is not None:
                    close_mic()
                    print("mic closed (battery)", flush=True)
            except Exception as e:
                if str(e) != last_err:
                    print(f"mic switch failed: {e}", flush=True)
                last_err, err_at = str(e), time.time()
                continue
            last_err = ""


def run_helper_events():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(EVENTS)
    print("ready (elevated hotkey helper). hold M1 / Ctrl+Win+Shift+G to talk", flush=True)
    while True:
        msg = s.recvfrom(64)[0].decode(errors="ignore").split()
        if msg[:1] == ["down"]:
            start(msg[1] if len(msg) > 1 else "paste")
        elif msg[:1] == ["up"]:
            stop()


def run_own_hotkeys():
    global HOTKEYS
    for hid, (mods, vk, _, name) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
            print(f"{name} is already taken (another dictate.py running?)", flush=True)
            sys.exit(1)
    checked = time.time()
    print("ready (own hotkeys, no helper). hold " + " / ".join(h[3] for h in HOTKEYS.values()) + " to talk",
          flush=True)
    msg = wt.MSG()
    key_vk, held, gone, m = None, False, 0, "paste"
    while True:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            if msg.message == 0x0312 and msg.wParam in HOTKEYS and not held:  # WM_HOTKEY
                _, key_vk, m, _ = HOTKEYS[msg.wParam]
        if key_vk and not user32.GetAsyncKeyState(key_vk) & 0x8000:
            key_vk = None
        if key_vk:
            gone = 0
            if not held:
                held = True
                start(m)
        elif held:
            gone += 1
            if gone >= RELEASE_POLLS:
                held = False
                stop()
        elif time.time() - checked > 1:  # new combinations from the Settings window
            checked = time.time()
            keys = voice_settings.hotkeys()
            if keys != HOTKEYS:
                for hid in HOTKEYS:
                    user32.UnregisterHotKey(None, hid)
                ok = all(user32.RegisterHotKey(None, h, k[0] | MOD_NOREPEAT, k[1]) for h, k in keys.items())
                if not ok:  # taken by another app: back to the previous set
                    for hid in keys:
                        user32.UnregisterHotKey(None, hid)
                    for h, k in HOTKEYS.items():
                        user32.RegisterHotKey(None, h, k[0] | MOD_NOREPEAT, k[1])
                    print("new hotkeys are taken, kept the old ones", flush=True)
                else:
                    HOTKEYS = keys
                    print("hotkeys: " + ", ".join(k[3] for k in keys.values()), flush=True)
        time.sleep(POLL)


try:
    import wake
    wake_listener = wake.WakeListener(os.path.join(HERE, "silero_vad.onnx"),
                                      transcribe=lambda a: speech_to_text(a, quiet=True), on_command=command,
                                      active=wake_active,
                                      beep=lambda: (beep(BEEP_START), ov("listen", "Слушаю…"), duck(25),
                                                    threading.Thread(target=lambda: agent().stop_speaking(),
                                                                     daemon=True).start()),
                                      log=lambda m: print(m, flush=True), on_barge=barge_text)
    print(f"wake phrase ready, listening {'on' if listening else 'off'} (works on AC power)", flush=True)
except Exception as e:
    print(f"wake listener disabled: {e!r}", flush=True)
start_overlay()
if want_warm():
    try:
        open_mic()
    except Exception as e:
        mic_failed(e)
threading.Thread(target=manage_mic, daemon=True).start()
threading.Thread(target=run_trainer_port, daemon=True).start()
threading.Thread(target=duck_worker, daemon=True).start()


def claw_profiles_worker():
    """Setting claw_buttons_auto: M1/M2 into the gamepad profile ClawTweaks makes (blank) for each new game profile."""
    try:
        import claw_buttons
    except Exception as e:
        print(f"claw buttons unavailable: {e!r}", flush=True)
        return
    while True:
        time.sleep(10)
        try:
            game = claw_buttons.fill_new_game_profiles(lambda m: print(m, flush=True))
            if game:
                ov("info", f"Кнопки M1 и M2 добавлены в профиль геймпада игры {game}", "ClawTweaks", 5000)
        except Exception as e:
            print(f"claw buttons: {e!r}", flush=True)


threading.Thread(target=claw_profiles_worker, daemon=True).start()
threading.Thread(target=lambda: agent().warm(), daemon=True).start()  # first command must not pay startup
if helper_alive():
    run_helper_events()
else:
    run_own_hotkeys()
