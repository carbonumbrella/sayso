# SaySo user commands: %APPDATA%\SaySo\commands\<lang>.json, a list of
#   {"say": ["открой почту", "проверь почту"], "do": "open_url", "url": "https://mail.google.com",
#    "reply": "Открываю почту"}
# A phrase matches when it equals one of "say" (case and punctuation ignored); a "*" in a phrase captures
# the rest: {"say": ["найди на ютубе *"], "do": "open_url", "url": "https://www.youtube.com/results?search_query={*}"}.
# Actions:
#   open_app   "app": name or path         open_url   "url"          open_folder  "path"
#   hotkey     "keys": "Ctrl+Shift+M"      type_text  "text"         webhook      "url", optional "json"
#   script     "command" (PowerShell, always asks for confirmation)  say  "reply" only
# JSON instead of YAML: the tray app's command editor reads and writes it without extra libraries.
import json, os, re, time, urllib.parse, urllib.request
import voice_settings

DIR = os.path.join(voice_settings.USER_DIR, "commands")
ACTIONS = ("open_app", "open_url", "open_folder", "hotkey", "type_text", "webhook", "script", "say")
_cache = {"path": None, "mtime": None, "items": []}


def path(lang=None):
    lang = lang or voice_settings.get("language") or "ru"
    return os.path.join(DIR, f"{'ru' if lang == 'auto' else lang}.json")


def items():
    p = path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return []
    if _cache["path"] != p or _cache["mtime"] != mt:
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
            _cache.update(path=p, mtime=mt, items=[c for c in data if isinstance(c, dict) and c.get("do") in ACTIONS])
        except (OSError, ValueError):
            _cache.update(path=p, mtime=mt, items=[])
    return _cache["items"]


def _norm(s):
    return re.sub(r"[^\w*\s-]", " ", s.lower().replace("ё", "е")).split()


def match(p):
    """-> (command, captured tail) or None."""
    words = _norm(p)
    for c in items():
        for phrase in c.get("say") or []:
            pw = _norm(phrase)
            if "*" in pw:
                i = pw.index("*")
                if words[:i] == pw[:i] and len(words) > i:
                    return c, " ".join(words[i:])
            elif words == pw:
                return c, ""
    return None


def _fill(s, tail):
    return str(s).replace("{*}", urllib.parse.quote(tail) if s.startswith("http") else tail)


def run(agent, c, tail):
    """Executes a matched command with the agent's helpers. -> (reply, speak_it)."""
    do, reply = c["do"], c.get("reply")
    if do == "open_app":
        target = _fill(c.get("app", ""), tail)
        if os.path.exists(target):
            os.startfile(target)
            return reply or f"Открываю {os.path.basename(target)}", False
        result = agent.open_app(target)
        return (reply or result), False
    if do in ("open_url", "open_folder"):
        os.startfile(_fill(c.get("url") or c.get("path", ""), tail))
        return reply or "Открываю", False
    if do == "hotkey":
        parsed = voice_settings.parse_hotkey(c.get("keys", ""))
        if not parsed:
            return f"Не понимаю сочетание {c.get('keys')}", True
        mods, vk = parsed
        keys = [vk2 for bit, vk2 in ((0x2, 0x11), (0x1, 0x12), (0x4, 0x10), (0x8, 0x5B)) if mods & bit] + [vk]
        agent._key(*keys)
        return reply or "Готово", False
    if do == "type_text":
        agent._paste_text(_fill(c.get("text", ""), tail))
        return reply or "Написала", False
    if do == "webhook":
        body = json.dumps(c.get("json") or {"text": tail}).encode("utf-8")
        req = urllib.request.Request(_fill(c["url"], tail), body, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return reply or "Отправила", False
    if do == "script":
        return agent.run_command(_fill(c.get("command", ""), tail)), True
    return reply or "Готово", True


def ensure_example():
    """First start: an example file so the user sees the format (all entries harmless)."""
    p = path()
    if os.path.exists(p):
        return
    os.makedirs(DIR, exist_ok=True)
    example = [
        {"say": ["открой почту", "проверь почту"], "do": "open_url", "url": "https://mail.google.com",
         "reply": "Открываю почту"},
        {"say": ["найди на ютубе *"], "do": "open_url", "url": "https://www.youtube.com/results?search_query={*}",
         "reply": "Ищу на ютубе"},
        {"say": ["выключи микрофон в созвоне"], "do": "hotkey", "keys": "Ctrl+Shift+M", "reply": "Готово"},
    ]
    if voice_settings.get("language") == "en":
        example = [{"say": ["open mail", "check mail"], "do": "open_url", "url": "https://mail.google.com",
                    "reply": "Opening mail"},
                   {"say": ["search youtube for *"], "do": "open_url",
                    "url": "https://www.youtube.com/results?search_query={*}", "reply": "Searching YouTube"}]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(example, f, ensure_ascii=False, indent=2)
