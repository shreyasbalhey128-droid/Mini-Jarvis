"""
Phone agent: Jarvis operates an ANDROID phone over USB (through ADB) whenever it is plugged into the laptop.

  * notices when the phone is plugged in / unplugged
  * announces incoming calls ("Incoming call from Mom") and can answer, decline or end them
  * makes calls by contact name or number, and can switch the speaker on during a call

One-time phone setup:  Settings > About phone > tap "Build number" 7 times, then
Settings > Developer options > turn on USB debugging. Plug in, and tap "Allow" on the phone.
(Samsung/Xiaomi/Oppo etc. may also need "USB debugging (Security settings)" for tapping buttons.)
ADB itself is downloaded automatically (about 12 MB) into a platform-tools folder next to voice.py.

Honest limits: the call's AUDIO stays on the phone (earpiece / speaker / Bluetooth headset). Windows cannot be a
phone's speaker and mic over USB. iPhones do not allow any of this; it is Android only.
"""
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from difflib import SequenceMatcher
from urllib.parse import quote

BASE = os.path.dirname(os.path.abspath(__file__))
ADB_DIR = os.path.join(BASE, "platform-tools")
ADB_ZIP_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VERSION = "2026-11-07-contacts"      # voice.py checks this, so a mismatched pair of files shows up in the log
POLL_SECONDS = 1.2           # how often the call state is checked while a phone is connected (cheap)


# ------------------------------------------------------------------ adb
def find_adb():
    return shutil.which("adb") or next(
        (p for p in (os.path.join(ADB_DIR, "adb.exe"), os.path.join(ADB_DIR, "adb")) if os.path.exists(p)), None)


