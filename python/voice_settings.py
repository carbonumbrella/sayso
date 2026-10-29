# SaySo settings, shared by dictate.py, gemma_agent.py, hotkey_helper.py, plugins and the tray app (its
# Settings window writes settings.json). User data lives in %APPDATA%\SaySo (or SAYSO_HOME): settings.json,
# commands\<lang>.json, plugins\*.py, secrets.bin. The app folder holds only code and models.
# Re-read only when the file changes, so callers can ask on every use.
import json, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.environ.get("SAYSO_HOME") or os.path.join(os.environ.get("APPDATA", HERE), "SaySo")
FILE = os.path.join(USER_DIR, "settings.json")
LEGACY = os.path.join(HERE, "voice_settings.json")  # before 2026-10-29 the settings sat next to the code

# Gemma 3 4B for the NPU server, next to the code (any install folder). Gated on Hugging Face: downloaded only
# with the user's own token after they accept Google's license ("sayso_cli.py gemma-download").
GEMMA_REPO = "OpenVINO/gemma-3-4b-it-int4-cw-ov"
GEMMA_FILES = [f"openvino_{p}.{e}" for p in ("language_model", "text_embeddings_model", "tokenizer", "detokenizer")
               for e in ("xml", "bin")]


def gemma_dir():
    """The folder with a complete Gemma model, or None. "-intel" is the name of the first manual download."""
    for name in ("gemma-3-4b-it-int4-cw-ov", "gemma-3-4b-it-int4-cw-ov-intel"):
        d = os.path.join(HERE, name)
        if all(os.path.isfile(os.path.join(d, f)) for f in GEMMA_FILES):
            return d
    return None
DEFAULTS = {
    "follow_sec": 6, "warm_mic": True, "tts_rate": 0, "tts_volume": 100, "tts_voice": "",
    "hotkey_dictation": "Ctrl+Alt+Win+W",   # rear M1 is mapped to it in ClawTweaks on the Claw
    "hotkey_gemma": "Ctrl+Win+Shift+G",
    "language": "ru",                        # commands, replies, recognition ("auto" = Whisper decides)
    "assistant_name": "Мурмур",              # the wake word and how the assistant calls itself
    "wake_names": ["мурмур", "мур-мур", "мур мур", "мурмура", "murmur", "мормор"],  # Whisper spellings
    "llm": {"provider": "none", "url": "http://127.0.0.1:8081", "model": ""},  # none | npu | server | cloud
    "offline": False,                        # no web search, weather, Wikipedia, cloud model
    "mic_device": "",                        # input device name ("" = system default)
    "stt_device": "auto",                    # auto | NPU | GPU | CPU
    "plugins_disabled": [],
    "screenshot_clipboard": True,            # every screenshot is also copied to the clipboard
}
_cache, _mtime = dict(DEFAULTS), None


def _migrate():
    if not os.path.exists(FILE) and os.path.exists(LEGACY):
        os.makedirs(USER_DIR, exist_ok=True)
        shutil.copy2(LEGACY, FILE)


def get(key):
    global _cache, _mtime
    try:
        _migrate()
        mt = os.path.getmtime(FILE)
        if mt != _mtime:
            with open(FILE, encoding="utf-8-sig") as f:  # a BOM (PowerShell, Notepad) is fine too
                _cache = {**DEFAULTS, **json.load(f)}
            _mtime = mt
    except (OSError, ValueError):
        pass
    return _cache.get(key, DEFAULTS.get(key))


def put(key, value):
    """Write one setting (used by voice commands like "зови меня ..."); the tray app writes the rest."""
    _migrate()
    os.makedirs(USER_DIR, exist_ok=True)
    try:
        with open(FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[key] = value
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)


MODS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
NAMED = {"pause": 0x13, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
         "insert": 0x2D, "delete": 0x2E, "printscreen": 0x2C, "scrolllock": 0x91, "numlock": 0x90,
         "capslock": 0x14, "tab": 0x09, "backspace": 0x08, "enter": 0x0D, "esc": 0x1B,
         "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


def parse_hotkey(text):
    """'Ctrl+Win+Shift+G' -> (RegisterHotKey modifiers, virtual key); None if it cannot be parsed.
    Keys: A-Z, 0-9, F1-F24, the NAMED ones, or a raw virtual-key code '0x7C'."""
    mods, vk = 0, None
    for part in str(text).replace(" ", "").split("+"):
        p = part.lower()
        if p in MODS:
            mods |= MODS[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        elif p[:1] == "f" and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            vk = 0x6F + int(p[1:])
        elif p in NAMED:
            vk = NAMED[p]
        elif p.startswith("0x"):
            try:
                vk = int(p, 16)
            except ValueError:
                return None
        else:
            return None
    return (mods, vk) if vk else None


def hotkeys():
    """{id: (mods, vk, mode, label)} for RegisterHotKey; a broken value falls back to the default."""
    out = {}
    for hid, key, mode in ((1, "hotkey_dictation", "paste"), (2, "hotkey_gemma", "agent")):
        text = get(key)
        parsed = parse_hotkey(text)
        if not parsed:
            text, parsed = DEFAULTS[key], parse_hotkey(DEFAULTS[key])
        out[hid] = (parsed[0], parsed[1], mode, text)
    return out


# ---------------------------------------------------------------- secrets (Telegram token, cloud API key)
# DPAPI (CryptProtectData, current user): readable only by this Windows user on this PC. The tray app
# writes the same file with System.Security.Cryptography.ProtectedData (CurrentUser scope).
SECRETS = os.path.join(USER_DIR, "secrets.bin")


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def secret(name):
    try:
        with open(SECRETS, "rb") as f:
            return json.loads(_dpapi(f.read(), False).decode("utf-8")).get(name, "")
    except (OSError, ValueError):
        return ""


def set_secret(name, value):
    try:
        with open(SECRETS, "rb") as f:
            data = json.loads(_dpapi(f.read(), False).decode("utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = value
    os.makedirs(USER_DIR, exist_ok=True)
    with open(SECRETS, "wb") as f:
        f.write(_dpapi(json.dumps(data).encode("utf-8"), True))
