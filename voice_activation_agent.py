"""
Voice lock for Jarvis: learns YOUR voice and ignores everyone else.

voice.py imports VoiceRecognitionAgent from this file and uses:
    available, has_voice, lock_active, set_lock(on), enroll(clips), check(audio, sr), forget_voice()

Needs:  pip install resemblyzer --no-deps   and   pip install torch
(see the notes Jarvis gave you; webrtcvad-wheels, librosa, numpy and scipy are already in requirements.txt)

The heavy libraries (torch, the speaker model) are only loaded the first time they are needed, so
Jarvis starts as fast as before when no voice is registered.
"""
import importlib
import importlib.util
import os
import pickle
import threading

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
VOICE_PROFILE_FILE = os.path.join(BASE, "known_voice.pkl")
SIMILARITY_THRESHOLD = 0.75   # cosine similarity needed to count as you. Lower = more forgiving.
MIN_SECONDS = 0.8             # clips shorter than this can't be judged reliably, so they are let through


class VoiceRecognitionAgent:
    def __init__(self):
        self.known_features = None
        self.threshold = SIMILARITY_THRESHOLD
        self._lock_on = False
        self._encoder = None
        self._preprocess = None
        self._mutex = threading.Lock()
        # checked once, without importing torch: just "is it installed?"
        self._avail = all(importlib.util.find_spec(n) is not None for n in ("resemblyzer", "torch"))
        self._load()

    # ------------------------------------------------------------------ state
    @property
    def available(self):
        return self._avail

    @property
    def has_voice(self):
        return self.known_features is not None

    @property
    def lock_active(self):
        return self._lock_on and self.has_voice and self._avail

    def set_lock(self, on):
        self._lock_on = bool(on)
        if self.has_voice:
            self._save()

    # ------------------------------------------------------------ persistence
    def _load(self):
        if not os.path.exists(VOICE_PROFILE_FILE):
            return
        try:
            with open(VOICE_PROFILE_FILE, "rb") as f:
                data = pickle.load(f)
            feats = data.get("features")
            if feats is not None:
                feats = np.asarray(feats, dtype=np.float32).reshape(-1)
                n = np.linalg.norm(feats)
                self.known_features = feats / n if n > 0 else None
            self._lock_on = bool(data.get("lock", False))
        except Exception as e:
            print(f"Error loading voice profile: {e}")

    def _save(self):
        tmp = VOICE_PROFILE_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"features": self.known_features, "lock": self._lock_on}, f)
        os.replace(tmp, VOICE_PROFILE_FILE)

    def forget_voice(self):
        self.known_features = None
        self._lock_on = False
        try:
            if os.path.exists(VOICE_PROFILE_FILE):
                os.remove(VOICE_PROFILE_FILE)
        except Exception as e:
            print(f"Couldn't delete the voice profile: {e}")

    # -------------------------------------------------------------- embedding
    def _ensure_models(self):
        if self._encoder is None:
            r = importlib.import_module("resemblyzer")
            self._preprocess = r.preprocess_wav
            self._encoder = r.VoiceEncoder(device="cpu", verbose=False)

    def _embed(self, audio, sr=16000):
        """Unit-length 256-number voice print of one clip (float audio, -1..1), or None if too short."""
        with self._mutex:
            self._ensure_models()
            wav = self._preprocess(np.asarray(audio, dtype=np.float32).reshape(-1), source_sr=sr)
            if len(wav) < int(16000 * MIN_SECONDS):
                return None
            emb = self._encoder.embed_utterance(wav)
        n = float(np.linalg.norm(emb))
        return (emb / n).astype(np.float32) if n > 0 else None

    # ------------------------------------------------------------- the API
    def enroll(self, clips):
        """Averages several spoken clips into one voice print and turns the lock on. -> (ok, message)"""
        if not self._avail:
            return False, "resemblyzer and torch are not installed."
        embs = []
        for clip in clips:
            try:
                e = self._embed(clip, 16000)
            except Exception as ex:
                return False, f"The voice model failed to load: {ex}"
            if e is not None:
                embs.append(e)
        if len(embs) < 2:
            return False, "I couldn't pick up enough clear speech. Try again somewhere quieter."
        mean = np.mean(embs, axis=0)
        n = float(np.linalg.norm(mean))
        if n <= 0:
            return False, "I couldn't build a voice print from those clips."
        self.known_features = (mean / n).astype(np.float32)
        self._lock_on = True
        self._save()
        return True, f"Voice print saved from {len(embs)} clips."

    def check(self, audio, sr=16000):
        """Is this clip you? -> (accepted, score or None, note). Fails open: if anything goes wrong
        the clip is accepted, so a glitch can never lock you out of your own assistant."""
        if not self.lock_active:
            return True, None, "lock off"
        try:
            emb = self._embed(audio, sr)
        except Exception as e:
            return True, None, f"check failed ({e}), allowed"
        if emb is None:
            return True, None, "too short to judge"
        score = float(np.dot(emb, self.known_features))
        ok = score >= self.threshold
        return ok, score, "your voice" if ok else "different voice"