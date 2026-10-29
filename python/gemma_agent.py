# -*- coding: utf-8 -*-
# Voice assistant for the MSI Claw: dictate.py (hold Ctrl+Win+Shift+G) hands the transcribed
# phrase to handle(). Routing:
#   1. noise filter (Whisper hallucinations on silence)
#   2. fast_route(): plain Python regex for basic commands (brightness, volume, mic, night light,
#      media, time/battery/..., screenshot, timer, lock, settings pages, arithmetic). Instant, no model.
#   3. Gemma 3 4B on the NPU server (:8081) for everything else: picks ONE tool as JSON.
# Dangerous actions (power, force-kill, arbitrary PowerShell) always go through confirm.ps1, a
# modern WPF Yes/No dialog with auto-cancel. Feedback: Windows toast; voice for answers/info.
# Standalone: python gemma_agent.py "<phrase>"   (--dry: show the route without executing)
import os, re, sys, json, time, math, base64, ctypes, difflib, socket, subprocess, threading, winreg, queue
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
import voice_settings
import importlib, net_skills  # internet skills: location, weather, currency, Wikipedia, web answers
import plugins, commands as usercmd  # SaySo: plugins (route/tool/app hooks) and the user's command files
net_skills = importlib.reload(net_skills)  # dictate.py reloads this module on edits: refresh the helper too
plugins, usercmd = importlib.reload(plugins), importlib.reload(usercmd)

HERE = os.path.dirname(os.path.abspath(__file__))


def shots_dir():
    """The system Screenshots folder (where Win+PrtScn saves; may be in OneDrive or localized)."""
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
    g = GUID(0xB7BEDE81, 0xDF94, 0x4682, (ctypes.c_ubyte * 8)(0xA7, 0xD8, 0x57, 0xA5, 0x26, 0x20, 0xB8, 0x6F))
    p = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0x8000, None, ctypes.byref(p)) == 0:  # KF_FLAG_CREATE
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return path
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")


SHOTS = shots_dir()
PY = sys.executable.replace("pythonw.exe", "python.exe")
PYW = sys.executable.replace("python.exe", "pythonw.exe")
TELEGRAM = os.path.expanduser(r"~\.claude\skills\telegram-sender\scripts\send_telegram.py")
CONFIRM = os.path.join(HERE, "confirm.ps1")
CONFIRM_PORT = 47331  # ClawVoice tray app
NO_WINDOW = 0x08000000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def log(msg):
    print(f"[agent] {msg}", flush=True)


# ====================================================================== feedback

class PSHost:
    """One persistent powershell.exe (ps_host.ps1) for helper calls: ~10-50 ms per call instead of
    ~0.4-0.7 s to spawn PowerShell and recompile the audio C# every time."""

    def __init__(self):
        self.p, self.q, self.lock = None, None, threading.Lock()

    def _start(self):
        self.p = subprocess.Popen(["powershell", "-NoProfile", "-NoLogo", "-ExecutionPolicy", "Bypass", "-File",
                                   os.path.join(HERE, "ps_host.ps1")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.q = queue.Queue()
        threading.Thread(target=lambda p=self.p, q=self.q: [q.put(l) for l in p.stdout], daemon=True).start()
        self._send(f"Add-Type -TypeDefinition @'\n{_AUDIO_CS}\n'@", 60)

    def _send(self, cmd, timeout):
        self.p.stdin.write(base64.b64encode(cmd.encode("utf-8")) + b"\n")
        self.p.stdin.flush()
        rc, _, out = self.q.get(timeout=timeout).decode().strip().partition(" ")
        return int(rc), base64.b64decode(out).decode("utf-8", "replace").strip()

    def run(self, cmd, timeout=30):
        with self.lock:
            try:
                if self.p is None or self.p.poll() is not None:
                    self._start()
                return self._send(cmd, timeout)
            except Exception as e:  # hung or dead worker: drop it, next call starts a fresh one
                log(f"ps host error: {e!r}")
                try:
                    self.p.kill()
                except Exception:
                    pass
                self.p = None
                return 1, str(e)


_host = PSHost()


def _ps_out(cmd, timeout=60):
    return _host.run(cmd, timeout)


def _ps_fresh(cmd, timeout=120):
    """Separate powershell.exe for user commands (run_command) so they cannot touch the host state."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    return r.returncode, (r.stdout or r.stderr).strip()


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


OVERLAY_PORT = 47330
_ov_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def overlay_running():
    """ClawOverlay holds the named mutex Local\\ClawOverlay while it runs."""
    h = kernel32.OpenMutexW(0x00100000, False, "Local\\ClawOverlay")  # SYNCHRONIZE
    if h:
        kernel32.CloseHandle(h)
    return bool(h)


def overlay(kind, text="", title="Gemma", ms=0):
    """Instant on-screen message (overlay/ClawOverlay.exe). False when the overlay is not running."""
    if not overlay_running():
        return False
    msg = {"kind": kind, "title": title, "text": text, "ms": ms}
    _ov_sock.sendto(json.dumps(msg, ensure_ascii=False).encode("utf-8"), ("127.0.0.1", OVERLAY_PORT))
    return True


def notify(text, title="Gemma", kind=None, ms=0):
    # own overlay first: Windows toasts queue, animate slowly and are hidden by Do Not Disturb / in games
    if overlay(kind or ("error" if "ошибка" in title else "ok"), text, title, ms):
        return
    threading.Thread(target=_ps_out, args=(f"Show-Toast {_q(title)} {_q(text)}",), daemon=True).start()


_speak_pending = threading.Event()  # set until SpeakAsync has actually been called in the host


SPOKEN = {"text": ""}  # what the voice is saying now: a stop word inside the answer is not a barge-in


EARLY = {}  # "text": first sentence already spoken while Gemma was still generating (web answers)


def speak(text):
    early = EARLY.pop("text", "")
    SPOKEN.update(text=text.lower(), t=time.time())
    _speak_pending.set()

    def run():
        try:
            if early and text.startswith(early):  # the start is already being said: queue only the rest
                rest = text[len(early):].strip()
                if rest:
                    _ps_out(f"SayMore {_q(rest)}")
                return
            voice = str(voice_settings.get("tts_voice") or "").replace("'", "")
            if voice:
                pick = f"try {{ $tts.SelectVoice('{voice}') }} catch {{}}; "
            else:  # no voice chosen: one that speaks the reply's language (Russian text, English text)
                culture = "ru" if re.search(r"[а-яё]", text, re.I) else "en"
                pick = (f"$v = $tts.GetInstalledVoices() | ? {{ $_.VoiceInfo.Culture.Name -like '{culture}-*' }} | "
                        f"Select-Object -First 1; if ($v) {{ $tts.SelectVoice($v.VoiceInfo.Name) }}; ")
            rate = max(-10, min(10, int(voice_settings.get("tts_rate"))))
            vol = max(0, min(100, int(voice_settings.get("tts_volume"))))
            _ps_out(f"{pick}$tts.Rate = {rate}; $tts.Volume = {vol}; Say {_q(text)}")
        finally:
            _speak_pending.clear()
    threading.Thread(target=run, daemon=True).start()


def stop_speaking():
    """Cut the TTS voice now (button press, wake phrase, "стоп"): like a smart speaker, talking over it wins."""
    _speak_pending.clear()
    _ps_out("$tts.SpeakAsyncCancelAll()", timeout=5)


def speaking():
    """True while the TTS voice talks: the follow-up window must not hear the assistant itself."""
    if _speak_pending.is_set():
        return True
    rc, out = _ps_out("[string]$tts.State", timeout=5)
    return out.strip() == "Speaking"


def warm():
    """Called once in the background by dictate.py: start the host and cache the Start menu apps."""
    _ps_out("1")
    start_apps()


def report(text, ok=True, vocal=False):
    log(("OK: " if ok else "FAIL: ") + text)
    notify(text, "Gemma" if ok else "Gemma: ошибка")
    if vocal:
        speak(text)


def confirm(title, message, detail="", ok="Выполнить", danger=True, timeout=20):
    """Modern dialog: the ClawVoice tray app (instant, UDP 47331), else confirm.ps1. True only on an explicit Yes."""
    req = {"title": title, "message": message, "detail": detail, "ok": ok, "cancel": "Отмена", "danger": danger,
           "timeout": timeout}
    user32.AllowSetForegroundWindow(-1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(req, ensure_ascii=False).encode("utf-8"), ("127.0.0.1", CONFIRM_PORT))
        s.settimeout(1.0)
        if json.loads(s.recv(256)).get("ack"):  # the tray shows the dialog; now wait for the answer
            s.settimeout(timeout + 10)
            yes = bool(json.loads(s.recv(256)).get("yes"))
            log(f"confirm '{title}': {'YES' if yes else 'no'}")
            return yes
    except (OSError, ValueError):
        pass  # no tray app (or it did not answer): the PowerShell dialog
    finally:
        s.close()
    payload = base64.b64encode(json.dumps({"title": title, "message": message, "detail": detail, "ok": ok,
                                           "cancel": "Отмена", "danger": danger, "timeout": timeout},
                                          ensure_ascii=False).encode("utf-8")).decode()
    user32.AllowSetForegroundWindow(-1)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", CONFIRM,
                            "-B64", payload], timeout=timeout + 15, creationflags=NO_WINDOW)
        yes = r.returncode == 0
    except Exception as e:
        log(f"confirm failed: {e}")
        yes = False
    log(f"confirm '{title}': {'YES' if yes else 'no'}")
    return yes


# ====================================================================== numbers

NUM_WORDS = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
    "тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысячу": 1000,
    "миллион": 10**6, "миллиона": 10**6, "миллионов": 10**6,
}


def words_to_number(tokens):
    total = cur = 0
    for t in tokens:
        v = NUM_WORDS[t]
        if v in (1000, 10**6):
            total += max(cur, 1) * v
            cur = 0
        else:
            cur += v
    return total + cur


def extract_number(text):
    """First number in the phrase: digits ("25", "2,5") or a run of number words ("двадцать пять")."""
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if m:
        v = float(m.group(0).replace(",", "."))
        return int(v) if v.is_integer() else v
    run = []
    for tok in re.findall(r"[а-яё]+", text.lower()):
        if tok in NUM_WORDS:
            run.append(tok)
        elif run:
            break
    return words_to_number(run) if run else None


def parse_level(p):
    """{'level': n} / {'step': +-n} / {} from phrases like 'громкость двадцать пять', 'ярче на 10'."""
    if re.search(r"\bмаксимум|\bна полную|\bмаксимальн", p):
        return {"level": 100}
    if re.search(r"\bминимум|\bминимальн", p):
        return {"level": 0}
    n = extract_number(p)
    up = re.search(r"\b(увелич|прибав|добав|громче|ярче|подним|повыс|поярче|погромче|плюс)", p)
    down = re.search(r"\b(уменьш|убав|сниз|тише|темнее|опуст|пониз|потише|потемнее|минус)", p)
    if up or down:
        step = int(n) if n is not None else 15
        return {"step": step if up else -step}
    if n is not None:
        return {"level": int(max(0, min(100, n)))}
    return {}


def eval_math(p):
    t = re.sub(r"^(сколько\s+будет|посчитай|вычисли|реши|скажи\s+сколько\s+будет)\s*", "", p).strip(" ?!.")
    m = re.fullmatch(r"(?:квадратный\s+)?корень\s+из\s+(.+)", t)
    if m:
        v = extract_number(m.group(1))
        if v is None:
            return None
        r = math.sqrt(v)
        return str(int(r)) if r.is_integer() else f"{r:.3g}".replace(".", ",")
    ops = [(r"\s*(?:\+|\bплюс\b|\bприбавить\b)\s*", "+"), (r"\s*(?:\bминус\b|\bотнять\b|\s-\s)\s*", "-"),
           (r"\s*(?:\*|\bумножить\s+на\b|\bумножить\b|\bна\b|\bх\b|\bx\b)\s*", "*"),
           (r"\s*(?:/|\bразделить\s+на\b|\bподелить\s+на\b)\s*", "/"),
           (r"\s*(?:\bв\s+степени\b|\^)\s*", "**"), (r"\s*\bпроцент\w*\s+от\b\s*", "%")]
    for pat, op in ops:
        parts = re.split(pat, t, maxsplit=1)
        if len(parts) != 2:
            continue
        a, b = (_pure_number(x) for x in parts)
        if a is None or b is None:
            continue
        if op == "/" and b == 0:
            return "На ноль делить нельзя"
        if op == "**" and (abs(b) > 100 or abs(a) > 1e6):
            return "Слишком большое число"
        r = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else 0, "**": a ** b, "%": a * b / 100}[op]
        if isinstance(r, float):
            r = int(r) if r.is_integer() else round(r, 4)
        return str(r).replace(".", ",")
    return None


def _pure_number(s):
    """Number only if the WHOLE fragment is a number (so 'открой хром' never becomes math)."""
    s = s.strip()
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", s):
        v = float(s.replace(",", "."))
        return int(v) if v.is_integer() else v
    toks = s.split()
    if toks and all(t in NUM_WORDS for t in toks):
        return words_to_number(toks)
    return None


# ====================================================================== system controls

_AUDIO_CS = r"""using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume{int f0();int f1();int f2();int f3();int SetMasterVolumeLevelScalar(float l,System.Guid c);
int f5();int GetMasterVolumeLevelScalar(out float l);int f7();int f8();int f9();int f10();
int SetMute([MarshalAs(UnmanagedType.Bool)]bool b,System.Guid c);int GetMute([MarshalAs(UnmanagedType.Bool)]out bool b);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice{int Activate(ref System.Guid id,int ctx,int p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator{int f();int GetDefaultAudioEndpoint(int d,int r,out IMMDevice e);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]class MMDeviceEnumerator{}
public static class Aud{static IAudioEndpointVolume V(int flow){var e=(IMMDeviceEnumerator)new MMDeviceEnumerator();
IMMDevice d;e.GetDefaultAudioEndpoint(flow,1,out d);var id=typeof(IAudioEndpointVolume).GUID;object o;d.Activate(ref id,23,0,out o);
return (IAudioEndpointVolume)o;}
public static int Get(){float l;V(0).GetMasterVolumeLevelScalar(out l);return (int)System.Math.Round(l*100);}
public static void Set(int p){var v=V(0);v.SetMute(false,System.Guid.Empty);v.SetMasterVolumeLevelScalar(p/100f,System.Guid.Empty);}
public static void Mute(int flow,bool m){V(flow).SetMute(m,System.Guid.Empty);}}"""


def _audio(call):
    rc, out = _ps_out(call)
    if rc != 0:
        raise RuntimeError(out[-200:])
    return out


def volume(level=None, step=None, mute=None):
    if mute is True:
        _audio("[Aud]::Mute(0,$true)")
        return "Звук выключен"
    if mute is False and level is None and step is None:
        _audio("[Aud]::Mute(0,$false)")
        return "Звук включён"
    if step is not None:
        level = int(_audio("[Aud]::Get()") or 50) + int(step)
    level = max(0, min(100, int(level if level is not None else 50)))
    _audio(f"[Aud]::Set({level})")
    return f"Громкость {level} процентов"


def mic(mute=True):
    _audio(f"[Aud]::Mute(1,${'true' if mute else 'false'})")
    return "Микрофон отключён" if mute else "Микрофон включён"


def brightness(level=None, step=None):
    if step is not None:
        rc, cur = _ps_out("(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness).CurrentBrightness")
        level = (int(cur) if rc == 0 and cur.isdigit() else 50) + int(step)
    level = max(0, min(100, int(level if level is not None else 50)))
    rc, out = _ps_out("Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods | Invoke-CimMethod "
                      f"-MethodName WmiSetBrightness -Arguments @{{Timeout=[UInt32]1; Brightness=[Byte]{level}}}")
    if rc != 0:
        raise RuntimeError(out[-200:])
    return f"Яркость {level} процентов"


NIGHT_KEY = (r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current"
             r"\default$windows.data.bluelightreduction.bluelightreductionstate"
             r"\windows.data.bluelightreduction.bluelightreductionstate")


def night_light(on=None):
    """Night light via its CloudStore blob: bytes 10 00 at offset 23 = active, byte 18 = payload length.
    Verified on this Claw 2026-10-26 (off and on both applied live)."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NIGHT_KEY) as k:
        d = bytearray(winreg.QueryValueEx(k, "Data")[0])
    is_on = d[23:25] == b"\x10\x00"
    if on is None:
        on = not is_on
    if on == is_on:
        return "Ночной свет уже включён" if on else "Ночной свет уже выключен"
    if on:
        d = d[:23] + b"\x10\x00" + d[23:]
        d[18] += 2
    else:
        d = d[:23] + d[25:]
        d[18] -= 2
    for i in range(10, 15):  # bump the change timestamp so the shell re-reads the state
        if d[i] != 0xFF:
            d[i] += 1
            break
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NIGHT_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Data", 0, winreg.REG_BINARY, bytes(d))
    return "Ночной свет включён" if on else "Ночной свет выключен"


def _key(*vks):
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, 2, 0)


def media(action):
    vk = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}.get(action, 0xB3)
    _key(vk)
    return {"play_pause": "Пауза или воспроизведение", "next": "Следующий трек",
            "prev": "Предыдущий трек", "stop": "Остановлено"}.get(action, "Готово")


SETTINGS = [  # (phrase regex, ms-settings page, Russian name)
    (r"блют|bluetooth|блу ?тус", "bluetooth", "Bluetooth"),
    (r"вай ?фай|wi-?fi|беспроводн|сет", "network-wifi", "Wi-Fi"),
    (r"звук|аудио", "sound", "звук"),
    (r"ночн", "nightlight", "ночной свет"),
    (r"экран|диспле|яркост|поворот", "display", "экран"),
    (r"батаре|питани|энерг", "powersleep", "питание"),
    (r"обновлен", "windowsupdate", "обновления"),
    (r"приложени|программ", "appsfeatures", "приложения"),
    (r"мыш|тачпад|сенсорн", "mousetouchpad", "мышь и тачпад"),
    (r"клавиатур|язык|раскладк", "regionlanguage", "язык"),
    (r"уведомлен", "notifications", "уведомления"),
    (r"хранилищ|памят|диск", "storagesense", "хранилище"),
    (r"тем|оформлен|фон|обои", "personalization", "персонализация"),
]


def open_settings(p):
    for pat, page, name in SETTINGS:
        if re.search(pat, p):
            os.startfile(f"ms-settings:{page}")
            return f"Открываю параметры: {name}"
    os.startfile("ms-settings:")
    return "Открываю параметры"


INFO = {
    "battery": "$b=Get-CimInstance Win32_Battery; if($b){$s=if($b.BatteryStatus -eq 2){', заряжается'}else{''}; "
               "'Заряд батареи {0} процентов{1}' -f $b.EstimatedChargeRemaining,$s}else{'Батарея не найдена'}",
    "disk": "'На диске C свободно {0:N0} гигабайт' -f ((Get-PSDrive C).Free/1GB)",
    "memory": "$o=Get-CimInstance Win32_OperatingSystem; 'Свободно {0:N1} из {1:N0} гигабайт памяти' -f "
              "($o.FreePhysicalMemory/1MB), ($o.TotalVisibleMemorySize/1MB)",
    "cpu": "'Загрузка процессора ' + (Get-CimInstance Win32_Processor).LoadPercentage + ' процентов'",
    "network": "$p=Get-NetConnectionProfile | Select -First 1; if($p){'Сеть ' + $p.Name}else{'Сеть не подключена'}",
    "ip": "'IP адрес ' + ((Get-NetIPAddress -AddressFamily IPv4 | ?{$_.IPAddress -notlike '127.*' -and "
          "$_.IPAddress -notlike '169.254.*'} | Select -First 1).IPAddress)",
    "uptime": "$t=(Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; "
              "'Компьютер работает {0} часов {1} минут' -f [int][math]::Floor($t.TotalHours), $t.Minutes",
}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def system_info(what):
    now = datetime.now()
    if what == "time":
        return f"Сейчас {now:%H:%M}"
    if what == "date":
        return f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}"
    if what not in INFO:
        return None
    return _ps_out(INFO[what])[1]


_timers = []


def timer(action="set", seconds=0, text=""):
    """Timer, or a reminder when `text` is given (toast + voice + Telegram at the due time)."""
    if action == "cancel":
        n = len(_timers)
        for t in _timers:
            t.cancel()
        _timers.clear()
        return "Таймер отменён" if n else "Активных таймеров нет"
    if seconds <= 0:
        return "Не поняла, на сколько поставить таймер"

    def ring():
        notify(text or "Время вышло!", "Напоминание" if text else "Таймер", kind="info", ms=15000)
        import winsound
        for _ in range(3):
            winsound.Beep(1760, 250)
            time.sleep(0.1)
        speak(f"Напоминаю: {text}" if text else "Таймер завершён")
        if text:
            try:
                telegram("--message", f"Напоминание: {text}")
            except Exception as e:
                log(f"reminder telegram failed: {e}")
        if t in _timers:
            _timers.remove(t)
    t = threading.Timer(seconds, ring)
    t.daemon = True
    _timers.append(t)
    t.start()
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    parts = " ".join(x for x in [f"{h} ч" * bool(h), f"{m} мин" * bool(m), f"{s} сек" * bool(s)] if x)
    return f"Напомню через {parts}: {text}" if text else f"Таймер на {parts}"


def _window_rect():
    """Foreground window bounds in physical pixels (DWM frame without the invisible resize border)."""
    old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: same pixels as the grab
    try:
        hwnd = user32.GetForegroundWindow()
        r = (ctypes.c_long * 4)()
        if not hwnd or ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9, ctypes.byref(r), 16):
            return None  # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        return tuple(r) if r[2] > r[0] and r[3] > r[1] else None
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


def _image_to_clipboard(img):
    """Put a PIL image on the clipboard as CF_DIB (pastes into messengers, Paint, Word)."""
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # a DIB is the BMP without its file header
    k32 = ctypes.WinDLL("kernel32")
    k32.GlobalAlloc.restype = k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    for _ in range(10):  # another app may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ctypes.memmove(k32.GlobalLock(h), data, len(data))
        k32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(8, h))  # CF_DIB; the clipboard owns h now
    finally:
        user32.CloseClipboard()


