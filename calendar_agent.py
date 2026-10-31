"""calendar_agent.py - read-only Google Calendar for Jarvis. Light: no Google account login, no API keys.

SETUP (one time, 2 minutes)
  1. Google Calendar on the web -> the gear icon -> Settings -> click YOUR calendar on the left
     -> "Integrate calendar" -> copy "Secret address in iCal format".
  2. Paste that link, alone on one line, into a text file called  calendar_url.txt  next to voice.py.
  Treat the link like a password: anyone who has it can read your calendar. Never share it.

HOW IT WORKS
  * A background thread downloads your calendar every SYNC_MINUTES and keeps a copy in
    calendar_cache.ics, so questions still work offline (they are as fresh as the last sync).
  * Everything else (understanding the question, repeating events, time zones) happens on your laptop.
  * It only READS. It cannot add or change events.
"""
import calendar as _cal
import json
import os
import re
import threading
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # very old Python
    ZoneInfo = None
try:
    import requests
except ImportError:
    requests = None

BASE = os.path.dirname(os.path.abspath(__file__))
URL_FILE = os.path.join(BASE, "calendar_url.txt")
CACHE_FILE = os.path.join(BASE, "calendar_cache.ics")
STATE_FILE = os.path.join(BASE, "calendar_state.json")     # remembers what he already told you today
SYNC_MINUTES = 15              # how often the calendar is downloaded again
REMIND_MINUTES = 10            # "Heads up, ... starts in 10 minutes" (0 = no reminders)
BRIEFING_HOUR = 8              # Jarvis messages you FIRST with your day, once, from this hour on...
EVENING_HOUR = 20              # ...and with a heads-up for tomorrow from this hour on (the briefing stops then)
SPEAK_LIMIT = 6                # at most this many events are read aloud (the full list goes on screen)

WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july",
                                          "august", "september", "october", "november", "december"))}
MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
               "oct": 10, "nov": 11, "dec": 12})
NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10}


# ====================================================================== reading the .ics file
def _unfold(text):
    """iCal wraps long lines: a line that starts with a space continues the previous one."""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


_ESC = re.compile(r"\\(.)")


def _unescape(s):
    return _ESC.sub(lambda m: " " if m.group(1) in "nN" else m.group(1), s)


def _prop(line):
    """'DTSTART;TZID=Asia/Kolkata:20261020T103000' -> ('DTSTART', {'TZID': 'Asia/Kolkata'}, '20261020T103000')"""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _offset(value):
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", value.strip())
    if not m:
        return None
    sec = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4) or 0)
    return timedelta(seconds=-sec if m.group(1) == "-" else sec)


_ZONES = {}


def _zone(tzid, fixed):
    """A tzinfo for a TZID: the system's time zone database first, then the offset written in the file."""
    if tzid in _ZONES:
        return _ZONES[tzid]
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tzid)
        except Exception:
            tz = None
    if tz is None and tzid in fixed:
        tz = timezone(fixed[tzid])
    _ZONES[tzid] = tz
    return tz


class Stamp:
    """A date or date-time as written in the file: wall-clock time + its time zone (None = your PC's)."""
    __slots__ = ("wall", "tz", "all_day")

    def __init__(self, wall, tz=None, all_day=False):
        self.wall, self.tz, self.all_day = wall, tz, all_day

    def aware(self):
        if self.all_day:
            return datetime.combine(self.wall, dtime.min).astimezone()      # local midnight
        if self.tz is None:
            return self.wall.astimezone()                                    # "floating" = your PC's zone
        return self.wall.replace(tzinfo=self.tz)

    def key(self):
        """Comparable identity of an instance (used for EXDATE / overridden instances)."""
        return self.wall if self.all_day else self.aware().astimezone(timezone.utc)


def _stamp(value, params, fixed):
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        return Stamp(date(int(value[:4]), int(value[4:6]), int(value[6:8])), None, True)
    utc = value.endswith("Z")
    v = value.rstrip("Z")
    wall = datetime(int(v[:4]), int(v[4:6]), int(v[6:8]), int(v[9:11]), int(v[11:13]),
                    int(v[13:15]) if len(v) >= 15 else 0)
    if utc:
        return Stamp(wall, timezone.utc)
    tzid = params.get("TZID")
    return Stamp(wall, _zone(tzid, fixed) if tzid else None)


def _duration(value):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _rrule(value, fixed):
    rule = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.upper()
        if k == "FREQ":
            rule["FREQ"] = v.upper()
        elif k in ("INTERVAL", "COUNT"):
            rule[k] = int(v)
        elif k == "UNTIL":
            rule["UNTIL"] = _stamp(v, {}, fixed)
        elif k == "WKST":
            rule["WKST"] = WD.get(v.upper()[:2], 0)
        elif k == "BYDAY":
            days = []
            for item in v.split(","):
                m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", item.strip().upper())
                if m:
                    days.append((int(m.group(1)) if m.group(1) else None, WD[m.group(2)]))
            rule["BYDAY"] = days
        elif k in ("BYMONTHDAY", "BYMONTH", "BYSETPOS"):
            rule[k] = [int(x) for x in v.split(",") if x.strip()]
    return rule if rule.get("FREQ") in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") else None


