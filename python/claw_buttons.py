"""MSI Claw back buttons in ClawTweaks for SaySo: M1 = the dictation hotkey (held), M2 = the command hotkey.
Used by sayso_cli (wizard / Settings: state, write) and dictate.py (fill_new_game_profiles, the "new game profiles"
switch). ClawTweaks gives every new game profile its own gamepad profile with M1/M2 blank (hard-coded in its helper:
"queued a blank per-game controller profile"), so without this the buttons are dead in that game."""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "plugins"))
import voice_settings

# ClawTweaks back buttons: the quick-settings pipe takes M1/M2 as a mapping JSON (Function 133/134), keyboard keys as
# HID usages; 367 = firmware keyboard mode, needed for a key that stays held while the button is. Taken from the
# helper's own code (ClawTweaks 1.0.2.17), not guessed.
CLAW_M1, CLAW_M2, CLAW_FW_KEYBOARD = 133, 134, 367
HID_MODS = {"ctrl": 224, "shift": 225, "alt": 226, "win": 227}
HID_NAMED = {"enter": 40, "esc": 41, "backspace": 42, "tab": 43, "space": 44, "capslock": 57, "printscreen": 70,
             "scrolllock": 71, "pause": 72, "insert": 73, "home": 74, "pageup": 75, "delete": 76, "end": 77,
             "pagedown": 78, "right": 79, "left": 80, "down": 81, "up": 82, "numlock": 83}


def hid_keys(text):
    """'Ctrl+Alt+Win+W' -> [224, 226, 227, 26] in the written order; None if a key has no HID usage here."""
    keys = []
    for part in str(text).replace(" ", "").split("+"):
        p = part.lower()
        if p in HID_MODS:
            keys.append(HID_MODS[p])
        elif len(p) == 1 and "a" <= p <= "z":
            keys.append(4 + ord(p) - ord("a"))
        elif len(p) == 1 and p.isdigit():
            keys.append(39 if p == "0" else 29 + int(p))
        elif p[:1] == "f" and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            n = int(p[1:])
            keys.append(57 + n if n <= 12 else 91 + n)
        elif p in HID_NAMED:
            keys.append(HID_NAMED[p])
        else:
            return None
    return keys


def hid_label(keys):
    names = {v: k.capitalize() for k, v in {**HID_MODS, **HID_NAMED}.items()}
    out = []
    for k in keys:
        if k in names:
            out.append(names[k])
        elif 4 <= k <= 29:
            out.append(chr(ord("A") + k - 4))
        elif 30 <= k <= 39:
            out.append("0" if k == 39 else str(k - 29))
        elif 58 <= k <= 69 or 104 <= k <= 115:
            out.append(f"F{k - 57 if k <= 69 else k - 91}")
        else:
            out.append(f"0x{k:02X}")
    return "+".join(out)


def claw_mapping_label(raw):
    """What a back button does now, in words: 'Ctrl+Alt+Win+W', 'не назначена' or 'другое действие'."""
    try:
        m = json.loads(raw or "{}")
    except ValueError:
        return "другое действие"
    if m.get("Type") == 1 and m.get("KeyboardKeys"):
        return hid_label(m["KeyboardKeys"])
    return "не назначена" if not m.get("Type") else "другое действие"


FN = {"M1": CLAW_M1, "M2": CLAW_M2}


def want():
    """The buttons SaySo manages: M1 = dictation, M2 = command; in the one-button mode only M1 (tap = command), M2 is
    the user's again."""
    w = {"M1": voice_settings.get("hotkey_dictation") or voice_settings.DEFAULTS["hotkey_dictation"]}
    if not voice_settings.get("one_button"):
        w["M2"] = voice_settings.get("hotkey_gemma") or voice_settings.DEFAULTS["hotkey_gemma"]
    return w


def mapping(keys):
    """The keyboard mapping JSON ClawTweaks stores for a back button, keys held while the button is."""
    return json.dumps({"Type": 1, "GamepadAction": 0, "GamepadMode": 0, "GamepadActions": [], "Turbo": False,
                       "KeyboardKeys": keys, "MouseButton": 0, "ActionParam": "", "MacroKeys": [], "MacroButtons": [],
                       "MacroDelayMs": 80, "MacroPressMs": 40, "MacroMode": 0, "MacroOnceHold": True},
                      separators=(",", ":"))