def screenshot(window=False):
    """Save to the system Screenshots folder and (setting screenshot_clipboard) copy to the clipboard."""
    from PIL import ImageGrab
    os.makedirs(SHOTS, exist_ok=True)
    stem = os.path.join(SHOTS, datetime.now().strftime("Снимок экрана %Y-%m-%d %H%M%S"))
    path, n = stem + ".png", 1
    while os.path.exists(path):  # two shots in one second
        n += 1
        path = f"{stem} ({n}).png"
    rect = _window_rect() if window else None
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    img.save(path)
    if voice_settings.get("screenshot_clipboard"):
        _image_to_clipboard(img)
    CTX["file"] = path  # "отправь его в телеграм" after "сделай скриншот"
    return path


_tg = None


def telegram(*args):
    """Send to the user's Telegram: their own bot from Settings (tg.py), else the telegram-sender skill script
    (this PC before SaySo), imported in-process. Args like the old script: --message text / --file path."""
    global _tg
    import importlib.util, io, contextlib
    opts = dict(zip(args[::2], args[1::2]))
    import tg
    if tg.configured() or not os.path.exists(TELEGRAM):
        if "--file" in opts:
            tg.send_file(opts["--file"], opts.get("--message") or "")
        else:
            tg.send_message(opts.get("--message", ""))
        return
    if _tg is None:
        spec = importlib.util.spec_from_file_location("send_telegram", TELEGRAM)
        _tg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_tg)
    opts = dict(zip(args[::2], args[1::2]))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        token, chat_id = _tg.get_credentials()
        if "--file" in opts:
            _tg.send_document(token, chat_id, opts["--file"], opts.get("--message"))
        else:
            _tg.send_message(token, chat_id, opts.get("--message", ""))
    if "successfully" not in out.getvalue():
        raise RuntimeError(out.getvalue().strip()[-300:] or "Telegram: нет ответа")


def tg_text(phrase):
    t = re.sub(r"(?i)^.*?(телеграм\w*|telegram|тг)[\s,:.-]*", "", phrase).strip()
    if not t:  # "отправь 56 в телеграм": the text sits between the verb and "в телеграм"
        m = re.search(r"(?i)(?:напиши|отправ\w*|скинь|пошли|перешли)\s+(?:мне\s+)?(.+?)\s+(?:в|во)\s+"
                      r"(?:телеграм|telegram|тг)", phrase)
        t = m.group(1).strip() if m else ""
    t = re.sub(r"(?i)^(сообщение|текст|напиши|что)\s+", "", t).strip()
    expr = re.search(r"(\d+(?:[.,]\d+)?)\s*([-+*/×xх])\s*(\d+(?:[.,]\d+)?)", t)
    if expr and re.search(r"(?i)ответ|решени|результат|сколько|посчитай", t):
        op = {"-": "минус", "+": "плюс", "/": "разделить на"}.get(expr.group(2), "умножить на")
        r = eval_math(f"{expr.group(1)} {op} {expr.group(3)}")
        if r is not None:
            t = f"{expr.group(1)} {expr.group(2)} {expr.group(3)} = {r}"
    return t[:1].upper() + t[1:] if t else ""


# ====================================================================== windows & apps

APPS = {  # key: (launch target, process names, window classes, Russian name)
    "explorer": ("explorer.exe", ["explorer"], ["CabinetWClass"], "Проводник"),
    "taskmgr": ("taskmgr.exe", ["taskmgr"], ["TaskManagerWindow"], "Диспетчер задач"),
    "chrome": ("chrome", ["chrome"], [], "Google Chrome"),
    "edge": ("msedge", ["msedge"], [], "Microsoft Edge"),
    "notepad": ("notepad.exe", ["notepad"], [], "Блокнот"),
    "calc": ("calc.exe", ["calculatorapp", "calculator"], [], "Калькулятор"),
    "terminal": ("wt.exe", ["windowsterminal"], ["CASCADIA_HOSTING_WINDOW_CLASS"], "Терминал"),
    "settings": ("ms-settings:", ["systemsettings"], [], "Параметры"),
    "steam": ("steam://open/main", ["steam", "steamwebhelper"], [], "Steam"),
    "telegram": ("tg://", ["telegram"], [], "Telegram"),
    "paint": ("mspaint.exe", ["mspaint"], [], "Paint"),
}
ALIASES = {"проводник": "explorer", "файлы": "explorer", "папки": "explorer", "диспетчер задач": "taskmgr",
           "диспетчер": "taskmgr", "хром": "chrome", "гугл хром": "chrome", "google chrome": "chrome",
           "браузер": "chrome", "эдж": "edge", "блокнот": "notepad", "калькулятор": "calc",
           "терминал": "terminal", "консоль": "terminal", "параметры": "settings", "настройки": "settings",
           "стим": "steam", "телеграм": "telegram", "тг": "telegram", "пейнт": "paint", "paint": "paint"}
FOLDERS = [(r"рабоч\w* стол|desktop", "shell:Desktop", "Рабочий стол"),
           (r"загрузк|download", "shell:Downloads", "Загрузки"),
           (r"документ|document", "shell:Personal", "Документы"),
           (r"изображен|картинк|фото|picture", "shell:My Pictures", "Изображения"),
           (r"видео|video", "shell:My Video", "Видео"), (r"музык|music", "shell:My Music", "Музыка"),
           (r"диск\w* [cс]\b", "C:\\", "Диск C")]
PROTECTED = {"explorer", "dwm", "csrss", "smss", "services", "lsass", "winlogon", "wininit", "svchost",
             "sihost", "fontdrvhost", "conhost", "python", "pythonw", "system", "registry", "audiodg",
             "ctfmon", "searchhost", "startmenuexperiencehost", "shellexperiencehost", "textinputhost", "applicationframehost"}


def _proc_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
    name = ""
    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
        name = os.path.basename(buf.value).lower().removesuffix(".exe")
    kernel32.CloseHandle(h)
    return name


def _uwp_pid(frame):
    """Process of the app hosted in a UWP ApplicationFrameWindow (its child Windows.UI.Core.CoreWindow)."""
    child = user32.FindWindowExW(frame, None, "Windows.UI.Core.CoreWindow", None)
    if not child:
        return 0
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
    return pid.value


def _cloaked(hwnd):
    """'Visible' but not on screen: suspended UWP CoreWindows, windows on other virtual desktops."""
    v = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 14, ctypes.byref(v), 4)  # DWMWA_CLOAKED
    return v.value != 0


def top_windows(procs=(), classes=(), title=""):
    """Visible top-level windows owned by any of `procs` or having any of `classes` (exact match only).
    UWP apps (Settings...) live in an ApplicationFrameWindow of ApplicationFrameHost: it matches by the
    hosted CoreWindow's process, or by `title` (the app's localized name) when that child is detached."""
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4) or _cloaked(hwnd):  # skip owned popups
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if cls.value == "ApplicationFrameWindow" and procs:
            uwp = _uwp_pid(hwnd)
            if uwp:
                pid = ctypes.c_ulong(uwp)
            elif title:
                text = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, text, 256)
                if text.value.lower().startswith(title.lower()):
                    found.append((hwnd, pid.value))
                return True
        if cls.value in classes or (procs and _proc_name(pid.value) in procs):
            if user32.GetWindowTextLengthW(hwnd) > 0:
                found.append((hwnd, pid.value))
        return True
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(proto(cb), 0)
    return found


def activate(hwnd):
    user32.AllowSetForegroundWindow(-1)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.ShowWindow(hwnd, 3)      # SW_MAXIMIZE (handheld: full screen is the usable layout)
    if not user32.SetForegroundWindow(hwnd):
        user32.SwitchToThisWindow(hwnd, True)


_start_apps, _start_apps_t = None, 0
TRANSLIT = dict(zip("абвгдеёзийклмнопрстуфхцыэ", "abvgdeeziiklmnoprstufhcye"))
TRANSLIT.update({"ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ь": "", "ъ": ""})


