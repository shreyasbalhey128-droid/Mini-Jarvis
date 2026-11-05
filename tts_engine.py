"""
Local neural voices for the six personas using piper-tts (offline, runs fine on CPU).

pip install piper-tts
Voices (~60-110 MB each) download once into ~/.jarvis_voices the first time a persona speaks.
If piper fails for any reason, it falls back to pyttsx3 so Jarvis is never silent.

XTTS-v2 (Coqui) gives voice *cloning* but needs ~2 GB and a GPU to be comfortable; on a laptop that
already overheats, piper is the better pick. This file is structured so a second backend can be added.
"""
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import wave

try:
    import winsound
except ImportError:          # not Windows
    winsound = None

VOICE_DIR = os.path.join(os.path.expanduser("~"), ".jarvis_voices")
os.makedirs(VOICE_DIR, exist_ok=True)

# persona -> piper voice. Change any of these to taste (browse voices at rhasspy.github.io/piper-samples).
PERSONA_VOICES = {
    "echo":   "en_US-ryan-high",          # daily driver, warm male
    "nova":   "en_US-amy-medium",         # code, bright female
    "aegis":  "en_GB-alan-medium",        # security, calm British male
    "vortex": "en_US-hfc_female-medium",  # web gathering, crisp female
    "atlas":  "en_US-joe-medium",         # data, steady male
    "silo":   "en_US-lessac-high",        # gets slowed and deepened below
}
SILO_PITCH = 0.82          # <1 = deeper and slower. Used for the evil Silo voice.
_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _voice_urls(name):
    lang_region, voice, quality = name.split("-", 2)
    lang = lang_region.split("_")[0]
    base = f"{_HF}/{lang}/{lang_region}/{voice}/{quality}/{name}"
    return base + ".onnx", base + ".onnx.json"


def ensure_voice(name):
    """Return the .onnx path, downloading the voice if needed."""
    model = os.path.join(VOICE_DIR, name + ".onnx")
    cfg = model + ".json"
    if not (os.path.exists(model) and os.path.exists(cfg)):
        m_url, c_url = _voice_urls(name)
        urllib.request.urlretrieve(c_url, cfg)
        urllib.request.urlretrieve(m_url, model + ".part")
        os.replace(model + ".part", model)
    return model


def _split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p for p in parts if p and re.search(r"\w", p)]


class NeuralTTS:
    def __init__(self):
        self._stop = threading.Event()
        self._fallback = None

    # ---------------------------------------------------------- public API
    def speak(self, text, persona="echo", blocking=True):
        if blocking:
            self._run(text, persona)
        else:
            threading.Thread(target=self._run, args=(text, persona), daemon=True).start()

    def stop(self):
        """Call from your 'stop' command; cuts speech immediately."""
        self._stop.set()
        if winsound:
            winsound.PlaySound(None, winsound.SND_PURGE)

    def warmup(self, persona="echo"):
        """Pre-download a persona's voice (call at startup in a thread, or when switching persona)."""
        try:
            ensure_voice(PERSONA_VOICES[persona])
        except Exception:
            pass

    # ---------------------------------------------------------- internals
    def _run(self, text, persona):
        self._stop.clear()
        persona = persona.lower()
        try:
            model = ensure_voice(PERSONA_VOICES.get(persona, PERSONA_VOICES["echo"]))
        except Exception:
            return self._speak_fallback(text)

        q = queue.Queue(maxsize=2)

        def producer():
            for s in _split_sentences(text):
                if self._stop.is_set():
                    break
                try:
                    q.put(self._synth(s, model, persona))
                except Exception:
                    q.put(None)
                    return
            q.put(None)

        threading.Thread(target=producer, daemon=True).start()

        while True:
            path = q.get()
            if path is None:
                break
            if not self._stop.is_set():
                self._play(path)
            try:
                os.remove(path)
            except OSError:
                pass

    def _synth(self, sentence, model, persona):
        out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [sys.executable, "-m", "piper", "--model", model, "--output_file", out],
            input=sentence.encode("utf-8"), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        if persona == "silo":
            self._deepen(out, SILO_PITCH)
        return out

    @staticmethod
    def _deepen(path, factor):
        with wave.open(path, "rb") as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
        with wave.open(path, "wb") as w:
            w.setnchannels(params.nchannels)
            w.setsampwidth(params.sampwidth)
            w.setframerate(int(params.framerate * factor))
            w.writeframes(frames)

    def _play(self, path):
        with wave.open(path, "rb") as w:
            seconds = w.getnframes() / float(w.getframerate())
        if winsound:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._stop.wait(seconds + 0.05)       # wakes early if stop() is called
            if self._stop.is_set():
                winsound.PlaySound(None, winsound.SND_PURGE)
        else:
            try:
                import sounddevice as sd, numpy as np
                with wave.open(path, "rb") as w:
                    data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                    sd.play(data, w.getframerate())
                    self._stop.wait(seconds + 0.05)
                    sd.stop()
            except Exception:
                pass

    def _speak_fallback(self, text):
        try:
            import pyttsx3
            if self._fallback is None:
                self._fallback = pyttsx3.init()
            self._fallback.say(text)
            self._fallback.runAndWait()
        except Exception:
            pass


tts = NeuralTTS()      # import and use:  from tts_engine import tts;  tts.speak("Hello", persona="nova")