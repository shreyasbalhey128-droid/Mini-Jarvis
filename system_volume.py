"""system_volume.py - lets Jarvis change the Windows volume by voice ("crank up the volume", ...).

No extra packages: it presses the keyboard's own volume keys, exactly like the keys on your laptop,
so it works with the speakers and with Bluetooth earbuds. Each key press moves Windows' volume by 2 %.
The parser is plain Python and does not need Windows, so it can be tested anywhere.
"""
import re
import sys
import time

VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
KEYEVENTF_KEYUP = 0x0002
STEP = 2                  # percent per key press on Windows 10 / 11
DEFAULT_CHANGE = 10       # "turn it up"
SMALL_CHANGE = 5          # "a bit", "a little", "a notch"
BIG_CHANGE = 25           # "crank it up", "way up", "a lot"
MAX_CHANGE = 50           # one command never moves it by more than this (kind to your ears with earbuds)


class SystemVolume:
    def __init__(self):
        self.available = sys.platform == "win32"

    def _press(self, vk, times=1):
        import ctypes
        user32 = ctypes.windll.user32
        for _ in range(max(0, int(times))):
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.006)                       # tiny gap so Windows does not drop presses

    def change(self, delta):
        """Raise (+) or lower (-) the volume by about `delta` percent."""
        presses = round(abs(delta) / STEP)
        self._press(VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN, presses)

    def set(self, percent):
        """Exact level: go to 0 (or 100) first, then climb to the target."""
        percent = max(0, min(100, int(percent)))
        if percent >= 100:
            self._press(VK_VOLUME_UP, 50)
        else:
            self._press(VK_VOLUME_DOWN, 50)
            self._press(VK_VOLUME_UP, round(percent / STEP))

    def toggle_mute(self):
        """Windows only offers a mute TOGGLE key, so this flips whatever the state is."""
        self._press(VK_VOLUME_MUTE, 1)


# ---------------------------------------------------------------- understanding the sentence
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
UP_WORDS = {"up", "raise", "increase", "boost", "crank", "pump", "blast", "louder", "higher", "bigger"}
DOWN_WORDS = {"down", "lower", "decrease", "reduce", "drop", "quieter", "softer", "lessen"}
VOICE_WORDS = {"voice", "your", "yourself", "jarvis", "speak", "speaking", "talk", "talking"}
VOLUME_WORDS = {"volume", "vol", "sound", "music"}


_WHO = r"(?:(?:system|pc|computer|laptop|voice|your)\s+)*"
SET_FORM = re.compile(
    r"^(?:(?:set|make|put|change|turn|bring|crank)\s+)?(?:the\s+|my\s+)?" + _WHO +
    r"(?:volume|vol|sound|music)\s+(?:(?:to|at|on)\s+)?(?:[a-z0-9]+\s?){1,3}%?$"
    r"|^(?:set\s+|make\s+)?(?:the\s+)?(?:max|maximum|full|minimum|lowest|half)\s+" + _WHO + r"(?:volume|vol)$")


def _number(text):
    """First number in the text: digits, or words like 'seventy five', 'half', 'max', 'zero'."""
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        return min(100, int(m.group(1)))
    toks = text.split()
    if "hundred" in toks or any(w in toks for w in ("max", "maximum", "full")):
        return 100
    if "half" in toks:
        return 50
    if any(w in toks for w in ("min", "minimum", "lowest")):
        return 0
    for i, w in enumerate(toks):
        if w in _TENS:
            n = _TENS[w]
            if i + 1 < len(toks) and 1 <= _UNITS.get(toks[i + 1], 0) <= 9:
                n += _UNITS[toks[i + 1]]
            return n
        if w in _UNITS:
            return _UNITS[w]
    return None


def parse_volume_command(c):
    """Turns a spoken/typed command into an action, or None if it is not about volume.

    {"target": "system" | "voice",       # the PC's volume, or Jarvis's own speaking voice
     "op": "up" | "down" | "set" | "mute",
     "amount": percent (change for up/down, level for set), "big": True for 'crank it up'}
    """
    t = " ".join(re.sub(r"[^a-z0-9% ]", " ", c.lower()).split())
    toks = set(t.split())
    if not toks:
        return None
    if re.fullmatch(r"(?:un)?mute (?:the |my )?(?:computer|pc|laptop|system|windows|sound|speakers?)"
                    r"(?: volume)?", t):
        return {"target": "system", "op": "mute", "amount": None, "big": False}

    vol_word = bool(toks & VOLUME_WORDS)
    it_phrase = re.search(r"\b(?:crank|turn|pump|bring|push|blast|boost) it (?:up|down)\b", t) is not None
    voice_word = bool(toks & VOICE_WORDS)
    up, down = bool(toks & UP_WORDS), bool(toks & DOWN_WORDS)
    if not (vol_word or it_phrase or {"speak", "talk"} & toks):
        return None
    if up and down:
        return None
    target = "voice" if voice_word else "system"
    num = _number(t)
    has_by = re.search(r"\bby\b", t) is not None
    has_to = re.search(r"\b(?:to|at)\b", t) is not None

    # absolute level: "set the volume to 40", "volume 30", "max volume", "turn it up to 80"
    if vol_word and num is not None and not has_by:
        if up or down:
            is_set = has_to                                    # "turn the volume up to 80"
        else:                                                  # must be SHORT and command-shaped, so
            is_set = SET_FORM.match(t) is not None             # "volume of the box is 20" is ignored
        if is_set:
            return {"target": target, "op": "set", "amount": num, "big": False}
    if not (up or down):
        return None

    if num is not None and (has_by or not has_to):
        amount = num                                          # "by 20", "volume up 20"
    elif re.search(r"\b(?:bit|little|slightly|touch|tad|notch|tiny)\b", t):
        amount = SMALL_CHANGE
    elif re.search(r"\b(?:crank|blast|pump|boost|way|lot|much|massively)\b", t):
        amount = BIG_CHANGE
    else:
        amount = DEFAULT_CHANGE
    amount = max(STEP, min(MAX_CHANGE, amount))
    return {"target": target, "op": "up" if up else "down", "amount": amount, "big": amount >= BIG_CHANGE}