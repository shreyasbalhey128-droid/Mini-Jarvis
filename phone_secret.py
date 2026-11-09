"""
Keeps your phone's PIN / password encrypted on this laptop, using Windows' own protection (DPAPI).
Only YOUR Windows account on THIS machine can decrypt it. The PIN is never written to voice.py, the log or any
conversation, never sent to the language model, and never spoken. Set it with phone_pin_setup.py, not by voice.
"""
import ctypes
import os
import sys
from ctypes import wintypes

PATH = os.path.join(os.path.expanduser("~"), ".jarvis_phone_pin.bin")
_ENTROPY = b"jarvis-phone-pin-v1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(data, protect):
    if sys.platform != "win32":
        raise RuntimeError("Encrypted PIN storage needs Windows.")
    inb, keep1 = _blob(data)
    ent, keep2 = _blob(_ENTROPY)
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    args = (ctypes.byref(inb), "Jarvis phone PIN" if protect else None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    if not fn(*args):
        raise RuntimeError("Windows could not " + ("encrypt" if protect else "decrypt") + " the PIN.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _protect(data):
    return _crypt(data, True)


def _unprotect(data):
    return _crypt(data, False)


def save_pin(pin):
    with open(PATH, "wb") as f:
        f.write(_protect(pin.encode("utf-8")))


def load_pin():
    """Returns the PIN, or None if none is saved / it can't be decrypted."""
    try:
        with open(PATH, "rb") as f:
            return _unprotect(f.read()).decode("utf-8")
    except Exception:
        return None


def has_pin():
    return os.path.exists(PATH)


def clear_pin():
    try:
        os.remove(PATH)
    except FileNotFoundError:
        pass