def _sound(s):
    """Rough phonetic key so 'клауди' ~ 'Claude', 'стим' ~ 'Steam'."""
    s = "".join(TRANSLIT.get(c, c) for c in s.lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("w", "v"), ("x", "ks"), ("ee", "i"),
                 ("oo", "u"), ("ou", "u"), ("au", "a"), ("y", "i"), ("e", "i")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def start_apps():
    """Start menu apps (incl. Store apps) + apps from plugins: [(name, AppID or path)].
    Rebuilt at startup and every 10 min, so newly installed apps and new game shortcuts are picked up."""
    global _start_apps, _start_apps_t
    if _start_apps is None or time.time() - _start_apps_t > 600:
        rc, out = _ps_out("Get-StartApps | ForEach-Object { $_.Name + [char]9 + $_.AppID }", timeout=30)
        apps = [tuple(l.split("\t", 1)) for l in out.splitlines() if "\t" in l]
        apps[:0] = plugins.extra_apps()  # e.g. the Claw plugin's Desktop\games shortcuts
        _start_apps, _start_apps_t = apps, time.time()
    return _start_apps


def find_app(query):
    q = _sound(query)
    if len(q) < 2:
        return None
    best, score = None, 0.0
    skip = re.compile(r"(?i)uninstall|удалить|readme|help|справк|documentation|website")
    for name, appid in start_apps():
        if skip.search(name):
            continue
        cand = [_sound(name)] + [_sound(w) for w in name.split()[:2]]
        s = max(difflib.SequenceMatcher(None, q, c).ratio() for c in cand if c)
        if s > score:
            best, score = (name, appid), s
    log(f"find_app {query!r} -> {best} ({score:.2f})")
    return best if score >= 0.72 else None


def open_app(name, phrase="", new_window=False):
    p = (phrase or name).lower()
    for pat, target, ru in FOLDERS:
        if re.search(pat, p) or re.search(pat, name.lower()):
            subprocess.Popen(["explorer.exe", target])
            return f"Открываю: {ru}"
    key = ALIASES.get(name.lower().strip(), name.lower().strip())
    new_window = new_window or bool(re.search(r"нов\w+ окн|ещё одн|еще одн", p))
    if key in APPS:
        target, procs, classes, ru = APPS[key]
        if not new_window:
            wins = top_windows(procs, classes, ru)
            if wins:
                activate(wins[0][0])
                return f"{ru}: на весь экран"
        before = {h for h, _ in top_windows(procs, classes)}
        if key == "explorer":
            subprocess.Popen(["explorer.exe", "shell:HomeFolder"])
        elif new_window and key in ("chrome", "edge"):
            subprocess.Popen(["cmd", "/c", "start", "", target, "--new-window"], creationflags=NO_WINDOW)
        elif new_window and key == "terminal":
            subprocess.Popen(["wt.exe", "-w", "-1"])
        else:
            os.startfile(target)
        threading.Thread(target=_activate_new, args=(procs, classes, before), daemon=True).start()
        return f"{'Новое окно' if new_window else 'Открываю'}: {ru}"
    app = find_app(name)
    if app:
        os.startfile(app[1] if os.path.exists(app[1]) else f"shell:AppsFolder\\{app[1]}")
        return f"Открываю: {app[0]}"
    return f"Не нашла программу «{name}»"


def _activate_new(procs, classes, before):
    for _ in range(40):
        time.sleep(0.15)
        new = [h for h, _ in top_windows(procs, classes) if h not in before]
        if new:
            activate(new[0])
            return


def running_process(name):
    """Process name of an open window that sounds like `name` (process name or title words), else None."""
    q = _sound(name)
    if len(q) < 2:
        return None
    best, score = None, 0.0
    seen = set()

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        seen.add((_proc_name(pid.value), buf.value))
        return True
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(proto(cb), 0)
    scores = {}
    for proc, title in seen:
        if not proc or proc in PROTECTED:
            continue
        cands = [_sound(proc)] + [_sound(w) for w in re.split(r"[\s\-–—|:]+", title)[:4] if w]
        s = max(difflib.SequenceMatcher(None, q, c).ratio() for c in cands if c)
        skel = lambda x: re.sub(r"[aeiou]", "", x)  # "клода" and "Claude" share the consonants k-l-d
        if len(skel(q)) >= 3 and any(skel(c) == skel(q) for c in cands if c):
            s = max(s, 0.85)
        scores[proc] = max(scores.get(proc, 0), s)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, score = ranked[0] if ranked else (None, 0.0)
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    log(f"running_process {name!r} -> {best} ({score:.2f}, next {second:.2f})")
    # Whisper garbles names ("Клода" -> "клаву" ~ claude 0.60): a weaker match is accepted when it is the
    # clear winner among the few open windows; closing is polite (WM_CLOSE) and force needs a confirmation
    return best if score >= 0.75 or (score >= 0.55 and score - second >= 0.2) else None


def window_app(name, minimize):
    """'сверни клауди' minimizes its windows; 'разверни/покажи/переключись на хром' brings it to the front."""
    key = ALIASES.get(name, name)
    procs, classes, ru = (APPS[key][1], APPS[key][2], APPS[key][3]) if key in APPS else ([key], [], name)
    wins = top_windows(procs, classes, ru)
    if not wins:
        return f"{ru}: окон не найдено"
    for h, _ in wins:
        if minimize:
            user32.ShowWindow(h, 6)  # SW_MINIMIZE
    if not minimize:
        activate(wins[0][0])
    return f"{ru}: {'свернула' if minimize else 'на экране'}"


def close_app(name):
    key = ALIASES.get(name.lower().strip(), name.lower().strip())
    if key in APPS:
        _, procs, classes, ru = APPS[key]
    else:
        procs, classes, ru = [re.sub(r"[^\w.-]", "", key).removesuffix(".exe")], [], name
    if key == "explorer":  # only folder windows; never the shell process
        wins = top_windows((), ["CabinetWClass"])
        for h, _ in wins:
            user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
        return "Окна проводника закрыты" if wins else "Окон проводника нет"
    wins = top_windows(procs, classes, ru)
    if not wins:
        return f"{ru}: окон не найдено"
    for h, _ in wins:
        user32.PostMessageW(h, 0x0010, 0, 0)  # polite close: the app may ask to save
    for _ in range(13):  # up to ~2.5 s, but return as soon as the windows are gone
        time.sleep(0.2)
        left = top_windows(procs, classes, ru)
        if not left:
            break
    if not left:
        return f"{ru} закрыт"
    pname = _proc_name(left[0][1])
    if pname in PROTECTED or not re.fullmatch(r"[\w.-]+", pname or ""):
        return f"{ru}: окно не закрылось, системный процесс не трогаю"
    if confirm(f"Завершить {ru} принудительно?", "Программа не закрылась сама, возможно, она ждёт сохранения. "
               "При принудительном завершении несохранённые данные пропадут.", f"taskkill /F /IM {pname}.exe",
               ok="Завершить"):
        subprocess.run(["taskkill", "/F", "/IM", f"{pname}.exe"], capture_output=True, creationflags=NO_WINDOW)
        return f"{ru} завершён принудительно"
    return f"{ru}: оставила открытым"


# ---------- files

HOME = os.path.expanduser("~")
FILE_ROOTS = [os.path.join(HOME, d) for d in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music",
                                               "OneDrive")] + [SHOTS]