class Ev:
    __slots__ = ("uid", "title", "location", "start", "end", "dur", "rule", "exdates", "rid", "cancelled")


def _build(props, fixed):
    ev = Ev()
    ev.uid, ev.title, ev.location = "", "(no title)", ""
    ev.start = ev.end = ev.dur = ev.rule = ev.rid = None
    ev.exdates, ev.cancelled = [], False
    for name, params, value in props:
        try:
            if name == "UID":
                ev.uid = value.strip()
            elif name == "SUMMARY":
                ev.title = _unescape(value).strip() or "(no title)"
            elif name == "LOCATION":
                ev.location = _unescape(value).strip()
            elif name == "STATUS":
                ev.cancelled = value.strip().upper() == "CANCELLED"
            elif name == "DTSTART":
                ev.start = _stamp(value, params, fixed)
            elif name == "DTEND":
                ev.end = _stamp(value, params, fixed)
            elif name == "DURATION":
                ev.dur = _duration(value)
            elif name == "RRULE":
                ev.rule = _rrule(value, fixed)
            elif name == "EXDATE":
                ev.exdates += [_stamp(v, params, fixed) for v in value.split(",") if v.strip()]
            elif name == "RECURRENCE-ID":
                ev.rid = _stamp(value, params, fixed)
        except Exception:
            continue                                     # one odd property must not lose the whole event
    return ev if ev.start is not None else None


def parse_ics(text):
    """All events in an .ics text (a list of Ev). Alarms and other nested parts are ignored."""
    raw, fixed = [], {}
    stack, cur, tz = [], None, None
    for line in _unfold(text):
        p = _prop(line) if line.strip() else None
        if not p:
            continue
        name, params, value = p
        if name == "BEGIN":
            v = value.strip().upper()
            stack.append(v)
            if v == "VEVENT":
                cur = []
            elif v == "VTIMEZONE":
                tz = {"id": None, "std": None, "any": None}
        elif name == "END":
            v = value.strip().upper()
            if stack and stack[-1] == v:
                stack.pop()
            if v == "VEVENT" and cur is not None:
                raw.append(cur)
                cur = None
            elif v == "VTIMEZONE" and tz is not None:
                off = tz["std"] if tz["std"] is not None else tz["any"]
                if tz["id"] and off is not None:
                    fixed[tz["id"]] = off
                tz = None
        else:
            top = stack[-1] if stack else ""
            if top == "VEVENT" and cur is not None:
                cur.append((name, params, value))
            elif tz is not None:
                if name == "TZID" and top == "VTIMEZONE":
                    tz["id"] = value.strip()
                elif name == "TZOFFSETTO" and top in ("STANDARD", "DAYLIGHT"):
                    off = _offset(value)
                    if off is not None:
                        tz["any"] = off if tz["any"] is None else tz["any"]
                        if top == "STANDARD":
                            tz["std"] = off
    return [e for e in (_build(r, fixed) for r in raw) if e is not None]


# ====================================================================== repeating events
def _month_dates(y, m, rule, d0):
    """The dates in one month that a MONTHLY / YEARLY rule selects."""
    n = _cal.monthrange(y, m)[1]
    bymd, byday = rule.get("BYMONTHDAY"), rule.get("BYDAY")
    days = set()
    if bymd:
        for dd in bymd:
            dd = dd if dd > 0 else n + 1 + dd
            if 1 <= dd <= n:
                days.add(dd)
        if byday:
            wds = {w for _, w in byday}
            days = {dd for dd in days if date(y, m, dd).weekday() in wds}
    elif byday:
        for ordn, wd in byday:
            same = [dd for dd in range(1, n + 1) if date(y, m, dd).weekday() == wd]
            if ordn is None:
                days.update(same)
            else:
                i = ordn - 1 if ordn > 0 else ordn
                if -len(same) <= i < len(same):
                    days.add(same[i])
    elif d0.day <= n:
        days.add(d0.day)
    out = [date(y, m, dd) for dd in sorted(days)]
    pos = rule.get("BYSETPOS")
    if pos and out:
        out = sorted({out[p - 1 if p > 0 else p] for p in pos if -len(out) <= (p - 1 if p > 0 else p) < len(out)})
    return out


