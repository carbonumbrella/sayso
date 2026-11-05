# Offline routing tests for gemma_agent.command_route (user commands, plugins, built-ins): every side-effecting action is stubbed,
# so this checks only WHICH action and arguments a phrase maps to. Run: python test_agent.py
import sys
sys.stdout.reconfigure(encoding="utf-8")
import gemma_agent as g
# the tests assume the normal online setup, whatever this PC's settings are now ("Полностью офлайн" without a
# model silences everything): the offline mode has its own block at the end
_settings_get = g.voice_settings.get
g.voice_settings.get = lambda key, *a: False if key == "offline" else _settings_get(key, *a)
g.start_apps()  # cache the real Start menu list before _ps_out is stubbed

calls = []
for name in ["volume", "mic", "brightness", "night_light", "media", "timer", "screenshot", "telegram", "power",
             "open_settings", "window", "system_info", "power", "open_app", "close_app", "send_file", "show_file", "_key", "_audio", "_ps_out",
             "fullscreen", "optiscaler"]:
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
g.open_own_app = lambda key, name: calls.append(("own_app", (key,), {})) or ("own", False)
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
    ("разверни на полный экран", "fullscreen", None),
    ("разверни наполний экран", "fullscreen", None),  # as Whisper wrote it (dictate.log 2026-11-04)
    ("сделай на весь экран", "fullscreen", None),
    ("открой оптискейлер", "optiscaler", None),
    ("меню оптискалера", "optiscaler", None),
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
    ("настройки подсветки", "own_app", ("ambilight",)),
    ("открой настройки подсветки", "own_app", ("ambilight",)),
    ("открой подсветку", "own_app", ("ambilight",)),
    ("настройки хаптика", "own_app", ("kbdhaptic",)),
    ("открой настройки вибрации", "own_app", ("kbdhaptic",)),
    ("настройки сейсо", "own_app", ("sayso",)),
    ("покажи настройки помощника", "own_app", ("sayso",)),
    ("включи подсветку", "ambilight", None),
    ("подсветка только кнопки", "ambilight", ()),
    ("только кнопки", "ambilight", ()),
    ("подсветка только кнопки красным", "ambilight", ()),
    ("подсветка кнопки", "ambilight", ()),
    ("подсветка кнопки красный", "ambilight", ()),
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

# the card's caption follows the assistant's name; a screenshot hides the card before the grab
sent = []
g.overlay_running = lambda: True
real_sock, real_get = g._ov_sock, g.voice_settings.get
g._ov_sock = type("S", (), {"sendto": staticmethod(lambda data, addr: sent.append(g.json.loads(data.decode("utf-8"))))})()
g.voice_settings.get = lambda key: "Вася" if key == "assistant_name" else real_get(key)
g.report("готово")
g.report("плохо", ok=False)
g.notify("таймер", "Таймер", kind="info")
titles = [m["title"] for m in sent]
ok = titles == ["Вася", "Вася: ошибка", "Таймер"] and sent[1]["kind"] == "error"
bad += not ok
print(("ok  " if ok else "FAIL"), "card captions ->", titles)
sent.clear()
g.hide_overlay_for_shot()
ok = [m["kind"] for m in sent] == ["hide"]
bad += not ok
print(("ok  " if ok else "FAIL"), "screenshot hides the card ->", sent)
g._ov_sock, g.voice_settings.get = real_sock, real_get

# an early-spoken first sentence (web and chat answers): "стоп" is heard at once, and a stop drops the rest
barge = []
g.on_speaking = barge.append
g.voice_settings.get = lambda key: False if key == "tts_one_speaker" else real_get(key)
calls.clear()
g.EARLY.clear()
g.EARLY["text"] = "Первое предложение."
g.speak_now("Первое предложение.")
g.time.sleep(0.2)
ok = barge == [True]
bad += not ok
print(("ok  " if ok else "FAIL"), "early voice turns the stop word listening on ->", barge)
g.stop_speaking()
calls.clear()
g.speak("Первое предложение. А это остаток ответа.")
g.time.sleep(0.2)
said = [c for c in calls if c[0] == "_ps_out" and "Say" in str(c[1])]
ok = not said and "text" not in g.EARLY
bad += not ok
print(("ok  " if ok else "FAIL"), "stop during generation: the rest is not spoken ->", said)
g.EARLY.clear()
calls.clear()
g.speak("Обычный ответ.")
g.time.sleep(0.2)
said = [c for c in calls if c[0] == "_ps_out" and "Say" in str(c[1])]
ok = len(said) == 1
bad += not ok
print(("ok  " if ok else "FAIL"), "a normal answer still speaks ->", len(said))
g.on_speaking, g.voice_settings.get = None, real_get

# "Полностью офлайн" without a model: commands only, short silent cards, nothing aloud
online_get = g.voice_settings.get
g.voice_settings.get = lambda key, *a: True if key == "offline" else {"provider": "none"} if key == "llm" else online_get(key, *a)
for phrase, want in (("купи слона", g.NO_COMMAND), ("какая погода", g.NEED_NET), ("кто такой бэтмен", g.NEED_NET),
                     ("давай поговорим", g.NO_COMMAND)):
    got = []
    real_notify, g.notify = g.notify, lambda text, *a, **k: got.append(text)
    calls.clear()
    g.handle(phrase)
    g.notify = real_notify
    said = [c for c in calls if c[0] == "_ps_out" and "Say" in str(c[1])]
    ok = got == [want] and not said
    bad += not ok
    print(("ok  " if ok else "FAIL"), f"offline, no model: {phrase} ->", got, said)
calls.clear()
g.speak("Таймер завершён")
g.time.sleep(0.2)
ok = not [c for c in calls if c[0] == "_ps_out" and "Say" in str(c[1])]
bad += not ok
print(("ok  " if ok else "FAIL"), "offline, no model: nothing is spoken")
g.voice_settings.get = online_get
print("FAILED:", bad)