FILE_KINDS = [  # (phrase regex, extensions)
    (r"фото|фотк|картинк|изображен|скрин|снимок", {".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".bmp"}),
    (r"видео|ролик|запис", {".mp4", ".mkv", ".mov", ".avi", ".webm"}),
    (r"pdf|пдф", {".pdf"}),
    (r"таблиц|эксел|excel", {".xlsx", ".xls", ".csv"}),
    (r"презентац", {".pptx", ".ppt"}),
    (r"документ|ворд|word", {".docx", ".doc", ".pdf", ".txt", ".md", ".odt", ".rtf"}),
    (r"музык|песн|трек|аудио", {".mp3", ".flac", ".m4a", ".wav", ".ogg"}),
    (r"архив", {".zip", ".7z", ".rar"}),
    (r"торрент", {".torrent"}),
]
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv", "AppData", "$RECYCLE.BIN", "cache", "caches",
             "temp", "tmp", "logs", "rockstar games", "my games", "npu_cache"}
SKIP_EXT = (".lnk", ".ini", ".tmp", ".bin", ".dat", ".db", ".log", ".cache", ".json", ".xml", ".sys", ".dll",
            ".pak", ".sav", ".lock", ".crdownload", ".part")
DOWNLOAD_WORDS = r"скач\w*|закач\w*|загруж\w*|загрузк\w*|загрузок"
TG_LIMIT = 50 * 1024 * 1024  # Telegram bot API upload limit


def _all_files():
    for root in dict.fromkeys(FILE_ROOTS):
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for f in fns:
                if not f.startswith(("~$", ".")) and not f.lower().endswith(SKIP_EXT):
                    yield os.path.join(dp, f)


def _file_query(p):
    """The name part of a file request: 'найди мне файл отчёт за май' -> 'отчёт за май'."""
    q = re.sub(r"(?:найди|найти|отправ\w*|скинь|пришли|перешли|покажи|открой|мне|пожалуйста|файл\w*|где|лежит|"
               r"в\s+(?:телеграм\w*|telegram|тг)|телеграм\w*|последн\w*|свеж\w*|недавн\w*|новы\w*|самы\w*|"
               r"(?:в|из)\s+(?:" + DOWNLOAD_WORDS + r")|" + DOWNLOAD_WORDS + r"|"
               r"под\s+названием|который\s+называется|называется|с\s+именем|с\s+рабочего\s+стола)",
               " ", p)
    for pat, _ in FILE_KINDS:
        q = re.sub(pat + r"\w*", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def find_file(p):
    """Best file for a spoken request: 'найди файл отчёт', 'последнее фото', 'pdf резюме'.
    Returns a path or None. Recent files win ties; 'последний' means the newest of the kind."""
    exts = next((e for pat, e in FILE_KINDS if re.search(pat, p)), None)
    q = _file_query(p)
    files = [f for f in _all_files() if exts is None or os.path.splitext(f)[1].lower() in exts]
    if not files:
        return None
    newest = lambda fs: max(fs, key=lambda f: os.path.getmtime(f))
    if re.search(DOWNLOAD_WORDS, p):  # "скачанный / загруженный / в загрузках" -> only Downloads
        files = [f for f in files if f.startswith(os.path.join(HOME, "Downloads"))] or files
    if not q:  # "отправь последнее фото" / "покажи последний скаченный файл"
        return newest(files) if re.search(r"последн|свеж|недавн|новы|" + DOWNLOAD_WORDS, p) else None
    qs, words = _sound(q), [_sound(w) for w in q.split() if len(w) > 2]
    skel = lambda x: re.sub(r"[aeiou]", "", x)  # vowels drift in speech recognition, consonants hold
    best, score = None, 0.0
    for f in files:
        stem = _sound(os.path.splitext(os.path.basename(f))[0])
        sc = difflib.SequenceMatcher(None, qs, stem).ratio()
        if words:
            hits = [1.0 if w in stem else 0.9 if len(skel(w)) >= 3 and skel(w) in skel(stem) else 0.0 for w in words]
            sc = max(sc, 0.95 * sum(hits) / len(hits))
        sc += min(os.path.getmtime(f) / 1e12, 0.002)  # tiny bias to newer files on ties
        if sc > score:
            best, score = f, sc
    log(f"find_file {q!r} exts={exts and sorted(exts)} -> {best} ({score:.2f})")
    return best if score >= 0.6 else None


def _size(n):
    return f"{n / 1024 / 1024:.1f} МБ" if n >= 1024 * 1024 else f"{max(1, n // 1024)} КБ"


# Whole-disk search by name ("найди файл договор аренды"): every fixed drive minus system, program and game
# folders. C: walks in ~1 s that way (about 17k files), so no index is kept and results are always fresh.
DISK_SKIP = {d.lower() for d in SKIP_DIRS} | {
    "windows", "program files", "program files (x86)", "programdata", "recovery", "system volume information",
    "site-packages", "steamapps", "windowsapps", "xboxgames", "games", "msocache", "perflogs", "onedrivetemp"}


def _disk_files():
    mask = kernel32.GetLogicalDrives()
    stack = [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1 and kernel32.GetDriveTypeW(f"{chr(65 + i)}:\\") == 3]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in DISK_SKIP and not e.name.startswith((".", "$")):
                            stack.append(e.path)
                    elif not e.name.startswith(("~$", ".")) and not e.name.lower().endswith(SKIP_EXT):
                        yield e.path
        except OSError:
            pass


FOUND = {"file": None, "t": 0.0}  # the last file found or sent: "скопируй найденный файл" even after other commands
FOUND_TTL = 3600


def _name_matches(files, names, top=12):
    """Files whose names are close to any of the name variants, best first: [(score, path)]. Single words
    also count in the parent folder ("чейнджлог амбилайт" = ClawAmbilight/CHANGELOG.md)."""
    skel = lambda x: re.sub(r"[aeiou]", "", x)  # vowels drift in speech recognition, consonants hold
    qs = [(_sound(n), [_sound(w) for w in n.split() if len(w) > 2]) for n in names if _sound(n)]
    out = []
    for f in files:
        stem = _sound(os.path.splitext(os.path.basename(f))[0])
        if not stem:
            continue
        where = _sound(os.path.basename(os.path.dirname(f))) + stem
        best = 0.0
        for q, words in qs:
            sc = difflib.SequenceMatcher(None, q, stem).ratio()
            if words:
                # a consonant-skeleton hit is weaker: in long names it finds "Clavier" for "кловер"
                hits = [1.0 if w in where else 0.8 if len(skel(w)) >= 4 and skel(w) in skel(where) else 0.0
                        for w in words]
                sc = max(sc, 0.95 * sum(hits) / len(hits))
            best = max(best, sc)
        if best >= 0.8:
            out.append((best, f))
    out.sort(key=lambda x: (-x[0], -os.path.getmtime(x[1])))
    return out[:top]


def _clear(found):
    return len(found) == 1 or (len(found) > 1 and found[0][0] >= 0.95 and found[1][0] < 0.85)


def _json_obj(text):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def locate_files(p):
    """Files for a spoken request, best first ([] = nothing). A bare kind ("последнее фото") uses the old
    recent-file logic in the user folders. A name goes over the whole disk: Python first; only when that is
    not clear-cut, the model turns the phrase into name variants (speech recognition writes "кловер", the file
    is "Clover"; "договор" may be "contract") and then picks among the closest names. Cloud calls take 1-8 s."""
    q = _file_query(p)
    if not q:
        f = find_file(p)
        return [f] if f else []
    exts = next((e for pat, e in FILE_KINDS if re.search(pat, p)), None)
    files = list(_disk_files())
    files = [f for f in files if exts is None or os.path.splitext(f)[1].lower() in exts] or files
    names = [q]
    found = _name_matches(files, names)
    if not _clear(found) and gemma_on():
        try:
            plan = _json_obj(ask_messages([{"role": "system", "content":
                "Пользователь голосом ищет файл на компьютере, распознавание речи могло исказить слова. "
                "Ответь одним JSON без текста вокруг: {\"names\": [3-6 вариантов имени файла без расширения: "
                "как сказано, исправленное, латиницей, английский перевод]}"},
                {"role": "user", "content": p}], max_tokens=200))
            # (its guessed extensions are not asked for: for "чейнджлог" it said .txt/.pdf, the file is .md)
            names += [str(n) for n in plan.get("names") or [] if str(n).strip()][:6]
            found = _name_matches(files, names)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 429):
                raise
            log(f"locate_files names: {e!r}")
    log(f"locate_files {p!r} names={names} exts={exts and sorted(exts)} -> {found[:5]}")
    if not _clear(found) and len(found) > 1 and gemma_on():
        listing = "\n".join(f"{i}. {f} ({datetime.fromtimestamp(os.path.getmtime(f)):%d.%m.%Y})"
                            for i, (_, f) in enumerate(found, 1))
        try:
            pick = ask_messages([{"role": "system", "content":
                "Пользователь голосом попросил найти файл. Ниже найденные файлы. Подходит только файл, в имени "
                "или пути которого есть искомое название (с поправкой на ошибки распознавания речи), похожее "
                "звучание другого слова не подходит. Ответь только номерами подходящих файлов через запятую, "
                "лучший первым, не больше пяти. Если ни один не подходит, ответь 0."},
                {"role": "user", "content": f"Просьба: «{p}»\n{listing}"}], max_tokens=30)
            nums = [int(n) for n in re.findall(r"\d+", pick) if 0 < int(n) <= len(found)]
            if nums or re.fullmatch(r"\D*0\D*", pick):
                return [found[n - 1][1] for n in dict.fromkeys(nums)]
        except Exception as e:
            log(f"locate_files pick: {e!r}")
    return [f for _, f in found]


def send_file(p):
    found = locate_files(p)
    f = found[0] if found else None
    if not f:
        return "Не нашла такой файл", True
    size = os.path.getsize(f)
    if size > TG_LIMIT:
        return f"Файл {os.path.basename(f)} больше 50 МБ, Телеграм-бот его не примет", True
    # no confirmation: the chat is the user's own, a wrong file there is harmless (limit checked above)
    CTX["file"] = f
    FOUND.update(file=f, t=time.time())
    telegram("--file", f, "--message", os.path.basename(f))
    return f"Отправила в Телеграм: {os.path.basename(f)}", False


def show_file(p):
    found = locate_files(p)
    if not found:
        return "Не нашла такой файл", True
    f = found[0]
    CTX["file"] = f  # "отправь его в телеграм"
    FOUND.update(file=f, t=time.time())
    subprocess.Popen(["explorer.exe", "/select,", f])
    folder = os.path.basename(os.path.dirname(f)) or os.path.dirname(f)
    more = "\nЕщё: " + ", ".join(os.path.basename(x) for x in found[1:4]) if len(found) > 1 else ""
    return f"Нашла: {os.path.basename(f)}, в папке {folder}{more}", False


# Xbox mode = Windows' gaming full screen experience. The state comes from its API; switching through the API
# needs rights this process lacks (CanSetGamingFullScreenExperience() is False here), so Win+F11 toggles it,
# the same fallback ClawTweaks uses. The state check keeps a toggle from going the wrong way.
XBOX = r"(?:xbox|x-box|иксбокс\w*|икс бокс\w*)"
TO_DESKTOP = (rf"(?:выйди|выход|выйти)\w*\s+из\s+(?:режима?\s+)?{XBOX}|(?:выключи|отключи|закрой)\w*\s+(?:режим\w*\s+)?{XBOX}|"
              r"(?:перейди|переключи\w*|верни\w*|вернись|давай|хочу)\s+.*(?:рабоч\w* стол|windows|виндовс|винд[уы])|"
              r"^(?:режим\s+)?(?:windows|виндовс|рабочего стола|рабочий стол)$")
TO_XBOX = (rf"(?:перейди|переключи\w*|включи|запусти|верни\w*|вернись|давай|хочу)\s+.*(?:в\s+{XBOX}|режим\w*\s+{XBOX}|"
           rf"консольн\w* режим|режим\w* консоли)|^(?:режим\s+)?{XBOX}(?:\s+режим)?$")


def xbox_mode(on):
    try:
        api = ctypes.WinDLL("api-ms-win-gaming-experience-l1-1-0")
        active = bool(api.IsGamingFullScreenExperienceActive())
    except (OSError, AttributeError):
        return "Режим Xbox на этом компьютере недоступен", True
    if active == on:
        return ("Уже в режиме Xbox" if on else "Уже в режиме Windows"), True
    if not (api.CanSetGamingFullScreenExperience() and api.SetGamingFullScreenExperience(bool(on)) == 0):
        _key(0x5B, 0x7A)  # Win+F11
    return ("Перехожу в режим Xbox" if on else "Перехожу в режим Windows"), False


def xbox_route(p):
    if re.search(TO_XBOX, p) and not re.search(r"приложени|\bапп?\b|app\b", p):  # "открой приложение Xbox" is an app
        return xbox_mode(True)
    if re.search(TO_DESKTOP, p):
        r = xbox_mode(False)
        # "перейди на рабочий стол" on the desktop already means "сверни окна": not ours
        if r[0].startswith("Уже") and not re.search(rf"{XBOX}|windows|виндовс|винд[уы]", p):
            return None
        return r
    return None


def window(action):
    if action == "minimize_all":
        _key(0x5B, 0x44)  # Win+D
        return "Окна свёрнуты"
    fg = user32.GetForegroundWindow()
    if action == "maximize" and fg:
        activate(fg)
        return "Окно развёрнуто"
    if action == "close" and fg:
        user32.PostMessageW(fg, 0x0010, 0, 0)
        return "Окно закрыто"
    if action == "keyboard":
        os.startfile(r"C:\Program Files\Common Files\microsoft shared\ink\TabTip.exe")
        return "Экранная клавиатура"
    return "Нет активного окна"


def power(action, delay=0):
    if action == "cancel":
        subprocess.run(["shutdown.exe", "/a"], capture_output=True, creationflags=NO_WINDOW)
        return "Выключение отменено"
    titles = {"sleep": ("Перевести в гибернацию?", "Сон на этом Claw отключён, будет гибернация.", "/h", "Гибернация"),
              "hibernate": ("Перевести в гибернацию?", "Открытые программы сохранятся.", "/h", "Гибернация"),
              "restart": ("Перезагрузить компьютер?", "Несохранённые данные пропадут.", "/r", "Перезагрузить"),
              "shutdown": ("Выключить компьютер?", "Несохранённые данные пропадут.", "/s", "Выключить")}
    if action not in titles:
        return "Не поняла, что сделать с питанием"
    title, msg, flag, ok = titles[action]
    if not confirm(title, msg, f"shutdown {flag}" + (f" /t {delay}" if delay and flag != "/h" else ""), ok=ok):
        return "Отменено"
    if flag == "/h":
        threading.Timer(max(delay, 1), lambda: subprocess.Popen(["shutdown.exe", "/h"])).start()
        return "Гибернация"
    subprocess.Popen(["shutdown.exe", flag, "/t", str(max(delay, 5)), "/c", "Голосовая команда"])
    return "Выключаю" if flag == "/s" else "Перезагружаю"


def run_command(command):
    if not command.strip():
        return "Не поняла, какую команду выполнить"
    if not confirm("Выполнить команду?", "Gemma предлагает выполнить команду PowerShell.", command, ok="Выполнить"):
        return "Команда отменена"
    rc, out = _ps_fresh(command)
    log(f"run_command rc={rc}: {out[:500]}")
    if rc != 0:
        raise RuntimeError(out[-200:] or f"код {rc}")
    return out[:200] or "Команда выполнена"


# ====================================================================== fast route (no model)

SITES = [(r"ют[уь]б|youtube", "https://www.youtube.com", "YouTube"), (r"гугл\b|google", "https://www.google.com", "Google"),
         (r"википеди|wikipedia", "https://ru.wikipedia.org", "Википедию"), (r"яндекс|yandex", "https://ya.ru", "Яндекс"),
         (r"\bвк\b|вконтакт", "https://vk.com", "ВКонтакте"), (r"гитхаб|github", "https://github.com", "GitHub"),
         (r"почт|gmail|джимейл", "https://mail.google.com", "почту"), (r"чат ?гпт|chatgpt", "https://chatgpt.com", "ChatGPT"),
         (r"твич|twitch", "https://www.twitch.tv", "Twitch"), (r"кинопоиск", "https://www.kinopoisk.ru", "Кинопоиск")]


def alias(name):
    """ALIASES key for an inflected name: 'проводника' -> explorer, 'хрома' -> chrome."""
    name = name.strip()
    if name in ALIASES or name in APPS:
        return ALIASES.get(name, name)
    for k in sorted(ALIASES, key=len, reverse=True):
        if len(k) >= 3 and name.startswith(k):
            return ALIASES[k]
    return None


def fast_route(p):
    """Basic commands handled by plain Python. Returns (result_text, vocal) or None."""
    if re.search(r"(обнови|пересканир|перечитай|проверь|просканир)\w*\s+(?:список\s+)?(?:установленн\w+\s+)?"
                 r"(приложени|программ|игр)", p):
        return rescan_apps(), True
    m = (re.search(r"\bя\s+(?:сейчас\s+|теперь\s+|уже\s+)?(?:нахожусь\s+|живу\s+|прилетел\w*\s+|приехал\w*\s+)?в\s+(.+)$", p)
         or re.search(r"(?:моё|мое|мой)\s+(?:местоположение|город)\s+(?:теперь\s+)?(.+)$", p))
    if m and not re.search(r"\bгде\b", p):
        return net_skills.set_location(m.group(1), llm()), True
    if re.search(r"где я\b|где мы\b|(?:моё|мое) местоположение$|какой у меня город|в каком я городе", p):
        return net_skills.where_am_i(), True
    if re.search(r"погод|сколько градусов|температур\w* (?:на улице|за окном)|зонт|будет ли дождь|будет ли снег", p):
        CTX["kind"] = "weather"
        return net_skills.weather(p, llm()), True
    if re.search(r"курс\w*|сколько стоит\s+(?:" + "|".join(c[0] for c in net_skills.CURRENCIES) + ")", p):
        CTX["kind"] = "rate"
        return net_skills.rate(p), True
    if re.search(r"отмени\w* (выключени|перезагруз)", p):
        return power("cancel"), False
    for pat, action in [(r"усып|спящ\w* режим|гибернац|в сон\b", "sleep"), (r"перезагру", "restart"),
                        (r"выключи\w* (компьютер|пк|консоль|ноутбук|систему)|заверши\w* работу", "shutdown")]:
        if re.search(pat, p):
            n = extract_number(p)
            delay = int(n * (3600 if re.search(r"\bчас", p) else 1 if re.search(r"секунд", p) else 60)) if n else 0
            return power(action, delay), False
    if re.search(r"^(открой|запусти|зайди на|перейди на)\b", p) and not re.search(r"настройк|параметр|папк|програм", p):
        for pat, url, ru in SITES:
            if re.search(pat, p):
                os.startfile(url)
                return f"Открываю {ru}", False
    # "включи AIMP" means open too (sound, mic, backlight, night light were matched above)
    m = re.match(r"^(?:откр\w*|запуст\w*|включи|вруби)\s+(?:мне\s+)?(?:приложение\s+|программу\s+|игру\s+)?(.+?)(?:\s+на\s+(?:пк|компьютере))?$", p)
    if m and not re.search(r"настройк|параметр", p):
        name = re.sub(r"^(новое|ещё одно|еще одно)\s+окно\s+", "", m.group(1)).strip()
        if alias(name) or any(re.search(f[0], name) for f in FOLDERS) or find_app(name):
            return open_app(alias(name) or name, p), False
    m = re.match(r"^(сверни|спрячь|убери|разверни|покажи|переключись на|перейди в|верни)\s+(?:окно\s+|программу\s+)?(.+)$", p)
    if m and not re.search(r"^(вс[её]|окна|рабочий стол)", m.group(2)):
        target = alias(m.group(2)) or running_process(m.group(2))
        if target:
            return window_app(target, minimize=m.group(1) in ("сверни", "спрячь", "убери")), False
    m = re.match(r"^(?:закр\w*|убей|убить|прибей|заверши\w*|выключи|выруби)\s+(?:все\s+)?(?:окна\s+|окно\s+)?"
                 r"(?:процесс\s+|программу\s+|приложение\s+)?(.+)$", p)
    if m:
        target = alias(m.group(1)) or running_process(m.group(1))  # "АИМП", "IIMP" -> aimp
        if target:
            return close_app(target), False
    files_word = r"файл|документ|фото|фотк|картинк|изображен|видео|ролик|pdf|пдф|таблиц|презентац|архив|песн|торрент"
    if re.search(files_word, p) and not re.search(r"сделай|снять|сними", p):
        if re.search(r"телеграм|telegram|\bтг\b|отправ|скинь|пришли|перешли", p):
            return send_file(p)
        if re.search(r"найди|найти|где|покажи", p):
            return show_file(p)
    if re.search(r"телеграм|telegram|\bтг\b", p) and not re.search(r"скрин|снимок", p):
        if re.search(r"буфер", p):
            return run_tool("clipboard_to_telegram", {}, p)
        text = tg_text(p)  # dictated words after "телеграм" are sent verbatim, never paraphrased
        if text and re.search(r"напиши|отправ|скинь|пошли|сообщ", p):
            telegram("--message", text)
            return f"Отправила в Телеграм: {text}", False
        return None  # no text after "телеграм": let Gemma figure it out
    if re.search(r"скрин|снимок экрана", p):
        if re.search(r"папк|открой\w* (?:все )?(?:скрин|снимки)", p):
            os.startfile(SHOTS)
            return "Папка скриншотов", False
        if re.search(r"област|част\w* экрана|выдел|фрагмент|кусо?к", p):
            _key(0x5B, 0x10, 0x53)  # Win+Shift+S: Windows' own snip, it saves to the same folder
            return "Выдели область", False
        win_only = bool(re.search(r"\bокн", p))  # (not "window": that local hid window() for "сверни окна")
        clip = " и скопирован в буфер" if voice_settings.get("screenshot_clipboard") else ""
        if re.search(r"телеграм|telegram|\bтг\b|отправ|скинь|пришли", p):
            telegram("--file", screenshot(win_only), "--message", "Скриншот")
            return "Скриншот отправлен в Телеграм", False
        if re.search(r"буфер|скопируй", p) and not clip:
            path = screenshot(win_only)
            from PIL import Image
            _image_to_clipboard(Image.open(path))
            return "Скриншот в буфере", False
        screenshot(win_only)
        return f"Скриншот {'окна ' if win_only else ''}сохранён{clip}", False
    if re.search(r"ночн\w* (свет|режим)|night ?light", p):
        on = None if re.search(r"переключ", p) else not re.search(r"\b(вы|от)ключ|убер|без ", p)
        return night_light(on), False
    if re.search(r"автоповорот|поворот\w* экран|ориентаци", p):
        _key(0x5B, 0x41)  # Win+A: the Auto-rotate tile (its flag lives in HKLM, needs admin)
        return "Открыла быстрые настройки: плитка «Автоповорот»", False
    if re.search(r"(настройк|параметр)\w*", p) and re.search(r"открой|покажи|зайди|перейди", p):
        return open_settings(p), False
    if re.search(r"микрофон", p):
        return mic(not re.search(r"\bвключ|\bразглуш|\bвключи", p) or bool(re.search(r"\b(вы|от)ключ", p))), False
    if re.search(r"\bярк|\bярче|\bтемнее", p):
        lv = parse_level(p)
        if not lv:
            return _ps_out("'Яркость ' + (Get-CimInstance -Namespace root/wmi -ClassName "
                           "WmiMonitorBrightness).CurrentBrightness + ' процентов'")[1], True
        return brightness(**lv), False
    if re.search(r"\bгромк|\bзвук|\bгромче|\bтише|\bпотише", p):
        if re.search(r"\b(вы|от)ключ\w* звук|без звука|\bзаглуш|\bmute", p):
            return volume(mute=True), False
        lv = parse_level(p)
        if re.search(r"\bвключ\w* звук|\bверни звук", p) and not lv:
            return volume(mute=False), False
        if not lv:
            return f"Громкость {_audio('[Aud]::Get()')} процентов", True
        return volume(**lv), False
    if re.search(r"\bпауз|останови (музык|видео|воспроизв)|продолж\w* (музык|воспроизв)|\bвоспроизв", p):
        return media("play_pause"), False
    if re.search(r"следующ\w* (трек|песн|композ)|переключи (трек|песн)", p):
        return media("next"), False
    if re.search(r"предыдущ\w* (трек|песн|композ)", p):
        return media("prev"), False
    if re.search(r"таймер|напомин|напомни", p):
        if re.search(r"(отмен|сброс|удали|выключи)\w*\s+(все\s+)?(таймер|напоминан)", p):
            return timer("cancel"), True
        text = ""
        if re.search(r"напомни", p):  # "напомни через 10 минут позвонить маме"
            m = re.search(r"(?:минут\w*|секунд\w*|час\w*|полчаса)\s+(.+)$", p)
            text = m.group(1).strip() if m else ""
            text = re.sub(r"^(что|чтобы|о том что|про)\s+", "", text)
            text = text[:1].upper() + text[1:]
        n = extract_number(p)
        if n is None and re.search(r"полчаса", p):
            n, unit = 30, 60
        else:
            unit = 3600 if re.search(r"\bчас", p) else 1 if re.search(r"секунд", p) else 60
            n = 1 if n is None and re.search(r"\b(минуту|час)\b", p) else n
        if n is None:
            return "Не поняла, на сколько поставить таймер", True
        return timer("set", int(n * unit), text), True
    if re.search(r"заблокир", p):
        user32.LockWorkStation()
        return "Компьютер заблокирован", False
    if re.search(r"сверн\w* (все )?окна|покажи рабочий стол", p):
        return window("minimize_all"), False
    if re.search(r"экранн\w* клавиатур", p):
        return window("keyboard"), False
    for pat, what in [(r"сколько времени|который час|\bвремя\b", "time"),
                      (r"какое (сегодня )?число|какая (сегодня )?дата|какой (сегодня )?день", "date"),
                      (r"заряд|батаре|аккумулятор", "battery"),
                      (r"мест\w* на диске|свободн\w* мест|диск\w* свобод", "disk"),
                      (r"оперативн|памят\w* свобод|свободн\w* памят", "memory"),
                      (r"загрузк\w* процессор|процессор загруж", "cpu"),
                      (r"\bip\b|айпи|ай пи", "ip"), (r"сколько (уже )?работает|аптайм", "uptime")]:
        if re.search(pat, p):
            return system_info(what), True
    r = eval_math(p)
    if r is not None:
        return r, True
    return internet_question(p)


def internet_question(p):
    """'что такое X' / 'кто такой X' -> Wikipedia; other factual questions (and 'узнай в интернете ...')
    -> DuckDuckGo + Gemma. None (Gemma answers from its own knowledge) when offline or not a question."""
    # "кто такой Бэтмен, найди в интернете": the tail is an instruction, not part of the query
    p = re.sub(r",?\s*(?:найди|поищи|посмотри|глянь|загугли)\w*\s*(?:(?:в|по)\s+(?:интернете|сети|гуглу?|гугле))?\s*$",
               "", p).strip(" ,")
    m = re.match(r"^(?:а\s+)?(?:что такое|кто такой|кто такая|кто такие|что значит|что означает|"
                 r"расскажи (?:мне )?(?:про|о|об))\s+(.+)$", p)
    if not m:  # "найди Илона Маска" / "поищи информацию про Марс": Gemma turned it into a browser search
        m = re.match(r"^(?:найди|поищи|загугли|погугли|узнай)\w*\s+(?:в\s+интернете\s+)?(?:(?:информацию|инфу|что-нибудь)\s+)?"
                     r"(?:(?:о|об|про)\s+)?(.+)$", p)
    try:
        if m:
            CTX.update(kind="wiki", query=m.group(1))
            text = net_skills.wikipedia(m.group(1)) or net_skills.web_answer(p, llm_voice())
            return (text, True) if text else None
        m = re.match(r"^(?:узнай|спроси|поищи ответ|найди ответ)\w*\s+(?:в интернете\s+)?(.+)$", p)
        if m or (re.match(r"^(?:а\s+)?(?:кто|что|где|когда|сколько|какой|какая|какое|какие|почему|зачем|как|чем|"
                           r"откуда|куда|чей|чья)\b", p) and not re.search(r"\b(тебя|ты|твой|твоё|тебе|себя|вас)\b", p)):
            CTX["kind"] = "web"
            text = net_skills.web_answer(m.group(1) if m else p, llm_voice())
            return (text, True) if text else None
    except Exception as e:
        log(f"internet answer failed, Gemma answers offline: {e!r}")
    return None


def rescan_apps():
    """'обнови список приложений': rebuild the Start-menu + Desktop\\games list now and name new entries."""
    global _start_apps
    old = {n for n, _ in (_start_apps or [])}
    _start_apps = None
    new = start_apps()
    added = sorted({n for n, _ in new} - old) if old else []
    msg = f"Нашла {len(new)} {net_skills.plural(len(new), 'программу', 'программы', 'программ')}"
    if added:
        msg += ". Новые: " + ", ".join(added[:5]) + (" и другие" if len(added) > 5 else "")
    elif old:
        msg += ", новых нет"
    return msg


GEMMA_STATE = os.path.join(HERE, "gemma_state.txt")  # "off": unloaded on purpose (tray / "выгрузи Гемму")


def provider():
    cfg = voice_settings.get("llm") or {}
    prov = cfg.get("provider", "npu")
    if voice_settings.get("offline") and prov == "cloud":
        return "none"  # offline mode: nothing leaves the PC
    return prov if prov in ("none", "npu", "server", "cloud") else "npu"


def llm_endpoint():
    url = ((voice_settings.get("llm") or {}).get("url") or "http://127.0.0.1:8081").rstrip("/")
    # a bare host (Ollama, LM Studio, our server) gets /v1; a URL with its own path is used as given
    # (Gemini: https://generativelanguage.googleapis.com/v1beta/openai)
    return url + "/v1" if urllib.parse.urlparse(url).path in ("", "/") else url


def llm_request(body):
    """POST /chat/completions to the configured model. Our npu_server understands "prefix"/"stop"/plain-text
    "stream"; other servers get a plain OpenAI request (+ model name, + API key)."""
    prov, cfg = provider(), voice_settings.get("llm") or {}
    if prov != "npu":
        # without our forced '{"tool": "' prefix a cloud model may open with a ```json line: the "\n" stop
        # would cut the whole answer, so it goes too
        body = {k: v for k, v in body.items() if k not in ("prefix", "stream", "repetition_penalty", "stop")}
        if "generativelanguage.googleapis.com" in llm_endpoint():
            body["reasoning_effort"] = "minimal"  # Gemini 3 thinks by default: 3.8 s -> 1.2 s for a short reply
        if cfg.get("model"):
            body["model"] = cfg["model"]
    headers = {"Content-Type": "application/json"}
    key = voice_settings.secret("llm_api_key") if prov in ("server", "cloud") else ""
    if key:
        headers["Authorization"] = "Bearer " + key
    return urllib.request.Request(llm_endpoint() + "/chat/completions", json.dumps(body).encode("utf-8"), headers)


def gemma_on():
    """A language model can be used now: provider set, and the built-in NPU one not unloaded on purpose."""
    prov = provider()
    if prov == "none":
        return False
    if prov != "npu":
        return True
    if not voice_settings.gemma_dir():
        return False  # not downloaded (Settings -> Model)
    try:
        return open(GEMMA_STATE).read().strip() == "on"
    except OSError:
        return False  # unloaded by default: ~3.7 GB of RAM for little gain; "загрузи Гемму" loads it


def llm():
    """Gemma for net_skills (city names, web summaries) or None while unloaded: they fall back to Python."""
    return ask_text if gemma_on() else None


def _speak_first(sentence):
    """Web answers: say the first real sentence while Gemma writes the rest (skips the echoed date line,
    "не нашла ответа" and junk; speak() later queues only what follows it)."""
    if EARLY.get("text") is not None or len(sentence) < 8:
        return
    if re.match(r"(?i)сегодня\s+\d+\s+\w+\s+\d{4}", sentence) or re.search(r"(?i)не нашла|\d{9,}|(.)\1{9,}", sentence):
        return
    s = re.sub(r"^(Нашла?|Нашёл|Нашел),?\s+(что\s+)?", "", sentence).strip()
    s = s[:1].upper() + s[1:]
    EARLY["text"] = s
    speak_now(s)


def speak_now(text):
    """Start the voice at once without touching EARLY (used for the early first sentence)."""
    SPOKEN.update(text=text.lower(), t=time.time())
    _speak_pending.set()

    def run():
        try:
            _ps_out(f"Say {_q(text)}")
        finally:
            _speak_pending.clear()
    threading.Thread(target=run, daemon=True).start()


def llm_voice():
    """llm() for spoken web answers: streamed, the first sentence starts the voice early."""
    return (lambda prompt: ask_text(prompt, on_sentence=_speak_first)) if gemma_on() else None


def gemma_load(on):
    """'загрузи / выгрузи Гемму': the state file is the switch, ensure_server() never starts Gemma while off."""
    if on and not voice_settings.gemma_dir():
        return "Gemma не скачана. Её можно скачать в настройках, вкладка «Модель», вариант «На NPU»"
    with open(GEMMA_STATE, "w") as f:
        f.write("on" if on else "off")
    if on:
        threading.Thread(target=ensure_server, daemon=True).start()
        return "Загружаю Gemma, это секунд десять"
    _ps_out("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
            "? { $_.CommandLine -match 'npu_server' } | % { Stop-Process -Id $_.ProcessId -Force }", timeout=15)
    return "Gemma выгружена, память свободна. Простые команды работают"


def ask_text(prompt, on_sentence=None):
    """Plain-text Gemma call (no tool JSON): spoken answers built from search results, city names.
    on_sentence: streamed, called with every finished sentence while the rest is still generated."""
    return ask_messages([{"role": "system", "content": "Ты голосовой помощник. Отвечай по-русски, кратко, без markdown."},
                         {"role": "user", "content": prompt}], on_sentence)


def ask_messages(messages, on_sentence=None, max_tokens=150, temperature=0, repetition_penalty=None):
    if not ensure_server():
        raise RuntimeError("сервер Gemma не запустился")
    if provider() != "npu":
        on_sentence = None  # plain-text streaming is our npu_server's extension
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "stream": on_sentence is not None}
    if repetition_penalty:
        body["repetition_penalty"] = repetition_penalty
    with urllib.request.urlopen(llm_request(body), timeout=120) as r:
        if on_sentence is None:
            text = json.load(r)["choices"][0]["message"]["content"].strip()
        else:
            raw, done = b"", 0
            while chunk := r.read1(256):
                raw += chunk
                t = raw.decode("utf-8", "ignore")
                for m in re.finditer(r"[^.!?]*[.!?](?=\s)", t[done:]):
                    on_sentence(m.group(0).strip())
                    done += m.end()
            text = raw.decode("utf-8", "replace").strip()
    log(f"gemma text: {text!r}")
    return re.sub(r"[*_#`]", "", text)


