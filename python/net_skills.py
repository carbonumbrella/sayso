# -*- coding: utf-8 -*-
# Internet-backed voice skills for gemma_agent.py: saved location ("я сейчас в Бангкоке"),
# weather (Open-Meteo, no key), Wikipedia definitions, and web search answered by Gemma.
# These are the only assistant skills that send data out: city names / coordinates to Open-Meteo,
# the question text to Wikipedia and DuckDuckGo.
import os, re, json, math, time, difflib, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOCATION_FILE = os.path.join(HERE, "location.json")
UA = {"User-Agent": "SaySo/0.1 (voice assistant; github.com/carbonumbrella)"}
GEMMA = "http://127.0.0.1:8081/v1/chat/completions"


def _online():
    """Offline mode (Settings): nothing goes to the internet."""
    import voice_settings
    if voice_settings.get("offline"):
        raise RuntimeError("включён офлайн-режим, интернет-ответы выключены")



def _wiki_lang():
    import voice_settings
    lang = voice_settings.get("language") or "ru"
    return "ru" if lang == "auto" else lang


def _get(url, params=None, timeout=10):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- location

def _geocode(name):
    r = _get("https://geocoding-api.open-meteo.com/v1/search",
             {"name": name, "count": 5, "language": "ru", "format": "json"}).get("results") or []
    return r


def _nominative_candidates(phrase):
    """'Южной Сахалинске' -> ['Южной Сахалинске', 'Южно-Сахалинск', ...]; the geocoder only knows the
    nominative case, while speech gives the locative ('в Бангкоке', 'в Казани', 'в Москве')."""
    words = phrase.strip(" .,!?").split()
    if not words:
        return []

    def forms(w):
        out = [w]
        for end, rep in (("ии", "ия"), ("ске", "ск"), ("ке", "к"), ("ге", "г"), ("ле", "ль"), ("и", "ь"),
                         ("е", "а"), ("е", ""), ("у", "а"), ("ой", "о"), ("ом", ""), ("ах", "ы")):
            if w.lower().endswith(end) and len(w) > len(end) + 2:
                out.append(w[: len(w) - len(end)] + rep)
        return list(dict.fromkeys(out))

    cands = [phrase.strip()]
    if len(words) == 1:
        cands += forms(words[0])
    else:
        lasts = forms(words[-1])
        heads = [" ".join(words[:-1])]
        first = words[0]
        if len(words) == 2:
            if re.search(r"(ой|ый|ий|ая|ом|ем|ого)$", first, re.I):
                heads.append(re.sub(r"(ой|ый|ий|ая|ом|ем|ого)$", "о", first, flags=re.I))  # Южной -> Южно-
            for end, rep in (("ем", "ий"), ("ом", "ый"), ("ой", "ая"), ("ей", "яя"), ("ом", "ой")):
                if first.lower().endswith(end):
                    heads.append(first[:-2] + rep)  # Нижнем -> Нижний, Южной -> Южная
        for h in heads:
            for l in lasts:
                cands += [f"{h} {l}", f"{h}-{l}"]
    return list(dict.fromkeys(c for c in cands if c))


def _best(results, cand):
    best, score = None, 0.0
    for r in results:
        s = difflib.SequenceMatcher(None, r["name"].lower().replace("-", " "), cand.lower().replace("-", " ")).ratio()
        if s < 0.8:
            continue
        s += min(math.log10((r.get("population") or 0) + 1) / 70, 0.1)  # a city of millions beats a namesake village
        if s > score:
            best, score = r, s
    return best, score


def find_place(phrase, ask_llm=None):
    _online()
    # Score every case form (in parallel) and keep the best: the raw locative can hit a namesake
    # village ("Париже" in Lithuania, "Москве" ~ "Москвец"), the right nominative is a big city.
    cands = _nominative_candidates(phrase)
    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = list(pool.map(lambda c: _best(_geocode(c), c), cands))
    best, score = max(hits, key=lambda h: h[1], default=(None, 0.0))
    if best:
        return best
    if ask_llm:  # last resort: let Gemma name the city in the nominative ("Южной Сахалинске" -> "Южно-Сахалинск")
        name = ask_llm(f"Назови населённый пункт из фразы «{phrase}» в именительном падеже. "
                       "Ответь только названием, без точки.").strip(" .«»\"'\n")
        if name:
            res = _geocode(name)
            if res:
                return res[0]
    return None


