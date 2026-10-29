# Wake phrase "привет, Гемма ..." for dictate.py (used only on AC power and while listening is on).
# Silero VAD (onnxruntime, CPU, ~0.2 ms per 32 ms chunk) gates the audio; each finished speech
# segment goes to the already loaded Whisper on the NPU, and only text that starts with the wake
# phrase is acted on. Other speech is transcribed locally, never logged or stored.
# "привет, Гемма" alone -> beep, the next phrase within 6 s is the command. The same window is opened by
# dictate.py after every answer (Alice-style follow-up): speech that STARTS inside it is a command.
import re, time, queue, threading, collections
import numpy as np
import onnxruntime as ort

import voice_settings

GREETING = r"(?:привет\w*|эй|окей|ok|хей|hey|hi)"
GEMMA = r"(?:г|дж|дэ)[еэиы]м+[аеуыи]|гам+[аеу]|(?:g|j|dj)[eiy]m+a"  # Whisper: Гима, Гимма, Джемма
_wake = {"key": None, "re": None}


def wake_re():
    """'[привет,] <name>, <command>' at the start of a phrase. The name and the spellings Whisper produces
    for it come from the settings (assistant_name + wake_names, later filled by the name trainer)."""
    names = [voice_settings.get("assistant_name") or ""] + list(voice_settings.get("wake_names") or [])
    key = tuple(n.lower().strip() for n in names if n and n.strip())
    if key != _wake["key"]:
        alts = [re.escape(n).replace("\\ ", "[\\s-]*").replace("\\-", "[\\s-]*") for n in key]
        if "гемма" in key:
            alts.append(GEMMA)
        _wake["re"] = re.compile(r"^\W*(?:" + GREETING + r"\W+)?(?:" + "|".join(alts or ["мурмур"]) + r")\b\W*(.*)$",
                                 re.I | re.S)
        _wake["key"] = key
    return _wake["re"]


CHUNK, RATE = 512, 16000                 # Silero v5 at 16 kHz: 512 samples + 64 samples of context
ON, OFF = 0.5, 0.35                      # speech starts above ON, ends after END_CHUNKS below OFF
END_CHUNKS, MIN_CHUNKS, MAX_CHUNKS = 14, 12, 470   # 0.45 s silence (was 0.6), 0.4 s min, 15 s max
FOLLOW_UP_SEC = 6
BARGE_WIN, BARGE_STEP = 50, 16           # while the voice talks: transcribe the last 1.6 s every 0.5 s


class WakeListener:
    def __init__(self, model_path, transcribe, on_command, active, beep, log, on_barge=None):
        so = ort.SessionOptions()
        so.intra_op_num_threads = so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
        self.transcribe, self.on_command, self.active, self.beep, self.log = transcribe, on_command, active, beep, log
        self.q = queue.Queue(maxsize=400)
        self.awaiting_until = 0.0
        self.on_barge, self.barge, self._barge_busy = on_barge, False, False
        self.recent = collections.deque(maxlen=BARGE_WIN)  # (chunk, vad prob) for barge-in
        self._since = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def feed(self, block):
        """Called from the audio callback: must never block."""
        try:
            self.q.put_nowait(block)
        except queue.Full:
            pass

    def _reset(self):
        self.state = np.zeros((2, 1, 128), np.float32)
        self.ctx = np.zeros(64, np.float32)
        self.buf = np.zeros(0, np.float32)
        self.speech, self.seg, self.silence = False, [], 0
        self.follow = False  # did the current speech start inside a follow-up window?
        self.pre = collections.deque(maxlen=10)  # 0.32 s before the speech onset

    def _prob(self, chunk):
        x = np.concatenate([self.ctx, chunk])[None]
        out, self.state = self.sess.run(None, {"input": x, "state": self.state, "sr": np.array(RATE, np.int64)})
        self.ctx = chunk[-64:]
        return float(out[0][0])

    def _loop(self):
        self._reset()
        was_active = False
        while True:
            block = self.q.get()
            if not self.active():
                if was_active:
                    self._reset()
                was_active = False
                continue
            was_active = True
            self.buf = np.concatenate([self.buf, block])
            while len(self.buf) >= CHUNK:
                chunk, self.buf = self.buf[:CHUNK], self.buf[CHUNK:]
                p = self._prob(chunk)
                self._barge_tick(chunk, p)
                if self.speech:
                    self.seg.append(chunk)
                    self.silence = self.silence + 1 if p < OFF else 0
                    if self.silence >= END_CHUNKS or len(self.seg) >= MAX_CHUNKS:
                        if len(self.seg) - self.silence >= MIN_CHUNKS:
                            audio = np.concatenate(self.seg)
                            threading.Thread(target=self._handle, args=(audio, self.follow), daemon=True).start()
                        self.speech, self.seg, self.silence = False, [], 0
                else:
                    self.pre.append(chunk)
                    if p > ON:
                        self.speech, self.seg, self.silence = True, list(self.pre), 0
                        self.follow = time.time() < self.awaiting_until

    def _barge_tick(self, chunk, p):
        """VAD segments do not end while the voice plays (it is speech too), so a "стоп" said over it
        is caught by a rolling window instead of waiting for 0.6 s of silence."""
        if not (self.barge and self.on_barge):
            self.recent.clear()
            self._since = 0
            return
        self.recent.append((chunk, p))
        self._since += 1
        if self._since < BARGE_STEP or self._barge_busy or len(self.recent) < 20:
            return
        self._since = 0
        if max(q for _, q in self.recent) < ON:
            return  # nobody talks (or the voice is too quiet to reach the mic)
        audio = np.concatenate([c for c, _ in self.recent])
        self._barge_busy = True

        def run():
            try:
                text = self.transcribe(audio)
                if text and self.barge and self.on_barge(text):
                    self.barge = False
                    self.recent.clear()
                    self.speech, self.seg, self.silence = False, [], 0  # drop the voice+"стоп" segment
            finally:
                self._barge_busy = False
        threading.Thread(target=run, daemon=True).start()

    def _handle(self, audio, follow=False):
        text = self.transcribe(audio)
        if not text:
            return
        if follow:  # follow-up: after a bare "привет, Гемма" or after an answer
            self.awaiting_until = 0
            W = wake_re()
            text = W.sub(lambda m: m.group(1), text) if W.match(text) else text  # "Гемма, а завтра?" too
            if not text.strip(" .,!?"):  # just "привет, Гемма" again: keep waiting for the command
                self.beep()
                self.awaiting_until = time.time() + FOLLOW_UP_SEC
                return
            self.log(f"[wake] command: {text}")
            self.on_command(text)
            return
        m = wake_re().match(text.strip())
        if not m:
            return
        cmd = m.group(1).lstrip(" .,!?").rstrip()  # keep the final "?"/"!": "напиши … как дела?" types it
        self.beep()
        if cmd:
            self.log(f"[wake] {text}")
            self.on_command(cmd)
        else:
            self.log("[wake] listening for a command")
            self.awaiting_until = time.time() + FOLLOW_UP_SEC
