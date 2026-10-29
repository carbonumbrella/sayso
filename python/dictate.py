# Push-to-talk dictation: Whisper (OpenVINO), text pasted into the focused window.
# Device: NPU default with whisper-small (~0.35s per 10s speech, iGPU stays free for games).
# DICTATE_DEVICE=GPU or CPU switches to large-v3-turbo (GPU ~0.4s, CPU ~7.5s). large-v3-turbo
# on the NPU hits DEVICE_LOST under OV 2026.4.1, so the NPU gets the small model only.
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
# Wake phrase "привет, Гемма ..." (wake.py: Silero VAD + this Whisper) works on AC power while
# listening is on; "выключи/включи слушание" toggles it (by voice or via the Gemma button), the
# state survives restarts in listen_state.txt.
import os, re, sys, json, time, socket, subprocess, threading, winsound, ctypes, collections, wave
import ctypes.wintypes as wt
import numpy as np
import sounddevice as sd
import pyperclip
import openvino_genai as ov_genai
import voice_settings  # follow_sec, warm_mic (written by the ClawVoice tray Settings window)

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.stdout is None:  # pythonw (autostart): no console, log to a file instead
    sys.stdout = sys.stderr = open(os.path.join(HERE, "dictate.log"), "w", encoding="utf-8", buffering=1)
import voice_settings as _vs  # stt_device / language from SaySo Settings (env vars still override)


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
    """small on the NPU (medium/large crash the Claw's NPU) and when nothing else is there; large-v3-turbo
    on a GPU/CPU if downloaded (better accuracy), else small."""
    small, large = os.path.join(HERE, "whisper-small-int8-ov"), os.path.join(HERE, "whisper-large-v3-turbo-int8-ov")
    if os.environ.get("DICTATE_MODEL"):
        return os.path.join(HERE, os.environ["DICTATE_MODEL"])
    return large if device != "NPU" and os.path.isdir(large) else small


DEVICE = _pick_device()
MODEL = _model_for(DEVICE)
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
_noted = DEVICE != "CPU" and not _crashed_on()
if _noted:
    with open(LOADING_FILE, "w") as _f:
        _f.write(DEVICE)
try:
    pipe = ov_genai.WhisperPipeline(MODEL, DEVICE, CACHE_DIR=os.path.join(HERE, "npu_cache"))
except Exception as e:  # an old NPU/GPU driver: fall back instead of leaving the user without dictation
    print(f"Whisper on {DEVICE} failed ({e!r}), falling back", flush=True)
    for DEVICE in [d for d in ("GPU", "CPU") if d != DEVICE]:
        try:
            MODEL = _model_for(DEVICE)
            pipe = ov_genai.WhisperPipeline(MODEL, DEVICE, CACHE_DIR=os.path.join(HERE, "npu_cache"))
            break
        except Exception as e2:
            print(f"Whisper on {DEVICE} failed too ({e2!r})", flush=True)
    else:
        raise
if _noted:  # loaded: not a crash. A note we skipped (previous crash) stays, the uninstaller removes it
    os.remove(LOADING_FILE)
print(f"Whisper {os.path.basename(MODEL)} on {DEVICE}, language {LANG or 'auto'}", flush=True)
whisper_lock = threading.Lock()  # hotkey recordings and the wake listener share one pipeline
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


def on_audio(data, frames, t, status):
    block = data[:, 0].copy()
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


def open_mic():
    global stream
    stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=800, callback=on_audio,
                            device=mic_index())
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
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


def ctrl_v():
    # Virtual-key codes, independent of the active keyboard layout.
    kb = user32.keybd_event
    kb(0x11, 0, 0, 0); kb(0x56, 0, 0, 0)
    kb(0x56, 0, 2, 0); kb(0x11, 0, 2, 0)


def paste(text):
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    ctrl_v()
    try:
        agent().note_typed(text)  # "отправь" and the editing commands work in this window afterwards
    except Exception:
        pass
    time.sleep(0.3)
    if old is not None:
        pyperclip.copy(old)


# On trailing silence Whisper appends subtitle credits from its training data ("Субтитры субтитров О.Мастеров",
# "Редактор субтитров А.Синецкая", "Продолжение следует..."): cut them off the end of the text
# (capital "Субтитры" only: "включи субтитры" at the end of a real phrase stays)
TAIL_JUNK = re.compile(r"\s*(?:(?:Редактор|Корректор|Автор)\w*\s+субтитр\w*|Субтитр\w*)"
                       r"(?i:\s+(?:субтитр\w*|сделал\w*|создавал\w*|подогнал\w*|редактор\w*|корректор\w*|"
                       r"\w\.\s?\w+|[a-z]\w*|\w{1,2}))*\W*$|"  # "\w{1,2}": the cut-off "Субтитры в"
                       r"\s*(?i:продолжение следует|спасибо за просмотр|подписывайтесь на канал)\W*$")


def is_silence_or_noise(text, audio=None):
    if audio is not None and len(audio) > 0:
        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < 0.003:
            return True
    t = text.strip().lower()
    if not t:
        return True
    letters = re.sub(r'[\W\d_]+', '', t)
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


