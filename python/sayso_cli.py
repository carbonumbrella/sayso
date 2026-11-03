# SaySo helper CLI for the tray app: everything that needs Python (devices, Telegram, geocoding, models).
# Prints one JSON object on stdout. Usage: python sayso_cli.py <command> [args]
#   mics                     input devices        hw            hardware + recommended speech device
#   tg-link <token>          save token, read chat id (after /start)   tg-test   send a test message
#   secret <name>            value from stdin -> DPAPI secrets          location <city>
#   models                   models: installed, size, in use            download [key|auto]   remove <key>
#   gemma-download           Hugging Face token from stdin -> NPU server packages + Gemma model
#   match <phrase>           which user command a phrase hits (command editor test)
#   name-test <phrase>       does the wake regex accept it (name trainer)
import json, os, sys, time

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_settings

def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def mics():
    """Full device names from WASAPI (MME cuts them to 31 characters); dictate.py opens the MME twin."""
    import sounddevice as sd
    apis = {i: h["name"] for i, h in enumerate(sd.query_hostapis())}
    names = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and "WASAPI" in apis.get(d["hostapi"], "") and d["name"].strip() not in names:
            names.append(d["name"].strip())
    default = sd.query_devices(kind="input")["name"].strip()
    return {"devices": names, "default": default}


def hw():
    info = {"openvino": [], "npu": False, "gpu": "", "nvidia": False}
    try:
        import openvino as ov
        core = ov.Core()
        info["openvino"] = core.available_devices
        info["npu"] = "NPU" in info["openvino"]
        if "GPU" in info["openvino"]:
            info["gpu"] = core.get_property("GPU", "FULL_DEVICE_NAME")
    except Exception as e:
        info["error"] = repr(e)
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5)
        info["nvidia"] = r.returncode == 0 and "GPU" in r.stdout
    except Exception:
        pass
    # NPU first (low power, frees the GPU for games), then an Intel GPU via OpenVINO, else the CPU.
    # NVIDIA via faster-whisper is not bundled yet: an NVIDIA-only PC uses the CPU path.
    info["recommend"] = "NPU" if info["npu"] else "GPU" if "intel" in info["gpu"].lower() else "CPU"
    return info


STATE_FILE = os.path.join(HERE, "stt_state.json")  # dictate.py: the Whisper folder it has loaded, one being prepared


def stt_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def active_model():
    return stt_state().get("model", "")


def models():
    """Every optional model: installed or not, size, in use. Whisper keys as in voice_settings.WHISPER, plus gemma.
    The bool per key stays for older callers."""
    act = active_model()
    info = {}
    for key, w in voice_settings.WHISPER.items():
        d = voice_settings.whisper_dir(key)
        info[key] = {"installed": voice_settings.whisper_complete(key), "mb": w["mb"], "devices": list(w["devices"]),
                     "active": os.path.basename(d) == act, "dir": os.path.basename(d)}
    g = voice_settings.gemma_dir()
    info["gemma"] = {"installed": g is not None, "mb": 3300, "devices": ["NPU"], "dir": os.path.basename(g or ""),
                     "active": g is not None and voice_settings.get("llm").get("provider") == "npu"}
    out = {k: v["installed"] for k, v in info.items()}
    out["info"] = info
    out["state"] = stt_state()
    return out


def remove(key):
    """Delete a model folder (and its compiled NPU cache). The one dictation has loaded stays."""
    import shutil
    if key == "gemma":
        d = voice_settings.gemma_dir()
        if not d:
            return {"ok": True}
        if voice_settings.get("llm").get("provider") == "npu":
            raise RuntimeError("Gemma выбрана на вкладке «Модель»: сначала выбери там другой вариант")
        shutil.rmtree(d)
        return {"ok": True}
    if key not in voice_settings.WHISPER:
        raise RuntimeError(f"unknown model {key!r}")
    d = voice_settings.whisper_dir(key)
    if os.path.basename(d) == active_model():
        raise RuntimeError("эта модель сейчас распознаёт речь: сначала выбери другую")
    if os.path.isdir(d):
        shutil.rmtree(d)
    cache = os.path.join(HERE, "npu_cache", os.path.basename(d))
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)
    return {"ok": True}


