"""
timers_agent.py - timers, alarms and reminders for Mini-Jarvis.

Voice / typed commands it understands (numbers can be spoken or digits):
  Timers     "set a timer for 10 minutes"  "5 minute timer"  "timer for an hour and a half"
             "set a pasta timer for 12 minutes"
  Alarms     "set an alarm for 7 am"  "alarm at half past six"  "wake me up at 6:30 tomorrow"
  Reminders  "remind me to call mom in 20 minutes"  "remind me at 5 pm to take my medicine"
             "remind me tomorrow to send the form"  "remind me on friday at 3 to pay the bill"
  Questions  "how much time is left on my timer"  "what alarms do I have"  "list my reminders"
  Cancel     "cancel the timer"  "cancel all alarms"  "delete my reminder"  "cancel the 5 minute timer"
  Ringing    "snooze"  "snooze for 10 minutes"  "stop the alarm"  "stop the timer"

Everything is saved in reminders.json, so timers, alarms and reminders survive a restart.
Something that came due while Jarvis was off is announced when it starts again.
Timers and alarms ring 3 times (25 seconds apart), reminders speak once.
One-time only: repeating alarms ("every day") are not supported, and it says so.

voice.py gives it a function that actually speaks:  on_fire(item, late, ring_no) -> True once delivered.
"""
import datetime as dt
import json
import os
import re
import threading
import time

RING_TIMES = 3            # how many times a timer / alarm rings
RING_GAP = 25             # seconds between rings
SNOOZE_MINUTES = 5
LATE_AFTER = 90           # seconds: later than this and it is announced as "missed"
MAX_SECONDS = 7 * 24 * 3600
PENDING_SECONDS = 90      # how long "for how long?" / "when?" waits for your answer

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ======================= reading the sentence =======================
def tokenize(text):
    t = text.lower().replace("a.m.", "am").replace("p.m.", "pm").replace("o'clock", "oclock")
    t = t.replace("o clock", "oclock").replace("'", "")
    t = re.sub(r"[^a-z0-9:. ]", " ", t)
    t = re.sub(r"(?<=\d)\.(?!\d)", " ", t)
    t = re.sub(r"(?<![0-9])\.", " ", t)
    t = re.sub(r"(?<=\d)(?=[a-z])", " ", t)
    return t.split()


def _read_small(tokens, i):
    """A number below 100 made of words starting at i: (value, next_i) or None."""
    if i >= len(tokens):
        return None
    t = tokens[i]
    if t in TENS:
        val, j = TENS[t], i + 1
        if j < len(tokens) and tokens[j] in UNITS and 1 <= UNITS[tokens[j]] <= 9:
            val, j = val + UNITS[tokens[j]], j + 1
        return val, j
    if t in UNITS:
        return UNITS[t], i + 1
    return None


def _read_number(tokens, i):
    res = _read_small(tokens, i)
    if not res:
        return None
    val, j = res
    if j < len(tokens) and tokens[j] == "hundred":
        val, j = val * 100, j + 1
        k = j + 1 if j < len(tokens) and tokens[j] == "and" else j
        sub = _read_small(tokens, k)
        if sub:
            val, j = val + sub[0], sub[1]
    return val, j


def normalize(tokens):
    """Number words become digits. Returns (norm_tokens, spans); spans[i] = (first, last+1) original token."""
    norm, spans, i = [], [], 0
    while i < len(tokens):
        res = _read_number(tokens, i)
        if res:
            norm.append(str(res[0]))
            spans.append((i, res[1]))
            i = res[1]
        else:
            norm.append(tokens[i])
            spans.append((i, i + 1))
            i += 1
    return norm, spans


_U = r"(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_DUR_ITEM = rf"(?:\d+(?:\.\d+)?|a|an) {_U}"
DUR = (rf"(?:half an hour|(?:a )?quarter of an hour|an hour and a half|\d+ and a half {_U}|"
       rf"{_DUR_ITEM}(?: (?:and )?{_DUR_ITEM})*(?: and a half)?)")
_DAY = rf"(?:today|tonight|tomorrow|(?:(?:on|next|this) )?(?:{'|'.join(WEEKDAYS)}))"
_PART = r"(?: (?:in the|at) (?:morning|afternoon|evening|night))"
_CLOCK = (r"(?:noon|midnight|half past \d{1,2}|(?:a )?quarter (?:past|to) \d{1,2}|\d{1,2} (?:past|to) \d{1,2}|"
          r"\d{1,2}(?::\d{2})? ?(?:am|pm)|\d{1,2} \d{2} (?:am|pm)|\d{1,2}:\d{2}|\d{1,2} oclock)")
