# SaySo helper CLI for the tray app: everything that needs Python (devices, Telegram, geocoding, models).
# Prints one JSON object on stdout. Usage: python sayso_cli.py <command> [args]
#   mics                     input devices        hw            hardware + recommended speech device
#   tg-link <token>          save token, read chat id (after /start)   tg-test   send a test message
#   secret <name>            value from stdin -> DPAPI secrets          location <city>
#   models                   speech models present                     download <NPU|GPU|CPU> [size]
#   match <phrase>           which user command a phrase hits (command editor test)
#   name-test <phrase>       does the wake regex accept it (name trainer)
import json, os, sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_settings

# OpenVINO IRs of Whisper on Hugging Face (int8: fits the NPU; small = what the Claw uses)
WHISPER = {"small": "OpenVINO/whisper-small-int8-ov", "medium": "OpenVINO/whisper-medium-int8-ov",
           "large-v3-turbo": "OpenVINO/whisper-large-v3-turbo-int8-ov", "base": "OpenVINO/whisper-base-int8-ov"}


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


# A model counts as present only with all of these: an interrupted download leaves the folder half full
# (a clean PC got everything but the encoder, and "folder exists" skipped it on the retry).
MODEL_FILES = [f"openvino_{p}_model.{e}" for p in ("encoder", "decoder") for e in ("xml", "bin")] + \
              [f"openvino_{p}.{e}" for p in ("tokenizer", "detokenizer") for e in ("xml", "bin")]


def complete(dest):
    return all(os.path.isfile(os.path.join(dest, f)) for f in MODEL_FILES)


def models():
    return {k: complete(os.path.join(HERE, repo.split("/")[1])) for k, repo in WHISPER.items()}


def download(device, size=None):
    # the NPU and small machines get "small" (larger ones crash the Claw's NPU with DEVICE_LOST)
    size = size or ("small" if device in ("NPU", "CPU") else "large-v3-turbo")
    repo = WHISPER[size]
    dest = os.path.join(HERE, repo.split("/")[1])
    if not complete(dest):
        from huggingface_hub import snapshot_download
        snapshot_download(repo, local_dir=dest)  # resumes: files already there are kept
        if not complete(dest):
            raise RuntimeError("the model download is incomplete")
    return {"model": dest, "size": size}


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "mics":
            return out(mics())
        if cmd == "hw":
            return out(hw())
        if cmd == "models":
            return out(models())
        if cmd == "download":
            return out(download(args[0] if args else hw()["recommend"], args[1] if len(args) > 1 else None))
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
