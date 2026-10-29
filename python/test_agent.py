# Offline routing tests for gemma_agent.command_route (user commands, plugins, built-ins): every side-effecting action is stubbed,
# so this checks only WHICH action and arguments a phrase maps to. Run: python test_agent.py
import sys
sys.stdout.reconfigure(encoding="utf-8")
import gemma_agent as g
g.start_apps()  # cache the real Start menu list before _ps_out is stubbed

calls = []
for name in ["volume", "mic", "brightness", "night_light", "media", "timer", "screenshot", "telegram", "power",
             "open_settings", "window", "system_info", "power", "open_app", "close_app", "send_file", "show_file", "_key", "_audio", "_ps_out"]:
    def stub(*a, _n=name, **k):
        calls.append((_n, a, k))
        return "shot.png" if _n == "screenshot" else ("50" if _n == "_audio" else (0, "x") if _n == "_ps_out" else _n)
    setattr(g, name, stub)
for name in ["set_location", "where_am_i", "weather", "wikipedia", "web_answer", "rate"]:  # internet skills
    def stub(*a, _n=name, **k):
        calls.append((_n, a, k))
        return _n
    setattr(g.net_skills, name, stub)
g.plugins.REG["modules"]["ambilight"].ambilight = lambda p: calls.append(("ambilight", (), {})) or ("amb", False)
g.usercmd.items = lambda: []  # the user's own command file must not change routing tests
g.running_process = lambda n: "aimp" if "имп" in n or "imp" in n else None
g._paste_text = lambda t: (calls.append(("type", (t,), {})), g.note_typed())
g.rescan_apps = lambda: calls.append(("rescan_apps", (), {})) or "rescan"
g.os.startfile = lambda u: calls.append(("startfile", (u,), {}))
g.user32 = type("U", (), {"LockWorkStation": staticmethod(lambda: calls.append(("lock", (), {})))})()