_BARE = r"(?:at|by|around) \d{1,2}(?: \d{2})?"
ABS = rf"(?:{_DAY} (?:at |by |around )?)?(?:(?:at |by |around )?{_CLOCK}|{_BARE}){_PART}?(?: {_DAY})?"
DAY_ONLY = rf"{_DAY}(?: (?:morning|afternoon|evening))?"

_UNIT_SECS = {"h": 3600, "m": 60, "s": 1}


def _unit_secs(word):
    return _UNIT_SECS[word[0]]


def parse_duration(s):
    """Seconds in a duration phrase like '1 hour 30 minutes' or 'half an hour', or None."""
    if s.startswith("half an hour"):
        return 1800
    if "quarter of an hour" in s:
        return 900
    if s.startswith("an hour and a half"):
        return 5400
    m = re.fullmatch(rf"(\d+) and a half ({_U})", s)
    if m:
        return (int(m.group(1)) + 0.5) * _unit_secs(m.group(2))
    total, last = 0.0, 0
    for q, u in re.findall(rf"(\d+(?:\.\d+)?|a|an) ({_U})", s):
        n = 1.0 if q in ("a", "an") else float(q)
        last = _unit_secs(u)
        total += n * last
    if s.endswith("and a half") and last:
        total += last / 2
    return total or None


def find_when(norm_str, allow_for=False, allow_bare=False, alarm=False):
    """Finds the time in a sentence. Returns (kind, match) where kind is 'dur' | 'abs' | 'day', or None."""
    pats = [("dur", rf"\b(?:in|after) ({DUR})\b")]
    if allow_for:
        pats.append(("dur", rf"\bfor ({DUR})\b"))
    pats.append(("dur", rf"\b({DUR}) (?:from now|later)\b"))
    pats.append(("abs", rf"\b({ABS})\b"))
    if alarm:
        pats.append(("abs", r"\b(for \d{1,2}(?: \d{2})?)\b"))
    if allow_bare:
        pats.append(("dur", rf"\b({DUR})\b"))
    pats.append(("day", rf"\b({DAY_ONLY})\b"))
    for kind, p in pats:
        m = re.search(p, norm_str)
        if m:
            return kind, m
    return None


