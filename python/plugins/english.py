# English commands for SaySo (language "en", or "auto" with an English phrase). The Russian built-ins stay
# as they are; this plugin answers English phrases first and replies in English. It reuses the agent's
# actions (volume, apps, timers, ...) and only the wording is its own.
import json, os, re, time, urllib.parse, urllib.request

ABOUT = "English commands: volume, brightness, apps, time, timers, weather, what is / who is, screenshot"
A = None

NUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
       "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
       "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
       "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "a": 1, "an": 1}


def number(p):
    """'40', 'forty five', 'a hundred' -> int, or None."""
    m = re.search(r"\d+", p)
    if m:
        return int(m.group())
    total, found = 0, False
    for w in re.findall(r"[a-z]+", p):
        if w in NUM and w not in ("a", "an"):  # "set a timer for five minutes": the article is not a number
            v = NUM[w]
            total = total * 100 if v == 100 and total else total + v
            found = True
    if found:
        return total
    m = re.search(r"\ban? (hundred|minute|hour|second)", p)  # "a minute", "a hundred"
    return (100 if m.group(1) == "hundred" else 1) if m else None


def active(p):
    lang = A.voice_settings.get("language")
    return lang == "en" or (lang == "auto" and re.search(r"[a-z]", p) and not re.search(r"[а-яё]", p))


def weather():
    try:
        with open(os.path.join(os.path.dirname(A.__file__), "location.json"), encoding="utf-8") as f:
            loc = json.load(f)
    except OSError:
        return "Tell me your city in the settings first"
    q = urllib.parse.urlencode({"latitude": loc["latitude"], "longitude": loc["longitude"],
                                "current": "temperature_2m,wind_speed_10m", "daily": "temperature_2m_max",
                                "timezone": "auto", "forecast_days": 1, "wind_speed_unit": "ms"})
    with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=10) as r:
        d = json.load(r)
    t, mx = round(d["current"]["temperature_2m"]), round(d["daily"]["temperature_2m_max"][0])
    return f"{loc['name']}: now {t} degrees, up to {mx} today, wind {round(d['current']['wind_speed_10m'])} meters per second"


def route(p, text):
    if not active(p):
        return None
    n = number(p)
    if re.search(r"\b(volume|sound)\b", p) and n is not None:
        A.volume(level=max(0, min(100, n)))
        return f"Volume {n} percent", False
    if re.search(r"\b(louder|volume up|turn it up)\b", p):
        A.volume(step=15)
        return "Louder", False
    if re.search(r"\b(quieter|volume down|turn it down)\b", p):
        A.volume(step=-15)
        return "Quieter", False
    if re.search(r"\b(mute|silence)\b", p) and "unmute" not in p:
        A.volume(mute=True)
        return "Muted", False
    if re.search(r"\bunmute\b", p):
        A.volume(mute=False)
        return "Sound on", False
    if re.search(r"\bbrightness\b", p) and n is not None:
        A.brightness(level=max(0, min(100, n)))
        return f"Brightness {n} percent", False
    if re.search(r"\b(what time|the time)\b", p):
        return time.strftime("It's %H:%M"), True
    if re.search(r"\b(what day|the date|what's the date|today's date)\b", p):
        return time.strftime("Today is %A, %B %d"), True
    m = re.search(r"\btimer (?:for )?(.+)$", p)
    if m and n is not None:
        sec = n * (3600 if "hour" in p else 1 if "second" in p else 60)
        A.timer("set", sec)
        return f"Timer set for {m.group(1)}", True
    if re.search(r"\bweather\b|\bhow (hot|cold|warm)\b", p):
        return weather(), True
    if re.search(r"\b(screenshot|screen shot)\b", p):
        A.screenshot()
        return "Screenshot saved", False
    if re.search(r"\block (the )?(computer|screen|pc)\b", p):
        A.user32.LockWorkStation()
        return "Locked", False
    if re.search(r"\b(minimi[sz]e (all|everything)|show (the )?desktop)\b", p):
        A.window("minimize_all")
        return "Desktop", False
    if re.search(r"\b(pause|play|resume)\b", p) and re.search(r"\b(music|song|video|track)?\b", p):
        A.media("play_pause")
        return "Okay", False
    if re.search(r"\bnext (song|track)\b", p):
        A.media("next")
        return "Next track", False
    m = re.match(r"^(?:open|launch|start|run)\s+(.+)$", p)
    if m:
        A.open_app(m.group(1), m.group(1))
        return f"Opening {m.group(1)}", False
    m = re.match(r"^(?:close|quit|exit)\s+(.+)$", p)
    if m:
        A.close_app(m.group(1))
        return f"Closing {m.group(1)}", False
    m = re.match(r"^(?:what is|what's|who is|who's|tell me about)\s+(?:a |an |the )?(.+)$", p)
    if m:
        text = A.net_skills.wikipedia(m.group(1))
        return (text or "I could not find that"), True
    if re.fullmatch(r"(stop|enough|be quiet|shut up)", p):
        A.stop_speaking()
        A.CTX["end"] = True
        return "Okay", False
    if re.search(r"\bwhat can you do\b|\bhelp\b", p):
        return ("I control volume and brightness, open and close apps, tell the time and the weather, set timers, "
                "answer what is and who is questions, and run your own commands from the settings"), True
    return None


def register(api):
    global A
    A = api.agent
    api.route(route, "before")
    api.help("🇬🇧 English: volume N · louder · mute · brightness N · open / close … · what time · timer 5 minutes · "
             "weather · what is … · screenshot · lock the computer")
