"""OpenAI-compatible server for a single OpenVINO LLM or VLM on the Intel NPU.
Runs the model on the NPU so the iGPU stays free (e.g. during games).
Separate port (default 8081); does NOT touch llama-swap on :8080.

Env:
  NPU_MODEL_DIR  IR dir (default: voice_settings.gemma_dir(), Gemma next to the code; Qwen3-4B abliterated is
                 C:\\AI\\openvino-npu\\qwen3-4b-abl-ov-int4). A dir with
                 openvino_language_model.xml is a VLM and runs via VLMPipeline (text only here).
  NPU_DEVICE     NPU | GPU | CPU (default NPU)
  NPU_MODEL_ID   model id shown to clients
  NPU_CACHE_DIR  NPU compile cache (speeds up later loads)
  NPU_PORT       default 8081
"""
import os, re, sys, time, uuid, threading, queue
from typing import List, Optional
if sys.stdout is None:  # pythonw (autostart): no console, log to a file instead
    sys.stdout = sys.stderr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "npu_server.log"),
                                   "w", encoding="utf-8", buffering=1)
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn
import openvino_genai as ov_genai

_state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemma_state.txt")
if not os.path.exists(_state) or open(_state).read().strip() != "on":
    sys.exit(0)  # unloaded by default and on purpose (tray / "выгрузи Гемму"): only "загрузи Гемму" loads it
import voice_settings
MODEL_DIR = os.environ.get("NPU_MODEL_DIR") or voice_settings.gemma_dir()
if not MODEL_DIR:
    print("Gemma is not downloaded (Settings -> Model -> On NPU)", flush=True)
    sys.exit(0)
DEVICE    = os.environ.get("NPU_DEVICE", "NPU")
IS_QWEN   = "qwen" in MODEL_DIR.lower()
IS_VLM    = os.path.exists(os.path.join(MODEL_DIR, "openvino_language_model.xml"))
MODEL_ID  = os.environ.get("NPU_MODEL_ID", "qwen3-4b-abliterated-npu" if IS_QWEN else "gemma-3-4b-npu")
CACHE_DIR = os.environ.get("NPU_CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "npu_cache"))
PORT      = int(os.environ.get("NPU_PORT", "8081"))

os.makedirs(CACHE_DIR, exist_ok=True)
print(f"loading {MODEL_DIR} on {DEVICE} (cache {CACHE_DIR}) ...", flush=True)
t0 = time.time()
Pipeline = ov_genai.VLMPipeline if IS_VLM else ov_genai.LLMPipeline
pipe_kwargs = {"CACHE_DIR": CACHE_DIR}
if DEVICE.upper() == "NPU":
    pipe_kwargs["MAX_PROMPT_LEN"] = 2048
    # Chunked prefill: the static prefill always ran the full 2048 window (1.39 s to the first token even
    # for "привет"); 256-token chunks: 0.17 s short / 0.65 s for the 845-token agent prompt (2026-10-29).
    pipe_kwargs["NPUW_LLM_PREFILL_HINT"] = "DYNAMIC"
    pipe_kwargs["NPUW_LLM_PREFILL_CHUNK_SIZE"] = 256
    # experiments: NPU_EXTRA_CONFIG='{"NPUW_LLM_PREFILL_CHUNK_SIZE": 512}'
    import json as _json
    pipe_kwargs.update(_json.loads(os.environ.get("NPU_EXTRA_CONFIG", "{}")))
pipe = Pipeline(MODEL_DIR, DEVICE, **pipe_kwargs)
tok  = pipe.get_tokenizer()
print(f"loaded in {time.time()-t0:.1f}s on {DEVICE}", flush=True)

app = FastAPI()
gen_lock = threading.Lock()  # one infer request on the NPU: parallel calls failed with "Infer Request is busy"

class Msg(BaseModel):
    role: str
    content: str

class ChatReq(BaseModel):
    model: Optional[str] = MODEL_ID
    messages: List[Msg]
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False
    think: Optional[bool] = False  # keep <think> blocks when True
    prefix: Optional[str] = ""     # forced start of the answer ('{"tool": "'): no ```json fence tokens
    stop: Optional[List[str]] = None
    repetition_penalty: Optional[float] = None  # free chat: the int4 model on the NPU loops ("за за за") without it

def build_prompt(history):
    try:
        return tok.apply_chat_template(history, True)  # add_generation_prompt=True
    except Exception:
        return "\n".join(f"{m['role']}: {m['content']}" for m in history) + "\nassistant:"

@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local-npu"}]}

def _stream(prompt, cfg, prefix, t0):
    q = queue.Queue()
    running = getattr(getattr(ov_genai, "StreamingStatus", None), "RUNNING", False)

    def cb(sub):
        q.put(sub)
        return running

    def run():
        with gen_lock:
            try:
                pipe.generate(prompt, generation_config=cfg, streamer=cb)
            except Exception as e:
                print(f"stream failed: {e!r}", flush=True)
            finally:
                if hasattr(pipe, "finish_chat"):
                    try:
                        pipe.finish_chat()
                    except Exception:
                        pass
                q.put(None)
    threading.Thread(target=run, daemon=True).start()
    if prefix:
        yield prefix
    while (chunk := q.get()) is not None:
        yield chunk
    print(f"generate (stream) {time.time() - t0:.2f}s, max_tokens {cfg.max_new_tokens}", flush=True)


@app.post("/v1/chat/completions")
def chat(req: ChatReq):
    history = [{"role": m.role, "content": m.content} for m in req.messages]
    # Qwen3 thinking is ON by default; append the /no_think soft switch for terse answers.
    if IS_QWEN and not req.think and history and history[-1]["role"] == "user":
        history[-1]["content"] = history[-1]["content"].rstrip() + " /no_think"
    prompt = build_prompt(history) + (req.prefix or "")
    cfg = ov_genai.GenerationConfig()
    cfg.max_new_tokens = req.max_tokens or 512
    cfg.apply_chat_template = False  # prompt is already templated by build_prompt
    if req.stop:
        cfg.stop_strings = set(req.stop)
    if req.temperature and req.temperature > 0:
        cfg.do_sample = True
        cfg.temperature = req.temperature
    if req.repetition_penalty:
        cfg.repetition_penalty = req.repetition_penalty
    t0 = time.time()
    if req.stream:  # plain text chunks as they are generated (gemma_agent speaks the first sentence early)
        return StreamingResponse(_stream(prompt, cfg, req.prefix or "", t0), media_type="text/plain; charset=utf-8")
    with gen_lock:
        try:
            out = pipe.generate(prompt, generation_config=cfg)
        finally:
            if hasattr(pipe, "finish_chat"):
                try:
                    pipe.finish_chat()
                except Exception:
                    pass
    text = (req.prefix or "") + (out.texts[0] if hasattr(out, "texts") else str(out))
    dt = time.time() - t0
    print(f"generate {dt:.2f}s, max_tokens {cfg.max_new_tokens}", flush=True)
    if not req.think:
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:12],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model or MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "_npu_seconds": round(dt, 2),
    }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
