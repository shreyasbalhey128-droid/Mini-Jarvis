"""
Silero VAD listener: replaces the energy-threshold check, so typing, fans, TV and chatter don't
trigger Jarvis. It records one utterance at a time and returns it as 16 kHz mono float32.

pip install silero-vad sounddevice numpy      (silero-vad pulls in torch)

Pair it with your Resemblyzer voice lock: VAD says "someone is speaking", the voice lock says
"it's Shreyas". Run the voice-lock check on the audio returned by listen_for_utterance().
"""
import collections
import threading

import numpy as np
import sounddevice as sd
import torch
from silero_vad import load_silero_vad

SAMPLE_RATE = 16000
CHUNK = 512                       # Silero needs exactly 512 samples per call at 16 kHz
SPEECH_ON = 0.55                  # probability above this = speech
SPEECH_OFF = 0.35                 # below this = silence
START_CHUNKS = 3                  # ~96 ms of speech needed before we start recording
END_SILENCE_MS = 750              # this much silence ends the utterance
PRE_ROLL_MS = 320                 # audio kept from before speech was detected
MIN_UTTERANCE_MS = 350            # shorter blips (keyboard clacks etc.) are discarded

torch.set_num_threads(1)          # keeps CPU use low on a laptop that runs hot


class SileroListener:
    def __init__(self):
        self.model = load_silero_vad()
        self.muted = False                  # hook this to your mic-mute button
        self._cancel = threading.Event()

    def cancel(self):
        """Makes a pending listen_for_utterance() return None (use on sleep mode / shutdown)."""
        self._cancel.set()

    def is_speech(self, chunk_f32):
        """Drop-in replacement for an energy check. chunk_f32: 512 float32 samples at 16 kHz."""
        with torch.no_grad():
            return float(self.model(torch.from_numpy(chunk_f32), SAMPLE_RATE)) >= SPEECH_ON

    def listen_for_utterance(self, max_seconds=20, on_speech_start=None):
        """Blocks until one full utterance is captured. Returns float32 numpy array, or None."""
        self._cancel.clear()
        self.model.reset_states()
        ms_per_chunk = CHUNK * 1000 / SAMPLE_RATE
        pre = collections.deque(maxlen=int(PRE_ROLL_MS / ms_per_chunk))
        voiced, started, silence_ms, run = [], False, 0.0, 0
        max_chunks = int(max_seconds * SAMPLE_RATE / CHUNK)
        total = 0

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=CHUNK) as stream:
            while not self._cancel.is_set():
                data, _ = stream.read(CHUNK)
                chunk = data[:, 0].copy()
                if self.muted:
                    pre.clear(); voiced.clear(); started = False; run = 0
                    continue
                with torch.no_grad():
                    p = float(self.model(torch.from_numpy(chunk), SAMPLE_RATE))

                if not started:
                    pre.append(chunk)
                    run = run + 1 if p >= SPEECH_ON else 0
                    if run >= START_CHUNKS:
                        started = True
                        voiced = list(pre)
                        silence_ms = 0.0
                        if on_speech_start:
                            on_speech_start()
                else:
                    voiced.append(chunk)
                    total += 1
                    silence_ms = silence_ms + ms_per_chunk if p < SPEECH_OFF else 0.0
                    if silence_ms >= END_SILENCE_MS or total >= max_chunks:
                        audio = np.concatenate(voiced)
                        dur_ms = len(audio) * 1000 / SAMPLE_RATE
                        if dur_ms - silence_ms < MIN_UTTERANCE_MS:
                            pre.clear(); voiced.clear(); started = False; run = 0
                            self.model.reset_states()
                            continue
                        return audio
        return None


def to_int16_wav_bytes(audio_f32):
    """Handy if your speech-to-text step wants WAV bytes instead of a numpy array."""
    import io, wave
    pcm = (np.clip(audio_f32, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


listener = None      # created lazily so importing this file doesn't load torch until needed


def get_listener():
    global listener
    if listener is None:
        listener = SileroListener()
    return listener