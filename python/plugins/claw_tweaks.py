# ClawTweaks plugin: the MSI Claw quick settings by voice (fans, TDP and power presets, CPU boost, charge limit,
# controller mode, in-game overlay, FPS limit, refresh rate, game pause/end, full screen experience, AI noise
# cancellation, Lossless Scaling, launchers, sensors). Talks to the ClawTweaks helper through _claw_ipc.
# A phrase is read as action + object + value, so the user does not have to remember exact wording:
# "убери фпс", "спрячь оверлей" and "выключи мониторинг" are the same command. Registers nothing on a PC
# without ClawTweaks.
import importlib, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import _claw_ipc as ct
importlib.reload(ct)  # dictate.py reloads plugins on file change, the helper module must follow

ABOUT = "MSI Claw через ClawTweaks: вентиляторы, мощность (TDP), буст, лимит заряда, мышь или геймпад, оверлей, лимит FPS, пауза игры, датчики"

A = None  # the agent module

# PL1 only (PL2 = PL1 + 1), or (PL1, PL2) for the separate PL2 mode
PRESETS = {"eco": (8, "эко"), "medium": (15, "средний"), "high": (30, "высокая производительность"),
           "turbo": ((30, 37), "турбо")}
OSD_LEVELS = {8: "базовый", 1: "минимальный", 2: "компактный", 3: "расширенный", 4: "полный"}
POS_NAMES = {1: "слева вверху", 2: "вверху по центру", 3: "справа вверху", 4: "слева внизу", 5: "внизу по центру",
             6: "справа внизу"}

# --- words ---------------------------------------------------------------------------------------------
# imperatives only: "включен ли буст" is a question, not "включи"
OFF = r"\b(?:выключи|выключай|отключи|отключай|выруби|убери|убрать|скрой|спрячь|деактивир|сними|сбрось)\w*|\bбез\b"
ON = r"\b(?:включи|включай|вруби|запусти|активируй|покажи|показывай|верни|открой|дай|поставь|сделай|установи|выстави)\w*"
ASK = r"\b(?:сколько|какой|какая|какое|какие|каков\w*|скажи|узнай|статус|насколько|проверь|ли)\b|\bчто с\b"

FAN = r"вентилятор|кулер|обдув|охлажд|охлади|вертушк|продув|\bfan"
FAN_MAX = r"максим|на полную|на всю|полн\w* (?:мощ|скор|обор)|\bтурбо|\b100\b|\bсто\b|сильн|громч|продуй|продув|охлади"
FAN_AUTO = r"авто|обычн|стандарт|норм|как было|по умолчан|тише|тих|сброс|верни"
POWER = r"тдп|\btdp\b|ти ?ди ?пи|тэ ?дэ ?пэ|мощност|\bватт|\bвт\b|энергопотреблен|потреблен"
ECO = (r"\bэко\w*|\beco\b|энергосбереж|тих\w* режим|режим тиш|для батаре|батаре\w* дольше|дольше от батаре|"
       r"минимальн\w* (?:мощн|режим|производительн)")
MEDIUM = (r"средн\w* (?:режим|мощн|тдп|ти ?ди ?пи|производительн|уровен)|(?:режим|тдп|мощност\w*) средн|"
          r"баланс|сбалансир|обычн\w* режим|стандартн\w* режим")
HIGH = (r"(?:высок\w*|максимальн\w*|полн\w*|всю) (?:производительн|мощн)|производительн\w* режим|режим производительн|"
        r"игров\w* режим|на полную мощн|\bperformance|перформанс")
TURBO = r"турбо|\bturbo|выжми"
BOOST = r"буст|boost"
CHARGE = r"заряд|заряж|батаре|аккумулятор"
CHARGE_LIMIT = r"огранич|лимит|береги|сбереги|бережн|защит|не больше|до \d+|до (?:восьмидесят|девяност|семидесят|шестидесят|пятидесят)"
CHARGE_FULL = r"до конца|полностью|до (?:100|ста)\b|(?:сними|убери|отключи|выключи)\w* (?:лимит|огранич)|без (?:лимит|огранич)"
MOUSE = r"\bмыш|\bмышк|курсор|режим рабочего стола|десктоп"
INPUT = r"режим\w* (?:управлени|ввода|контроллер|геймпад)"
PAD = r"геймпад|гейм ?пад|геймпат|гимпат|джойстик|контроллер"
EXT_PAD = r"внешн\w* (?:геймпад|гейм ?пад|контроллер|джойстик)"
OSD = (r"оверле|[оаэеуі]верл|everly|overlay|\bосд\b|\bosd\b|счетчик\w* (?:фпс|кадр)|мониторинг|\bфпс\b|\bfps\b|"
       r"статистик\w* (?:фпс|игр|на экран)|показател\w* на экран")