# ====================================================================== Gemma

SYSTEM = """Ты {name}, голосовой помощник на компьютере с Windows. Отвечай только по-русски.
О себе ничего не выдумывай: у тебя нет возраста, биографии и другого имени.
Ответ всегда ОДИН JSON-объект без текста вокруг: {"tool": "<имя>", "args": {...}, "say": "<короткая фраза>"}

Инструменты:
- open_app {"name": str, "new_window": bool}  открыть программу или папку (название как услышал)
- close_app {"name": str}  закрыть программу
- open_url {"url": str}  открыть сайт (полный адрес https://...)
- web_search {"query": str}  найти в интернете
- telegram_message {"text": str}  отправить текст в Telegram (text это само сообщение)
- clipboard_to_telegram {}  отправить в Telegram текст из буфера обмена
- media {"action": "play_pause"|"next"|"prev"}  музыка
- window {"action": "minimize_all"|"maximize"|"close"}  окна
- power {"action": "sleep"|"shutdown"|"restart"|"cancel", "delay": секунды}  питание компьютера
- run_command {"command": str}  команда PowerShell, только если ничего выше не подходит
{extra_tools}- answer {}  ответить на вопрос или поговорить: ответ в "say", 1-2 предложения

Правила: если просят действие, которого нет в списке и нельзя сделать через run_command, выбери answer
и честно скажи, что не умеешь. Если фраза обрывочная или непонятная, выбери answer и переспроси,
предположив смысл по контексту. В answer НИКОГДА не пиши, что что-то включено, открыто или сделано.

Примеры:
"открой клауди" -> {"tool": "open_app", "args": {"name": "клауди"}, "say": "Открываю."}
"открой новое окно хрома" -> {"tool": "open_app", "args": {"name": "chrome", "new_window": true}, "say": "Новое окно."}
"закрой диспетчер задач" -> {"tool": "close_app", "args": {"name": "диспетчер задач"}, "say": "Закрываю."}
"открой ютуб" -> {"tool": "open_url", "args": {"url": "https://www.youtube.com"}, "say": "Открываю Ютуб."}
"кто такой бэтмен, найди в интернете" -> {"tool": "web_search", "args": {"query": "кто такой Бэтмен"}, "say": "Ищу."}
"напиши в телеграм купить молоко" -> {"tool": "telegram_message", "args": {"text": "Купить молоко"}, "say": "Отправила."}
"выключи компьютер через минуту" -> {"tool": "power", "args": {"action": "shutdown", "delay": 60}, "say": "Выключаю."}
"какие процессы едят больше всего памяти" -> {"tool": "run_command", "args": {"command": "Get-Process | Sort WS -Desc | Select -First 3 | % {'{0} {1:N0} МБ' -f $_.Name,($_.WS/1MB)}"}, "say": ""}
"что такое собака" -> {"tool": "answer", "args": {}, "say": "Собака это домашнее животное, потомок волка."}
"включи режим полёта" -> {"tool": "answer", "args": {}, "say": "Это я пока не умею."}"""