def _iter_rule(start_wall, rule):
    """The wall-clock starts of a repeating rule in order, beginning at the first one (endless; the
    caller stops it). Supports DAILY / WEEKLY / MONTHLY / YEARLY with INTERVAL, BYDAY (also 1MO, -1FR),
    BYMONTHDAY, BYMONTH, BYSETPOS and WKST."""
    is_dt = isinstance(start_wall, datetime)
    t = start_wall.time() if is_dt else None
    d0 = start_wall.date() if is_dt else start_wall
    freq, step = rule["FREQ"], max(1, rule.get("INTERVAL", 1))
    byday, bymd, bymo = rule.get("BYDAY"), rule.get("BYMONTHDAY"), rule.get("BYMONTH")

    def make(d):
        return datetime.combine(d, t) if is_dt else d

    empty = 0
    if freq == "DAILY":
        wds = {w for _, w in byday} if byday else None
        d = d0
        while empty < 5000:
            ok = (wds is None or d.weekday() in wds) and (not bymo or d.month in bymo)
            if ok and bymd:
                n = _cal.monthrange(d.year, d.month)[1]
                ok = d.day in bymd or (d.day - n - 1) in bymd
            if ok:
                empty = 0
                yield make(d)
            else:
                empty += 1
            d += timedelta(days=step)
    elif freq == "WEEKLY":
        wkst = rule.get("WKST", 0)
        wds = sorted({w for _, w in byday}, key=lambda w: (w - wkst) % 7) if byday else [d0.weekday()]
        ws = d0 - timedelta(days=(d0.weekday() - wkst) % 7)
        while True:
            for wd in wds:
                d = ws + timedelta(days=(wd - wkst) % 7)
                if d >= d0 and (not bymo or d.month in bymo):
                    yield make(d)
            ws += timedelta(weeks=step)
    elif freq == "MONTHLY":
        y, m = d0.year, d0.month
        while empty < 1000:
            got = [d for d in _month_dates(y, m, rule, d0) if d >= d0 and (not bymo or d.month in bymo)]
            empty = 0 if got else empty + 1
            for d in got:
                yield make(d)
            m += step
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
    else:                                                    # YEARLY
        y = d0.year
        months = sorted(bymo) if bymo else [d0.month]
        while empty < 1000:
            got = []
            for m in months:
                got += [d for d in _month_dates(y, m, rule, d0) if d >= d0]
            empty = 0 if got else empty + 1
            for d in sorted(got):
                yield make(d)
            y += step


class Occ:
    """One concrete occurrence of an event."""
    __slots__ = ("uid", "title", "location", "start", "end", "all_day")

    def __init__(self, uid, title, location, start, end, all_day):
        self.uid, self.title, self.location = uid, title, location
        self.start, self.end, self.all_day = start, end, all_day


def _until(stamp, st):
    """The UNTIL limit as an aware datetime (a bare date means 'to the end of that day')."""
    if stamp is None:
        return None
    if stamp.all_day:
        if st.all_day:
            return datetime.combine(stamp.wall, dtime.max).astimezone()
        return Stamp(datetime.combine(stamp.wall, dtime.max), st.tz, False).aware()
    return stamp.aware()


def _expand(ev, w0, w1, skip=()):
    """Occurrences of one event that overlap the window [w0, w1) (aware datetimes)."""
    st = ev.start
    if st.all_day:
        days = (ev.end.wall - st.wall).days if ev.end is not None and ev.end.all_day else 1
        dur = timedelta(days=max(1, days))
    elif ev.end is not None:
        dur = max(timedelta(0), ev.end.aware() - st.aware())
    elif ev.dur is not None:
        dur = max(timedelta(0), ev.dur)
    else:
        dur = timedelta(0)
    out = []

    def emit(wall):
        s = Stamp(wall, st.tz, st.all_day)
        if s.key() in skip:
            return
        a = s.aware().astimezone()           # always shown in YOUR PC's time zone (a UTC call at 12:00Z is 5:30 PM in India)
        b = a + dur
        if a < w1 and (b > w0 or (b == a and a >= w0)):
            out.append(Occ(ev.uid, ev.title, ev.location, a, b, st.all_day))

    if not ev.rule:
        emit(st.wall)
        return out
    rule = ev.rule
    count, until, n = rule.get("COUNT"), _until(rule.get("UNTIL"), st), 0
    for wall in _iter_rule(st.wall, rule):
        a = Stamp(wall, st.tz, st.all_day).aware()
        if until is not None and a > until:
            break
        n += 1
        if (count is not None and n > count) or a >= w1 or n > 60000:
            break
        emit(wall)
    return out


# ====================================================================== understanding the question
def normalize(c):
    c = c.lower().replace("\u2019", "'")
    c = re.sub(r"\b([ap])\s?\.\s?m\b\.?", r"\1m", c)
    c = c.replace("'", "")
    c = re.sub(r"[^a-z0-9: ]", " ", c)
    return " ".join(c.split())


