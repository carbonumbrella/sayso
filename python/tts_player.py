# The assistant's voice from wav files (SAPI written to a file). Playing it ourselves allows one speaker only (setting tts_one_speaker): the far microphone then hears much less of
# it and a "стоп" said over the voice gets through (MSI Claw 8 AI+: the left speaker reaches mic 1 9 dB louder than
# mic 0, dictate.py listens on mic 0 meanwhile).
# Not reloaded with gemma_agent: the player and its queue survive an agent reload.
import os, queue, threading, wave
import numpy as np
import sounddevice as sd

BLOCK = 1024  # frames per write: a stop takes effect within ~40 ms


class Player:
    def __init__(self):
        self.q = queue.Queue()
        self.gen = 0          # bumped by stop(): queued and playing phrases of an older generation are dropped
        self.active = False
        threading.Thread(target=self._loop, daemon=True, name="tts-player").start()

    def play(self, path, after=False, left_only=False):
        """Play a wav; after=True queues it behind what is being said (the next sentence of the answer)."""
        if not after:
            self.stop()
        self.q.put((self.gen, path, left_only))

    def stop(self):
        self.gen += 1
        while True:
            try:
                _, path, _ = self.q.get_nowait()
            except queue.Empty:
                break
            _remove(path)

    def busy(self):
        return self.active or not self.q.empty()

    def _loop(self):
        out, out_rate = None, None  # one output stream for a run of sentences: opening it costs ~160 ms
        while True:
            try:
                gen, path, left_only = self.q.get(timeout=0.3)
            except queue.Empty:
                if out is not None:  # the queue ran dry: release the device
                    out.close()
                    out = None
                continue
            try:
                if gen != self.gen:
                    continue
                with wave.open(path, "rb") as w:
                    rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
                    pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
                if width != 2:
                    continue
                mono = pcm.reshape(-1, ch).mean(1) if ch > 1 else pcm
                start = max(0, int(np.argmax(np.abs(mono) > 0.01)) - rate // 40)  # SAPI leads with ~180 ms of silence
                mono = mono[start:]
                stereo = np.zeros((len(mono), 2), np.float32)
                stereo[:, 0] = mono
                if not left_only:
                    stereo[:, 1] = mono
                self.active = True
                if out is None or out_rate != rate:
                    if out is not None:
                        out.close()
                    out, out_rate = sd.OutputStream(samplerate=rate, channels=2, dtype="float32"), rate
                    out.start()
                for i in range(0, len(stereo), BLOCK):
                    if gen != self.gen:
                        out.abort()  # drop what is buffered, the voice stops at once
                        out.close()
                        out = None
                        break
                    out.write(stereo[i:i + BLOCK])
            except Exception as e:
                print(f"[tts] playback failed: {e!r}", flush=True)
                out = None
            finally:
                self.active = False
                _remove(path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


PLAYER = Player()