def gemma_download(token):
    """Gemma for the NPU server: its packages, then the gated model with the user's own Hugging Face token
    (the user accepted Google's license on the model page). The token is used once and not stored."""
    import importlib.util, subprocess
    if not token:
        raise RuntimeError("вставь токен Hugging Face")
    if not all(importlib.util.find_spec(m) for m in ("fastapi", "uvicorn", "pydantic")):
        req = os.path.join(HERE, "requirements-npu.txt")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", req],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("не установились пакеты сервера: " + (r.stderr or r.stdout).strip()[-300:])
    dest = os.path.join(HERE, voice_settings.GEMMA_REPO.split("/")[1])
    try:
        snapshot_watched(voice_settings.GEMMA_REPO, dest, "gemma", 3300, token=token)  # resumes a partial download
    except FetchError as e:
        if e.kind == "GatedRepoError":  # (also what a wrong token gets: the gated files answer 401 as "no access")
            raise RuntimeError("нет доступа: проверь токен и прими лицензию Gemma на странице модели тем же аккаунтом")
        if e.status == 401:
            raise RuntimeError("токен не подошёл")
        raise
    if not voice_settings.gemma_dir():
        raise RuntimeError("модель скачалась не полностью, запусти загрузку ещё раз")
    return {"model": dest}


def download(key="auto"):
    """A Whisper model by catalog key; "auto" (the installer) = the one setting stt_model wants on this PC's device."""
    if key not in voice_settings.WHISPER:
        key = voice_settings.whisper_key(voice_settings.get("stt_model"), hw()["recommend"])
    if key == "turbo-npu" and not voice_settings.whisper_complete("small"):
        download("small")  # small dictates while turbo is compiled for the NPU (~6 min the first time)
    dest = voice_settings.whisper_dir(key)
    if not voice_settings.whisper_complete(key):
        snapshot_watched(voice_settings.WHISPER[key]["repo"], dest, key, voice_settings.WHISPER[key]["mb"])
        if not voice_settings.whisper_complete(key):
            raise RuntimeError("the model download is incomplete")
    return {"model": dest, "key": key}


def _dir_bytes(d):
    total = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


PROGRESS_FILE = os.path.join(HERE, "download_progress.json")  # read by the installer and Settings while a model downloads


SLOW = ("модель качается слишком медленно: huggingface.co почти не отдаёт файлы. Возможно, для скачивания "
        "потребуется VPN. Запусти загрузку ещё раз, скачанное сохранится")


def snapshot_watched(repo, dest, key="", total_mb=0, token=None, window=90, min_rate=30_000):
    """snapshot_download (it resumes: files already there are kept) that does not hang. Without a VPN (Russia, 2026-11)
    only the hf_xet transfer crawls at ~1 KB/s; plain HTTP from huggingface.co ran at full speed there (244 MB in 36 s),
    so the child process downloads with xet off. A crawl still ends the try instead of hanging the installer.
    (hf-mirror.com was tried: huggingface_hub fails on it, its API redirects back to huggingface.co.)
    Progress: download_progress.json."""
    try:
        res = _fetch_watched(repo, dest, "https://huggingface.co", token, key, total_mb, window, min_rate)
        if res == "slow":
            raise RuntimeError(SLOW)
        if res is not None:
            raise res
    finally:
        try:
            os.remove(PROGRESS_FILE)
        except OSError:
            pass


class FetchError(RuntimeError):
    def __init__(self, msg, kind="", status=0):
        super().__init__(msg)
        self.kind, self.status = kind, status