def download_adb():
    """Fetches Google's platform-tools (adb) once. Returns the path to adb or None."""
    try:
        zpath = os.path.join(BASE, "platform-tools.zip")
        urllib.request.urlretrieve(ADB_ZIP_URL, zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(BASE)
        os.remove(zpath)
    except Exception as e:
        print("Could not download adb:", e)
    return find_adb()


# ------------------------------------------------------------------ pure parsers (unit-tested)
def parse_devices(out):
    """adb devices output -> (state, serial). state: device / unauthorized / offline / none."""
    found = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+(device|unauthorized|offline)\s*(?:\S.*)?$", line.strip())
        if m and not line.startswith("*") and not line.startswith("List of"):
            found.setdefault(m.group(2), m.group(1))
    for st in ("device", "unauthorized", "offline"):
        if st in found:
            return st, found[st]
    return "none", None


def parse_call_state(out):
    """dumpsys telephony.registry lines -> (state 0 idle / 1 ringing / 2 in call, incoming number or '')."""
    states = [int(x) for x in re.findall(r"mCallState=(\d)", out)]
    nums = [n.strip() for n in re.findall(r"mCallIncomingNumber=(\S*)", out) if n.strip() not in ("", "null")]
    return (max(states) if states else 0), (nums[0] if nums else "")


def parse_contacts(out):
    """content query rows -> [(name, number)]"""
    rows = []
    for line in out.splitlines():
        m = re.search(r"display_name=(.*?), data1=(.*)$", line.strip())
        if m:
            rows.append((m.group(1).strip(), m.group(2).strip()))
    return rows


def parse_ui_nodes(xml_text):
    """uiautomator dump -> [{label, bounds(x1,y1,x2,y2), checked}]"""
    nodes = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return nodes
    for n in root.iter("node"):
        label = " ".join(filter(None, (n.get("text", ""), n.get("content-desc", "")))).strip()
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
        if label and m:
            nodes.append({"label": label.lower(), "bounds": tuple(int(g) for g in m.groups()),
                          "checked": n.get("checked") == "true", "clickable": n.get("clickable") == "true"})
    return nodes


def _words(text):
    return re.sub(r"[\W_]+", " ", text.lower()).split()          # hyphens, dots, emoji and extra spaces all just separate words


def _score(spoken, name):
    """How well a spoken name matches a contact name. An exact match (ignoring spaces and hyphens) always wins (3.0)."""
    qw, nw = _words(spoken), _words(name)
    if not qw or not nw:
        return 0.0
    qs, ns = "".join(qw), "".join(nw)
    if qs == ns:
        return 3.0
    extra = max(len(nw) - len(qw), 0)
    score = max(SequenceMatcher(None, " ".join(qw), " ".join(nw)).ratio(), SequenceMatcher(None, qs, ns).ratio())
    if set(qw) <= set(nw):                                     # every spoken word is in the name ("rahul" in "Rahul Sharma")
        score = max(score, 0.9 - 0.03 * extra)
    elif len(qw) == 1:
        score = max(score, 0.9 * max(SequenceMatcher(None, qs, w).ratio() for w in nw) - 0.03 * extra)
    return score - 0.01 * extra                                # on a tie, the shorter name wins


def ranked_contacts(contacts, spoken, limit=3):
    """Best matches first, one entry per contact name: [(name, number), ...]. An exact match is returned alone."""
    q = "".join(_words(spoken))
    if not q:
        return []
    need = 0.66 if len(q) <= 4 else 0.72                       # short names (mum/mom) need a looser match
    seen, scored = set(), []
    for name, number in contacts:
        key = " ".join(_words(name))
        if not key or key in seen:
            continue
        seen.add(key)
        sc = _score(spoken, name)
        if sc >= need:
            scored.append((sc, len(_words(name)), name, number))
    scored.sort(key=lambda t: (-t[0], t[1]))
    if not scored:
        return []
    if scored[0][0] >= 3.0:
        return [(scored[0][2], scored[0][3])]
    return [(n, num) for sc, _, n, num in scored[:limit] if sc >= scored[0][0] - 0.25]


def best_contact(contacts, spoken):
    """The single best match as (name, number), or None."""
    r = ranked_contacts(contacts, spoken, limit=1)
    return r[0] if r else None


_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_MULT = {"double": 2, "triple": 3}


def spoken_to_number(text):
    """'nine eight double seven 1 2 3' / '+91 98765 43210' -> digits, or None if it isn't a phone number."""
    toks, out, mult = text.lower().replace("-", " ").split(), "", 1
    plus = text.strip().startswith("+") or toks[:1] == ["plus"]
    for t in toks:
        if t == "plus":
            continue
        if t in _MULT:
            mult = _MULT[t]
            continue
        d = _DIGIT_WORDS.get(t) or (re.sub(r"\D", "", t) if re.fullmatch(r"\+?[\d]+", t) else "")
        if not d:
            return None
        out += d * mult
        mult = 1
    return ("+" if plus else "") + out if len(out) >= 3 else None


# ---- command grammar (c = the already-cleaned lowercase command) ----
_ANSWER = re.compile(r"^(?:answer|accept|pick up|take)\b(?: the| this| that| my| it)*(?: incoming)?(?: phone)?(?: call)?$")
_END = re.compile(r"^(?:decline|reject|ignore|dismiss|silence|hang up|hang up on|end|cut|disconnect|drop|finish|cancel)\b"
                  r"(?: the| this| that| my| it| him| her| them)*(?: incoming| phone)*(?: call| phone)?$")
_CALL = re.compile(r"^(?:please )?(?:make a (?:phone )?call to|place a call to|call|phone|ring|dial|give)\s+(.+?)(?: a call| a ring)?$")
_STATUS = re.compile(r"\b(?:is my phone (?:connected|plugged in)|phone status|check (?:my )?phone|is the phone connected|"
                     r"(?:why )?(?:can'?t|cannot|couldn'?t) (?:you )?see my phone|phone (?:not|isn'?t) (?:connected|detected|working)|"
                     r"diagnose (?:my )?phone|fix (?:my )?phone connection)\b")


def parse_command(c):
    """-> (action, arg) with action in answer / end / call / speaker / status, or None."""
    if c in ("answer", "pick up", "answer it", "pick it up", "answer that", "answer the phone", "pick up the phone"):
        return ("answer", None)
    if _ANSWER.match(c) and re.search(r"\b(call|phone)\b", c):
        return ("answer", None)
    if c in ("decline", "reject", "decline it", "reject it", "hang up", "end it") or \
            (_END.match(c) and re.search(r"\b(call|phone|hang up)\b", c)):
        return ("end", None)
    if "speaker" in c and re.search(r"\b(on speaker|speaker ?phone|speaker on|turn on (?:the )?speaker|"
                                    r"put (?:it|him|her|them|the call) on speaker)\b", c):
        return ("speaker", None)
    x = _parse_texts(c) or _parse_extras(c)
    if x:
        return x
    if _STATUS.search(c):
        return ("status", None)
    m = _CALL.match(c)
    if m and m.group(1):
        who = m.group(1).strip()
        words = who.split()
        if words[0] in ("it", "me", "that", "this", "a", "an", "the", "off", "out", "back", "for", "my", "us") \
                or (len(words) > 4 and spoken_to_number(who) is None):
            return None                      # "call it a day", "call me later" are just talk
        return ("call", who)
    return None


# ------------------------------------------------------------------ extras: battery, screenshots, photos, apps, media, toggles
PICTURES = os.path.join(os.path.expanduser("~"), "Pictures", "Jarvis")

KNOWN_APPS = {          # spoken name -> package, or ("action", intent) for system screens
    "instagram": "com.instagram.android", "whatsapp": "com.whatsapp", "youtube": "com.google.android.youtube",
    "chrome": "com.android.chrome", "gmail": "com.google.android.gm", "maps": "com.google.android.apps.maps",
    "google maps": "com.google.android.apps.maps", "photos": "com.google.android.apps.photos",
    "spotify": "com.spotify.music", "telegram": "org.telegram.messenger", "snapchat": "com.snapchat.android",
    "facebook": "com.facebook.katana", "messenger": "com.facebook.orca", "twitter": "com.twitter.android",
    "x": "com.twitter.android", "netflix": "com.netflix.mediaclient", "play store": "com.android.vending",
    "google": "com.google.android.googlequicksearchbox", "drive": "com.google.android.apps.docs",
    "calendar": "com.google.android.calendar", "clock": "com.google.android.deskclock",
    "calculator": "com.google.android.calculator", "contacts": "com.google.android.contacts",
    "messages": "com.google.android.apps.messaging", "phonepe": "com.phonepe.app", "paytm": "net.one97.paytm",
    "google pay": "com.google.android.apps.nbu.paisa.user", "gpay": "com.google.android.apps.nbu.paisa.user",
    "linkedin": "com.linkedin.android", "discord": "com.discord", "reddit": "com.reddit.frontpage",
    "pinterest": "com.pinterest",
    "camera": ("action", "android.media.action.STILL_IMAGE_CAMERA"),
    "settings": ("action", "android.settings.SETTINGS"),
    "dialer": ("action", "android.intent.action.DIAL"),
}
_URL = re.compile(r"^(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:[/?#]\S*)?$", re.I)
_BAD = re.compile(r"Exception|Error|not found|Unknown command|Permission Denial|No activities", re.I)
_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_TOGGLES = {"wifi": "wifi", "wi-fi": "wifi", "wi fi": "wifi", "bluetooth": "bluetooth", "airplane mode": "airplane",
            "airplane": "airplane", "aeroplane mode": "airplane", "flight mode": "airplane",
            "do not disturb": "dnd", "dnd": "dnd"}
_MEDIA = re.compile(r"^(pause|resume|play|stop|next|skip|previous|mute|unmute)(?: the)?"
                    r"(?: music| song| track| video| playback| this| it)*\s+on (?:my|the) phone$")
_OPEN = re.compile(r"^(?:open|launch|start|show)\s+(.+?)\s+on\s+(?:my|the)\s+phone$")
_TOGGLE_A = re.compile(r"^(?:turn|switch)\s+(on|off)\s+(?:the\s+)?(.+?)\s+on\s+(?:my|the)\s+phone$")
_TOGGLE_B = re.compile(r"^(?:turn|switch)\s+(on|off)\s+(?:my\s+)?phone(?:'s)?\s+(.+)$")
_TOGGLE_C = re.compile(r"^(enable|disable)\s+(?:the\s+)?(.+?)\s+on\s+(?:my|the)\s+phone$")


def _adb_text(text):
    """Escapes text for `input text` on the phone (spaces become %s, shell characters get a backslash)."""
    return re.sub(r"([&|;<>()$`\\\"'*?#~!{}\[\]])", r"\\\1", text).replace(" ", "%s")


def parse_battery(out):
    """dumpsys battery -> (level, charging) or None"""
    m = re.search(r"level:\s*(\d+)", out)
    if not m:
        return None
    status = re.search(r"status:\s*(\d+)", out)
    plugged = re.search(r"(?:AC|USB|Wireless) powered:\s*true", out)
    return int(m.group(1)), bool((status and status.group(1) in ("2", "5")) or plugged)


_TEXTS = re.compile(r"^(?:please\s+)?(?:read|check|show|tell me|any|do i have|have i got|did i get)\b.*?"
                    r"\b(?:my|any|new|unread|latest|last|recent)\b.*?\b(?:texts?|sms|text messages?)\b(.*)$")


def _parse_texts(c):
    """'read my texts', 'any new texts', 'read my last text from mom', 'read the last 3 texts'"""
    m = _TEXTS.match(c)
    if not m:
        return None
    unread = bool(re.search(r"\b(new|unread|any|do i have|did i get|have i got)\b", c))
    nm = re.search(r"\b(\d+|" + "|".join(_NUMWORDS) + r")\b", c)
    if nm:
        n = int(nm.group(1)) if nm.group(1).isdigit() else _NUMWORDS[nm.group(1)]
    elif re.search(r"\b(latest|last|recent)\s+(?:text|sms|text message)\b", c):
        n = 1
    else:
        n = 5 if unread else 3
    who = re.search(r"\bfrom\s+(.+?)\s*$", m.group(1))
    return ("texts", {"unread": unread, "n": max(1, min(n, 10)), "sender": who.group(1) if who else None})


def _parse_extras(c):
    """Phone-only commands. Every one of them names the phone, so they can't be mistaken for laptop commands."""
    if "phone" not in c:
        return None
    m = re.match(r"^(?:please\s+)?unlock\s+(?:my|the)\s+phone(\s+with\s+(?:my\s+)?(?:fingerprint|finger|thumb))?$", c)
    if m:
        return ("unlock", "fingerprint" if m.group(1) else None)
    if re.match(r"^(?:set|change|save|store|add|update)\s+(?:my\s+)?phone\s+(?:pin|password|passcode)$", c):
        return ("pin_setup", None)
    if re.match(r"^(?:forget|delete|remove|clear)\s+(?:my\s+)?phone\s+(?:pin|password|passcode)$", c):
        return ("pin_forget", None)
    if re.search(r"\b(battery|charge|charged|charging)\b", c):
        return ("battery", None)
    if re.search(r"screen ?shot|screen capture|capture (?:the )?screen", c) and not re.search(r"\b(send|share)\b", c):
        return ("screenshot", None)
    if re.search(r"\b(photos?|pictures?|pics?)\b", c) and re.search(r"\b(from|off|on)\b.*\bphone\b", c) \
            and re.search(r"\b(copy|get|grab|transfer|download|bring|pull|save|fetch)\b", c):
        m = re.search(r"\b(\d+|" + "|".join(_NUMWORDS) + r")\b", c)
        n = (int(m.group(1)) if m.group(1).isdigit() else _NUMWORDS[m.group(1)]) if m else \
            (1 if re.search(r"\b(photo|picture|pic)\b", c) else 5)
        return ("photos", max(1, min(n, 20)))
    m = _MEDIA.match(c)
    if m:
        w = m.group(1)
        return ("media", {"pause": "playpause", "resume": "playpause", "play": "playpause", "stop": "stop",
                          "next": "next", "skip": "next", "previous": "previous", "mute": "mute", "unmute": "mute"}[w])
    if "volume" in c:
        if re.search(r"\b(up|louder|raise|increase|higher)\b", c):
            return ("volume", 1)
        if re.search(r"\b(down|lower|quieter|reduce|decrease|softer)\b", c):
            return ("volume", -1)
    for rx in (_TOGGLE_A, _TOGGLE_B, _TOGGLE_C):
        m = rx.match(c)
        if m and m.group(2).strip() in _TOGGLES:
            return ("toggle", (_TOGGLES[m.group(2).strip()], m.group(1) in ("on", "enable")))
    if re.match(r"^(?:lock|sleep)\s+(?:my|the)\s+phone$", c) or re.match(r"^turn off (?:my|the) phone(?:'s)? screen$", c):
        return ("screen", "lock")
    if re.match(r"^(?:wake|wake up)\s+(?:my|the)\s+phone$", c) or re.match(r"^turn on (?:my|the) phone(?:'s)? screen$", c):
        return ("screen", "wake")
    m = _OPEN.match(c)
    if m:
        return ("open", m.group(1).strip())
    return None


# ------------------------------------------------------------------ the phone
class Phone:
    def __init__(self, on_connect=None, on_disconnect=None, on_incoming=None,
                 on_call_end=None, on_unauthorized=None, on_status=None):
        self.on_connect, self.on_disconnect, self.on_incoming = on_connect, on_disconnect, on_incoming
        self.on_call_end, self.on_unauthorized, self.on_status = on_call_end, on_unauthorized, on_status
        self.status, self.detail = "starting", "Looking for ADB..."
        self._pin_failures = 0
        self.adb, self.serial, self.name = None, None, ""
        self.connected, self.call_state, self.last_number = False, 0, ""
        self._contacts, self._contacts_at = [], 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- plumbing
    def start(self):
        if self.adb is None:
            self.adb = find_adb()
        threading.Thread(target=self._watch, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self, *args, timeout=10, binary=False):
        cmd = [self.adb] + (["-s", self.serial] if self.serial and args and args[0] != "devices" else []) + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NOWINDOW)
            out = p.stdout if binary else p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
            return p.returncode, out
        except Exception as e:
            return 1, str(e)

    def _poll_state(self):
        rc, out = self._run("shell", 'dumpsys telephony.registry | grep -E "mCallState|mCallIncomingNumber"')
        if rc == 0:
            self.call_state, num = parse_call_state(out)
            if num:
                self.last_number = num
        return self.call_state

    # ---- background watcher
    def _fire(self, fn, *a):
        if fn:
            threading.Thread(target=fn, args=a, daemon=True).start()

    def _set_status(self, status, detail):
        changed = status != self.status
        self.status, self.detail = status, detail
        if changed:
            print(f"Phone: {status} - {detail}")
            if status != "device":
                self._fire(self.on_status, status, detail)

    _TIPS = {
        "no_device": "ADB can't see any phone. Check that: the cable carries data (many cables only charge), the phone's USB mode "
                     "is 'File transfer', USB debugging is on, and Windows has the phone's driver "
                     "(Device Manager should show 'Android Composite ADB Interface' or your phone under Portable Devices).",
        "unauthorized": "The phone is waiting for you: unlock it and tap Allow on the 'Allow USB debugging?' prompt "
                        "(tick 'Always allow'). If no prompt shows, unplug, replug, or revoke USB debugging authorizations in "
                        "Developer options.",
        "offline": "ADB sees the phone but it's offline. Unplug and replug the cable, or switch USB debugging off and on.",
    }

    def explain(self):
        """(spoken reason, details for the log)"""
        st = self.status
        if self.connected:
            return f"Yes, your {self.name} is connected.", ""
        if st == "adb_missing":
            return ("I don't have the ADB tools yet, so I can't talk to the phone. I'm trying to download them; "
                    "the details are in the log."), self.detail
        if st == "adb_error":
            return "ADB itself has a problem. The details are in the log.", self.detail
        if st == "unauthorized":
            return "Your phone is asking to allow USB debugging. Unlock it and tap Allow.", self._TIPS["unauthorized"]
        if st == "offline":
            return "ADB sees your phone but it shows as offline. Unplug it and plug it back in.", self._TIPS["offline"]
        if st == "no_device":
            return ("ADB can't see any phone. Check the cable, and set the USB mode to file transfer. "
                    "More detail is in the log."), self._TIPS["no_device"]
        return "I'm still checking for your phone.", self.detail

    def _watch(self):
        warned, prev_state, last_dl, reset_done = False, 0, 0.0, False
        while not self._stop.is_set():
            if self.adb is None:                          # no adb yet: look for it, and retry the download every minute
                self.adb = find_adb()
                if self.adb is None:
                    if time.time() - last_dl > 60:
                        last_dl = time.time()
                        self._set_status("adb_missing", "Downloading ADB (Google platform-tools)...")
                        self.adb = download_adb()
                        if self.adb is None:
                            self._set_status("adb_missing", "Could not download ADB. Check your internet or firewall, or "
                                             "install Android platform-tools yourself and add adb to your PATH.")
                    time.sleep(3)
                    continue
            rc, out = self._run("devices")
            if rc != 0 or re.search(r"cannot connect|failed to start|mismatch|unable to|error:", out, re.I):
                self._set_status("adb_error", out.strip()[:300] or "adb did not run")
                if not reset_done:                        # a different adb version already running is the usual cause
                    reset_done = True
                    self._run("kill-server")
                time.sleep(3)
                continue
            st, serial = parse_devices(out)
            if st == "device":
                warned = False
                if not self.connected:
                    self.serial, self.connected = serial, True
                    _, model = self._run("shell", "getprop ro.product.model")
                    self.name = model.strip() or "phone"
                    self._set_status("device", "connected")
                    self._fire(self.on_connect, self.name)
                state = self._poll_state()
                if state == 1 and prev_state != 1:
                    if not self.last_number:
                        time.sleep(0.6)
                        self._poll_state()
                    number = self.last_number
                    self._fire(self.on_incoming, number, self.lookup_name(number) if number else None)
                elif prev_state in (1, 2) and state == 0:
                    self._fire(self.on_call_end, "missed" if prev_state == 1 else "ended")
                if state == 0:
                    self.last_number = ""
                prev_state = state
                time.sleep(POLL_SECONDS)
            else:
                if self.connected:
                    self.connected, self.serial, self.call_state, prev_state = False, None, 0, 0
                    self._fire(self.on_disconnect)
                key = "no_device" if st == "none" else st
                self._set_status(key, self._TIPS[key])
                if st == "unauthorized" and not warned:
                    warned = True
                    self._fire(self.on_unauthorized)
                time.sleep(3)

    # ---- contacts
    def contacts(self, max_age=300):
        if time.time() - self._contacts_at > max_age or not self._contacts:
            rc, out = self._run("shell", "content query --uri content://com.android.contacts/data/phones "
                                         "--projection display_name:data1", timeout=25)
            rows = parse_contacts(out) if rc == 0 else []
            if rows:
                self._contacts, self._contacts_at = rows, time.time()
        return self._contacts

    def lookup_name(self, number):
        digits = re.sub(r"[^\d]", "", number)[-10:]
        if not digits:
            return None
        for name, num in self.contacts():
            if re.sub(r"[^\d]", "", num).endswith(digits):
                return name
        return None

    def find_contact(self, spoken):
        return best_contact(self.contacts(), spoken)

    def find_contacts(self, spoken, limit=3):
        """Ranked candidates for a spoken name, so Jarvis can offer the next one if you say no."""
        return ranked_contacts(self.contacts(), spoken, limit)

    # ---- tapping buttons by their label (works on any dialer, as a fallback and for the speaker)
    def _tap_label(self, words, skip_checked=False):
        for _ in range(2):
            self._run("shell", "uiautomator dump /sdcard/jv_ui.xml", timeout=15)
            rc, raw = self._run("exec-out", "cat /sdcard/jv_ui.xml", binary=True, timeout=15)
            nodes = parse_ui_nodes(raw.decode("utf-8", "replace")) if rc == 0 else []
            for n in nodes:
                if any(w in n["label"] for w in words):
                    if skip_checked and n["checked"]:
                        return True
                    x1, y1, x2, y2 = n["bounds"]
                    self._run("shell", f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
                    return True
            time.sleep(0.5)
        return False

    # ---- actions. Each returns a short spoken result.
    def answer(self):
        with self._lock:
            if self._poll_state() != 1:
                return "Nothing is ringing right now."
            self._run("shell", "input keyevent KEYCODE_CALL")
            time.sleep(1.0)
            if self._poll_state() == 2:
                return "Answered."
            self._tap_label(("answer", "accept"))
            time.sleep(1.0)
            return "Answered." if self._poll_state() == 2 else "I couldn't answer it. Your phone may block that from the computer."

    def end(self):
        """Declines a ringing call or ends an active one."""
        with self._lock:
            st = self._poll_state()
            if st == 0:
                return "There's no call right now."
            self._run("shell", "input keyevent KEYCODE_ENDCALL")     # only sent during a call: it can lock the screen otherwise
            time.sleep(1.0)
            if self._poll_state() == 0:
                return "Declined." if st == 1 else "Call ended."
            self._tap_label(("decline", "reject") if st == 1 else ("end call", "hang up", "end"))
            time.sleep(1.0)
            return ("Declined." if st == 1 else "Call ended.") if self._poll_state() == 0 else "I couldn't end it from here."

    def speaker(self):
        with self._lock:
            if self._poll_state() != 2:
                return "There's no active call."
            return "Speaker on." if self._tap_label(("speaker",), skip_checked=True) else "I couldn't find the speaker button."

    def call_number(self, number):
        number = re.sub(r"[^\d+*#]", "", number)
        if len(re.sub(r"\D", "", number)) < 3:
            return False
        uri = "tel:" + quote(number, safe="+*")
        with self._lock:
            rc, out = self._run("shell", "am", "start", "-a", "android.intent.action.CALL", "-d", uri)
            if rc == 0 and not re.search(r"Error|Exception|Permission Denial", out):
                return True
            self._run("shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", uri)      # fallback: open the dialer, press green
            time.sleep(1.5)
            self._run("shell", "input keyevent KEYCODE_CALL")
            time.sleep(1.5)
            return self._poll_state() in (1, 2)

    # ================= extras =================
    @staticmethod
    def _ok(out):
        return not _BAD.search(out or "")

    def battery(self):
        rc, out = self._run("shell", "dumpsys battery")
        info = parse_battery(out) if rc == 0 else None
        if not info:
            return "I couldn't read the battery."
        level, charging = info
        return f"Your {self.name or 'phone'} is at {level} percent" + (" and charging." if charging else ".")

    def screenshot(self):
        """Saves the phone's screen as a PNG in Pictures/Jarvis. Returns the path, or None."""
        rc, data = self._run("exec-out", "screencap", "-p", binary=True, timeout=25)
        if rc != 0 or not data.startswith(b"\x89PNG"):
            return None
        os.makedirs(PICTURES, exist_ok=True)
        path = os.path.join(PICTURES, time.strftime("phone_%Y%m%d_%H%M%S.png"))
        with open(path, "wb") as f:
            f.write(data)
        return path

    def latest_photos(self, n=5):
        """Copies the n newest photos to Pictures/Jarvis/phone photos. Returns (folder, [paths])."""
        names, folder = [], ""
        for folder in ("/sdcard/DCIM/Camera", "/sdcard/DCIM", "/sdcard/Pictures"):
            rc, out = self._run("shell", f"ls -t {folder}")
            names = [x.strip() for x in out.splitlines() if re.search(r"\.(jpe?g|png|heic|webp)$", x.strip(), re.I)]
            if names:
                break
        if not names:
            return None, []
        dest = os.path.join(PICTURES, "phone photos")
        os.makedirs(dest, exist_ok=True)
        got = []
        for name in names[:n]:
            rc, _ = self._run("pull", f"{folder}/{name}", dest, timeout=90)
            if rc == 0:
                got.append(os.path.join(dest, name))
        return dest, got

    def find_app(self, name):
        key = re.sub(r"\s+app$", "", re.sub(r"^the\s+", "", name.lower().strip()))
        if key in KNOWN_APPS:
            return KNOWN_APPS[key]
        return self._search_package(key)

    def _search_package(self, key):
        flat = key.replace(" ", "")
        for flag in ("-3", ""):                              # user-installed apps first, then system ones
            rc, out = self._run("shell", f"pm list packages {flag}".strip())
            pk = [l[8:].strip() for l in out.splitlines() if l.startswith("package:")]
            hits = sorted((p for p in pk if flat in p.lower()), key=len)
            if hits:
                return hits[0]
        return None

    def _launch(self, spec):
        if isinstance(spec, tuple):
            rc, out = self._run("shell", "am", "start", "-a", spec[1])
            return self._ok(out)
        rc, out = self._run("shell", "monkey", "-p", spec, "-c", "android.intent.category.LAUNCHER", "1")
        return "Events injected: 1" in out

    def open_target(self, target):
        t = target.strip()
        if " " not in t and _URL.match(t):
            url = t if t.lower().startswith("http") else "https://" + t
            rc, out = self._run("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
            return f"Opened {t} on your phone." if self._ok(out) else "I couldn't open that link."
        spec = self.find_app(t)
        if spec is not None and self._launch(spec):
            return f"Opened {t} on your phone."
        alt = self._search_package(re.sub(r"^the\s+", "", t.lower()))          # the usual package isn't installed here
        if alt and alt != spec and self._launch(alt):
            return f"Opened {t} on your phone."
        return f"I couldn't find an app called {t} on your phone."

    def media(self, what):
        key = {"playpause": "KEYCODE_MEDIA_PLAY_PAUSE", "next": "KEYCODE_MEDIA_NEXT", "previous": "KEYCODE_MEDIA_PREVIOUS",
               "stop": "KEYCODE_MEDIA_STOP", "mute": "KEYCODE_VOLUME_MUTE"}[what]
        self._run("shell", "input", "keyevent", key)
        return {"playpause": "Done.", "next": "Next track.", "previous": "Previous track.",
                "stop": "Stopped.", "mute": "Toggled mute."}[what]

    def volume(self, direction):
        key = "KEYCODE_VOLUME_UP" if direction > 0 else "KEYCODE_VOLUME_DOWN"
        for _ in range(3):
            self._run("shell", "input", "keyevent", key)
        return "Volume up." if direction > 0 else "Volume down."

    def toggle(self, what, on):
        word = "enable" if on else "disable"
        label = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth", "airplane": "Airplane mode", "dnd": "Do Not Disturb"}[what]
        if what == "wifi":
            rc, out = self._run("shell", "svc", "wifi", word)
        elif what == "bluetooth":
            rc, out = self._run("shell", "svc", "bluetooth", word)
            if not self._ok(out) or rc != 0:
                rc, out = self._run("shell", "cmd", "bluetooth_manager", word)
        elif what == "airplane":
            rc, out = self._run("shell", "cmd", "connectivity", "airplane-mode", word)
            if not self._ok(out) or rc != 0:
                rc, out = self._run("shell", "settings", "put", "global", "airplane_mode_on", "1" if on else "0")
        else:
            rc, out = self._run("shell", "cmd", "notification", "set_dnd", "on" if on else "off")
            if not self._ok(out) or rc != 0:
                rc, out = self._run("shell", "settings", "put", "global", "zen_mode", "1" if on else "0")
        if rc != 0 or not self._ok(out):
            return f"I couldn't change {label} on this phone."
        return f"{label} {'on' if on else 'off'}."

    def screen(self, what):
        self._run("shell", "input", "keyevent", "KEYCODE_WAKEUP" if what == "wake" else "KEYCODE_SLEEP")
        return "Phone screen on." if what == "wake" else "Phone screen off."

    # ================= reading texts (SMS inbox) =================
    @staticmethod
    def ago(ms):
        sec = max(0, time.time() - ms / 1000.0)
        if sec < 90:
            return "just now"
        unit = lambda n, w: f"{n} {w}{'' if n == 1 else 's'} ago"
        if sec < 3600:
            return unit(int(sec // 60), "minute")
        if sec < 86400:
            return unit(int(sec // 3600), "hour")
        return unit(int(sec // 86400), "day")

    def texts(self, unread=False, n=3, sender=None):
        """Newest SMS from the phone's inbox -> [{'who','when','body'}], [] if none, None if the phone refuses."""
        cmd = 'content query --uri content://sms/inbox --projection address:date:body --sort "date DESC"'
        if unread:
            cmd += ' --where "read=0"'
        rc, out = self._run("shell", cmd + " | head -c 60000", timeout=30)
        if rc != 0 or re.search(r"Permission Denial|SecurityException|Exception", out):
            return None
        rows = []
        for chunk in re.split(r"(?m)^Row: \d+ ", out):
            m = re.match(r"address=(.*?), date=(\d+), body=(.*)$", chunk.strip(), re.S)
            if m:
                rows.append({"address": m.group(1).strip(), "ms": int(m.group(2)), "body": " ".join(m.group(3).split())})
        if sender:
            hit = self.find_contact(sender)
            tail = re.sub(r"\D", "", hit[1] if hit else sender)[-10:]
            rows = [r for r in rows if tail and re.sub(r"\D", "", r["address"]).endswith(tail)]
        result = []
        for r in rows[:n]:
            result.append({"who": self.lookup_name(r["address"]) or r["address"], "when": self.ago(r["ms"]), "body": r["body"]})
        return result

    # ================= unlocking: Jarvis wakes the screen, YOU touch the fingerprint sensor =================
    def is_locked(self):
        """Best-effort: is the phone's lock screen showing? (False if it can't tell.)"""
        _, out = self._run("shell", "dumpsys window | grep -E 'isKeyguardShowing|mShowingLockscreen|mDreamingLockscreen|mCurrentFocus'")
        if re.search(r"(?:isKeyguardShowing|mShowingLockscreen|mDreamingLockscreen)\W*=?\s*true", out):
            return True
        if re.search(r"mCurrentFocus=.*(?:Keyguard|NotificationShade|lockscreen)", out, re.I):
            return True
        _, kg = self._run("shell", "dumpsys window policy | grep -A8 KeyguardServiceDelegate")      # older Android versions
        return bool(re.search(r"\bshowing=true\b", kg))

    def unlock_wait(self, say=None, cancel=None, timeout=30):
        """Wakes the phone, brings up the unlock prompt, then waits for you to unlock it with your fingerprint.
        Jarvis never touches your fingerprint, PIN or pattern. Returns a short spoken result."""
        self._run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(0.8)
        if not self.is_locked():
            return "Your phone is already unlocked."
        _, size = self._run("shell", "wm size")
        m = re.search(r"(\d+)x(\d+)", size)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        self._run("shell", f"input swipe {w // 2} {int(h * .85)} {w // 2} {int(h * .35)} 250")     # reveals the unlock prompt
        if say:
            say("Touch your fingerprint sensor to unlock the phone.")
        end = time.time() + timeout
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                return "Okay, I'll leave it locked."
            time.sleep(1.0)
            if not self.is_locked():
                return "Unlocked."
        return "It's still locked. Say unlock my phone when you're ready."

    # ================= unlocking with the saved PIN (see phone_secret.py) =================
    def _shell_stdin(self, commands):
        """Runs shell commands on the phone through stdin, so a PIN never appears in a process's command line."""
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + ["shell"]
        try:
            p = subprocess.run(cmd, input=("\n".join(commands) + "\nexit\n").encode("utf-8"),
                               capture_output=True, timeout=20, creationflags=NOWINDOW)
            return p.returncode
        except Exception:
            return 1

    def unlock_with_pin(self, say=None, cancel=None):
        """Types the saved PIN once. Never retries (a few wrong tries lock the phone), and stops after two failures."""
        try:
            import phone_secret
            pin = phone_secret.load_pin()
        except Exception:
            pin = None
        if not pin:
            return "I don't have a PIN saved for your phone. Run phone pin setup in a terminal to add it."
        if self._pin_failures >= 2:
            return ("I've failed to unlock it twice, so I'm stopping before your phone locks you out. "
                    "Check the saved PIN with phone pin setup, then restart me.")
        self._run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(0.8)
        if not self.is_locked():
            return "Your phone is already unlocked."
        _, size = self._run("shell", "wm size")
        m = re.search(r"(\d+)x(\d+)", size)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        self._run("shell", f"input swipe {w // 2} {int(h * .85)} {w // 2} {int(h * .35)} 250")     # reveals the PIN pad
        time.sleep(1.0)
        if cancel is not None and cancel.is_set():
            return "Okay, I'll leave it locked."
        rc = self._shell_stdin([f"input text {_adb_text(pin)}", "input keyevent KEYCODE_ENTER"])
        pin = None
        end = time.time() + 6
        while time.time() < end:
            time.sleep(0.8)
            if not self.is_locked():
                self._pin_failures = 0
                return "Unlocked."
        self._pin_failures += 1
        return "That didn't unlock it. I only try once, so it may be the saved PIN, or this phone blocks typed input on the lock screen."

    def unlock_auto(self, say=None, cancel=None, confirm=None, use_pin=True):
        """Saved PIN if there is one (after a spoken yes), otherwise it waits for your fingerprint."""
        try:
            import phone_secret
            has = use_pin and phone_secret.has_pin()
        except Exception:
            has = False
        if not has or confirm is None:
            return self.unlock_wait(say=say, cancel=cancel)
        self._run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(0.6)
        if not self.is_locked():
            return "Your phone is already unlocked."
        if not confirm("Unlock your phone with the saved PIN?"):
            return "Okay, leaving it locked."
        return self.unlock_with_pin(say=say, cancel=cancel)