def _clock_parts(text):
    """(hour, minute, meridiem) from a time phrase; hour is as spoken (1-12, or 0-23). None if unreadable."""
    mer = None
    if re.search(r"\bpm\b|\b(?:afternoon|evening)\b|\btonight\b", text):
        mer = "pm"
    elif re.search(r"\bam\b|\bmorning\b", text):
        mer = "am"
    hour = minute = None
    if "noon" in text:
        return 12, 0, "pm"
    if "midnight" in text:
        return 12, 0, "am"
    m = re.search(r"half past (\d{1,2})", text)
    if m:
        hour, minute = int(m.group(1)), 30
    if hour is None:
        m = re.search(r"quarter past (\d{1,2})", text)
        if m:
            hour, minute = int(m.group(1)), 15
    if hour is None:
        m = re.search(r"quarter to (\d{1,2})", text)
        if m:
            hour, minute = (int(m.group(1)) - 1) or 12, 45
    if hour is None:
        m = re.search(r"\b(\d{1,2}) (past|to) (\d{1,2})\b", text)
        if m:
            n, h = int(m.group(1)), int(m.group(3))
            hour, minute = (h, n) if m.group(2) == "past" else ((h - 1) or 12, 60 - n)
    if hour is None:
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", text) or re.search(r"\b(\d{1,2}) (\d{2}) (?:am|pm)\b", text)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
    if hour is None:
        m = re.search(r"\b(\d{1,2}) ?(?:am|pm|oclock)\b", text) or re.search(r"\b(?:at|by|around|for) (\d{1,2})(?: (\d{2}))?\b", text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0
    if hour is None:
        return None
    if mer is None and re.search(r"\bat night\b|\bin the night\b", text) and 6 <= hour <= 11:
        mer = "pm"
    if mer is None and re.search(r"\bat night\b|\bin the night\b", text) and 1 <= hour <= 5:
        mer = "am"
    return hour, minute, mer


def resolve_abs(text, now, prefer_am=False):
    """The datetime for a phrase like 'tomorrow at 7 am' / 'at 5' / 'friday at 3 pm'.
    Returns (datetime, error); exactly one of them is None."""
    parts = _clock_parts(text)
    day_m = re.search(_DAY, text)
    day = day_m.group(0) if day_m else None
    if parts is None:
        if day:                                     # only a day: nine in the morning
            parts = (9, 0, "am")
        else:
            return None, "I didn't catch the time."
    hour, minute, mer = parts
    if minute > 59 or hour > 24 or (hour == 24 and minute):
        return None, "That doesn't look like a real time."
    hour = 0 if hour == 24 else hour
    today = now.date()
    base = today
    if day:
        word = day.split()[-1]
        if word == "tomorrow":
            base = today + dt.timedelta(days=1)
        elif word in WEEKDAYS:
            ahead = (WEEKDAYS.index(word) - today.weekday()) % 7
            if day.startswith("next") and ahead == 0:
                ahead = 7
            base = today + dt.timedelta(days=ahead)
    if hour >= 13 or hour == 0:
        hours = [hour]
    elif mer == "am":
        hours = [hour % 12]
    elif mer == "pm":
        hours = [hour % 12 + 12]
    elif day not in (None, "today", "tonight"):
        hours = [hour % 12 + 12] if (hour == 12 or hour <= 6) else [hour]      # 3 -> 3 PM, 8 -> 8 AM, 12 -> noon
    elif prefer_am and 4 <= hour <= 11:
        hours = [hour]                                                          # an alarm "at 7" means 7 AM
    else:
        hours = [hour % 12, hour % 12 + 12]
    cands = sorted(dt.datetime.combine(base, dt.time(h, minute)) for h in hours)
    if day in ("today", "tonight"):
        if day == "tonight" and cands[0].hour < 6 and len(cands) == 1:
            cands = [cands[0] + dt.timedelta(days=1)]          # 'tonight at 12' is the coming midnight
        future = [c for c in cands if c > now]
        if not future:
            return None, "That time has already passed today."
        return future[0], None
    if day is None:
        future = [c for c in cands if c > now] or [c + dt.timedelta(days=1) for c in cands]
        return future[0], None
    if base == today and day.split()[-1] in WEEKDAYS:
        future = [c for c in cands if c > now] or [c + dt.timedelta(days=7) for c in cands]
        return future[0], None
    future = [c for c in cands if c > now]
    return (future[0], None) if future else (cands[0], None)


# ======================= words for speaking =======================
def dur_phrase(secs):
    secs = int(round(secs))
    h, rest = divmod(secs, 3600)
    m, s = divmod(rest, 60)
    out = []
    if h:
        out.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        out.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        out.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(out) or "0 seconds"


def timer_name(secs):
    secs = int(round(secs))
    if secs % 60 == 0:
        m = secs // 60
        return f"{m // 60} hour" if m % 60 == 0 else f"{m} minute"
    return f"{secs} second"


def clock_phrase(d):
    h = d.hour % 12 or 12
    ap = "AM" if d.hour < 12 else "PM"
    return f"{h} {ap}" if d.minute == 0 else f"{h}:{d.minute:02d} {ap}"


def when_phrase(d, now):
    delta = (d.date() - now.date()).days
    if delta == 0:
        return f"today at {clock_phrase(d)}"
    if delta == 1:
        return f"tomorrow at {clock_phrase(d)}"
    if delta < 7:
        return f"on {d.strftime('%A')} at {clock_phrase(d)}"
    return f"on {d.strftime('%A')} the {d.day} at {clock_phrase(d)}"


_PRONOUNS = {"my": "your", "me": "you", "myself": "yourself", "i": "you", "im": "you are", "ive": "you have",
             "mine": "yours", "ill": "you will", "id": "you would"}


def swap_pronouns(text):
    return " ".join(_PRONOUNS.get(w, w) for w in text.split())


# ======================= the agent =======================
class TimerAgent:
    def __init__(self, path, on_fire):
        self.path = path
        self.on_fire = on_fire
        self.items = []
        self.pending = None                 # {'kind', 'text', 'name', 't'} while it waits for the missing time
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._started = False
        self._load()

    # ----- saving -----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = [i for i in data if isinstance(i, dict) and "due" in i and "kind" in i]
        except Exception:
            self.items = []

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _add(self, kind, due, text="", name="", secs=0):
        with self._lock:
            item = {"id": int(time.time() * 1000), "kind": kind, "due": due, "text": text, "name": name,
                    "secs": secs, "created": time.time(), "rings": 0}
            self.items.append(item)
            self.items.sort(key=lambda i: i["due"])
            self._save()
        self._wake.set()
        return item

    # ----- the clock thread -----
    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="timers").start()

    def _loop(self):
        while True:
            try:
                wait = self._tick()
            except Exception:
                import traceback
                traceback.print_exc()
                wait = 5.0
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def _tick(self):
        """Fires what is due. Returns how many seconds to sleep until the next thing."""
        now = time.time()
        with self._lock:
            due = [i for i in self.items if i["due"] <= now]
        for item in due:
            late = item["rings"] == 0 and now - item["due"] > LATE_AFTER
            try:
                delivered = bool(self.on_fire(item, late, item["rings"]))
            except Exception:
                import traceback
                traceback.print_exc()
                delivered = False
            with self._lock:
                if item not in self.items:
                    continue
                if not delivered:
                    item["due"] = time.time() + 5               # he is busy: try again in a few seconds
                    continue
                item["rings"] += 1
                if item["kind"] in ("timer", "alarm") and item["rings"] < RING_TIMES:
                    item["due"] = time.time() + RING_GAP
                    item["ringing"] = True
                else:
                    self.items.remove(item)
                self._save()
        with self._lock:
            nxt = min((i["due"] for i in self.items), default=None)
        if nxt is None:
            return 30.0
        return max(0.2, min(nxt - time.time(), 30.0))

    # ----- helpers -----
    def _of_kind(self, kind):
        return [i for i in self.items if i["kind"] == kind]

    def _ringing(self):
        return [i for i in self.items if i.get("ringing")]

    def _describe(self, item, now=None):
        now = now or dt.datetime.now()
        left = max(0, item["due"] - time.time())
        if item["kind"] == "timer":
            nm = item.get("name") or timer_name(item["secs"])
            return f"{nm} timer, {dur_phrase(left)} left"
        due = dt.datetime.fromtimestamp(item["due"])
        if item["kind"] == "alarm":
            return f"alarm {when_phrase(due, now)}"
        return f"reminder {when_phrase(due, now)}" + (f": {item['text']}" if item.get("text") else "")

    def summary(self):
        """Everything pending, one per line (for the on-screen log)."""
        with self._lock:
            return "\n".join(self._describe(i) for i in self.items)

    # ----- the commands -----
    def handle(self, command, now=None):
        """Spoken reply for a timer / alarm / reminder command, or None if this is something else."""
        now = now or dt.datetime.now()
        norm, spans = normalize(tokenize(command))
        if not norm:
            return None
        t = " ".join(norm)
        words = set(norm)

        has_intent = bool(words & {"timer", "timers", "alarm", "alarms", "reminder", "reminders", "remind",
                                   "snooze", "dismiss"}) or t.startswith("wake me up")
        if self.pending and not has_intent:
            if time.time() - self.pending["t"] <= PENDING_SECONDS:
                reply = self._finish_pending(t, norm, spans, now)
                if reply:
                    return reply
            self.pending = None
        elif has_intent:
            self.pending = None
        if not has_intent:
            return None

        with self._lock:
            # --- ringing right now ---
            m = re.search(r"\bsnooze\b(?: for)?(?: (" + DUR + r"))?", t)
            if m:
                return self._snooze(parse_duration(m.group(1)) if m.group(1) else SNOOZE_MINUTES * 60)
            if re.search(r"\b(stop|dismiss|silence|turn off|shut off|quiet)\b", t) and self._ringing() \
                    and not re.search(r"\b(all|every)\b", t):
                ringing = self._ringing()
                for i in ringing:
                    self.items.remove(i)
                self._save()
                kinds = {i["kind"] for i in ringing}
                return "Okay, stopped." if len(kinds) != 1 else f"Okay, {kinds.pop()} stopped."

            # --- cancel ---
            if re.search(r"\b(cancel|delete|remove|clear|forget|stop|turn off|get rid of|scrap)\b", t):
                return self._cancel(t)

            # --- questions ---
            asking = (re.search(r"\b(left|remaining|how long|how much time|list|show|read|any|do i have|have i got|"
                                r"how many|when|are set|is set|are there|is there)\b", t)
                      or (t.startswith(("what", "which", "tell me")) and re.search(r"\b(my|i have|are)\b", t)))
            if asking and not re.search(r"\b(start|make|create|add|remind me to)\b", t) \
                    and not (re.search(r"\bset\b", t) and not re.search(r"\b(are|is) set\b", t)):
                return self._list(t)

        # --- creating ---
        if re.search(r"\b(every|daily|weekly|each)\b", t) and re.search(r"\b(day|morning|evening|night|week|"
                                                                          r"weekday|weekdays|monday|tuesday|wednesday|"
                                                                          r"thursday|friday|saturday|sunday|daily|"
                                                                          r"weekly)\b", t):
            return "I can only set one-time timers, alarms and reminders for now."
        if "timer" in words:
            return self._make_timer(t, norm, spans, now)
        if "remind" in words or "reminder" in words:
            return self._make_reminder(t, norm, spans, now, tokenize(command))
        if "alarm" in words or t.startswith("wake me up"):
            return self._make_alarm(t, norm, spans, now)
        return None

    # ----- creating -----
    def _make_timer(self, t, norm, spans, now):
        found = find_when(t, allow_for=True, allow_bare=True)
        label = self._label(norm, spans, t, found)
        if not found or found[0] != "dur":
            if re.search(r"\b(set|start|make|create|begin|give me|put|run|add)\b", t):
                self.pending = {"kind": "timer", "text": "", "name": label, "t": time.time()}
                return "For how long?"
            return None
        secs = parse_duration(found[1].group(1))
        return self._new_timer(secs, label)

    def _new_timer(self, secs, label):
        if not secs or secs < 1:
            return "I didn't catch how long."
        if secs > MAX_SECONDS:
            return "That's longer than I can keep. The most is 7 days."
        name = label
        self._add("timer", time.time() + secs, name=name, secs=secs)
        return f"{'Timer' if not name else name.capitalize() + ' timer'} set for {dur_phrase(secs)}."

    def _make_alarm(self, t, norm, spans, now):
        found = find_when(t, allow_for=False, allow_bare=False, alarm=True)
        if not found:
            if re.search(r"\b(set|make|create|add|wake me up|schedule|put)\b", t):
                self.pending = {"kind": "alarm", "text": "", "name": "", "t": time.time()}
                return "What time?"
            return None
        return self._new_at("alarm", found, "", now)

    def _make_reminder(self, t, norm, spans, now, orig_tokens):
        found = find_when(t, allow_for=False, allow_bare=False)
        text = self._reminder_text(orig_tokens, spans, t, found)
        if not found:
            self.pending = {"kind": "reminder", "text": text, "name": "", "t": time.time()}
            return "When should I remind you?"
        return self._new_at("reminder", found, text, now)

    def _new_at(self, kind, found, text, now):
        what, m = found
        if what == "dur":
            secs = parse_duration(m.group(1))
            if not secs or secs < 1:
                return "I didn't catch how long."
            if secs > MAX_SECONDS:
                return "That's further away than I can keep. The most is 7 days."
            due = time.time() + secs
            when = f"in {dur_phrase(secs)}"
        else:
            d, err = resolve_abs(m.group(1), now, prefer_am=(kind == "alarm"))
            if err:
                return err
            due = d.timestamp()
            when = when_phrase(d, now)
        self._add(kind, due, text=text)
        if kind == "alarm":
            return f"Alarm set {when}."
        return (f"Okay, I'll remind you to {text} {when}." if text and not re.match(r"^(about|the|my|your|that) ", text)
                else f"Okay, I'll remind you about {text} {when}." if text else f"Okay, reminder set {when}.")

    def _finish_pending(self, t, norm, spans, now):
        p = self.pending
        found = find_when(t, allow_for=True, allow_bare=True)
        if p["kind"] == "timer":
            if found and found[0] == "dur":
                self.pending = None
                return self._new_timer(parse_duration(found[1].group(1)), p["name"])
            return None
        if not found:
            return None
        self.pending = None
        return self._new_at(p["kind"], found, p["text"], now)

    # ----- picking the words out of the sentence -----
    def _label(self, norm, spans, t, found):
        drop = set()
        if found:
            drop = {n for n in self._norm_idx(t, found[1])}
        keep = [w for n, w in enumerate(norm) if n not in drop]
        filler = {"set", "a", "an", "the", "timer", "timers", "for", "please", "start", "make", "create", "me",
                  "my", "of", "to", "give", "put", "run", "add", "new", "begin", "can", "could", "you", "would",
                  "will", "and", "on", "that", "this", "now", "up", "called", "named", "minute", "minutes", "hour",
                  "hours", "second", "seconds", "countdown", "another", "one", "i", "want", "need", "like"}
        left = [w for w in keep if w not in filler and not re.fullmatch(r"[\d:.]+", w)]
        return " ".join(left) if 0 < len(left) <= 3 else ""

    @staticmethod
    def _norm_idx(t, match):
        offsets, pos = [], 0
        for n, tok in enumerate(t.split()):
            offsets.append((pos, pos + len(tok)))
            pos += len(tok) + 1
        return [n for n, (a, b) in enumerate(offsets) if a < match.end() and b > match.start()]

    def _reminder_text(self, orig_tokens, spans, t, found):
        drop = set()
        if found:
            for n in self._norm_idx(t, found[1]):
                drop.update(range(spans[n][0], spans[n][1]))
        words = [w for i, w in enumerate(orig_tokens) if i not in drop]
        text = " ".join(words)
        text = re.sub(r"^(?:please )?(?:(?:can|could|would|will) you )?(?:set (?:a |an |me a )?reminder|add (?:a )?reminder|"
                      r"create (?:a )?reminder|remind me|reminder)(?: for me)?\b", "", text.strip())
        parts = text.split()
        lead = {"to", "about", "that", "for", "of", "on", "at", "and", "please", "me", "in", "by", "around"}
        trail = {"to", "for", "at", "on", "in", "by", "and", "please", "around", "after", "this", "next"}
        while parts and parts[0] in lead:
            parts.pop(0)
        while parts and parts[-1] in trail:
            parts.pop()
        return swap_pronouns(" ".join(parts))

    # ----- ringing / cancelling / listing -----
    def _snooze(self, secs):
        ringing = self._ringing()
        if not ringing:
            return "Nothing is ringing right now."
        for i in ringing:
            i["due"] = time.time() + secs
            i["rings"] = 0
            i["ringing"] = False
        self._save()
        self._wake.set()
        return f"Snoozed for {dur_phrase(secs)}."

    def _cancel(self, t):
        kind = ("timer" if re.search(r"\btimers?\b", t) else "alarm" if re.search(r"\balarms?\b", t)
                else "reminder" if re.search(r"\breminders?\b", t) else None)
        if kind is None:
            return None
        mine = self._of_kind(kind)
        if not mine:
            return f"You don't have any {kind}s set."
        if re.search(r"\b(all|every|everything)\b", t) or re.search(rf"\b{kind}s\b", t):
            for i in mine:
                self.items.remove(i)
            self._save()
            return f"Done. All {len(mine)} {kind}{'s' if len(mine) != 1 else ''} cancelled."
        target = None
        if len(mine) == 1:
            target = mine[0]
        else:
            nums = re.findall(r"\d+", t)
            for i in mine:
                if kind == "timer" and nums and str(int(i["secs"] // (60 if i["secs"] % 60 == 0 else 1))) in nums:
                    target = i
                    break
                if kind == "timer" and i.get("name") and i["name"] in t:
                    target = i
                    break
                if kind != "timer":
                    due = dt.datetime.fromtimestamp(i["due"])
                    if nums and (str(due.hour % 12 or 12) in nums or str(due.hour) in nums):
                        target = i
                        break
                    if i.get("text") and any(w in t.split() for w in i["text"].split() if len(w) > 3):
                        target = i
                        break
        if target is None:
            eg = {"timer": "cancel the 5 minute timer", "alarm": "cancel the 8 am alarm",
                  "reminder": "cancel the reminder about the form"}[kind]
            return f"You have {len(mine)} {kind}s. Say cancel all {kind}s, or say which one, like {eg}."
        self.items.remove(target)
        self._save()
        return f"Okay, cancelled the {self._describe(target).split(',')[0]}."

    def _list(self, t):
        kind = ("timer" if re.search(r"\btimers?\b", t) else "alarm" if re.search(r"\balarms?\b", t)
                else "reminder" if re.search(r"\breminders?\b", t) else None)
        if kind is None:
            return None
        mine = self._of_kind(kind)
        if not mine:
            return f"You don't have any {kind}s set."
        now = dt.datetime.now()
        if kind == "timer" and re.search(r"\b(left|remaining|how long|how much time)\b", t):
            parts = [f"{(i.get('name') or timer_name(i['secs']))} timer has {dur_phrase(max(0, i['due'] - time.time()))} left"
                     for i in mine]
            return (parts[0] if len(parts) == 1 else "; ".join(parts)).capitalize() + "."
        if len(mine) == 1:
            return f"You have one {self._describe(mine[0], now)}."
        return f"You have {len(mine)} {kind}s: " + "; ".join(self._describe(i, now) for i in mine) + "."