def state():
    """State for the wizard / Settings: is ClawTweaks there, what M1/M2 do now, what SaySo wants on them."""
    import _claw_ipc as c
    want_ = want()
    if not c.installed():
        return {"installed": False, "want": want_}
    raw = {b: c.get(FN[b]) for b in want_}
    if any(v is None for v in raw.values()):
        return {"installed": True, "reachable": False, "want": want_}
    now = {b: claw_mapping_label(r) for b, r in raw.items()}

    def same(b):  # the same keys in any order ("Win+Ctrl+Alt+W" = "Ctrl+Alt+Win+W")
        try:
            m = json.loads(raw[b] or "{}")
        except ValueError:
            return False
        return m.get("Type") == 1 and sorted(m.get("KeyboardKeys") or []) == sorted(hid_keys(want_[b]) or [None])

    return {"installed": True, "reachable": True, "want": want_, "now": now,
            "same": all(same(b) for b in raw), "one_button": "M2" not in want_,
            # "free": nothing else is on them, writing loses nothing; otherwise the UI asks first
            "free": all(now[b] == "не назначена" or same(b) for b in raw),
            "fw_keyboard": (c.get(CLAW_FW_KEYBOARD) or "").strip().lower() == "true",
            "game": c.running_game().get("name", "")}


def write():
    """M1 = dictation hotkey, M2 = command hotkey, held while pressed, into ClawTweaks' global profile. Refused while
    a game runs: ClawTweaks saves into the current profile, and during a game that is the game's own one."""
    st = state()
    if not st.get("reachable"):
        return {"error": "ClawTweaks не отвечает: откройте его один раз и повторите"}
    if st["game"]:
        return {"error": f"Сейчас запущена игра ({st['game']}): закройте её, иначе кнопки попадут в её профиль"}
    import _claw_ipc as c
    for button in st["want"]:
        fn = FN[button]
        keys = hid_keys(st["want"][button])
        if not keys:
            return {"error": f"{st['want'][button]}: эту комбинацию нельзя назначить на кнопку"}
        c.set_(fn, mapping(keys))
        time.sleep(0.3)
    if not st["fw_keyboard"]:
        c.set_(CLAW_FW_KEYBOARD, True)
    time.sleep(0.5)
    after = state()
    return {"ok": bool(after.get("same")), **after}


# ---- the "new game profiles" switch (setting claw_buttons_auto, on after the wizard set the buttons)
SEEN = os.path.join(voice_settings.USER_DIR, "claw_buttons_seen.json")  # games whose gamepad profile was handled


def _profile_games():
    """Game exes that have their own gamepad profile in ClawTweaks (its controller-profiles.tsv: exe TAB name)."""
    import _claw_ipc as c
    try:
        with open(os.path.join(c.PACKAGE_STATE, "controller-profiles.tsv"), encoding="utf-8-sig") as f:
            return {line.split("\t", 1)[0].strip().lower() for line in f if "\t" in line}
    except OSError:
        return set()


def _exe_of(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return buf.value.lower() if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        k32.CloseHandle(h)


def _blank(raw):
    try:
        return not json.loads(raw or "{}").get("Type")
    except ValueError:
        return False


REAPPLY_SEC = 90  # a fresh game profile: ClawTweaks pushes its blank M1/M2 once more after creating it
_recent = {}  # exe -> time SaySo filled it


def _write_ours(c):
    w = want()
    keys = {b: hid_keys(v) for b, v in w.items()}
    if not all(keys.values()):
        return False
    for b, k in keys.items():
        c.set_(FN[b], mapping(k))
        time.sleep(0.3)
    return True


def fill_new_game_profiles(log=print):
    """One pass (dictate.py calls it every 10 s): a game with its own gamepad profile that SaySo has not handled yet,
    running now, gets M1/M2 there if both are blank. A profile that already has something on them is left alone.
    Right after a profile is created ClawTweaks applies its blank buttons once more and wipes ours (they worked only
    from the second start): for REAPPLY_SEC after filling, blank buttons in that game are filled again.
    Returns the game name when it filled one."""
    if not voice_settings.get("claw_buttons_auto"):
        return ""
    import _claw_ipc as c
    if not c.installed():
        return ""
    for e, t in list(_recent.items()):
        if time.time() - t > REAPPLY_SEC:
            del _recent[e]
    games = _profile_games()
    try:
        with open(SEEN, encoding="utf-8") as f:
            seen = set(json.load(f))
    except (OSError, ValueError):
        seen = set()
    if not games - seen and not _recent:
        return ""
    game = c.running_game()
    exe = _exe_of(game["pid"]) if game else ""
    if not exe or exe not in games or (exe in seen and exe not in _recent):
        return ""
    name = game.get("name") or os.path.basename(exe)
    now = {b: c.get(FN[b]) for b in want()}  # while the game runs: the values of its own gamepad profile
    if any(v is None for v in now.values()):
        return ""
    blank = all(_blank(v) for v in now.values())
    if exe in _recent:  # the second, blank push of a fresh profile: ours again, quietly
        if blank and _write_ours(c):
            log(f"claw buttons: M1/M2 wiped by ClawTweaks in {name}, put back")
        return ""
    filled = ""
    if blank:
        if _write_ours(c):
            filled = name
            _recent[exe] = time.time()
            log(f"claw buttons: M1/M2 put into the gamepad profile of {name}")
    else:
        log(f"claw buttons: {name} has its own M1/M2, left alone")
    seen.add(exe)
    os.makedirs(voice_settings.USER_DIR, exist_ok=True)
    with open(SEEN, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)
    return filled