CASES = [  # phrase, expected first call name, expected args/kwargs subset (or None)
    ("громкость двадцать пять", "volume", {"level": 25}),
    ("сделай громкость 30%", "volume", {"level": 30}),
    ("сделай громче на пять", "volume", {"step": 5}),
    ("потише", "volume", {"step": -15}),
    ("выключи звук", "volume", {"mute": True}),
    ("включи звук", "volume", {"mute": False}),
    ("яркость семьдесят пять процентов", "brightness", {"level": 75}),
    ("яркость на максимум", "brightness", {"level": 100}),
    ("сделай экран темнее на десять", "brightness", {"step": -10}),
    ("отключи микрофон", "mic", (True,)),
    ("включи микрофон", "mic", (False,)),
    ("отключи ночной свет", "night_light", (False,)),
    ("включи ночной свет", "night_light", (True,)),
    ("включи автоповорот экрана", "_key", None),
    ("отправь скриншот экрана в телеграм", "screenshot", None),
    ("сделай скриншот", "screenshot", None),
    ("поставь таймер на двадцать пять минут", "timer", ("set", 1500)),
    ("таймер на 30 секунд", "timer", ("set", 30)),
    ("отмени таймер", "timer", ("cancel",)),
    ("напомни через десять минут позвонить маме", "timer", ("set", 600, "Позвонить маме")),
    ("напомни через 2 часа что надо выключить духовку", "timer", ("set", 7200, "Надо выключить духовку")),
    ("пауза", "media", ("play_pause",)),
    ("следующий трек", "media", ("next",)),
    ("открой настройки блютуз", "open_settings", None),
    ("сверни все окна", "window", ("minimize_all",)),
    ("сколько времени", "system_info", ("time",)),
    ("какой сегодня день", "system_info", ("date",)),
    ("сколько заряда у батареи", "system_info", ("battery",)),
    ("сколько места на диске", "system_info", ("disk",)),
    ("заблокируй компьютер", "lock", None),
    ("усыпи консоль", "power", ("sleep", 0)),
    ("выключи компьютер через пять минут", "power", ("shutdown", 300)),
    ("перезагрузи компьютер", "power", ("restart", 0)),
    ("отмени выключение", "power", ("cancel",)),
    ("открой ютуб", "startfile", ("https://www.youtube.com",)),
    ("открой хром", "open_app", ("chrome",)),
    ("запусти игру форза", "open_app", ("форза",)),
    ("отправь мне в телеграм файл оптискейлер", "send_file", None),
    ("отправь последнее фото в телеграм", "send_file", None),
    ("скинь мне pdf резюме", "send_file", None),
    ("найди файл оптискейлер", "show_file", None),
    ("покажи мне последний скаченный файл", "show_file", None),
    ("покажи мне последний файл в загрузках", "show_file", None),
    ("где торрент лего бэтмен", "show_file", None),
    ("открой папку видео", "open_app", None),
    ("запусти одни из нас", "open_app", ("одни из нас",)),
    ("откроемте гравити", "open_app", ("гравити",)),
    ("открой программу клауди на пк", "open_app", ("клауди",)),
    ("открой новое окно терминала", "open_app", ("terminal",)),
    ("открой проводник на рабочем столе", "open_app", None),
    ("закрой все окна проводника", "close_app", ("explorer",)),
    ("закрой диспетчер задач", "close_app", ("taskmgr",)),
    ("отправь 56 в телеграмм", "telegram", ("--message", "56")),
    ("я сейчас в бангкоке", "set_location", ("бангкоке",)),
    ("моё местоположение москва", "set_location", ("москва",)),
    ("где я", "where_am_i", ()),
    ("какая погода", "weather", ("какая погода",)),
    ("погода в казани завтра", "weather", ("погода в казани завтра",)),
    ("нужен ли зонт", "weather", ("нужен ли зонт",)),
    ("обнови список приложений", "rescan_apps", ()),
    ("какой курс рубля к доллару", "rate", ()),
    ("сколько стоит евро", "rate", ()),
    ("выключаем бинт-лайт", "ambilight", ()),
    ("выключи по цветку", "ambilight", ()),
    ("закрой аимп", "close_app", ("aimp",)),
    ("убей аимп", "close_app", ("aimp",)),
    ("выключи аимп", "close_app", ("aimp",)),
    ("включи aimp", "open_app", ("aimp",)),
    ("выключи ambient light", "ambilight", ()),
    ("курс бата к доллару", "rate", ()),
    ("что такое собака", "wikipedia", ("собака",)),
    ("кто такой бэтмен найди в интернете", "wikipedia", ("бэтмен",)),
    ("кто сейчас президент франции", "web_answer", ("кто сейчас президент франции",)),
]
MATH = [("сколько будет 22 плюс 54", "76"), ("семь на восемь", "56"), ("сто минус тридцать", "70"),
        ("двадцать пять умножить на четыре", "100"), ("корень из 144", "12"), ("10 разделить на 4", "2,5"),
        ("десять в степени миллион", "Слишком большое число"), ("открой хром", None), ("два х три", "6")]
GEMMA = ["открой какую-нибудь хрень", "как тебя зовут", "расскажи анекдот"]

bad = 0
for phrase, name, want in CASES:
    calls.clear()
    try:
        g.command_route(phrase, phrase)
    except Exception as e:
        calls.append(("EXC", (repr(e),), {}))
    got = calls[0] if calls else ("NONE", (), {})
    ok = got[0] == name and (want is None or (isinstance(want, dict) and all(got[2].get(k) == v for k, v in want.items()))
                              or (isinstance(want, tuple) and got[1][:len(want)] == want))
    bad += not ok
    print(("ok  " if ok else "FAIL"), phrase, "->", got[0], got[1], got[2])
for phrase, want in MATH:
    r = g.eval_math(phrase)
    bad += r != want
    print(("ok  " if r == want else "FAIL"), phrase, "->", r)
for phrase in GEMMA:
    calls.clear()
    r = g.command_route(phrase, phrase)
    bad += r is not None
    print(("ok  " if r is None else "FAIL"), phrase, "-> goes to Gemma" if r is None else f"-> fast {r}")
print("FAILED:", bad)