def speech_to_text(audio, quiet=False):
    """Whisper on the NPU. quiet=True (wake listener): background speech is never logged."""
    if len(audio) < RATE * 0.3:
        return ""
    kw = {"task": "transcribe"}
    if LANG:
        kw["language"] = LANG
    t = time.time()
    with whisper_lock:
        text = pipe.generate(audio.tolist(), **kw).texts[0].strip()
    # Whisper loops on a short repeated sound: a lone "Мурмур" came back as "Мурмурмурмур…" (hundreds of
    # times). Three or more repeats of one 2-5 letter unit collapse to two ("мурмур").
    text = re.sub(r"(?i)(\w{2,5}?)\1{2,}", r"\1\1", text)
    text = TAIL_JUNK.sub("", text).strip() or text  # junk alone is dropped by is_silence_or_noise
    if is_silence_or_noise(text, audio):
        if not quiet:
            print(f"[{time.time() - t:.1f}s] (ignored silence/noise: {text!r})", flush=True)
        return ""
    if not quiet:
        print(f"[{time.time() - t:.1f}s] {text}", flush=True)
    return text


OVERLAY_EXE = os.path.join(HERE, "overlay", "bin", "publish", "ClawOverlay.exe")
_ov_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def ov(kind, text="", title="Gemma", ms=0):
    """State pill in ClawOverlay ("Слушаю…" while the button is held). Fire-and-forget UDP."""
    msg = json.dumps({"kind": kind, "title": title, "text": text, "ms": ms}, ensure_ascii=False)
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
        msg = "Слушаю. Скажи: привет, Гемма."
    else:
        msg = "Слушание включено, заработает от зарядки."
    print(f"listening {'on' if on else 'off'}", flush=True)
    agent().report(msg, vocal=True)


def command(text):
    """A spoken command (Gemma button or wake phrase). Listening on/off is handled here, the
    rest goes to the agent."""
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
            sec = max(1.0, min(8.0, float(req.get("sec", 3))))
            with lock:
                if stream is None or not stream.active:
                    open_mic()
            TRAIN["buf"] = []
            time.sleep(sec)
            audio = np.concatenate(TRAIN["buf"]) if TRAIN["buf"] else np.zeros(0, np.float32)
            TRAIN["buf"] = None
            text = speech_to_text(audio, quiet=True) if len(audio) else ""
            s.sendto(json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"), addr)
        except Exception as e:
            TRAIN["buf"] = None
            s.sendto(json.dumps({"error": str(e)}).encode("utf-8"), addr)


def wake_active():
    # the VAD runs for the wake phrase (AC + listening on) and, on any power, during a follow-up window
    # (+15 s so a phrase that started inside the window can finish) or while the voice talks (barge-in)
    return not recording and stream is not None and ((listening and on_ac()) or in_followup(15) or barge)


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


def barge_text(text):
    """A rolling transcript of the last ~1.6 s while the voice talks. Mostly it is the voice itself
    (speakers -> mic), so only a stop word that is NOT in the answer being read counts."""
    m = STOP_WORDS.search(text.lower()) or re.fullmatch(r"\W*(сто)\W*", text.lower())  # Whisper: "Сто."
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
            open_mic()  # battery: the mic sleeps between commands, wake it for the window
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
    ov("mic", ms=int(sec * 1000))
    threading.Timer(sec + 0.3, end_followup).start()


def end_followup():
    if in_followup() or (wake_listener is not None and wake_listener.speech):
        return  # renewed, or a phrase is still being spoken: its command will open a new window
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
    _agent.on_speaking = on_speaking
    return _agent


last_stop_time = 0


def start(m="paste"):
    global recording, chunks, session, mode
    if m == "agent":  # Gemma button is a tap: open the 6 s listening window, no hold needed
        threading.Thread(target=lambda: agent().stop_speaking(), daemon=True).start()
        open_followup(BEEP_START)
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
            open_mic()  # cold mic (battery): 0.1-2 s to wake
        beep(BEEP_START)  # after the mic is live, so the beep means "speak now"
        threading.Thread(target=lambda: agent().stop_speaking(), daemon=True).start()  # pressing talks over the voice
        ov("listen", "Слушаю…", "Gemma" if m == "agent" else "Диктовка")
        recording = True
        session += 1
        sid = session
    threading.Timer(MAX_SEC, lambda: session == sid and stop()).start()


def stop():
    global recording, last_stop_time
    with lock:
        if not recording:
            return
        last_stop_time = time.time()
        recording = False
        if not want_warm():
            close_mic()
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    beep(BEEP_STOP)
    ov("think", "Распознаю…", "Gemma" if mode == "agent" else "Диктовка")
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
            warm, active = want_warm() or in_followup(15) or barge, stream is not None and stream.active
            try:
                if warm and not active:
                    close_mic()
                    open_mic()
                    print("mic warm (AC power)", flush=True)
                elif not warm and stream is not None:
                    close_mic()
                    print("mic closed (battery)", flush=True)
            except Exception as e:
                print(f"mic switch failed: {e}", flush=True)


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
                                      beep=lambda: (beep(BEEP_START), ov("listen", "Слушаю…"),
                                                    threading.Thread(target=lambda: agent().stop_speaking(),
                                                                     daemon=True).start()),
                                      log=lambda m: print(m, flush=True), on_barge=barge_text)
    print(f"wake phrase ready, listening {'on' if listening else 'off'} (works on AC power)", flush=True)
except Exception as e:
    print(f"wake listener disabled: {e!r}", flush=True)
start_overlay()
if want_warm():
    open_mic()
threading.Thread(target=manage_mic, daemon=True).start()
threading.Thread(target=run_trainer_port, daemon=True).start()
threading.Thread(target=lambda: agent().warm(), daemon=True).start()  # first command must not pay startup
if helper_alive():
    run_helper_events()
else:
    run_own_hotkeys()
