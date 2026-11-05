# Telegram Bot API for SaySo: the user's own bot (token in the DPAPI secrets, chat id in settings).
# "Привязать" in Settings: the user writes /start to the bot, link() reads the chat id from getUpdates.
import json, mimetypes, os, uuid, urllib.error, urllib.request
import voice_settings

API = "https://api.telegram.org/bot{token}/{method}"


def configured():
    return bool(voice_settings.secret("telegram_token") and voice_settings.get("telegram_chat_id"))


def _call(method, data=None, files=None, token=None, timeout=30):
    token = token or voice_settings.secret("telegram_token")
    if not token:
        raise RuntimeError("Телеграм не настроен: нужен токен бота в настройках")
    url = API.format(token=token, method=method)
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (data or {}).items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, path in files.items():
            name = os.path.basename(path)
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                         f"Content-Type: {ctype}\r\n\r\n").encode() + f.read() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, json.dumps(data or {}).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:  # Telegram explains itself in the body: show that, not "HTTP Error 400"
        try:
            desc = json.load(e).get("description", "")
        except Exception:
            desc = ""
        if e.code == 401:
            raise RuntimeError("токен не подошёл: скопируй его у @BotFather ещё раз")
        if e.code == 409:
            raise RuntimeError("этого бота уже читает другая программа (webhook или второй SaySo): отключи её")
        if "chat not found" in desc or "chat_id is empty" in desc:
            raise RuntimeError("бот не знает чат: нужно написать ему /start и нажать «Привязать»")
        raise RuntimeError(f"Телеграм ответил ошибкой {e.code}: {desc or e.reason}")
    if not res.get("ok"):
        raise RuntimeError(f"Телеграм: {res.get('description', res)}")
    return res["result"]


def send_message(text):
    if not voice_settings.get("telegram_chat_id"):
        raise RuntimeError("бот ещё не привязан: нужно написать ему /start и нажать «Привязать»")
    _call("sendMessage", {"chat_id": voice_settings.get("telegram_chat_id"), "text": text[:4000]})


def send_file(path, caption=""):
    _call("sendDocument", {"chat_id": voice_settings.get("telegram_chat_id"), "caption": caption[:1000]},
          {"document": path}, timeout=120)


def link(token=None):
    """Chat id of the last person who wrote to the bot (after they sent /start). Saves it. -> (chat_id, name).
    A /start sent long ago may be gone (read by another program, or older than a day): it then waits up to 20 s
    for a new message, so writing to the bot right after pressing «Привязать» also works."""
    updates = _call("getUpdates", {"timeout": 0}, token=token) or \
        _call("getUpdates", {"timeout": 20}, token=token, timeout=30)
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            voice_settings.put("telegram_chat_id", chat["id"])
            return chat["id"], chat.get("first_name") or chat.get("username") or ""
    return None, ""
