"""vision.py - lets Jarvis see: screen capture, webcam capture, and a local vision model (via Ollama).

Nothing here runs by itself. Images are only taken when you press SEE SCREEN / CAMERA or ask for it,
and they go only to the Ollama server on your own computer."""
import base64, ctypes, io, os, re, shutil, time
import requests
from PIL import Image

try:                                   # optional: without it Jarvis still starts, just without OCR
    import pytesseract
except ImportError:
    pytesseract = None

_TESS_PATHS = (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
               r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
               os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"))



def grab_screen_raw():
    """Screenshot of the main monitor in real pixels (also on 125% / 150% scaled displays)."""
    from PIL import ImageGrab
    u, old = None, None
    try:
        u = ctypes.windll.user32
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        old = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))     # per-monitor v2, this thread only
    except Exception:
        u = None
    try:
        return ImageGrab.grab().convert("RGB")
    finally:
        if u is not None and old:
            try:
                u.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


def grab_camera(index=0):
    """One photo from the webcam. The camera light is on only for about a second."""
    try:
        import cv2
    except ImportError:
        raise RuntimeError("The camera needs OpenCV. Run: pip install opencv-python")
    cap = None
    try:
        for backend in (getattr(cv2, "CAP_DSHOW", 0), 0):             # DirectShow first, then default
            cap = cv2.VideoCapture(index, backend)
            if cap.isOpened():
                break
            cap.release()
            cap = None
        if cap is None:
            raise RuntimeError("I can't open the camera. Another app may be using it, or Windows is "
                               "blocking it (Settings > Privacy & security > Camera > "
                               "allow desktop apps).")
        frame = None
        for _ in range(10):                                           # let the exposure settle
            ok, f = cap.read()
            if ok:
                frame = f
            time.sleep(0.06)
        if frame is None:
            raise RuntimeError("The camera opened but gave me no picture.")
        return Image.fromarray(frame[:, :, ::-1])                     # BGR -> RGB
    finally:
        if cap is not None:
            cap.release()


def to_b64(img, max_side=896, quality=85):
    img = img.convert("RGB")
    w, h = img.size
    k = max_side / max(w, h)
    if k < 1:
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def extract_text(img, max_chars=1500):
    """Text found on an image with Tesseract OCR. Returns "" (never an error message) if OCR is
    unavailable or finds nothing, because the result is fed straight into the vision prompt and used
    to be able to contain text like "OCR Error: tesseract is not installed". Capped so the small
    vision model is not flooded."""
    if pytesseract is None:
        print("OCR skipped: run  pip install pytesseract  and install Tesseract-OCR")
        return ""
    try:
        if not shutil.which("tesseract"):                 # not on PATH: look in the usual install folders
            for p in _TESS_PATHS:
                if os.path.isfile(p):
                    pytesseract.pytesseract.tesseract_cmd = p
                    break
        text = pytesseract.image_to_string(img.convert("L")).strip()
    except Exception as e:
        print(f"OCR error: {e}")
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def describe(url, model, prompt, img, keep_alive="30s", max_side=896, timeout=300):

    """Asks the local vision model about an image and returns its answer as text."""
    try:
        r = requests.post(f"{url}/api/chat", json={
            "model": model, "stream": False, "keep_alive": keep_alive,
            "messages": [{"role": "user", "content": prompt, "images": [to_b64(img, max_side)]}],
            "options": {"num_predict": 220, "temperature": 0.2}}, timeout=timeout)
        data = r.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"I could not reach the vision model ({e}).")
    if "error" in data:
        msg = str(data["error"])
        if "not found" in msg.lower():
            raise RuntimeError(f"The vision model is not installed. In a Command Prompt run: ollama pull {model}")
        raise RuntimeError(msg)
    return (data.get("message", {}).get("content") or "").strip()