FPS_ASK = r"(?:сколько|какой|какая|какие)\s+(?:сейчас\s+)?(?:фпс|fps|кадр)|фпс сейчас"
FPS_LIMIT = (r"(?:огранич\w*|лимит\w*|предел\w*|потолок|залоч\w*|зафиксир\w*)\s+(?:\w+\s+)?(?:фпс|fps|кадр)|"
             r"(?:фпс|fps|кадр\w*)\s+(?:до|не больше|на)\s+\S+")
GAME = r"\bигр[уаеыоой]*\b|\bигрушк"
FREEZE = r"замороз|заморож|пауз|приостанов|останови"
UNFREEZE = r"размороз|разморож|продолж|возобнов|сними\b.*пауз|сними\b.*стоп|с паузы"
KILL = r"\b(?:заверши|закрой|убей|выруби|прибей|выкинь)\w*\s+(?:\w+\s+)?игр|игра завис|зависш\w* игр|игра не отвеча"
FSE = (r"полноэкранн\w* (?:режим|интерфейс|xbox|иксбокс)|режим консоли|консольн\w* режим|(?:xbox|иксбокс)[ -]режим|"
       r"режим (?:xbox|иксбокс)|\bfse\b|full ?screen experience")
NOISE = r"шумодав|шумоподав|подавлени\w* шум|шумопод|\bnoise"
LOSSLESS = r"lossless|лос+лес|лослесс|лоселес|лосслес"
KEYBOARD = r"(?:экранн|сенсорн|виртуальн)\w* клавиатур|(?:покажи|вызови|открой|дай)\w* клавиатур"
TEMP = r"температур|градус|горяч|нагрел|нагре[вт]|перегре"
HW = r"процессор|консол|чип|ядр|приставк|железк|желез|\bcpu\b|видеокарт|\bгпу\b|\bgpu\b|клав\b|девайс|устройств"
STATUS = r"(?:состояни|статус|сводк|отчет)\w* (?:консоли|железа|системы|клав|приставки)|как (?:там )?консоль"
REFRESH = r"частот\w* (?:экрана|обновлени|кадров экрана)|герц|\bгц\b"
LAUNCH = [(r"big picture|биг ?пикчер|бигпикчер", ct.ACT_STEAM_BIG_PICTURE, "Steam Big Picture"),
          (r"playnite|плейнайт", ct.ACT_PLAYNITE, "Playnite"),
          (r"(?:приложени\w*|апп?) (?:xbox|иксбокс)|xbox app", ct.ACT_XBOX_APP, "приложение Xbox"),
          (r"(?:клав|клоу|claw|кло)\s*твикс|clawtweaks|клавтвикс|клотвикс", ct.ACT_CLAWTWEAKS, "ClawTweaks"),
          (r"библиотек\w* игр", ct.ACT_LIBRARY, "библиотеку игр")]