def _fetch_watched(repo, dest, endpoint, token, key, total_mb, window, min_rate):
    """One snapshot_download in a child process: None when done, "slow" when it crawled (killed), else the error."""
    import subprocess
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_fetch", repo, dest, endpoint],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000)
    p.stdin.write((token or "").encode())
    p.stdin.close()
    mark, at = _dir_bytes(dest), time.time()
    while p.poll() is None:
        time.sleep(1)
        now = _dir_bytes(dest)
        _progress(key, now // (1 << 20), total_mb)
        if time.time() - at >= window:
            if now - mark < min_rate * window:
                p.kill()
                p.wait()
                return "slow"
            mark, at = now, time.time()
    try:
        res = json.loads(p.stdout.read().decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return FetchError(f"загрузка оборвалась (код {p.returncode})")
    return None if res.get("ok") else FetchError(res.get("error", ""), res.get("kind", ""), res.get("status", 0))


def _fetch_child(repo, dest, endpoint):
    # plain HTTP: files grow in place (.cache\...\*.incomplete), so the progress and the crawl check can see them;
    # hf_xet assembles them in its own cache out of sight
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import snapshot_download
    token = sys.stdin.read().strip() or None
    try:
        snapshot_download(repo, local_dir=dest, token=token, endpoint=endpoint)
        out({"ok": True})
    except Exception as e:
        out({"error": str(e), "kind": type(e).__name__,
             "status": getattr(getattr(e, "response", None), "status_code", 0) or 0})


def _progress(key, mb, total_mb):
    try:
        with open(PROGRESS_FILE + ".tmp", "w", encoding="ascii") as f:
            json.dump({"key": key, "mb": int(mb), "total": int(total_mb)}, f)
        os.replace(PROGRESS_FILE + ".tmp", PROGRESS_FILE)
    except OSError:
        pass


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "mics":
            return out(mics())
        if cmd == "hw":
            return out(hw())
        if cmd == "models":
            return out(models())
        if cmd == "gemma-download":  # token on stdin, never on the command line
            return out(gemma_download(sys.stdin.read().strip()))
        if cmd == "_fetch":  # internal: one download try, see snapshot_watched
            return _fetch_child(*args[:3])
        if cmd == "download":
            return out(download(args[0] if args else "auto"))
        if cmd == "setup":  # setup <full|minimal> <auto|small|turbo>: the installer's choices
            # every update runs this again: "minimal" turns the assistant off only on a fresh profile, never one the
            # user has turned on since (each update over a minimal install switched it off again)
            fresh = "assistant" not in voice_settings.load_raw()
            if args[0] != "minimal" or fresh:
                voice_settings.put("assistant", args[0] != "minimal")
            if len(args) > 1 and args[1] in ("small", "turbo"):  # auto keeps the user's earlier choice
                voice_settings.put("stt_model", args[1])
            return out({"ok": True})
        if cmd == "remove":
            return out(remove(args[0]))
        if cmd == "tg-link":
            import tg
            token = args[0].strip()
            chat, name = tg.link(token)
            voice_settings.set_secret("telegram_token", token)
            return out({"chat_id": chat, "name": name})
        if cmd == "tg-test":
            import tg
            tg.send_message("SaySo: проверка связи. Всё работает 👍")
            return out({"ok": True})
        if cmd == "secret":
            voice_settings.set_secret(args[0], sys.stdin.read().strip())
            return out({"ok": True})
        if cmd == "location":
            import net_skills
            return out({"text": net_skills.set_location(" ".join(args))})
        if cmd == "match":
            import commands
            hit = commands.match(" ".join(args).lower())
            return out({"command": hit[0] if hit else None, "tail": hit[1] if hit else ""})
        if cmd == "llm-test":  # the model chosen in Settings answers a short prompt
            import time, urllib.request
            import gemma_agent as g
            if g.provider() == "none":
                return out({"error": "выбрано «Без модели»"})
            if not g.gemma_on():
                return out({"error": "Gemma выгружена: включи её на вкладке «Основное»"})
            t0 = time.time()
            body = {"messages": [{"role": "user", "content": "Ответь одним словом: работает"}], "max_tokens": 10,
                    "temperature": 0}
            with urllib.request.urlopen(g.llm_request(body), timeout=90) as r:
                text = json.load(r)["choices"][0]["message"]["content"].strip()
            return out({"text": text, "seconds": round(time.time() - t0, 1)})
        if cmd == "name-test":
            import wake
            m = wake.wake_re().match(" ".join(args))
            return out({"wake": bool(m), "command": m.group(1) if m else ""})
        return out({"error": f"unknown command {cmd!r}"})
    except Exception as e:
        return out({"error": str(e)})


if __name__ == "__main__":
    main(sys.argv[1:])
    sys.stdout.flush()
    os._exit(0)  # a download given up on still has its threads (hf_xet): do not wait for them