def system_prompt():
    """SYSTEM with the assistant's name, plugin tools and the user's own commands (so a command added to
    commands\\<lang>.json is usable through the model too)."""
    extra = "".join(f"- {name} {args}  {desc}\n" for name, (args, desc, _, _) in plugins.REG["tools"].items())
    says = [c["say"][0] for c in usercmd.items() if c.get("say")][:30]
    if says:
        extra += ('- user_command {"say": str}  команда пользователя, "say" одна из: '
                  + "; ".join(says) + "\n")
    name = voice_settings.get("assistant_name") or "Мурмур"
    return SYSTEM.replace("{name}", name).replace("{extra_tools}", extra)


CLAIMS = re.compile(r"(включен|выключен|отключен|открыт|закрыт|отправлен|установлен|сделан|готово)", re.I)


def ensure_server():
    """Start the Gemma NPU server if it is down (autostart can be killed). Returns True when ready.
    Never while unloaded on purpose (gemma_state.txt = off)."""
    if not gemma_on():
        return False
    if provider() != "npu":
        return True  # someone else's server: nothing to start here
    try:
        urllib.request.urlopen(llm_endpoint() + "/models", timeout=3)
        return True
    except Exception:
        pass
    report("Gemma загружается, это секунд десять", ok=True)
    subprocess.Popen([PYW, os.path.join(HERE, "npu_server.py")], cwd=HERE)
    for _ in range(80):
        time.sleep(2)
        try:
            urllib.request.urlopen(llm_endpoint() + "/models", timeout=3)
            return True
        except Exception:
            pass
    return False


def ask_gemma(text):
    if not ensure_server():
        raise RuntimeError("сервер Gemma не запустился")
    L = last()
    if L:  # one previous exchange, so "а почему?" / "а он ..." make sense (short: NPU prompt limit)
        text = f"(Контекст: перед этим спросили «{L['q'][:120]}», ты ответила «{L['a'][:200]}».)\n{text}"
    # the answer is forced to start as JSON and stops at the line end: no ```json fence tokens (~0.05 s each)
    body = {"messages": [{"role": "system", "content": system_prompt()}, {"role": "user", "content": text}],
            "max_tokens": 200, "temperature": 0, "prefix": '{"tool": "', "stop": ["\n"]}
    with urllib.request.urlopen(llm_request(body), timeout=120) as r:
        reply = json.load(r)["choices"][0]["message"]["content"]
    log(f"gemma: {reply!r}")
    m = re.search(r"\{.*\}", reply, re.S)
    if m:
        try:
            plan = json.loads(m.group(0))
            if isinstance(plan, dict):
                return plan
        except ValueError:
            pass
    t = re.search(r'"tool"\s*:\s*"(\w+)"', reply)
    if t:
        return {"tool": t.group(1), "args": {}, "say": ""}
    clean = re.sub(r"[{}\[\]\"`]", "", reply).strip()
    return {"tool": "answer", "args": {}, "say": clean[:200]}


# ---------------------------------------------------------------- conversation mode
# "давай поговорим" turns it on: phrases go to the model as a chat with memory (free text, no tool JSON);
# commands said as commands ("сделай громче", "открой ...") still run. One-off: "скажи мне ...",
# "как думаешь ...". Off: "хватит болтать" / "закончим разговор", or CHAT_IDLE seconds of silence.
CHAT = {"on": False, "t": 0.0, "history": []}
CHAT_IDLE = 180
CHAT_TURNS = 8  # messages kept (the NPU prompt is 2048 tokens)
CHAT_FOLLOW_SEC = 12
# short on purpose: the int4 Gemma on the NPU answers worse (garbled words, loops) with a long system prompt
CHAT_SYSTEM = ("Ты {name}, голосовой помощник. Сейчас просто разговариваешь с пользователем на «ты». "
               "Отвечай по-русски, по существу, 1-3 коротких предложения, без markdown и без приветствий. "
               "Не выдумывай погоду, курсы и новости. Если фраза непонятная, переспроси, что имелось в виду.")
CHAT_ON = re.compile(r"(?:давай\s+|хочу\s+|можно\s+)?(?:поговорим|поболтаем|пообщаемся|побеседуем|поговорить|"
                     r"поболтать|пообщаться)(?:\s+с\s+тобой)?(?:\s+немного)?|поговори\s+со\s+мной|"
                     r"(?:включи|начни|давай)\s+(?:режим\s+)?(?:разговор\w*|болтовн\w*|беседу)|режим\s+разговора")
CHAT_OFF = re.compile(r"(?:хватит|закончи\w*|заверши\w*|выйди\s+из|выключи|останови|прекрати)\s+"
                      r"(?:режим\w*\s+)?(?:разговор\w*|болта\w*|болтовн\w*|беседу|общени\w*)|конец\s+разговора|"
                      r"давай\s+закончим")
CHAT_ONCE = re.compile(r"^(?:скажи\s+мне|как\s+(?:ты\s+)?думаешь|что\s+ты\s+думаешь|давай\s+обсудим|"
                       r"поговори\s+со\s+мной\s+(?:о|об|про)|посоветуй|мне\s+нужен\s+совет)\b")
COMMAND_VERB = re.compile(r"^(?:включи|выключи|отключи|открой|закрой|запусти|сделай|поставь|отправь|сверни|"
                          r"разверни|убери|прибавь|убавь|заблокируй|перезагрузи|напиши|найди|скинь|"
                          r"громче|тише|ярче|темнее|скриншот)\w*")


CONTINUE_SEC = 120
NUMS_ONLY = re.compile(r"(?:(?:а|и|ну|тогда|лучше|давай|теперь|может|на|сделай|поставь)\s+)*(\d+(?:[\s,.-]+\d+)*)")


def continue_last(p):
    """Only numbers right after a command with numbers: the same command with them ("ТДП 2530" -> "25 30"
    runs "ТДП 25 30"; "громкость 30" -> "а 50" runs "громкость 50"). Built-in commands only, no model."""
    m = NUMS_ONLY.fullmatch(p)
    L = last()
    if not m or not L or time.time() - L["t"] > CONTINUE_SEC or not re.search(r"\d", L["q"]):
        return None
    nums = " ".join(re.findall(r"\d+", m.group(1)))
    q = re.sub(r"\d+(?:[\s,.-]+\d+)*", nums, L["q"], count=1)
    r = command_route(q.lower().strip(" .!?,"), q)
    if r:
        log(f"continued «{L['q']}» as «{q}»")
        CTX["continued"] = q  # the dialog memory keeps the full command, so the next numbers work too
    return r


def chat_followup():
    """dictate.py: listen longer after an answer while talking."""
    return CHAT_FOLLOW_SEC if CHAT["on"] else 0


def chat_reply(text):
    """One turn of free conversation with the model (history kept in CHAT)."""
    if not gemma_on():
        PENDING["load"] = time.time() + 20
        return "Для разговора нужна Gemma. Загрузить её?", True
    name = voice_settings.get("assistant_name") or "Мурмур"
    msgs = [{"role": "system", "content": CHAT_SYSTEM.replace("{name}", name)}]
    L = last()
    if L and all(m["content"] != L["q"] for m in CHAT["history"]):  # the last command outside the chat,
        # so "25 30" right after "ТДП 2530" can be understood
        msgs[0]["content"] += (f"\nПоследняя команда пользователя была «{L['q'][:120]}», помощник ответил "
                               f"«{L['a'][:200]}». Обрывочная фраза скорее всего продолжает её.")
    msgs += CHAT["history"][-CHAT_TURNS:] + [{"role": "user", "content": text}]

    def first(sentence):  # start the voice on the first sentence while the rest is generated
        sentence = re.sub(r"\s+", " ", re.sub(r"[*_#`]", "", sentence)).strip()
        if EARLY.get("text") is None and len(sentence) >= 4:
            EARLY["text"] = sentence
            speak_now(sentence)
    reply = ask_messages(msgs, on_sentence=first, max_tokens=180, temperature=0, repetition_penalty=1.2)
    reply = re.sub(r"\s+", " ", reply).strip()
    if EARLY.get("text") and not reply.startswith(EARLY["text"]):
        EARLY.pop("text")  # the spoken start differs from the final text: say it all again rather than garble
    CHAT["history"] = (CHAT["history"] + [{"role": "user", "content": text},
                                          {"role": "assistant", "content": reply}])[-CHAT_TURNS:]
    CHAT["t"] = time.time()
    return reply or "Не расслышала, повтори, пожалуйста", True


def chat_route(text, p):
    """Conversation mode switches and turns. None = not a conversation phrase (the normal routes go on)."""
    if CHAT["on"] and time.time() - CHAT["t"] > CHAT_IDLE:
        CHAT.update(on=False, history=[])
    if CHAT_OFF.search(p):
        was = CHAT["on"]
        CHAT.update(on=False, history=[])
        CTX["end"] = True
        return ("Хорошо, поговорим потом" if was else "Мы и не разговаривали"), True
    m = CHAT_ON.match(re.sub(r"^(?:ну|а|слушай|эй)\s+", "", p))
    if m:
        CHAT.update(on=True, t=time.time(), history=[])
        topic = re.sub(r"^\W+", "", re.sub(r"^(?:ну|а|слушай|эй)\s+", "", p)[m.end():])
        if topic or not gemma_on():  # "давай поговорим о космосе": answer right away
            return chat_reply(text)
        return "Давай. О чём поговорим?", True
    if CHAT["on"]:
        if COMMAND_VERB.match(p):  # a command said during the conversation still runs as a command
            return None
        return chat_reply(text)
    if CHAT_ONCE.match(p):
        return chat_reply(text)
    return None


def russian(say):
    if not say or (re.search(r"[A-Za-z]{3,}", say) and not re.search(r"[А-Яа-яЁё]{3,}", say)):
        return ""
    s = re.split(r"(?<=[.!?])\s+", say.strip())
    return " ".join(s[:2])


def run_tool(tool, args, phrase):
    """Execute Gemma's choice. Returns (result_text, vocal)."""
    if tool == "open_app":
        return open_app(str(args.get("name", "")), phrase, bool(args.get("new_window"))), False
    if tool == "close_app":
        return close_app(str(args.get("name", ""))), False
    if tool == "open_url":
        url = re.sub(r"[^\w\-.:/?=&%~+]", "", str(args.get("url", "")))
        if not url:
            raise ValueError("нет адреса")
        os.startfile(url if "://" in url else "https://" + url)
        return "Открываю сайт", False
    if tool == "web_search":  # answer by voice (a browser tab is not an answer)
        q = str(args.get("query") or phrase)
        CTX.update(kind="web", query=q)
        text = net_skills.wikipedia(q) or net_skills.web_answer(q, llm_voice())
        return (text or "Не нашла ответа"), True
    if tool in plugins.REG["tools"]:
        return plugins.REG["tools"][tool][2](args, phrase)
    if tool == "user_command":
        hit = usercmd.match(str(args.get("say", "")))
        if hit:
            return usercmd.run(sys.modules[__name__], *hit)
    if tool == "telegram_message":
        text = str(args.get("text") or "").strip() or tg_text(phrase)
        if not text:
            return "Не поняла, что отправить", True
        telegram("--message", text)
        return "Отправила в Телеграм", False
    if tool == "clipboard_to_telegram":
        import pyperclip
        text = pyperclip.paste().strip()
        if not text:
            return "Буфер обмена пуст", True
        telegram("--message", text)
        return "Буфер обмена отправлен в Телеграм", False
    if tool == "media":
        return media(str(args.get("action", "play_pause"))), False
    if tool == "window":
        return window(str(args.get("action", "minimize_all"))), False
    if tool == "power":
        return power(str(args.get("action", "")), int(args.get("delay") or 0)), False
    if tool == "run_command":
        return run_command(str(args.get("command", ""))), True
    return None, True  # answer / unknown


# ---------------------------------------------------------------- typing into the focused field

TYPED = {"hwnd": 0, "t": 0.0, "buf": "", "last": "", "case": ""}  # window that last got voice text (here or
#   via M1 in dictate.py), the text typed there by voice (to delete words or sentences exactly), the last
#   piece, and a pending "с большой / с маленькой буквы" for the next piece
PENDING = {}                    # "type": deadline after a bare "напиши" (the next utterance is the text)
TYPED_TTL = 300                # "отправь" presses Enter only in that window and only this long after


def note_typed(text=""):
    hwnd = user32.GetForegroundWindow()
    if hwnd != TYPED["hwnd"] or time.time() - TYPED["t"] > TYPED_TTL:
        TYPED["buf"] = ""
    TYPED.update(hwnd=hwnd, t=time.time(), buf=TYPED["buf"] + text, last=text)


def shape_dictation(text):
    """A dictated piece after "с большой / с маленькой буквы"."""
    case, TYPED["case"] = TYPED["case"], ""
    if case == "upper":
        return text[:1].upper() + text[1:]
    if case == "lower" and not re.match(r"[A-ZА-ЯЁ]{2}", text):  # not an abbreviation
        return text[:1].lower() + text[1:]
    return text


def _backspace(n):
    for _ in range(n):
        _key(0x08)


def _erase(chars, keep):
    """Delete the last `chars` characters typed by voice; the buffer becomes `keep`."""
    _backspace(chars)
    TYPED.update(buf=keep, last="", t=time.time())


EDIT_COUNTS = {"одно": 1, "один": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
             "восемь": 8, "девять": 9, "десять": 10}
PUNCT = {"точка": ".", "запятая": ",", "вопрос": "?", "вопросительный знак": "?", "знак вопроса": "?",
         "восклицательный знак": "!", "восклицание": "!", "двоеточие": ":", "точка с запятой": ";",
         "многоточие": "...", "открой скобку": "(", "закрой скобку": ")", "кавычки": "\""}