def _midnight(d):
    return datetime.combine(d, dtime.min).astimezone()


def _end_of(d):
    return datetime.combine(d + timedelta(days=1), dtime.min).astimezone()


def parse_when(c, now):
    """Finds 'today / tomorrow / friday / next week / 25 october / tomorrow evening / at 5 pm' in a
    normalized sentence. Returns {'start','end','label','multi','part','at'} or None."""
    today = now.date()
    d0 = d1 = None
    label = None
    if re.search(r"\bday after tomorrow\b", c):
        d0, label = today + timedelta(days=2), "the day after tomorrow"
    elif re.search(r"\btomorrow\b", c):
        d0, label = today + timedelta(days=1), "tomorrow"
    elif re.search(r"\byesterday\b", c):
        d0, label = today - timedelta(days=1), "yesterday"
    elif re.search(r"\bnext week\b", c):
        d0 = today - timedelta(days=today.weekday()) + timedelta(days=7)
        d1, label = d0 + timedelta(days=6), "next week"
    elif re.search(r"\bthis week\b", c):
        d0, d1, label = today, today + timedelta(days=6 - today.weekday()), "this week"
    elif re.search(r"\b(?:this )?weekend\b", c):
        sat = today + timedelta(days=(5 - today.weekday()) % 7) if today.weekday() < 5 else today
        d0, d1, label = sat, today + timedelta(days=6 - today.weekday()) if today.weekday() >= 5 else sat + timedelta(days=1), "this weekend"
    elif re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening)|tonights)\b", c):
        d0, label = today, "tonight" if re.search(r"\btonight\b", c) else "today"
    else:
        m = re.search(r"\bin (\d+|a|an|one|two|three|four|five|six|seven|ten) days?\b", c)
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1)]
            d0, label = today + timedelta(days=n), f"in {n} day{'s' if n != 1 else ''}"
    if d0 is None:
        m = re.search(r"\b(?:(next|this|on|coming)\s+)?(" + "|".join(DAY_NAMES) + r")\b", c)
        if m:
            wd = DAY_NAMES.index(m.group(2))
            if m.group(1) == "next":
                d0 = today - timedelta(days=today.weekday()) + timedelta(days=7 + wd)
            else:
                d0 = today + timedelta(days=(wd - today.weekday()) % 7)
            label = "today" if d0 == today else f"on {m.group(2).title()}"
    if d0 is None:
        mon = "|".join(sorted(MONTHS, key=len, reverse=True))
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + mon + r")\b", c) or None
        day = month = None
        if m:
            day, month = int(m.group(1)), MONTHS[m.group(2)]
        else:
            m = re.search(r"\b(" + mon + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", c)
            if m:
                day, month = int(m.group(2)), MONTHS[m.group(1)]
        if day:
            try:
                d = date(today.year, month, day)
                if d < today - timedelta(days=1):
                    d = date(today.year + 1, month, day)
                d0, label = d, f"on {d.day} {d.strftime('%B')}"
            except ValueError:
                return None
        else:
            m = re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", c)
            if m:
                day = int(m.group(1))
                y, mo = today.year, today.month
                if day < today.day:
                    mo += 1
                    y += mo > 12
                    mo = (mo - 1) % 12 + 1
                try:
                    d0 = date(y, mo, day)
                    label = f"on the {day}{'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')}"
                except ValueError:
                    return None
    part, at = None, None
    for name, (h0, h1) in (("morning", (5, 12)), ("afternoon", (12, 17)), ("evening", (17, 22)),
                           ("night", (18, 24))):
        if re.search(r"\b" + name + r"\b", c) or (name == "night" and re.search(r"\btonight\b", c)):
            part = (name, h0, h1)
            break
    m = re.search(r"\bat (\d{1,2})(?:[: ]?(\d{2}))?\s*(am|pm)?\b", c)
    if m and 0 <= int(m.group(1)) <= 24:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif ap is None:
            if h == 12 or 1 <= h <= 6 or (part and part[0] in ("evening", "night") and h < 12):
                h = h + 12 if h < 12 else h
        if h < 24 and mi < 60:
            at = (h, mi)
    if d0 is None and part is None and at is None:
        return None
    if d0 is None:
        d0, label = today, "today"
    multi = d1 is not None and d1 != d0
    start, end = _midnight(d0), _end_of(d1 or d0)
    if not multi and at is not None:
        start = datetime.combine(d0, dtime(at[0], at[1])).astimezone()
        end = start + timedelta(hours=1)
        label += f" at {_clock(start)}"
    elif not multi and part is not None:
        start = datetime.combine(d0, dtime(part[1]))
        end = datetime.combine(d0, dtime(0)) + timedelta(hours=part[2])
        start, end = start.astimezone(), end.astimezone()
        if part[0] == "night":
            label = "tonight" if label in ("today", "tonight") else f"{label} night"
        else:
            label = f"this {part[0]}" if label == "today" else f"{label} {part[0]}"
    return {"start": start, "end": end, "label": label, "multi": multi, "part": part, "at": at}


def _clock(dt):
    h = dt.hour % 12 or 12
    ap = "AM" if dt.hour < 12 else "PM"
    return f"{h} {ap}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {ap}"


def _day_word(d, today):
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if 0 < (d - today).days < 7:
        return d.strftime("%A")
    return f"{d.day} {d.strftime('%B')}"


def _short(title, n=60):
    title = " ".join(title.split())
    return title if len(title) <= n else title[:n - 1].rstrip() + "\u2026"


def _join(items):
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _when_phrase(o, today):
    if o.all_day:
        return "all day"
    return "at " + _clock(o.start)


# ====================================================================== the agent
class CalendarAgent:
    def __init__(self, on_remind=None, on_notify=None, url_file=URL_FILE, cache_file=CACHE_FILE,
                 state_file=STATE_FILE):
        self.url_file, self.cache_file, self.state_file = url_file, cache_file, state_file
        self.on_remind = on_remind          # f(title, minutes_left, occ) -> True if it was announced
        self.on_notify = on_notify          # f(kind, text) -> True if delivered; kind = "briefing" | "preview"
        self.events = []
        self.by_uid = {}
        self.lock = threading.RLock()
        self.last_sync = None               # when the calendar was last downloaded
        self.last_error = ""
        self.state = {"briefed": "", "previewed": "", "briefing_on": True, "evening_on": True,
                      "reminders_on": True}
        self._load_state()
        self._notified = set()
        self._threads = []
        self._wake = threading.Event()
        self._load(self._read(self.cache_file))

    @property
    def reminders_on(self):
        return self.state["reminders_on"]

    # ------------------------------------------------------------ what he told you today (survives restarts)
    def _load_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state.update({k: v for k, v in json.load(f).items() if k in self.state})
        except (OSError, ValueError):
            pass

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    # ------------------------------------------------------------ data
    @property
    def url(self):
        u = os.environ.get("JARVIS_CALENDAR_URL", "")
        if not u and os.path.exists(self.url_file):
            try:
                with open(self.url_file, "r", encoding="utf-8") as f:
                    u = next((ln.strip() for ln in f if ln.strip() and not ln.startswith("#")), "")
            except OSError:
                u = ""
        return ("https://" + u[len("webcal://"):]) if u.lower().startswith("webcal://") else u

    @property
    def configured(self):
        return self.url.startswith("http")

    @staticmethod
    def _read(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def _load(self, text):
        if not text.strip():
            return
        events = parse_ics(text)
        by_uid = {}
        for ev in events:
            g = by_uid.setdefault(ev.uid or id(ev), {"master": None, "overrides": []})
            if ev.rid is not None:
                g["overrides"].append(ev)
            else:
                g["master"] = ev
        with self.lock:
            self.events, self.by_uid = events, by_uid

    def occurrences(self, w0, w1):
        """Every occurrence overlapping [w0, w1), sorted by start (cancelled ones are left out)."""
        out = []
        with self.lock:
            groups = list(self.by_uid.values())
        for g in groups:
            master, overrides = g["master"], g["overrides"]
            if master is not None and not master.cancelled:
                skip = {s.key() for s in master.exdates} | {o.rid.key() for o in overrides}
                out += _expand(master, w0, w1, skip)
            elif master is None:
                pass
            for o in overrides:
                if not o.cancelled:
                    out += _expand(o, w0, w1)
        out.sort(key=lambda o: (o.start, o.title))
        return out

    # ------------------------------------------------------------ syncing
    def sync(self):
        """Downloads the calendar now. Returns (ok, message). Never raises."""
        if not self.configured:
            return False, "no calendar link"
        if requests is None:
            return False, "the requests package is missing"
        try:
            r = requests.get(self.url, timeout=20, headers={"User-Agent": "Jarvis-Calendar"})
            r.raise_for_status()
            text = r.text
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("that link did not return a calendar")
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, self.cache_file)
            self._load(text)
            self.last_sync, self.last_error = time.time(), ""
            return True, "ok"
        except Exception as e:
            # never put the link itself in an error message: it is a secret
            self.last_error = type(e).__name__ + (": " + str(e).replace(self.url, "[link]")[:120] if str(e) else "")
            return False, self.last_error

    def start(self):
        """Starts the background sync + reminder threads."""
        if self._threads:
            return
        for fn in (self._sync_loop, self._remind_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def _sync_loop(self):
        while True:
            try:
                if self.configured:
                    ok, msg = self.sync()
                    if not ok:
                        print(f"[calendar] sync failed ({msg}); using the saved copy")
            except Exception:
                traceback.print_exc()
            self._wake.wait(SYNC_MINUTES * 60)
            self._wake.clear()

    def _remind_loop(self):
        while True:
            time.sleep(30)
            try:
                self.check_reminders()
                self.check_proactive()
            except Exception:
                traceback.print_exc()

    def check_reminders(self, now=None):
        """Announces events that start within REMIND_MINUTES (each one once)."""
        if not (REMIND_MINUTES and self.reminders_on and self.on_remind and self.events):
            return
        now = now or datetime.now().astimezone()
        for o in self.occurrences(now, now + timedelta(minutes=REMIND_MINUTES + 1)):
            if o.all_day or o.start < now - timedelta(minutes=1):
                continue
            key = (o.uid, o.start.isoformat())
            left = (o.start - now).total_seconds() / 60
            if key in self._notified or left > REMIND_MINUTES:
                continue
            if self.on_remind(o.title, max(0, round(left)), o):
                self._notified.add(key)
        if len(self._notified) > 500:
            self._notified.clear()

    # ------------------------------------------------------------ Jarvis speaks first
    def _day_parts(self, d, now, only_remaining):
        occs = self.occurrences(_midnight(d), _end_of(d))
        timed = [o for o in occs if not o.all_day and (not only_remaining or o.end > now or o.start >= now)]
        return timed, [o for o in occs if o.all_day]

    def briefing_text(self, now):
        """Today's events (the ones still to come), plus all-day ones like birthdays. None if nothing."""
        today = now.date()
        timed, allday = self._day_parts(today, now, True)
        parts = []
        if timed:
            parts.append(self._read_out(timed, "today", False, today))
        if allday:
            parts.append("All day today: " + _join([_short(o.title) for o in allday]) + ".")
        return " ".join(parts) or None

    def preview_text(self, now):
        """Tomorrow's events + all-day ones. None if nothing."""
        today = now.date()
        timed, allday = self._day_parts(today + timedelta(days=1), now, False)
        parts = []
        if timed:
            parts.append(self._read_out(timed, "tomorrow", False, today))
        if allday:
            parts.append("All day tomorrow: " + _join([_short(o.title) for o in allday]) + ".")
        return " ".join(parts) or None

    def check_proactive(self, now=None):
        """Messages you first, once a day: the briefing from BRIEFING_HOUR (until the evening) and the
        preview of tomorrow from EVENING_HOUR. If Jarvis is busy the delivery is retried 30 s later."""
        if not (self.on_notify and self.events):
            return
        now = now or datetime.now().astimezone()
        today = now.date().isoformat()
        st = self.state
        if st["briefing_on"] and st["briefed"] != today and BRIEFING_HOUR <= now.hour < EVENING_HOUR:
            body = self.briefing_text(now)
            if body is None or self.on_notify("briefing", body):       # nothing on today: stay quiet
                st["briefed"] = today
                self._save_state()
        if st["evening_on"] and st["previewed"] != today and now.hour >= EVENING_HOUR:
            body = self.preview_text(now)
            if body is None or self.on_notify("preview", body):
                st["previewed"] = today
                self._save_state()

    def _switch(self, c):
        """'turn off the morning briefing', 'enable reminders', 'stop messaging me first'. Returns text or None."""
        off = re.search(r"\b(?:off|stop|disable|dont|do not|no more|never)\b", c)
        on = re.search(r"\b(?:on|enable|start|resume|again)\b", c)
        first = re.search(r"\b(?:message|messaging|messages|texting|text|speaking|speak|talking|talk)\b.*\bme first\b|\bproactive\b", c)
        if not (off or on or first):
            return None
        want = not off                                      # "message me first" alone means: switch it on
        if first:
            for k in ("briefing_on", "evening_on", "reminders_on"):
                self.state[k] = want
            self._save_state()
            return ("Okay. I'll message you first about your calendar." if want else
                    "Okay. I'll stay quiet until you ask. No briefings and no reminders.")
        if not (off or on):
            return None
        for pattern, key, name in ((r"\bevening (?:preview|briefing|heads up|summary)\b", "evening_on", "the evening preview"),
                                   (r"\b(?:morning|daily) (?:briefing|brief|summary)\b|\bbriefing\b", "briefing_on", "the morning briefing"),
                                   (r"\b(?:calendar|event|meeting) reminders?\b|\breminders? (?:for|about) (?:my )?(?:events|meetings|calendar)\b", "reminders_on", "calendar reminders")):
            if re.search(pattern, c):
                self.state[key] = want
                self._save_state()
                return f"Okay, {name} {'on' if want else 'off'}."
        return None

    # ------------------------------------------------------------ answering
    def answer(self, c, now=None):
        """Understands a calendar question. Returns (speech, detail) - detail is the full list for the
        screen, or None - or None if the sentence is not about the calendar."""
        c = normalize(c)
        now = now or datetime.now().astimezone()
        cal_word = re.search(r"\b(?:calendar|schedule|agenda|meetings?|appointments?|events?)\b", c)

        if re.search(r"\b(?:refresh|sync|reload|update)\b.*\bcalendar\b|\bcalendar\b.*\b(?:refresh|sync|reload)\b", c):
            if not self.configured:
                return self._setup_help(), None
            ok, msg = self.sync()
            return ("Calendar updated." if ok else f"I couldn't reach your calendar. {self._why(msg)}"), None
        if re.search(r"\bcalendar status\b|\bis my calendar (?:connected|synced|working)\b", c):
            return self._status(now), None
        switched = self._switch(c)
        if switched:
            return switched, None
        if re.search(r"\bbrief me\b|\b(?:give|read|tell) me (?:my |the )?(?:daily |morning |evening |todays |tomorrows )?(?:briefing|brief)\b"
                     r"|^(?:morning|daily|evening) briefing$", c):
            if not self.events:
                return (self._setup_help() if not self.configured else "I haven't loaded your calendar yet."), None
            tomorrow = re.search(r"\b(?:tomorrow|tomorrows|evening)\b", c)
            body = self.preview_text(now) if tomorrow else self.briefing_text(now)
            return (body or ("Nothing on your calendar tomorrow." if tomorrow else
                             "Your calendar is clear for the rest of today.")), None
        if cal_word and re.match(r"^(?:add|create|schedule|book|set up|put|make|cancel|delete|remove|move|reschedule|change)\b", c) \
                and re.search(r"\b(?:calendar|meeting|appointment|event)\b", c):
            return "I can only read your calendar for now. I can't add, move or cancel events.", None

        when = parse_when(c, now)
        personal = re.search(r"\b(?:my|i have|ive got|i got|do i have|have i got|am i)\b", c)
        q_word = re.search(r"\b(?:what|whats|which|any|anything|show|read|tell|list|how many|do i have)\b", c)
        asks_next = re.search(r"\bnext (?:meeting|event|appointment|thing)\b|\bwhats next on my\b|\bwhen is my next\b"
                              r"|\bwhat is next on my\b|\bwhats my next\b|\bwhat is my next\b", c)
        asks_free = re.search(r"\b(?:am i|are you|will i be|do i have time|is my (?:calendar|schedule|day))\b.*\b(?:free|busy|available|open|clear)\b"
                              r"|\b(?:free|busy|available)\b.*\b(?:am i)\b", c)
        asks_agenda = bool(
            (re.search(r"\b(?:calendar|schedule|agenda)\b", c) and (personal or when))
            or (re.search(r"\b(?:meetings?|appointments?|events?)\b", c) and (personal or (when and re.search(r"\bany\b", c))))
            or (when and re.search(r"\bwhat (?:do|have) i (?:have|got)\b|\bwhat am i (?:doing|up to)\b"
                                   r"|\bdo i have (?:anything|any plans|something|plans)\b|\bany plans\b", c)))
        if not asks_next and re.match(r"^when (?:is|s|are) ", c):
            found = self._find_event(c, now)
            if found:
                return found
            if not (asks_free or (when and asks_agenda)):
                return None
        if not (asks_next or asks_free or asks_agenda):
            return None
        if not self.configured and not self.events:
            return self._setup_help(), None
        if not self.events and self.last_sync is None:
            return "I haven't loaded your calendar yet. Say 'refresh calendar' once you're online.", None

        if asks_next:
            return self._next(now), None
        if asks_free:
            return self._free(when, now)
        return self._agenda(when, now)

    # --- the individual answers
    def _agenda(self, when, now):
        when = when or parse_when("today", now)
        w0, w1, label = when["start"], when["end"], when["label"]
        occs = self.occurrences(w0, w1)
        shown = [o for o in occs if o.end > now or (o.end == o.start and o.start >= now)] if w0 <= now < w1 else occs
        today = now.date()
        if not occs:
            return f"You have nothing on your calendar {label}.", None
        if not shown:
            return f"You have nothing else on your calendar {label}.", None
        speech = self._read_out(shown, label, when["multi"], today)
        detail = "\n".join(self._line(o, today, when["multi"]) for o in shown)
        return speech, (detail if len(shown) > 1 or when["multi"] else None)

    def _line(self, o, today, multi):
        pre = (_day_word(o.start.date(), today).title() + "  ") if multi else ""
        when = "all day" if o.all_day else _clock(o.start) + ("-" + _clock(o.end) if o.end > o.start else "")
        return f"{pre}{when}  {o.title}" + (f"  ({o.location})" if o.location else "")

    def _read_out(self, occs, label, multi, today):
        n = len(occs)
        head = f"You have {'one event' if n == 1 else str(n) + ' events'} {label}"
        use = occs[:SPEAK_LIMIT]
        if multi:
            days = []
            for d in sorted({o.start.date() for o in use}):
                items = [f"{_short(o.title)} {_when_phrase(o, today)}" for o in use if o.start.date() == d]
                if d == today:
                    name = "Today"
                elif d == today + timedelta(days=1):
                    name = "Tomorrow"
                elif (d - today).days < 7:
                    name = d.strftime("%A")
                else:
                    name = f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"
                days.append(f"{name}: {_join(items)}")
            body = ". ".join(days)
        else:
            body = _join([f"{_short(o.title)} {_when_phrase(o, today)}" for o in use])
        more = f". And {n - len(use)} more, they're on screen" if n > len(use) else ""
        return f"{head}: {body}{more}."

    def _next(self, now):
        occs = [o for o in self.occurrences(now, now + timedelta(days=30)) if o.end > now or o.start >= now]
        occs = [o for o in occs if o.start >= now] or occs
        if not occs:
            return "I don't see anything coming up in the next 30 days."
        o = occs[0]
        today = now.date()
        mins = int((o.start - now).total_seconds() // 60)
        if o.start <= now < o.end and not o.all_day:
            return f"{_short(o.title)} is happening right now, until {_clock(o.end)}."
        if o.all_day:
            return f"Your next event is {_short(o.title)}, {_day_word(o.start.date(), today)}, all day."
        rel = ""
        if 0 <= mins < 60:
            rel = f", in {mins} minute{'s' if mins != 1 else ''}"
        elif 60 <= mins < 360:
            rel = f", in {mins // 60} hour{'s' if mins // 60 != 1 else ''}"
        return f"Your next event is {_short(o.title)}, {_day_word(o.start.date(), today)} at {_clock(o.start)}{rel}."

    def _free(self, when, now):
        when = when or parse_when("today", now)
        occs = [o for o in self.occurrences(when["start"], when["end"]) if o.end > now]
        label, today = when["label"], now.date()
        day_label = label.split(" at ")[0] if when["at"] else label       # "on Friday at 3 PM" -> "on Friday"
        if not occs:
            return f"Yes, you're free {label}.", None
        if when["at"] or when["part"]:
            first = occs[0]
            return (f"No, you have {_short(first.title)} {_when_phrase(first, today)} {day_label}"
                    + (f", and {len(occs) - 1} more" if len(occs) > 1 else "") + "."), None
        speech = self._read_out(occs, label, when["multi"], today)
        return "You're busy. " + speech, "\n".join(self._line(o, today, when["multi"]) for o in occs)

    def _find_event(self, c, now):
        """'when is my dentist appointment' - answered only if the calendar really has such an event."""
        m = re.search(r"\bwhen (?:is|s) (?:my |the )?(.+?)(?: appointment| meeting| event)?$", c)
        if not m or not self.events:
            return None
        words = [w for w in re.split(r"\s+", m.group(1)) if w not in ("next", "my", "the", "a", "an", "is", "on")]
        if not words or len(m.group(1)) > 40:
            return None
        hits = [o for o in self.occurrences(now, now + timedelta(days=120))
                if all(w in o.title.lower() for w in words)]
        if not hits:
            return None
        o, today = hits[0], now.date()
        return f"{_short(o.title)} is {_day_word(o.start.date(), today)} {_when_phrase(o, today)}.", None

    def _status(self, now):
        if not self.configured:
            return self._setup_help()
        n = len(self.occurrences(now, now + timedelta(days=30)))
        if self.last_sync:
            mins = int((time.time() - self.last_sync) // 60)
            ago = "just now" if mins < 1 else f"{mins} minute{'s' if mins != 1 else ''} ago"
            return f"Your calendar was updated {ago}, with {n} events in the next 30 days."
        if self.events:
            return f"I'm using the saved copy of your calendar, {n} events in the next 30 days. The last update failed."
        return "I haven't been able to load your calendar yet. " + self._why(self.last_error)

    @staticmethod
    def _why(msg):
        m = (msg or "").lower()
        if "connection" in m or "timeout" in m or "resolve" in m or "max retries" in m:
            return "It looks like there's no internet right now."
        if "404" in m or "403" in m or "did not return" in m:
            return "The link may be wrong or has been reset. Check calendar_url.txt."
        return "Check calendar_url.txt and your internet connection." if msg else ""

    @staticmethod
    def _setup_help():
        return ("I need your calendar link first. In Google Calendar open Settings, pick your calendar, "
                "copy the secret address in iCal format, and paste it into a file called "
                "calendar_url.txt next to voice.py. Then say refresh calendar.")
