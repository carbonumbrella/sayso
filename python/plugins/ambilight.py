# ClawAmbilight plugin: the MSI Claw stick LEDs (ClawAmbilight app) by voice. Idle when the app is not installed.
import json, os, re, subprocess

ABOUT = "Подсветка стиков MSI Claw голосом (нужна программа ClawAmbilight)"

A = None  # the agent module (set in register)

AMBI_EXE = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "ClawAmbilight", "ClawAmbilight.exe")  # installer path
AMBI_CFG = os.path.join(os.environ["APPDATA"], "ClawAmbilight", "settings.json")
AMBI_COLORS = [(r"красн", "#FF3B30"), (r"оранж", "#FF9500"), (r"жёлт|желт", "#FFCC00"), (r"зел[её]н", "#34C759"),
               (r"бирюз|голуб", "#00C7BE"), (r"син", "#3E7BFA"), (r"фиолет|сиренев", "#AF52DE"),
               (r"розов|малин", "#FF2D55"), (r"бел", "#FFFFFF")]


def ambilight(p):
    """Controller backlight (ClawAmbilight.exe) by voice: the app watches its settings.json."""
    try:
        with open(AMBI_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    say = None
    if re.search(r"\b(вы|от)ключ|погаси|убери", p):
        cfg["enabled"], say = False, "Подсветка выключена"
    else:
        cfg["enabled"] = True
        color = next((hexc for pat, hexc in AMBI_COLORS if re.search(pat, p)), None)
        # "левый (стик) синий, правый красный", "левый-зелёный", "правый и синий": color within the next words
        left = re.search(r"лев\w*[\s,-]+(?:стик\w*[\s,-]+)?(?:и[\s,-]+)?(\w+)", p)
        right = re.search(r"прав\w*[\s,-]+(?:стик\w*[\s,-]+)?(?:и[\s,-]+)?(\w+)", p)
        side = lambda m: m and next((h for pat, h in AMBI_COLORS if re.search(pat, m.group(1))), None)
        if re.search(r"нинтендо|nintendo|джой ?кон|joy-?con|свитч|switch", p):
            cfg.update(mode="static", staticLeft="#0AB9E6", staticRight="#FF3C28")
            say = "Подсветка как у Joy-Con"
        elif side(left) or side(right):
            cfg["mode"] = "static"
            if side(left):
                cfg["staticLeft"] = side(left)
            if side(right):
                cfg["staticRight"] = side(right)
            say = "Цвета сторон подсветки изменены"
        elif color:
            cfg.update(mode="static", staticColor=color, staticLeft=color, staticRight=color)
            say = "Подсветка одним цветом"
        elif re.search(r"ярк\w* режим|режим\w* ярк|насыщ|вибрант|\bярко\b", p):
            cfg["mode"], say = "vibrant", "Подсветка в ярком режиме"
        elif re.search(r"мягк|спокойн|эмбиент|амбиент|ambient|по экрану|от экрана|за экраном", p):
            cfg["mode"], say = "ambient", "Подсветка в мягком режиме"
        if re.search(r"как (у )?экран|синхрон|под экран", p):
            cfg["brightnessSource"], say = "display", "Яркость подсветки как у экрана"
        n = A.parse_level(p).get("level")  # "50", "пятьдесят", "на максимум"
        if n is not None:
            cfg["brightnessSource"], cfg["brightness"] = "manual", int(n)
            say = f"Яркость подсветки {cfg['brightness']} процентов"
        say = say or "Подсветка включена"
    os.makedirs(os.path.dirname(AMBI_CFG), exist_ok=True)
    tmp = AMBI_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AMBI_CFG)
    rc, out = A._ps_out("@(Get-Process ClawAmbilight -ErrorAction SilentlyContinue).Count")
    if out.strip() in ("", "0") and os.path.exists(AMBI_EXE):
        subprocess.Popen([AMBI_EXE, "--tray"])
    return say, False


SIDE_COLOR = (r"(?:лев|прав)\w*[\s,-]+(?:стик\w*[\s,-]+)?(?:и[\s,-]+)?"
              r"(?:красн|оранж|ж[её]лт|зел|бирюз|голуб|син|фиолет|сиренев|розов|малин|бел)")
# Whisper hears "подсветку" as "по цветку" and "эмбиент лайт" as "бинт-лайт"
BACKLIGHT = (r"подсвет|посветк|пациентк|по\s?цвет|по\s?свет\w*\s|\brgb\b|светодиод|эмбиент|амбиент|амбилайт|"
             r"ambilight|ambient|бинт\W?лайт")


def route(p, text):
    if re.search(BACKLIGHT + "|" + SIDE_COLOR, p) and not re.search(r"ночн\w* (свет|режим)", p):
        return ambilight(p)
    return None


def register(api):
    global A
    A = api.agent
    if not (os.path.exists(AMBI_EXE) or os.path.exists(AMBI_CFG)):
        api.log("ClawAmbilight is not installed, plugin idle")
        return
    api.route(route, "before")
    api.tool("ambilight", '{"what": str}', "подсветка стиков Claw: вкл/выкл, цвет, режим, яркость",
             lambda args, phrase: ambilight(phrase.lower()))
    api.help("🌈 Подсветка: вкл/выкл · мягкая / яркая · красным · как у нинтендо · левый … правый … · яркость N")