def edit_route(text, dictation=True):
    """Editing by voice when the whole utterance is a command: delete the last piece / words / sentence,
    replace a word, new line, punctuation, undo, select / copy / cut / paste, cursor to start or end, Enter.
    Commands that delete or undo act only in the window that got the voice text (Ctrl+A + Delete in
    Explorer would delete files). With the command button (dictation=False) every edit needs that window,
    so "скопируй" there still copies the last answer. Returns the reply or None (not an edit command)."""
    p = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))).strip()
    same = _same_window()
    if not p or (not dictation and not same):
        return None
    buf = TYPED["buf"] if same else ""

    def need():  # destructive edits: only where the voice text went
        return None if same else "Не вижу текста, который я напечатала в этом окне"

    if re.fullmatch(r"(?:отправь|отправляй|отправить|отправь сообщение|ввод|энтер|enter)", p):
        if not same:
            return "Не вижу, куда отправлять. Сначала надиктуй текст"
        _key(0x0D)
        TYPED.update(t=0, buf="")  # one Enter per typed text
        return "Отправлено"
    m = re.fullmatch(r"(?:замени|исправь|поменяй) (.+?) на (.+)", p)
    if m:
        if need():
            return need()
        src = re.sub(r"[^\w\s]", " ", text).split()
        old, new = m.group(1), " ".join(src[len(src) - len(m.group(2).split()):])  # keep the spoken case
        i = buf.lower().replace("ё", "е").rfind(old)
        if i < 0:
            return f"Не нашла «{old}» в надиктованном"
        tail = buf[i + len(old):]
        _erase(len(buf) - i, buf[:i])
        _paste_text(new + tail)
        return f"Заменила на «{new}»"
    m = re.fullmatch(r"(?:удали|сотри|стереть|убери) (?:последн\w+ )?(\d+|\w+ )?слов\w*", p)
    if m:
        if need():
            return need()
        n = m.group(1).strip() if m.group(1) else "1"
        n = int(n) if n.isdigit() else EDIT_COUNTS.get(n, 1)
        if buf.strip():
            keep = re.sub(r"(?:\S+\s*){1,%d}$" % n, "", buf)
            _erase(len(buf) - len(keep), keep)
        else:  # typed before this window was tracked: the editor's own word delete
            for _ in range(n):
                _key(0x11, 0x08)
            TYPED["buf"] = ""
        return "Удалила слово" if n == 1 else f"Удалила слов: {n}"
    if re.fullmatch(r"(?:удали|сотри|стереть|убери) (?:последн\w+ )?(?:предложени\w*|фраз\w*)", p):
        if need():
            return need()
        if not buf.strip():
            return "Не помню, что тут было написано"
        body = re.sub(r"[.!?…]+\s*$", "", buf.rstrip())
        cut = max(body.rfind(c) for c in ".!?…\n")
        keep = buf[:cut + 1] if cut >= 0 else ""
        if cut >= 0 and buf[cut + 1:cut + 2] == " ":
            keep += " "
        _erase(len(buf) - len(keep), keep)
        return "Удалила предложение"
    if re.fullmatch(r"(?:удали|сотри|стереть|смотри)(?: это| последнее| написанное| что написал\w*)?", p):  # Whisper: "Смотри"
        if need():
            return need()
        if TYPED["last"] and buf.endswith(TYPED["last"]):
            _erase(len(TYPED["last"]), buf[:-len(TYPED["last"])])
        elif buf.strip():  # right after another edit: Ctrl+Z would bring the deleted text back
            keep = re.sub(r"\S+\s*$", "", buf)
            _erase(len(buf) - len(keep), keep)
        else:
            _key(0x11, 0x5A)  # Ctrl+Z undoes the last paste
            TYPED["buf"] = ""
        return "Стёрла"
    if re.fullmatch(r"(?:очисти|очисти поле|удали вс\w|сотри вс\w|удали весь текст|сотри весь текст)", p):
        if need():
            return need()
        _key(0x11, 0x41)
        _key(0x2E)
        TYPED["buf"] = ""
        return "Поле очищено"
    if re.fullmatch(r"(?:новая строка|с новой строки|следующая строка|перенос строки|перенеси строку|enter строка)", p):
        _key(0x10, 0x0D)  # Shift+Enter: a line break; "отправь" is the Enter
        note_typed("\n")
        TYPED["case"] = "upper"
        return "Новая строка"
    if re.fullmatch(r"(?:новый абзац|абзац|с нового абзаца)", p):
        _key(0x10, 0x0D)
        _key(0x10, 0x0D)
        note_typed("\n\n")
        TYPED["case"] = "upper"
        return "Новый абзац"
    if p in PUNCT:
        mark = PUNCT[p]
        if same and mark not in "(\"":
            # "слово " + "." -> "слово. "; Whisper's own end mark is replaced: "кефира. " + "?" -> "кефира? "
            tail = re.search(r"[.,!?:;…]*\s?$", buf).group() if buf.endswith(" ") or buf[-1:] in ".,!?:;…" else ""
            if "\n" not in tail:
                _backspace(len(tail))
                TYPED["buf"] = buf[:len(buf) - len(tail)]
        _paste_text(mark + ("" if mark in "(\"" else " "))
        TYPED["case"] = "upper" if mark in ".?!..." else "lower" if mark in ",:;" else ""
        return mark
    if re.fullmatch(r"(?:с большой буквы|с заглавной|с заглавной буквы)", p):
        TYPED["case"] = "upper"
        return "Следующее с большой буквы"
    if re.fullmatch(r"(?:с маленькой буквы|со строчной|со строчной буквы|с маленькой)", p):
        TYPED["case"] = "lower"
        return "Следующее с маленькой буквы"
    if re.fullmatch(r"(?:отмени|отменить|отмена действия|верни как было|назад)", p):
        if need():
            return need()
        _key(0x11, 0x5A)
        TYPED["buf"] = ""
        return "Отменила"
    if re.fullmatch(r"(?:верни обратно|повтори действие|вперед)", p):
        if need():
            return need()
        _key(0x11, 0x59)
        TYPED["buf"] = ""
        return "Вернула"
    if re.fullmatch(r"(?:выдели вс\w|выдели весь текст)", p):
        _key(0x11, 0x41)
        return "Выделила всё"
    if re.fullmatch(r"(?:скопируй вс\w|скопируй весь текст)", p):
        _key(0x11, 0x41)
        _key(0x11, 0x43)
        _key(0x11, 0x23)  # Ctrl+End: drop the selection, cursor back at the end
        return "Текст скопирован"
    if re.fullmatch(r"(?:вырежи вс\w|вырежи весь текст)", p):
        if need():
            return need()
        _key(0x11, 0x41)
        _key(0x11, 0x58)
        TYPED["buf"] = ""
        return "Текст вырезан"
    if dictation and re.fullmatch(r"(?:скопируй|копируй|скопировать)", p):
        _key(0x11, 0x43)
        return "Скопировала"
    if dictation and re.fullmatch(r"(?:вырежи|вырезать)", p):
        if need():
            return need()
        _key(0x11, 0x58)
        TYPED["buf"] = ""
        return "Вырезала"
    if re.fullmatch(r"(?:вставь|вставить|вставь из буфера)", p):
        _key(0x11, 0x56)
        TYPED["buf"] = ""  # unknown text now
        return "Вставила"
    if re.fullmatch(r"(?:в начало|в начало текста|курсор в начало)", p):
        _key(0x11, 0x24)
        TYPED["buf"] = ""  # the cursor left the end: word and sentence deletes would miss
        return "В начале"
    if re.fullmatch(r"(?:в конец|в конец текста|курсор в конец)", p):
        _key(0x11, 0x23)
        return "В конце"
    return None


def _paste_text(text):
    import pyperclip
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    _key(0x11, 0x56)  # Ctrl+V by virtual keys: layout independent
    time.sleep(0.25)
    if old is not None:
        pyperclip.copy(old)
    note_typed(text)


def _same_window():
    return TYPED["hwnd"] and user32.GetForegroundWindow() == TYPED["hwnd"] and time.time() - TYPED["t"] < TYPED_TTL


def typing_route(text, p):
    """'напиши …' types into the focused field like M1, 'отправь' presses Enter (only in the window that
    just got voice text: a stray Enter in a terminal would run a command), plus допиши / с новой строки /
    сотри / очисти поле. 'напиши в телеграм …' is not here: it goes to the Telegram bot."""
    if re.match(r"^(напиши|отправь|скинь|пошли)\w*\s+(?:мне\s+)?(?:в|во)\s+(телеграм|telegram|тг)\b", p):
        return None  # "напиши в телеграм купить хлеб": the Telegram bot, not the focused field
    if PENDING.get("type", 0) > time.time() and not re.fullmatch(r"(отмена|не надо|ничего)", p):
        PENDING.pop("type")  # after a bare "напиши": this whole utterance is the text
        body = text.strip()
        _paste_text(body[:1].upper() + body[1:])
        return "Написала", False
    if re.fullmatch(r"(напиши|допиши|пиши)", p):
        PENDING["type"] = time.time() + 15
        return "Что написать?", True
    m = re.match(r"^\W*(напиши|допиши|с новой строки|новой строкой|новая строка)(?:\s+мне)?[\s:,.-]+(.+)$",
                 text.strip(), re.I | re.S)
    if m:
        verb, body = m.group(1).lower(), m.group(2).strip()
        if verb == "допиши":
            body = " " + body
        else:
            body = body[:1].upper() + body[1:]
            if verb != "напиши":  # new line
                _key(0x10, 0x0D)  # Shift+Enter: a line break, not "send", in messengers
        _paste_text(body)
        return {"напиши": "Написала", "допиши": "Дописала"}.get(verb, "Написала с новой строки"), False
    if re.fullmatch(r"(отправь|отправляй|отправить|отправь сообщение|ввод|энтер|enter)", p):
        if not _same_window():
            return "Не вижу, куда отправлять. Сначала скажи: напиши и текст", True
        _key(0x0D)
        TYPED["t"] = 0  # one Enter per typed text
        return "Отправила", False
    edited = edit_route(text, dictation=False)  # the same editing commands as with the dictation button
    if edited:
        return edited, False
    if re.fullmatch(r"(сотри|удали|удали это|удали последнее|смотри|очисти|очисти поле)", p):
        return "Не вижу текста, который я напечатала в этом окне", True
    return None


# ---------------------------------------------------------------- dialog (Alice-style follow-ups)

LAST = {"q": "", "a": "", "kind": "", "file": None, "query": "", "t": 0.0}  # the previous exchange
CTX = {}            # filled by skills during one command: kind, file, query, end
after_reply = None  # set by dictate.py: opens the 6 s follow-up listening window
on_speaking = None  # set by dictate.py: on_speaking(True/False) around the voice (barge-in "стоп")
LAST_TTL = 180      # follow-ups refer to answers younger than this


def last():
    return LAST if LAST["a"] and time.time() - LAST["t"] < LAST_TTL else None


HELP_SHORT = ("Я управляю звуком и яркостью, открываю и закрываю программы и игры, пишу текст "
              "в поле ввода, отправляю скриншоты и файлы в Телеграм, отвечаю на вопросы, говорю погоду и курс "
              "валют, ставлю таймеры. Скажи: отправь список команд в телеграм")
HELP_FILE = os.path.join(HERE, "commands.txt")  # shown by the SaySo tray menu ("Что умеет")
HELP_FULL = """🎙 SaySo: голосовой помощник

🎙 Вызов: кнопка диктовки (держать) · кнопка команды (нажать) · по имени: «{name}, …» (от сети) · после ответа слушает · стоп перебивает
💬 Разговор: повтори · отправь это в телеграм · скопируй · подробнее · а завтра? / а евро? · спасибо
⌨️ Ввод: напиши … · допиши … · с новой строки … · отправь (Enter)
✏️ Правка (кнопкой диктовки, фраза целиком): удали · удали слово / три слова · удали предложение · удали всё · замени … на … · новая строка · новый абзац · точка, запятая, вопрос · с большой / маленькой буквы · отмени, верни обратно · выдели / скопируй / вырежи всё · скопируй, вставь · в начало, в конец · отправить · «напиши удали» печатает само слово
🗣 Разговор с Gemma: давай поговорим (о …) · дальше просто говори, команды тоже работают · хватит болтать · разово: скажи мне … / как думаешь … / посоветуй …
🧠 Модель: выгрузи / загрузи Гемму (без неё работают все команды, кроме свободного разговора) · слушание вкл/выкл
✍️ Свои команды: настройки → «Команды» (открыть сайт, программу, нажать клавиши, webhook)
🔊 Система: громкость / яркость N, громче, тише · звук, микрофон, ночной свет вкл/выкл · пауза, следующий трек · сверни окна · заблокируй · перейди в режим Windows / выйди из Xbox · перейди в режим Xbox
ℹ️ Инфо: время · дата · заряд · диск · память · процессор · IP
📂 Программы: открой / закрой любую программу или игру · открой ютуб · настройки блютуз · обнови список приложений · найди файл … (по всему диску) · скопируй найденный файл · отправь этот файл в телеграм · открой найденный файл
❓ Помощь: что ты умеешь · список команд · что ты умеешь с файлами (команды по теме) · отправь список команд в телеграм
📸 Скриншоты (в «Изображения → Снимки экрана» и в буфер): скриншот · скриншот окна · скриншот области · открой папку скриншотов
📨 Telegram: скриншот в телеграм · напиши в телеграм … · отправь файл / последнее фото / скачанный файл
🌍 Интернет: я сейчас в … · где я · погода (завтра, в городе) · курс валют · что такое / кто такой · любые вопросы
⏱ Прочее: таймер · напомни через … · калькулятор · выключи / перезагрузи / усыпи (с подтверждением)"""


def help_full():
    name = voice_settings.get("assistant_name") or "Мурмур"
    return HELP_FULL.replace("{name}", name) + "".join("\n" + line for line in plugins.REG["help"])


# "список команд": ready phrases, instant and offline (the model took 4-14 s and once said "Удалі")
HELP_TOP = ["громкость 50", "сделай скриншот", "скриншот в телеграм", "напиши в телеграм купить хлеб",
            "найди файл отчёт за май", "погода на завтра", "таймер на 10 минут", "открой ютуб", "давай поговорим"]
HELP_TOPICS = {"тг": "телеграм", "telegram": "телеграм", "звук": "громк", "музык": "трек", "снимк": "скриншот",
               "скрин": "скриншот", "диктовк": "правка", "текст": "правка", "редактир": "правка",
               "питани": "мощност", "тдп": "мощност", "вентилятор": "вентилятор", "игр": "игр", "интернет": "интернет",
               "фото": "фото", "систем": "система", "модел": "модель", "гемм": "модель", "свет": "подсветк"}


def _help_lines(topic):
    """Items of the full list (plugins included) about a topic: a whole section when its title matches,
    else the single items that mention it."""
    stems = set()
    for w in re.findall(r"\w{3,}", topic):
        stems.add(w[:max(4, len(w) - 3)])  # "файлами" -> "файл", "телеграмом" -> "телегра"
        stems.update(v for k, v in HELP_TOPICS.items() if w.startswith(k))
    out = []
    for line in help_full().splitlines():
        title, _, body = line.partition(":")
        if not body or title.startswith("❓"):  # the help section itself
            continue
        items = [i.strip() for i in re.split(r"\s+·\s+", body.strip()) if i.strip()]
        if any(s in title.lower() for s in stems):
            out += items
        else:
            out += [i for i in items if any(s in i.lower() for s in stems)]
    return list(dict.fromkeys(out))


def _help_list(items):
    return "Можно сказать:\n" + "\n".join("• " + i.rstrip(".;") + "." for i in items[:12]) + \
        "\nПолный список: «отправь список команд в телеграм»."  # the dot: a pause in TTS


