# Telegram Bot API for SaySo: the user's own bot (token in the DPAPI secrets, chat id in settings).
# "Привязать" in Settings: the user writes /start to the bot, link() reads the chat id from getUpdates.
import json, mimetypes, os, uuid, urllib.request
import voice_settings

API = "https://api.telegram.org/bot{token}/{method}"


def configured():
    return bool(voice_settings.secret("telegram_token") and voice_settings.get("telegram_chat_id"))


def _call(method, data=None, files=None, token=None, timeout=30):
    token = token or voice_settings.secret("telegram_token")
    if not token:
        raise RuntimeError("Телеграм не настроен: добавь токен бота в настройках")
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
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Телеграм: {res.get('description', res)}")
    return res["result"]


def send_message(text):
    _call("sendMessage", {"chat_id": voice_settings.get("telegram_chat_id"), "text": text[:4000]})


def send_file(path, caption=""):
    _call("sendDocument", {"chat_id": voice_settings.get("telegram_chat_id"), "caption": caption[:1000]},
          {"document": path}, timeout=120)


def link(token=None):
    """Chat id of the last person who wrote to the bot (after they sent /start). Saves it. -> (chat_id, name)."""
    updates = _call("getUpdates", {"timeout": 0}, token=token)
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            voice_settings.put("telegram_chat_id", chat["id"])
            return chat["id"], chat.get("first_name") or chat.get("username") or ""
    return None, ""
