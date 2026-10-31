import os
import pickle
import importlib
import numpy as np

try:
    resemblyzer = importlib.import_module("resemblyzer")
    VoiceEncoder = resemblyzer.VoiceEncoder
    preprocess_wav = resemblyzer.preprocess_wav
    HAS_RESEMBLYZER = True
except ImportError:
    HAS_RESEMBLYZER = False

BASE = os.path.dirname(os.path.abspath(__file__))
VOICE_PROFILE_FILE = os.path.join(BASE, "known_voice.pkl")
SIMILARITY_THRESHOLD = 0.75  # Resemblyzer sweet spot (0.70 - 0.80)

class VoiceRecognitionAgent:
    def __init__(self):
        self.known_features = None
        self.encoder = None
        if HAS_RESEMBLYZER:
            self.encoder = VoiceEncoder()
        self.load_voice_profile()

    @property
    def available(self):
        return HAS_RESEMBLYZER

    @property
    def has_voice(self):
        return self.available and self.known_features is not None

    def load_voice_profile(self):
        if os.path.exists(VOICE_PROFILE_FILE):
            try:
                with open(VOICE_PROFILE_FILE, "rb") as f:
                    data = pickle.load(f)
                self.known_features = data.get("features", None)
            except Exception as e:
                print(f"Error loading voice profile: {e}")

    def save_voice_profile(self, features):
        self.known_features = features
        tmp = VOICE_PROFILE_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"features": features}, f)
        os.replace(tmp, VOICE_PROFILE_FILE)

    def _extract_features(self, audio_data, sr=16000):
        """Extracts a 256-d deep speaker embedding using Resemblyzer."""
        if not HAS_RESEMBLYZER:
            raise RuntimeError("resemblyzer is not installed.")
        
        # Ensure float32 normalized audio
        y = audio_data.astype(np.float32)
        if np.max(np.abs(y)) > 1.0:
            y = y / 32768.0  # Normalize int16 range if raw PCM is passed
            
        # Preprocess waveform to 16kHz mono as expected by Resemblyzer
        wav = preprocess_wav(y, source_sr=sr)
        
        # Generate 256-dimensional embedding vector
        embedding = self.encoder.embed_utterance(wav)
        return embedding

    def register_user_voice(self, audio_data, sr=16000):
        if not self.available:
            return False, "Resemblyzer is not installed. Run: pip install resemblyzer webrtcvad-wheels"
        try:
            features = self._extract_features(audio_data, sr)
            self.save_voice_profile(features)
            return True, "Voice profile registered successfully with Resemblyzer."
        except Exception as e:
            return False, str(e)

    def verify_speaker(self, audio_data, sr=16000):
        if not self.has_voice:
            return True  # If no voice profile is set up yet, allow it
        try:
            features = self._extract_features(audio_data, sr)
            # Compute cosine similarity between 256-d feature vectors
            sim = np.dot(features, self.known_features) / (
                np.linalg.norm(features) * np.linalg.norm(self.known_features)
            )
            print(f"🔍 Voice Match Score: {sim:.2f}")
            return sim >= SIMILARITY_THRESHOLD
        except Exception as e:
            print(f"Voice verification error: {e}")
            return False