def help_answer(p):
    """'список команд' / 'что ты умеешь' / 'что ты умеешь с файлами': phrases to say, one per line, from the
    full list in Python. The model only answers a topic that no section or item mentions."""
    m = re.search(r"(?:умеешь|можешь|команды|команд)\s+(?:делать\s+)?(?:с|со|про|для|по|в|во|о|об|насчёт|насчет)\s+(.+)$", p)
    if not m:
        top = HELP_TOP + (["экорежим", "турбо режим"] if any(l.startswith("🎮 ClawTweaks") for l in plugins.REG["help"]) else [])
        return _help_list(top)
    items = _help_lines(m.group(1))
    if items:
        return _help_list(items)
    if gemma_on():
        try:
            return _help_llm(p)
        except Exception as e:
            log(f"help_answer: {e!r}")
    return f"Про «{m.group(1)}» команд нет. Скажи: список команд"


def _help_llm(p):
    """A topic Python did not find: the model picks from the full list."""
    text = ask_messages([{"role": "system", "content":
        "Ты голосовой помощник. Ниже полный список твоих голосовых команд. Ответь списком готовых фраз, которые "
        "пользователь может сказать: каждая с новой строки, начинается с «• », по-русски, без пояснений и "
        "markdown. Каждая фраза конкретная, с примером вместо многоточия: «громкость 50», «найди файл отчёт за "
        "май», «напиши в телеграм купить хлеб». Если спрашивают вообще, дай 10 самых полезных из разных "
        "разделов, без «что ты умеешь»; если про тему, то только команды этой темы, до 10.\n\n" + help_full()},
        {"role": "user", "content": p}], max_tokens=400)
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith(("•", "-"))]
    if len(lines) < 3:
        raise ValueError(f"not a list: {text!r}")
    lines = ["• " + l.lstrip("•- ").rstrip(".;…").strip("«»\"") + "." for l in lines[:12]]  # the dot: a pause in TTS
    return "Можно сказать:\n" + "\n".join(lines) + "\nПолный список: «отправь список команд в телеграм»."


def dialog_route(p):
    """Follow-ups to the previous answer: repeat, send/copy it, more detail, "а завтра?"/"а евро?", goodbye."""
    gemma = r"(?:гемм?[ауые]|джемм?[ауые]|гим+[ауые]|гемм?ой|gemma|модель|нейросеть)"
    if re.fullmatch(rf"(?:выгрузи|выгрузить|отключи|отключить|выключи|выключить|останови|закрой)\w*\s+{gemma}", p):
        return gemma_load(False), True
    if re.fullmatch(rf"(?:загрузи|загрузить|включи|включить|запусти|запустить|подними)\w*\s+{gemma}", p) or (
            PENDING.get("load", 0) > time.time() and re.fullmatch(r"(да|давай|загрузи|ага|конечно)\w*", p)):
        PENDING.pop("load", None)
        return gemma_load(True), True
    if PENDING.get("load", 0) > time.time() and re.fullmatch(r"(нет|не надо|не нужно|не|отмена|не стоит|потом|"
                                                             r"не загружай|не хочу)\w*", p):
        PENDING.pop("load", None)
        CTX["end"] = True
        return "Хорошо, без Gemma", False
    PENDING.pop("load", None)
    if re.fullmatch(r"(стоп|ступ|стопе|хватит|замолчи|молчи|тихо|умолкни|помолчи|достаточно)", p):  # Whisper: "Ступ!"
        stop_speaking()
        CTX["end"] = True
        return "Молчу", False
    # small talk about the assistant itself: answered here, never searched on the web (a search for
    # "как дела" once gave "Я Тинка, 26 лет", and the dialog memory then repeated it)
    if re.fullmatch(r"(а\s+)?(ты\s+)?кто\s+ты(\s+такая)?|(а\s+)?ты\s+кто(\s+такая)?|как\s+тебя\s+зовут|"
                    r"представься|твоё имя|твое имя", p):
        name = voice_settings.get("assistant_name") or "Мурмур"
        return f"Я {name}, голосовой помощник. Скажи: что ты умеешь", True
    if re.fullmatch(r"(ну\s+)?(а\s+)?(как\s+(у\s+тебя\s+|твои\s+|то\s+и\s+|ты\s+и\s+)?дела|как\s+ты|как\s+жизнь|как\s+поживаешь|что\s+нового)"
                    r"(\s+у\s+тебя)?", p):
        return "Всё хорошо, работаю. Чем помочь?", True
    if re.fullmatch(r"(спасибо\w*|благодарю|вс[её]|отбой|пока|ладно|понятно|ок|окей|отлично)", p):
        CTX["end"] = True  # closes the conversation: no follow-up window after this
        return ("Пожалуйста" if re.match(r"спасиб|благодар", p) else "Хорошо"), True
    if re.search(r"что ты (?:умеешь|можешь)|что умеешь|какие (?:у тебя )?(?:есть )?команды|(?:список|перечень)\w* "
                 r"команд|перечисли\w*(?: мне)?(?: (?:свои|все|твои))? команды|помощь$|справка$", p):
        if re.search(r"телеграм|telegram|\bтг\b", p) and re.search(r"отправ|скинь|пришли|перешли|пошли", p):
            telegram("--message", help_full())
            return "Отправила список команд в Телеграм", False
        return help_answer(p), True
    f = FOUND["file"]
    if (f and time.time() - FOUND["t"] < FOUND_TTL and re.search(r"\bфайл|найденн", p)
            and re.search(r"\b(?:эт\w*|его|найденн\w*)\b", p)):  # "скопируй найденный файл", "этот файл в тг"
        if not os.path.exists(f):
            return "Найденного файла уже нет на месте", True
        CTX.update(meta=True, file=f)
        if re.search(r"копир", p):
            rc, out = _ps_out(f"Set-Clipboard -LiteralPath {_q(f)}")  # as a file: pastes in Explorer, Telegram
            return (f"Скопировала файл {os.path.basename(f)}", False) if rc == 0 else (f"Не скопировала: {out}", True)
        if re.search(r"телеграм|telegram|\bтг\b", p):
            if os.path.getsize(f) > TG_LIMIT:
                return f"Файл {os.path.basename(f)} больше 50 МБ, Телеграм-бот его не примет", True
            telegram("--file", f, "--message", os.path.basename(f))
            return f"Отправила в Телеграм: {os.path.basename(f)}", False
        if re.search(r"открой|запусти", p):
            os.startfile(f)
            return f"Открываю {os.path.basename(f)}", False
        if re.search(r"покажи|где", p):
            subprocess.Popen(["explorer.exe", "/select,", f])
            return f"{os.path.basename(f)}, в папке {os.path.dirname(f)}", False
    L = last()
    if not L:
        return None
    if re.match(r"(повтори|ещё раз|еще раз|не расслышал|не услышал|что ты сказала|что-что|не поняла?\b)", p):
        CTX["meta"] = True
        return L["a"], True
    if (re.search(r"(отправ|скинь|пришли|перешли|пошли)\w*", p) and re.search(r"телеграм|telegram|\bтг\b", p)
            and not re.search(r"скрин|снимок|сделай|фото|файл|скачан", p)):  # "сделай скриншот и отправь" is a new task
        rest = tg_text(p).lower()
        if not rest or re.match(r"(это|его|её|ее|ответ|результат|курс|погод|прогноз|свой|мне|вс[её])", rest):
            if L["file"] and os.path.exists(L["file"]) and not re.search(r"ответ|текст", rest):
                telegram("--file", L["file"], "--message", os.path.basename(L["file"]))
                CTX["meta"] = True
                return "Отправила файл в Телеграм", False
            telegram("--message", L["a"])
            CTX["meta"] = True
            return "Отправила ответ в Телеграм", False
    if re.match(r"(скопируй|копируй)\w*", p):
        import pyperclip
        pyperclip.copy(L["a"])
        CTX["meta"] = True
        return "Скопировала ответ", False
    if re.match(r"(а\s+)?(подробнее|поподробнее|расскажи (подробнее|больше|ещё|еще)|больше)\b", p):
        if L["kind"] == "wiki" and L["query"]:
            text = net_skills.wikipedia(L["query"], sentences=5)
            if text:
                CTX.update(kind="wiki", query=L["query"])
                return text, True
        if not gemma_on():
            return "Подробнее без Gemma не расскажу. Скажи: загрузи Гемму", True
        return ask_text(f"Предыдущий вопрос: {L['q']}\nТвой ответ: {L['a']}\n"
                        "Расскажи подробнее, 3-4 коротких предложения."), True
    m = re.match(r"(?:а|и)\s+(.+)$", p)
    if m:
        rest = m.group(1)
        if L["kind"] == "weather":
            CTX["kind"] = "weather"
            return net_skills.weather("погода " + rest, llm()), True
        if L["kind"] == "rate":
            CTX["kind"] = "rate"
            return net_skills.rate("курс " + rest), True
        if L["kind"] in ("wiki", "web"):  # "а сколько ему лет": search with the previous subject
            CTX["kind"] = "web"
            text = net_skills.web_answer(f"{rest} ({L['q']})", llm_voice())
            if text:
                return text, True
    return None


def _finish(text, result):
    if CTX.get("meta"):  # repeat / send / copy act ON the last answer: keep it as the dialog context
        LAST["t"] = time.time()
    else:
        LAST.update(q=CTX.get("continued") or text, a=result or "", kind=CTX.get("kind", ""), file=CTX.get("file"),
                    query=CTX.get("query", ""), t=time.time())
    if (after_reply is None or CTX.get("end")) and not _speak_pending.is_set():
        return

    def run():  # open the follow-up window only after the voice has finished (no self-hearing)
        talking = speaking()
        if talking and on_speaking:
            on_speaking(True)  # the mic listens for "стоп" while the voice talks
        try:
            time.sleep(0.25)
            for _ in range(240):
                if not speaking():
                    break
                time.sleep(0.25)
        finally:
            if talking and on_speaking:
                on_speaking(False)
        if after_reply is None or CTX.get("end"):
            return
        try:
            after_reply()
        except Exception as e:
            log(f"follow-up failed: {e!r}")
    threading.Thread(target=run, daemon=True).start()


def command_route(p, text):
    """Everything that needs no model, in order: the user's commands, plugins, built-ins, plugin fallbacks."""
    hit = usercmd.match(p)
    if hit:
        return usercmd.run(sys.modules[__name__], *hit)
    # "найди файл клоу амбилайт": a file name may contain any plugin's word (that one turned the LEDs on)
    if re.search(r"\bфайл", p) and re.match(r"(?:а\s+)?(?:найди|найти|поищи|где|покажи)", p):
        return show_file(p)
    xb = xbox_route(p)  # ahead of plugins: ClawTweaks' "режим Xbox" toggle needs its helper running
    if xb:
        return xb
    return plugins.route("before", p, text) or fast_route(p) or plugins.route("after", p, text)


def handle(text, dry=False):
    p = text.lower().strip(" .!?,")
    t0 = time.time()
    CTX.clear()
    EARLY.clear()
    if not dry:
        try:
            typed = typing_route(text, p)  # before the cut-off guard: a bare "отправь" means Enter here
        except Exception as e:
            typed = (f"Не получилось: {e}", True)
        if typed:
            report(typed[0], vocal=typed[1])
            CTX["meta"] = True  # typing does not replace the last spoken answer
            _finish(text, typed[0])
            return
    said = SPOKEN.get("text", "")  # self-echo: its own (maybe cut) voice from the speakers, not a command
    norm = lambda t: re.sub(r"gemma|(?:дж|г)[еиэ]м+а", "гемма", t)  # Whisper hears "Gemma" as Гимма/Джемма
    if not dry and said and time.time() - SPOKEN.get("t", 0) < 30 and len(p) >= 8 and difflib.SequenceMatcher(
            None, norm(p), norm(said)[:len(p) + 4]).ratio() > 0.7:
        log(f"ignored self-echo: {text!r}")
        return
    if not dry and len(p.split()) == 1 and re.search(r"ключ|откр|закр|запуст|отправ|покаж|найди", p):
        report("Не поняла, что именно", vocal=True)  # a cut-off command ("выключаем."): Gemma would only guess
        _finish(text, None)
        return
    try:
        if not dry:
            fast = dialog_route(p) or chat_route(text, p) or command_route(p, text) or continue_last(p)
            if fast:
                report(fast[0], vocal=fast[1])
                log(f"done (fast) in {time.time() - t0:.1f}s")
                _finish(text, fast[0])
                return
        if not gemma_on():  # no model: no guessing
            if provider() == "npu" and voice_settings.gemma_dir():  # unloaded: offer to load ("да" loads it)
                PENDING["load"] = time.time() + 20
                report("Без Gemma это не умею. Загрузить её?", vocal=True)
            else:
                report("Это я умею только с языковой моделью. Её можно подключить в настройках, вкладка «Модель»",
                       vocal=True)
            _finish(text, None)
            return
        plan = ask_gemma(text)
        tool, args = plan.get("tool", "answer"), plan.get("args") or {}
        say = russian(str(plan.get("say") or ""))
        if dry:
            print(f"{text!r} -> {tool} {args} say={say!r}")
            return plan
        result, vocal = run_tool(tool, args, text)
        if result is None:  # answer: Gemma must not pretend an action happened
            if not say or re.match(r"(?i)(?:я\s+)?(?:не\s+знаю|не\s+понимаю|не\s+поняла)\W*$", say):
                # the tool prompt gave up. A real question goes to the conversation model; a fragment gets
                # an honest ask-back (on fragments the small model only produces noise like "2540.")
                if len(p.split()) >= 3:
                    say, vocal = chat_reply(text)  # its first sentence is already being said (EARLY)
                else:
                    L = last()
                    say = (f"Не поняла «{text.strip(' .')}». Если это к команде «{L['q']}», скажи её целиком"
                           if L and time.time() - L["t"] < CONTINUE_SEC else
                           f"Не поняла «{text.strip(' .')}». Скажи иначе или «давай поговорим»")
                    vocal = True
            elif CLAIMS.search(say) and re.search(r"\b(включи|выключи|открой|закрой|сделай|поставь)", p):
                say = "Это я пока не умею"
            result = say
        report(result, vocal=vocal)
        _finish(text, result)
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:  # the cloud model's free quota
            report("Лимит запросов к модели исчерпан, попробуй через минуту", ok=False, vocal=True)
        elif isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
            report("Модель не приняла ключ API, проверь его в настройках", ok=False, vocal=True)
        else:
            report(f"Не получилось: {e}", ok=False)
        _finish(text, None)
    log(f"done in {time.time() - t0:.1f}s")


plugins.load(sys.modules[__name__])
usercmd.ensure_example()
try:
    with open(HELP_FILE, "w", encoding="utf-8") as _f:
        _f.write(help_full())
except OSError:
    pass


def watched_files():
    """dictate.py reloads this module when any of these change (code of the agent and of the plugins)."""
    return [__file__, net_skills.__file__, plugins.__file__, usercmd.__file__] + list(plugins.files().values())


def handle_async(text):
    threading.Thread(target=handle, args=(text,), daemon=True).start()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry" in sys.argv
    phrase = " ".join(a for a in sys.argv[1:] if a != "--dry").strip()
    if not phrase:
        print('Usage: python gemma_agent.py [--dry] "<phrase>"')
    else:
        handle(phrase, dry=dry)