def _place_text(p):
    parts = [p["name"]]
    if p.get("admin1") and p["admin1"] != p["name"]:
        parts.append(p["admin1"])
    if p.get("country"):
        parts.append(p["country"])
    return ", ".join(parts)


def set_location(phrase, ask_llm=None):
    p = find_place(phrase, ask_llm)
    if not p:
        return f"Не нашла место «{phrase}»"
    loc = {"name": p["name"], "admin1": p.get("admin1"), "country": p.get("country"),
           "latitude": p["latitude"], "longitude": p["longitude"], "timezone": p.get("timezone"),
           "set_at": datetime.now().isoformat(timespec="seconds"), "said": phrase}
    with open(LOCATION_FILE, "w", encoding="utf-8") as f:
        json.dump(loc, f, ensure_ascii=False, indent=2)
    return f"Запомнила: {_place_text(loc)}"


def get_location():
    try:
        with open(LOCATION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def where_am_i():
    loc = get_location()
    if not loc:
        return "Я не знаю, где ты. Скажи, например: я сейчас в Южно-Сахалинске"
    return f"Ты здесь: {_place_text(loc)}"  # no locative case needed


# ---------------------------------------------------------------- weather

WMO = {0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "изморозь",
       51: "слабая морось", 53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "ледяная морось",
       61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
       71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа", 80: "ливень", 81: "ливень",
       82: "сильный ливень", 85: "снегопад", 86: "сильный снегопад", 95: "гроза", 96: "гроза с градом", 99: "гроза с градом"}


def plural(n, one, few, many):
    """Russian agreement: 1 метр, 2 метра, 5 метров."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _pct(n):
    return f"{n} {plural(n, 'процент', 'процента', 'процентов')}"


def _deg(t):
    t = round(t)
    return ("плюс " if t > 0 else "минус " if t < 0 else "") + f"{abs(t)}"


def weather(p, ask_llm=None):
    """'какая погода', 'погода завтра', 'погода в Бангкоке', 'нужен ли зонт'."""
    _online()
    m = re.search(r"\bв\s+([А-ЯЁа-яёA-Za-z\- ]+?)(?:\s+(?:сегодня|завтра|послезавтра|сейчас))?$", p)
    if m and not re.search(r"^(в\s+)?(сегодня|завтра|послезавтра)$", m.group(1)):
        place = find_place(m.group(1), ask_llm)
        if not place:
            return f"Не нашла место «{m.group(1)}»"
        loc = {"name": place["name"], "latitude": place["latitude"], "longitude": place["longitude"]}
    else:
        loc = get_location()
        if not loc:
            return "Сначала скажи, где ты. Например: я сейчас в Южно-Сахалинске"
    day = 2 if "послезавтра" in p else 1 if "завтра" in p else 0
    d = _get("https://api.open-meteo.com/v1/forecast", {
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 3})
    dl = d["daily"]
    rain = dl["precipitation_probability_max"][day] or 0
    if re.search(r"зонт|дожд|осадк", p):
        verdict = "Зонт лучше взять" if rain >= 50 else "Зонт, скорее всего, не нужен"
        return f"{verdict}: вероятность осадков {_pct(rain)}"
    if day == 0:
        c = d["current"]
        wind = round(c["wind_speed_10m"])
        return (f"{loc['name']}: сейчас {_deg(c['temperature_2m'])}, ощущается как {_deg(c['apparent_temperature'])}, "
                f"{WMO.get(c['weather_code'], 'без осадков')}, ветер {wind} {plural(wind, 'метр', 'метра', 'метров')} "
                f"в секунду. Днём до {_deg(dl['temperature_2m_max'][0])}, осадки {_pct(rain)}")
    when = "завтра" if day == 1 else "послезавтра"
    return (f"{loc['name']}, {when}: от {_deg(dl['temperature_2m_min'][day])} до {_deg(dl['temperature_2m_max'][day])}, "
            f"{WMO.get(dl['weather_code'][day], '')}, осадки {_pct(rain)}")


# ---------------------------------------------------------------- currency

# (phrase regex, code, name, genitive singular for "33,8 бата")
CURRENCIES = [(r"доллар|бакс", "USD", "доллар", "доллара"), (r"евро", "EUR", "евро", "евро"),
              (r"юан", "CNY", "юань", "юаня"), (r"\bбат|тайск", "THB", "бат", "бата"),
              (r"[ий]ен|иен", "JPY", "иена", "иены"), (r"\bвон\b|\bвоны|корейск", "KRW", "вона", "воны"),
              (r"фунт", "GBP", "фунт", "фунта"), (r"тенге", "KZT", "тенге", "тенге"), (r"лир", "TRY", "лира", "лиры"),
              (r"дирхам", "AED", "дирхам", "дирхама"), (r"рупи", "INR", "рупия", "рупии"),
              (r"белорусск", "BYN", "белорусский рубль", "белорусского рубля"), (r"франк", "CHF", "франк", "франка"),
              (r"рубл", "RUB", "рубль", "рубля")]


def _num(x):
    return (f"{x:.2f}" if x >= 1 else f"{x:.4f}").rstrip("0").rstrip(".").replace(".", ",")


def rate(p):
    """Central Bank of Russia rates: 'курс доллара', 'сколько стоит евро', 'курс бата к доллару' (cross)."""
    _online()
    # currencies in the order they are spoken
    hits = sorted(((m.start(), c) for c in CURRENCIES for m in [re.search(c[0], p)] if m), key=lambda h: h[0])
    hits = [c for _, c in hits]
    d = _get("https://www.cbr-xml-daily.ru/daily_json.js")
    date = datetime.fromisoformat(d["Date"]).strftime("%d.%m")
    rub = lambda code: 1.0 if code == "RUB" else d["Valute"][code]["Value"] / d["Valute"][code]["Nominal"]
    foreign = [c for c in hits if c[1] != "RUB" and (c[1] in d["Valute"])]
    # two currencies -> cross rate ("бат к доллару", Whisper's "баток доллару"), unless listed with "и"/comma
    if len(foreign) >= 2 and not re.search(r"\bи\b|,", p):
        a, b = foreign[0], foreign[1]
        ab = rub(b[1]) / rub(a[1])
        return (f"Курс ЦБ на {date}: 1 {b[2]} = {_num(ab)} {a[3]}, 1 {a[2]} = {_num(1 / ab)} {b[3]}")
    if not foreign:
        foreign = [CURRENCIES[0], CURRENCIES[1]]  # "какой курс" -> dollar and euro
    parts = []
    for c in foreign[:3]:
        per = rub(c[1])
        parts.append(f"{c[2]}: {_num(per)} {'рубля' if per % 1 else plural(per, 'рубль', 'рубля', 'рублей')}")
    return f"Курс ЦБ на {date}. " + ", ".join(parts)


# ---------------------------------------------------------------- knowledge

def wikipedia(query, sentences=2):
    """First sentences of the best ru.wikipedia match (full-text search handles inflected words).
    sentences=2 for a spoken answer, more for "подробнее" (then the next sentences are returned)."""
    _online()
    hits = _get(f"https://{_wiki_lang()}.wikipedia.org/w/api.php", {"action": "query", "list": "search", "srsearch": query,
                                                       "srlimit": 1, "format": "json"})["query"]["search"]
    if not hits:
        return None
    s = _get(f"https://{_wiki_lang()}.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(hits[0]["title"].replace(" ", "_")))
    text = re.sub(r"\s*\([^()]*\)", "", s.get("extract") or "")  # drop "(род. 1952)", transliterations
    text = re.sub(r"\s+([,.;:])", r"\1", text.replace("\u0301", ""))  # stress marks confuse the TTS voice
    sent = re.split(r"(?<=[.!?])\s+", text.strip())
    if not text:
        return None
    if sentences > 2:  # "подробнее": the summary has ~2 sentences, take the article intro and continue
        pages = _get(f"https://{_wiki_lang()}.wikipedia.org/w/api.php", {"action": "query", "prop": "extracts", "exintro": 1,
                                                            "explaintext": 1, "titles": hits[0]["title"],
                                                            "format": "json"})["query"]["pages"]
        intro = re.sub(r"\s*\([^()]*\)", "", next(iter(pages.values())).get("extract", "")).replace("́", "")
        allsent = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", intro).strip())
        more = " ".join(allsent[2:sentences + 1])
        return more[:600] or None
    out = " ".join(sent[:2])
    return out if len(out) <= 260 else sent[0]  # a spoken answer: long second sentences are cut


SEARCH_BACKENDS = ("duckduckgo", "bing", "google", "brave", "mojeek")


def _search(question, timeout=6.0):
    """All search backends at once, the first non-empty result wins. One by one they took 5-13 s here:
    DuckDuckGo rate-limits (202 / "No results found"), Bing and Google answer in 2.7-6 s."""
    from ddgs import DDGS
    import concurrent.futures as cf

    def one(backend):
        return DDGS(timeout=int(timeout)).text(question, region="wt-wt", max_results=5, backend=backend) or []
    pool = ThreadPoolExecutor(max_workers=len(SEARCH_BACKENDS))
    futures = [pool.submit(one, b) for b in SEARCH_BACKENDS]
    try:
        for f in cf.as_completed(futures, timeout=timeout):
            try:
                res = f.result()
            except Exception:
                continue
            if res:
                return res
    except cf.TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # slow backends finish in the background
    return []


def _best_sentence(question, bodies):
    """No-LLM answer: score snippet sentences by shared word stems with the question (+2 for a number when
    it asks сколько/когда/какой...), skip fragments and foreign-script junk."""
    stems = {w[:5] for w in re.findall(r"[а-яё]{4,}", question.lower())} - {"сколь", "какой", "какая", "какое",
                                                                            "почем", "когда", "котор"}
    wants_num = bool(re.match(r"\s*(сколько|когда|какой|какая|какое|в каком)", question.lower()))
    best, best_score = None, 0.0
    for body in bodies:
        for s in re.split(r"(?<=[.!?])\s+", body):
            s = s.strip()
            if not 30 <= len(s) <= 260 or re.search(r"[^\x00-ӿ‐-›€№°«»]", s):
                continue
            words = {w[:5] for w in re.findall(r"[а-яё]{4,}", s.lower())}
            score = len(stems & words) + (2 if wants_num and re.search(r"\d", s) else 0)
            if score > best_score:
                best, best_score = s, score
    return best if best_score >= 2 else None


def web_answer(question, ask_llm):
    """DuckDuckGo snippets -> Gemma writes a 1-2 sentence spoken answer (kept short: NPU prompt limit)."""
    _online()
    res = _search(question)
    clean = lambda t: re.sub(r"\s+", " ", t.replace("́", "").replace("\xa0", " "))  # stress marks derail Gemma
    ctx = "\n".join(f"- {clean(r['title'])}: {clean(r['body'])}" for r in res)[:1400]
    if not ctx:
        return wikipedia(question)  # search is down: the best Wikipedia match is still better than nothing
    if ask_llm is None:  # Gemma unloaded: the snippet sentence that matches the question best, read as is
        return _best_sentence(question, [clean(r["body"]) for r in res]) or wikipedia(question)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
    now = datetime.now()
    answer = ask_llm(f"Сегодня {now.day} {months[now.month - 1]} {now.year} года.\n"
                     f"Сведения из интернета:\n{ctx}\n\nВопрос: {question}\n"
                     "Ответь простым разговорным предложением, как человек вслух, не длиннее двух предложений. "
                     "Бери факты только из сведений выше; возраст и сроки считай от сегодняшней даты. "
                     "Если ответа в сведениях нет, скажи: не нашла ответа.")
    # Gemma echoes the date line, at the start or the end
    answer = re.sub(r"\s*Сегодня\s+\d+\s+\w+\s+\d{4}\s+года\.?\s*", " ", clean(answer)).strip()
    answer = re.sub(r"^(Нашла?|Нашёл|Нашел),?\s+(что\s+)?", "", answer).strip()
    # a 4B model sometimes loops ("1.0000000...") or pastes page junk ("2 hours ago"): do not speak that
    if not answer or re.search(r"\d{9,}|(.)\1{9,}|\b(hours?|minutes?|days?) ago\b", answer) or len(answer) > 400:
        return wikipedia(question) or "Не нашла надёжного ответа, попробуй спросить иначе"
    return answer[:1].upper() + answer[1:]