# --- numbers -------------------------------------------------------------------------------------------
_UNITS = {"ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "пяти": 5,
          "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9}
_TEENS = {"десять": 10, "десяти": 10, "одиннадцать": 11, "двенадцать": 12, "двенадцати": 12, "тринадцать": 13,
          "четырнадцать": 14, "пятнадцать": 15, "пятнадцати": 15, "шестнадцать": 16, "семнадцать": 17,
          "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "двадцати": 20, "тридцать": 30, "тридцати": 30, "сорок": 40, "сорока": 40,
         "пятьдесят": 50, "пятидесяти": 50, "шестьдесят": 60, "шестидесяти": 60, "семьдесят": 70,
         "семидесяти": 70, "восемьдесят": 80, "восьмидесяти": 80, "девяносто": 90, "девяноста": 90,
         "сто": 100, "ста": 100}


def numbers(p):
    """Numbers in a phrase, digits or words: 'двадцать пять' -> 25, 'до восьмидесяти' -> 80, '12' -> 12."""
    out, words, i = [], re.findall(r"\d+|[а-я]+", p), 0
    while i < len(words):
        w = words[i]
        if w.isdigit():
            out.append(int(w))
        elif w in _TENS:
            n = _TENS[w]
            if n < 100 and i + 1 < len(words) and words[i + 1] in _UNITS:
                n, i = n + _UNITS[words[i + 1]], i + 1
            out.append(n)
        elif w in _TEENS:
            out.append(_TEENS[w])
        elif w in _UNITS:
            out.append(_UNITS[w])
        i += 1
    return out


def norm(p):
    p = p.lower().replace("ё", "е")
    p = re.sub(r"(\d)\s*%", r"\1 процентов", p)
    return re.sub(r"\s+", " ", re.sub(r"[^\w%\s-]", " ", p)).strip()


def has(rx, p):
    return re.search(rx, p) is not None


NO_ANSWER = ("ClawTweaks не отвечает, проверь, что он запущен", False)


def n_(n, one, few, many):
    """'54 градуса', '9.2 ватта', '25 ватт': the number with the Russian plural."""
    if isinstance(n, float) and not n.is_integer():
        return f"{n} {few}"
    n = int(n or 0)
    k = abs(n) % 100
    form = many if 11 <= k <= 14 else one if k % 10 == 1 else few if 2 <= k % 10 <= 4 else many
    return f"{n} {form}"


DEG, PCT, WATT = ("градус", "градуса", "градусов"), ("процент", "процента", "процентов"), ("ватт", "ватта", "ватт")
RPM, FPS = ("оборот", "оборота", "оборотов"), ("кадр", "кадра", "кадров")


# --- handlers: each returns (reply, speak) or None -------------------------------------------------------
def fans(p):
    if not has(FAN, p):
        return None
    if has(r"оборот|скорост|как крут|громко ли|шумят", p) or (has(ASK, p) and not has(ON, p)):
        m = ct.metrics()
        if not m:
            return NO_ANSWER
        full = " (на максимуме)" if (m.get("fanRpm") or 0) >= 7000 else ""  # the duty field stays 0 on the Claw
        return (f"Вентиляторы: {n_(m.get('fanRpm', 0), *RPM)} в минуту{full}" if m.get('fanRpm') else "Вентиляторы сейчас стоят"), True
    if has(OFF, p) or has(FAN_AUTO, p):
        return ("Вентиляторы в автоматическом режиме", False) if ct.extra("MsiFanFullBlast", "off") else NO_ANSWER
    if has(FAN_MAX, p) or has(ON, p):
        return ("Вентиляторы на максимуме", False) if ct.extra("MsiFanFullBlast", "on") else NO_ANSWER
    return None


# ClawTweaks has two TDP modes: one slider (PL2 = PL1 + 1) and a separate PL2 ("TDP boost", up to 37 W).
# "тдп 25" = one slider; "тдп 25 30", "тдп 2530", "тдп двадцать пять тридцать" = PL1 25 / PL2 30; "pl2 35"
PL1_MAX, PL2_MAX = 30, 37
# Whisper writes PL glued or with hyphens: "PL25", "PL-1-25, PL-2-30", "пл два тридцать", "пиэль 1 20"
PL_WORD = r"\bp[\s-]?l(?=[\s\d-]|$)|\bпл(?=[\s\d-]|$)|\bп[еэи][\s-]?[еэ]?л[ьл]?(?=[\s\d-]|$)"
PL_ORDINAL = {1: r"перв\w* лимит", 2: r"втор\w* лимит"}


def pl_values(p):
    """{1: PL1, 2: PL2, 0: one-slider value} said with "PL": "pl2 35", "pl-1-25 pl-2-30", "pl 125",
    "pl25" (no index: the one slider)."""
    s = re.sub(PL_WORD, " pl ", p).replace("-", " ")
    for k, rx in PL_ORDINAL.items():
        s = re.sub(rx, f" pl {k} ", s)
    out = {}
    for seg in s.split(" pl ")[1:]:
        nums = numbers(re.sub(r"\b(one|two)\b", lambda m: "1" if m.group(1) == "one" else "2", seg))[:2]
        if len(nums) == 2 and nums[0] in (1, 2):
            out[nums[0]] = nums[1]
        elif len(nums) == 2:  # "pl 25 30"
            out[1], out[2] = nums
        elif nums and 100 <= nums[0] <= 999 and str(nums[0])[0] in "12":  # "pl125", "pl230"
            out[int(str(nums[0])[0])] = int(str(nums[0])[1:])
        elif nums and 1000 <= nums[0] <= 9999:  # "pl 2530"
            out[1], out[2] = divmod(nums[0], 100)
        elif nums:
            out[0] = nums[0]
    return out


def _limits():
    """(PL1, PL2) the helper applies now, None when it does not answer."""
    m = re.search(r"PL1:(\d+)W PL2:(\d+)W", ct.get(ct.CURRENT_TDP) or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _say_limits(lead=""):
    lim = _limits()
    if not lim:
        return NO_ANSWER
    return f"{lead}PL1 {n_(lim[0], *WATT)}, PL2 {n_(lim[1], *WATT)}", False


def _set_tdp(w, label=""):
    if not 5 <= w <= PL1_MAX:
        return f"Мощность можно от 5 до {PL1_MAX} ватт", False
    if ct.set_(ct.TDP_BOOST, False) is None or ct.set_(ct.TDP, w) is None:
        return NO_ANSWER
    return _say_limits(f"Режим {label}: " if label else "")


def _set_pair(pl1, pl2):
    if not 5 <= pl1 <= PL1_MAX:
        return f"PL1 можно от 5 до {PL1_MAX} ватт", False
    if not pl1 < pl2 <= PL2_MAX:
        return f"PL2 должен быть больше PL1 и не выше {PL2_MAX} ватт", False
    for fn, v in ((ct.TDP_BOOST, True), (ct.TDP, pl1), (ct.TDP_BOOST_PL2, pl2)):
        if ct.set_(fn, v) is None:
            return NO_ANSWER
    return _say_limits()


def _set_preset(key):
    w, label = PRESETS[key]
    if isinstance(w, tuple):
        msg, ok = _set_pair(*w)
        return (f"Режим {label}: {msg}" if msg.startswith("PL1 ") else msg), ok
    return _set_tdp(w, label)


def power(p):
    for rx, key in ((TURBO, "turbo"), (ECO, "eco"), (MEDIUM, "medium"), (HIGH, "high")):
        if has(rx, p) and not has(FAN, p):
            return _set_preset(key)
    pl = pl_values(p)
    if not has(POWER, p) and not pl and not has(PL_WORD, p):
        return None
    if pl and not has(ASK, p):
        if 1 in pl and 2 in pl:
            return _set_pair(pl[1], pl[2])
        if 2 in pl:
            cur = _limits()
            return _set_pair(cur[0], pl[2]) if cur else NO_ANSWER
        if 1 in pl:
            if ct.get_bool(ct.TDP_BOOST):  # separate mode: keep the PL2 that is set
                cur = _limits()
                return _set_pair(pl[1], max(cur[1], pl[1] + 1)) if cur else NO_ANSWER
            return _set_tdp(pl[1])
        return _set_tdp(pl[0])
    nums = [n for n in numbers(p) if 4 <= n <= 40]
    if not nums:  # "тдп 2530": PL1 and PL2 said as one number
        nums = [n for x in re.findall(r"\b\d{4}\b", p) for n in divmod(int(x), 100)]
    if nums and not has(ASK, p):
        return _set_pair(nums[0], nums[1]) if len(nums) > 1 else _set_tdp(nums[0])
    if has(ASK, p) or has(r"потребля|потреблени", p):
        lim, m = _limits(), ct.metrics()
        if not lim:
            return NO_ANSWER
        now = f", сейчас потребляет {n_(m['cpuWattage'], *WATT)}" if m.get("cpuWattage") is not None else ""
        return f"PL1 {n_(lim[0], *WATT)}, PL2 {n_(lim[1], *WATT)}{now}", True
    if has(r"максим", p):
        return _set_preset("high")
    if has(r"миним", p):
        return _set_preset("eco")
    return None


def boost(p):
    if not has(BOOST, p):
        return None
    if has(ASK, p) and not has(ON + "|" + OFF, p):
        b = ct.get_bool(ct.CPU_BOOST)
        return NO_ANSWER if b is None else (f"Буст процессора {'включён' if b else 'выключен'}", True)
    on = not has(OFF, p)
    got = ct.set_(ct.CPU_BOOST, on)
    if got is None:
        return NO_ANSWER
    return f"Буст процессора {'включён' if got.lower() == 'true' else 'выключен'}", False


def charge(p):
    if not has(CHARGE, p) or not (has(CHARGE_LIMIT, p) or has(CHARGE_FULL, p)):
        return None
    if has(ASK, p) and not has(ON, p):
        s, m = ct.settings(), ct.metrics()
        if not s.get("MsiChargeLimit_On"):
            return "Лимит заряда выключен, батарея заряжается до конца", True
        return f"Батарея заряжается до {n_(m.get('chargeLimit') or s.get('MsiChargeLimit_Pct', 80), *PCT)}", True
    pct = next((n for n in numbers(p) if 50 <= n <= 100), None)
    if has(CHARGE_FULL, p) or pct == 100:
        ok = ct.extra("MsiChargeLimit", f"false:{ct.settings().get('MsiChargeLimit_Pct', 80)}")
        return ("Лимит заряда снят, батарея зарядится до конца", False) if ok else NO_ANSWER
    pct = pct or 80
    ok = ct.extra("MsiChargeLimit", f"true:{pct}")
    return (f"Батарея будет заряжаться до {pct} процентов", False) if ok else NO_ANSWER


def controller(p):
    if has(EXT_PAD, p):
        on = not has(OFF, p)
        got = ct.set_(365, on)
        return NO_ANSWER if got is None else (f"Режим внешнего геймпада {'включён' if on else 'выключен'}", False)
    mouse, pad = has(MOUSE, p), has(PAD, p)
    if not (mouse or pad or (has(INPUT, p) and has(ASK, p))) or has(r"клик|нажми|подвинь|кнопк|свет|цвет|вибрац", p):
        return None
    # ClawTweaks has two paths: with its virtual controller on, MsiClawControllerMode is the switch
    # (True = gamepad); in hardware controller mode a write there only toggles the firmware mouse, so the
    # firmware mouse (366, True = mouse) is set directly
    virtual = ct.get_bool(ct.EMULATION)
    if virtual is None:
        return NO_ANSWER
    fn = ct.CONTROLLER_MODE if virtual else ct.HW_MOUSE
    if has(ASK, p) and not has(ON + "|" + OFF, p):
        v = ct.get_bool(fn)
        return NO_ANSWER if v is None else (f"Сейчас режим {'геймпада' if v == virtual else 'мыши'}", True)
    if not (has(ON + "|" + OFF, p) or has(r"режим|стик", p)):
        return None
    want_pad = pad if not has(OFF, p) else not pad  # "выключи мышь" = геймпад
    got = ct.set_(fn, want_pad if virtual else not want_pad)
    if got is None:
        return NO_ANSWER
    is_pad = (got.lower() == "true") == virtual
    return ("Режим геймпада" if is_pad else "Режим мыши: стики управляют курсором"), False


def _osd_position(p):
    row = "top" if has(r"верх|вверху|сверху|наверх", p) else "bottom" if has(r"низ|ниж|внизу|снизу|вниз", p) else None
    col = ("left" if has(r"\bлев|слева|налево", p) else "right" if has(r"\bправ|справа|направо", p)
           else "mid" if has(r"центр|середин|посередин", p) else None)
    if not row and not col:
        return None
    cur = ct.get_int(ct.OSD_POSITION, 1)
    row = row or ("top" if cur <= 3 else "bottom")
    col = col or {1: "left", 2: "mid", 3: "right"}[(cur - 1) % 3 + 1]
    return {"top": 0, "bottom": 3}[row] + {"left": 1, "mid": 2, "right": 3}[col]


def overlay(p):
    if has(FPS_ASK, p):
        g = ct.running_game()
        if not g:
            return "Игра не запущена, кадры считать не в чем", True
        return f"{g['name']}: {n_(g['fps'], *FPS)} в секунду", True
    if has(FPS_LIMIT, p) or (has(r"фпс|fps|кадр", p) and has(r"лимит|огранич", p)):
        if has(OFF, p) and not numbers(p):
            got = ct.set_(ct.FPS_LIMIT, 0)
            return NO_ANSWER if got is None else ("Ограничение кадров снято", False)
        n = next((n for n in numbers(p) if 15 <= n <= 240), None)
        if n is None:
            cur = ct.get_int(ct.FPS_LIMIT, 0)
            return (f"Кадры ограничены {cur} в секунду" if cur else "Кадры не ограничены"), True
        got = ct.set_(ct.FPS_LIMIT, n)
        return NO_ANSWER if got is None else (f"Кадры ограничены {got} в секунду", False)
    if not has(OSD, p):
        return None
    level = ct.get_int(ct.OSD)
    if level is None:
        return NO_ANSWER
    if has(OFF, p):
        ct.set_(ct.OSD, 0)
        return "Оверлей выключен", False
    pos = _osd_position(p)
    style = next((lv for rx, lv in ((r"базов|прост|только фпс", 8), (r"миним|маленьк|горизонт|строк|полоск", 1),
                                    (r"компакт|средн", 2), (r"расширен|больш", 3),
                                    (r"полн|подробн|детальн|вертикал|максим|все показател", 4)) if has(rx, p)), None)
    if has(r"следующ|друг\w* вид|смени|переключи|по кругу", p) and not pos and style is None:
        ct.action(ct.ACT_OSD_CYCLE)
        new = ct.get_int(ct.OSD, 0)
        return (f"Оверлей: {OSD_LEVELS.get(new, new)}" if new else "Оверлей выключен"), False
    if style is not None:
        ct.set_(ct.OSD, style)
    elif level == 0:
        ct.action(ct.ACT_OVERLAY_ONOFF)  # the widget's on: the last used look
    if pos:
        ct.set_(ct.OSD_POSITION, pos)
        return f"Оверлей {POS_NAMES[pos]}", False
    if style is not None:
        return f"Оверлей: {OSD_LEVELS[style]}", False
    return "Оверлей включён", False


def game(p):
    if has(KILL, p):
        g = ct.running_game()
        name = g.get("name") or "активная игра"
        if not A.confirm(f"Закрыть {name}?", "Игра будет завершена принудительно, несохранённый прогресс пропадёт.",
                         ok="Закрыть игру"):
            return "Отменено", False
        return ("Игра закрывается", False) if ct.extra("EndForegroundTask", "") else NO_ANSWER
    if not has(GAME, p):
        return None
    unfreeze, freeze = has(UNFREEZE, p), has(FREEZE, p)
    if not (freeze or unfreeze):
        return None
    g = ct.running_game()
    if not g:
        if unfreeze:  # a suspended game may not be reported any more; the helper resumes what it paused
            ct.action(ct.ACT_SUSPEND)
            return "Снимаю паузу", False
        return "Игра не найдена", False
    state = ct.suspended(g["pid"])
    if unfreeze and state is False:
        return f"{g['name']} не на паузе", False
    if freeze and not unfreeze and state:
        return f"{g['name']} уже на паузе", False
    ct.action(ct.ACT_SUSPEND)  # ClawTweaks pauses the whole process and warns about anti-cheat games itself
    return (f"{g['name']} продолжается" if unfreeze else f"{g['name']} на паузе"), False


def switches(p):
    for rx, fn, name, e in ((FSE, ct.FSE, "Полноэкранный режим Xbox", ""), (NOISE, ct.AI_NOISE, "Шумоподавление микрофона", "о")):
        if has(rx, p):
            if has(ASK, p) and not has(ON + "|" + OFF, p):
                v = ct.get_int(fn)
                return NO_ANSWER if v is None else (f"{name} {'включен' + e if v == 1 else 'недоступн' + (e or 'ен') if v < 0 else 'выключен' + e}", True)
            got = ct.set_(fn, 0 if has(OFF, p) else 1)
            if got is None:
                return NO_ANSWER
            if got.startswith("-"):
                return f"{name} на этом устройстве недоступн{e or 'ен'}", False
            return f"{name} {('включен' if got == '1' else 'выключен') + e}", False
    if has(LOSSLESS, p):
        if ct.get_bool(ct.LS_INSTALLED) is False:
            return "Lossless Scaling не установлен", False
        on = not has(OFF, p)
        got = ct.set_(ct.LS_ENABLED, on)
        return NO_ANSWER if got is None else (f"Lossless Scaling {'включён' if on else 'выключен'}", False)
    if has(KEYBOARD, p) and not has(OFF, p):
        return ("Экранная клавиатура", False) if ct.extra("ToggleTouchKeyboard", "") else NO_ANSWER
    if has(REFRESH, p):
        rates = [int(x) for x in re.findall(r"\d+", ct.get(ct.REFRESH_RATES) or "")]
        n = next((n for n in numbers(p) if n in rates), None)
        if n:
            got = ct.set_(ct.REFRESH_RATE, n)
            return NO_ANSWER if got is None else (f"Частота экрана {got} герц", False)
        if has(r"максим", p) and rates:
            got = ct.set_(ct.REFRESH_RATE, max(rates))
            return NO_ANSWER if got is None else (f"Частота экрана {got} герц", False)
        cur = ct.get(ct.REFRESH_RATE)
        if cur is None:
            return NO_ANSWER
        return f"Частота экрана {cur} герц, доступно: {', '.join(map(str, rates))}", True
    if has(r"открой|запусти|покажи|включи|вызови", p):
        for rx, act, name in LAUNCH:
            if has(rx, p):
                return (f"Открываю {name}", False) if ct.action(act) else NO_ANSWER
    return None


def sensors(p):
    if has(STATUS, p):
        m, tdp, g = ct.metrics(), ct.get_int(ct.TDP), ct.running_game()
        if not m:
            return NO_ANSWER
        parts = [f"процессор {n_(m.get('cpuTemp'), *DEG)}, {n_(m.get('cpuWattage'), *WATT)} из {tdp}",
                 f"вентиляторы {n_(m.get('fanRpm'), *RPM)}" if m.get("fanRpm") else "вентиляторы стоят", f"батарея {n_(m.get('batteryLevel'), *PCT)}"]
        if g:
            parts.append(f"{g['name']}, {n_(g['fps'], *FPS)}")
        return "Консоль: " + ", ".join(parts), True
    if has(TEMP, p) and (has(HW, p) or has(r"насколько горяч|не перегре|перегрева", p)):
        m = ct.metrics()
        if not m:
            return NO_ANSWER
        gpu = f", видеокарта {n_(m['gpuTemp'], *DEG)}" if (m.get("gpuTemp") or -1) > 0 else ""
        return f"Процессор {n_(m.get('cpuTemp'), *DEG)}{gpu}, нагрузка {n_(m.get('cpuUsage'), *PCT)}", True
    if has(r"какая игра|во что (?:я )?игра|что за игра запущ", p):
        g = ct.running_game()
        return (f"Запущена {g['name']}, {n_(g['fps'], *FPS)} в секунду" if g else "Игра не запущена"), True
    return None


HANDLERS = (game, charge, overlay, fans, boost, power, controller, switches, sensors)


def route(p, text):
    p = norm(text or p)
    for h in HANDLERS:
        r = h(p)
        if r:
            return r
    return None


def tool(args, phrase):
    """LLM fallback: the model passes the request in its own words; the same parser handles it."""
    return route(norm(str(args.get("what") or phrase)), str(args.get("what") or phrase)) or (
        "Такой команды для ClawTweaks нет", False)


def register(api):
    global A
    A = api.agent
    if not ct.installed():
        api.log("ClawTweaks is not installed, plugin idle")
        return
    api.route(route, stage="before")
    api.tool("claw_tweak", '{"what": str}',
             "настройки MSI Claw через ClawTweaks: вентиляторы, мощность и режимы питания, буст, лимит заряда, "
             "мышь или геймпад, оверлей и его позиция, лимит FPS, частота экрана, пауза или закрытие игры, "
             "режим Xbox, шумоподавление, датчики. what = просьба пользователя своими словами", tool)
    api.help("🎮 ClawTweaks: вентиляторы на максимум / как было · мощность 15 ватт, тдп 25 30 (PL1 и PL2), pl2 35, эко / средний / "
             "высокая производительность / турбо режим · буст вкл/выкл · береги батарею / заряжай до конца · режим мыши / геймпада · "
             "покажи / убери фпс, оверлей слева внизу, подробный оверлей · ограничь фпс до 40 · частота экрана 60 · "
             "поставь игру на паузу / продолжи / закрой игру · режим Xbox · шумоподавление · "
             "температура процессора, сколько фпс, как там консоль")
