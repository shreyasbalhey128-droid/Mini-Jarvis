import os
import pickle
import numpy as np
import vision

# face_recognition needs dlib, which is the hardest package to install on Windows.
# If it is missing, Jarvis should still start; only the face features switch off.
try:
    import face_recognition
except ImportError:
    face_recognition = None

# Next to this file (the old relative path depended on where jarvis.bat was started from,
# so the saved face could "disappear" when launched from a different folder)
BASE = os.path.dirname(os.path.abspath(__file__))
ENCODINGS_FILE = os.path.join(BASE, "known_faces.pkl")
TOLERANCE = 0.5          # lower = stricter. 0.6 is the library default and lets lookalikes through
MAX_SIDE = 480           # frames are shrunk before analysis: much faster and cooler on a laptop


class FaceRecognitionAgent:
    def __init__(self):
        self.known_encodings = []
        self.known_names = []
        self.load_known_faces()

    @property
    def available(self):
        return face_recognition is not None

    @property
    def has_faces(self):
        """True only if the library is installed AND someone has been registered."""
        return self.available and bool(self.known_encodings)

    def load_known_faces(self):
        """Loads stored face encodings from disk."""
        if os.path.exists(ENCODINGS_FILE):
            try:
                with open(ENCODINGS_FILE, "rb") as f:
                    data = pickle.load(f)
                self.known_encodings = data.get("encodings", [])
                self.known_names = data.get("names", [])
            except Exception as e:
                print(f"Error loading known faces: {e}")

    def save_known_face(self, name, encoding):
        """Saves a new face encoding to disk (written to a temp file first so it cannot be corrupted)."""
        self.known_encodings.append(encoding)
        self.known_names.append(name)
        tmp = ENCODINGS_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"encodings": self.known_encodings, "names": self.known_names}, f)
        os.replace(tmp, ENCODINGS_FILE)

    @staticmethod
    def _to_array(img):
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        return np.ascontiguousarray(np.array(img))

    def register_user(self, name):
        """Takes one photo and registers the face in it."""
        if not self.available:
            return False, "Face recognition is not installed. Run: pip install face_recognition"
        try:
            rgb = self._to_array(vision.grab_camera())
        except Exception as e:
            return False, str(e)
        locations = face_recognition.face_locations(rgb)
        if not locations:
            return False, "No face detected. Please try again."
        if len(locations) > 1:
            return False, "I can see more than one face. Please make sure only you are in front of the camera."
        encodings = face_recognition.face_encodings(rgb, locations)
        if not encodings:
            return False, "I could not read your face clearly. Please try again."
        self.save_known_face(name, encodings[0])
        return True, f"Successfully registered {name}."

    def identify_user(self):
        """Returns the name of the best-matching known person in front of the camera, or None.
        The camera is not even opened if nobody has been registered."""
        if not self.has_faces:
            return None
        try:
            rgb = self._to_array(vision.grab_camera())
            locations = face_recognition.face_locations(rgb)
            if not locations:
                return None
            known = np.array(self.known_encodings)
            for enc in face_recognition.face_encodings(rgb, locations):
                dist = face_recognition.face_distance(known, enc)
                best = int(np.argmin(dist))            # closest match, not just the first one under the limit
                if dist[best] <= TOLERANCE:
                    return self.known_names[best]
        except Exception as e:
            print(f"Face recognition error: {e}")
        return None