"""
habits_agent.py - habit tracking for Mini-Jarvis.

START     "start tracking meditation"   "new habit workout 4 times a week"   "track a habit called drinking water"
LOG       "I worked out"   "log that I meditated"   "I drank water"   "check in reading"   "I ran yesterday"
          "I did my workout two days ago"
CHECK     "how's my workout streak"   "what's my streak"   "did I meditate today"   "which habits are left today"
          "weekly habit summary"   "how am I doing with my habits"   "what habits am I tracking"
FIX       "I didn't actually work out today"   "undo my last habit log"   "set my workout goal to 3 times a week"
          "stop tracking meditation"  (asks first)

How it counts: one check-in per habit per day. A new day starts at 4 AM, so logging your late-night workout
at 1 AM still counts for the day that just ended. A streak is the days in a row up to today (a streak is not
broken until a whole day passes without a check-in). Weeks run Monday to Sunday.

"I worked out" only logs when it matches a habit you are tracking, so normal chat is never mistaken for a check-in.
Different wordings match the same habit (workout / worked out / working out, meditation / meditated / meditating).

Everything is saved in habits.json next to voice.py.

voice.py speaks the replies. handle() returns {"say": text, "show": text for the screen (optional)} or
{"confirm": question, "do": function() -> dict} when it needs a yes first, or None if it is not a habit command.
"""
import datetime as dt
import difflib
import json
import os
import re

DAY_STARTS_HOUR = 4                 # before 4 AM still counts as yesterday
MILESTONES = (3, 7, 14, 21, 30, 50, 75, 100, 150, 200, 365)

_NUMS = {"once": 1, "one": 1, "twice": 2, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_IRREGULAR = {"ran": "run", "drank": "drink", "ate": "eat", "wrote": "write", "swam": "swim", "went": "go",
              "slept": "sleep", "did": "do", "studied": "study", "studying": "study", "cycled": "cycl", "biked": "bik",
              "jogged": "jog", "stretched": "stretch", "walked": "walk"}
_STOP = {"my", "the", "a", "an", "some", "for", "to", "of", "i", "today", "just", "did", "do", "done", "had", "have",
         "minutes", "minute", "hour", "hours", "again", "habit", "session", "time", "out", "in", "at", "on", "went",
         "go", "was", "am", "and", "then", "also", "really", "actually", "finally", "now"}


def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


def _canon(text):
    """Words reduced so 'worked out', 'working out' and 'workout' all become 'workout'."""
    t = _norm(text)
    t = re.sub(r"\bwork(?:ed|ing|s)? out\b", "workout", t)
    t = re.sub(r"\bwork out\b", "workout", t)
    t = re.sub(r"\bworkouts\b", "workout", t)
    return t


def _stem(w):
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    for suf in ("ing", "ed", "es", "s", "ion", "e"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    if len(w) > 3 and w[-1] == w[-2]:
        w = w[:-1]                                   # runn -> run
    return w


def _keys(text):
    return {_stem(w) for w in _canon(text).split() if w not in _STOP}


def _goal_from(text):
    """(text without the goal, goal per week or None)."""
    m = re.search(r"\b(\d+|once|twice|one|two|three|four|five|six|seven) (?:times? )?(?:a|per|each|every) week\b", text)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMS[m.group(1)]
        return (text[:m.start()] + text[m.end():]).strip(), max(1, min(7, n))
    m = re.search(r"\b(every ?day|daily|each day|seven days a week)\b", text)
    if m:
        return (text[:m.start()] + text[m.end():]).strip(), 7
    m = re.search(r"\bon weekdays\b|\bweekdays\b", text)
    if m:
        return (text[:m.start()] + text[m.end():]).strip(), 5
    return text, None


def _when(text):
    """(text without the day words, days ago)."""
    for pat, back in ((r"\b(?:two|2) days ago\b", 2), (r"\b(?:three|3) days ago\b", 3),
                      (r"\b(?:yesterday|last night|yesterday evening|yesterday morning)\b", 1),
                      (r"\b(?:today|this morning|this afternoon|this evening|tonight|earlier today)\b", 0)):
        m = re.search(pat, text)
        if m:
            return (text[:m.start()] + " " + text[m.end():]).strip(), back
    return text, 0


def _clean_name(x):
    x = _norm(x)
    x = re.sub(r"^(?:a habit |habit )?(?:called |named |of |for )?(?:my |the |a |an )?", "", x)
    x = re.sub(r"^(?:called |named )", "", x)
    return x.strip()


def _join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def streak_of(days, today):
    d = today if today in days else today - dt.timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= dt.timedelta(days=1)
    return n


def longest_streak(days):
    best = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    return best


class Habits:
    def __init__(self, path):
        self.path = path
        self.habits = {}
        self._load()

    # ----- the file -----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            for name, h in (d.get("habits") or {}).items():
                if isinstance(h, dict):
                    self.habits[name] = {"name": h.get("name") or name, "goal": h.get("goal"),
                                         "created": h.get("created") or dt.date.today().isoformat(),
                                         "log": sorted({x for x in h.get("log", []) if isinstance(x, str)})}
        except Exception:
            pass

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"habits": self.habits}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def today(now=None):
        now = now or dt.datetime.now()
        return (now - dt.timedelta(hours=DAY_STARTS_HOUR)).date()

    def _days(self, h):
        return {dt.date.fromisoformat(x) for x in h["log"]}

    # ----- finding a habit by what you said -----
    def find(self, text, exact=False):
        keys = _keys(text)
        if not keys or not self.habits:
            return None
        best, best_score = None, 0.0
        for name, h in self.habits.items():
            hk = _keys(h["name"])
            if not hk:
                continue
            if exact and not all(any(k == j or difflib.SequenceMatcher(None, k, j).ratio() >= 0.8 for j in hk) for k in keys):
                continue                                     # "I read the news" is not your "reading" habit
            overlap = len(keys & hk) / len(hk)
            fuzzy = 0.0
            if overlap < 1:                                  # allow small mishearings (meditashun, excersize)
                fuzzy = sum(1 for k in keys for j in hk if difflib.SequenceMatcher(None, k, j).ratio() >= 0.8) / len(hk)
            score = max(overlap, min(fuzzy, 1.0) * 0.9)
            if score > best_score:
                best, best_score = name, score
        return best if best_score >= 0.75 else None

    # ----- actions -----
    def start(self, raw, goal=None):
        name = _clean_name(raw)
        if not name:
            return {"say": "What habit should I track?"}
        if self.find(name):
            return {"say": f"You're already tracking {self.habits[self.find(name)]['name']}."}
        self.habits[name] = {"name": name, "goal": goal, "created": self.today().isoformat(), "log": []}
        self._save()
        extra = f", with a goal of {goal} day{'s' if goal != 1 else ''} a week" if goal else ""
        return {"say": f"Okay, I'll track {name}{extra}. Tell me when you do it, like I did my {name}."}

    def log(self, key, back=0, now=None):
        h = self.habits[key]
        today = self.today(now)
        day = today - dt.timedelta(days=back)
        label = {0: "today", 1: "yesterday"}.get(back, f"{back} days ago")
        if day.isoformat() in h["log"]:
            return {"say": f"You already logged {h['name']} {label}."}
        h["log"] = sorted(set(h["log"]) | {day.isoformat()})
        self._save()
        days = self._days(h)
        n = streak_of(days, today)
        say = f"Logged {h['name']} for {label}."
        if n >= 2:
            say += f" That's a {n} day streak." if n in MILESTONES else f" Streak: {n} days."
        elif n == 1 and len(days) == 1:
            say += " That's your first one. Nice start."
        if h.get("goal"):
            monday = today - dt.timedelta(days=today.weekday())
            done = len([d for d in days if monday <= d <= today])
            say += f" {done} of {h['goal']} this week."
            if done == h["goal"]:
                say += " Goal reached!"
        return {"say": say}

    def unlog(self, key, back=0, now=None):
        h = self.habits[key]
        day = (self.today(now) - dt.timedelta(days=back)).isoformat()
        if day not in h["log"]:
            return {"say": f"You haven't logged {h['name']} {'today' if back == 0 else 'then'}."}
        h["log"].remove(day)
        self._save()
        return {"say": f"Okay, I took {h['name']} off {'today' if back == 0 else 'that day'}."}

    def streak_report(self, key=None, now=None):
        today = self.today(now)
        if not self.habits:
            return {"say": "You're not tracking any habits yet. Say start tracking, then the habit."}
        keys = [key] if key else list(self.habits)
        rows = []
        for k in keys:
            h = self.habits[k]
            days = self._days(h)
            rows.append((h["name"], streak_of(days, today), longest_streak(days), today in days))
        if key:
            name, s, best, done = rows[0]
            if s == 0:
                return {"say": f"Your {name} streak is at zero. Your best is {best} day{'s' if best != 1 else ''}. "
                               "Today is a good day to start again."}
            return {"say": f"Your {name} streak is {s} day{'s' if s != 1 else ''}"
                           f"{'' if done else ', and you have not done it yet today'}. Your best is {best}."}
        live = [(n, s) for n, s, _, _ in rows if s > 0]
        if not live:
            return {"say": "None of your habits have a streak going right now."}
        live.sort(key=lambda x: -x[1])
        return {"say": "Your streaks: " + _join(f"{n} {s} day{'s' if s != 1 else ''}" for n, s in live[:5]) + "."}

    def today_report(self, now=None):
        today = self.today(now)
        if not self.habits:
            return {"say": "You're not tracking any habits yet."}
        done = [h["name"] for h in self.habits.values() if today.isoformat() in h["log"]]
        left = [h["name"] for h in self.habits.values() if today.isoformat() not in h["log"]]
        if not left:
            return {"say": "You've done all your habits today: " + _join(done) + ". Well done."}
        say = ("Still to do today: " + _join(left) + ".")
        if done:
            say = "Done today: " + _join(done) + ". " + say
        return {"say": say}

    def did_today(self, key, now=None):
        h = self.habits[key]
        return {"say": f"Yes, you logged {h['name']} today." if self.today(now).isoformat() in h["log"]
                else f"Not yet. You haven't logged {h['name']} today."}

    def list_habits(self):
        if not self.habits:
            return {"say": "You're not tracking any habits yet. Say start tracking, then the habit."}
        parts = [h["name"] + (f" ({h['goal']} a week)" if h.get("goal") else "") for h in self.habits.values()]
        n = len(parts)
        return {"say": f"You're tracking {n} habit{'s' if n != 1 else ''}: {_join(parts)}."}

    def summary(self, now=None):
        today = self.today(now)
        if not self.habits:
            return {"say": "You're not tracking any habits yet."}
        monday = today - dt.timedelta(days=today.weekday())
        lines = ["Week of " + monday.strftime("%b %d"), " " * 16 + "M T W T F S S"]
        spoken, best = [], ("", 0)
        for h in self.habits.values():
            days = self._days(h)
            marks = []
            for i in range(7):
                d = monday + dt.timedelta(days=i)
                marks.append("·" if d > today else "●" if d in days else "○")
            done = len([d for d in days if monday <= d <= today])
            goal = h.get("goal")
            lines.append(f"{h['name'][:15]:<16}" + " ".join(marks) + f"   {done}" + (f"/{goal}" if goal else ""))
            spoken.append(f"{h['name']} {done}" + (f" of {goal}" if goal else f" day{'s' if done != 1 else ''}"))
            s = streak_of(days, today)
            if s > best[1]:
                best = (h["name"], s)
        say = f"This week so far: {_join(spoken[:6])}."
        if best[1] >= 2:
            say += f" Best streak right now: {best[0]}, {best[1]} days."
        left = [h["name"] for h in self.habits.values() if today.isoformat() not in h["log"]]
        if left:
            say += f" Not done today yet: {_join(left[:4])}."
        return {"say": say, "show": "\n".join(lines)}

    def set_goal(self, key, goal):
        self.habits[key]["goal"] = goal
        self._save()
        return {"say": f"Goal set: {self.habits[key]['name']}, {goal} day{'s' if goal != 1 else ''} a week."}

    def delete(self, key):
        name = self.habits[key]["name"]
        n = len(self.habits[key]["log"])

        def do():
            self.habits.pop(key, None)
            self._save()
            return {"say": f"Okay, I've stopped tracking {name}."}
        return {"confirm": f"Stop tracking {name} and delete its {n} check-in{'s' if n != 1 else ''}?", "do": do}

    # ----- what you said -----
    def handle(self, command, now=None):
        t = _norm(command)
        if not t:
            return None
        h = self.habits

        # --- delete / goal / start ---
        m = re.fullmatch(r"(?:stop tracking|delete|remove|drop|forget) (?:the |my )?(?:habit )?(?P<x>.+?)(?: habit)?", t)
        if m and ("stop tracking" in t or re.search(r"\bhabit\b", t)):
            k = self.find(m.group("x"))
            return self.delete(k) if k else (
                {"say": f"I'm not tracking {m.group('x')}."} if "stop tracking" in t or "habit" in t else None)
        m = (re.fullmatch(r"(?:set|change|make|update) (?:my )?(?P<x>.+?)(?: habit)?(?: goal| target) (?:to|at) (?P<n>.+)", t)
             or re.fullmatch(r"my (?P<x>.+?) goal is (?P<n>.+)", t))
        if m:
            _, g = _goal_from(m.group("n") if re.search(r"week|day", m.group("n")) else m.group("n") + " times a week")
            k = self.find(m.group("x"))
            if k and g:
                return self.set_goal(k, g)
            if k:
                return {"say": "Say it like: set my workout goal to 4 times a week."}
        m = (re.fullmatch(r"(?:please )?(?:start|begin) tracking (?P<x>.+)", t)
             or re.fullmatch(r"(?:please )?(?:track|add|create|start|new) (?:a |another |new )?habit(?: called| named| of| for)? (?P<x>.+)", t)
             or re.fullmatch(r"i want to (?:build|start|track) (?:a )?habit (?:of|called|for) (?P<x>.+)", t))
        if m:
            x, goal = _goal_from(m.group("x"))
            return self.start(x, goal)

        # --- undo ---
        m = (re.fullmatch(r"(?:undo|remove|delete|cancel) (?:my )?(?:last )?(?:habit )?(?:log|check in|checkin|entry)(?: for (?P<x>.+))?", t)
             or re.fullmatch(r"i (?:didnt|did not|havent|have not) (?:actually |really )?(?P<x>.+?)(?: (?:today|yet|yesterday))?", t)
             or re.fullmatch(r"unlog (?P<x>.+)", t))
        if m:
            x, back = _when(m.group("x") or "")
            if "yesterday" in t:
                back = 1
            k = self.find(x) if x else (self._last_logged() if h else None)
            if k:
                return self.unlog(k, back, now)
            if re.search(r"\b(undo|unlog|log|check in)\b", t):
                return {"say": "I couldn't tell which habit to undo."}

        # --- questions ---
        if re.fullmatch(r"(?:please )?(?:give me |show me |read me |read )?(?:my )?(?:weekly )?habit (?:summary|report|review|status|stats)|"
                        r"(?:give me |show me |read me |read )?(?:my )?weekly (?:habit )?(?:summary|report|review)|"
                        r"how am i doing(?: with| on)?(?: my)? habits|how are my habits(?: going)?|how did i do (?:this|last) week(?: with my habits)?|"
                        r"habit summary", t):
            return self.summary(now)
        if re.fullmatch(r"what habits (?:am i|do i) (?:track|tracking|have)|(?:show|read|list|what are) (?:me )?(?:my )?habits|what are my habits", t):
            return self.list_habits()
        if re.fullmatch(r"(?:which|what) habits (?:have i|did i) (?:done|do|completed|logged)(?: today)?|"
                        r"(?:which|what) habits (?:are|have i) (?:left|remaining)(?: today| to do)?|what habits (?:are )?left(?: today)?|"
                        r"what s left (?:for )?today(?: habit| habits)?|habits (?:left|for today)", t):
            return self.today_report(now)
        m = re.fullmatch(r"(?:what s|whats|what is|how s|hows|how is|how are) (?:my )?(?P<x>.+?) streak|"
                         r"(?:how many days|how long)(?: in a row| straight)? (?:have i|i have) (?:been )?(?P<x2>.+)", t)
        if m:
            x = m.group("x") or m.group("x2")
            if x in ("my", "the", "") and not self.habits:
                return self.streak_report(None, now)
            k = self.find(x)
            if k:
                return self.streak_report(k, now)
        if re.fullmatch(r"(?:what s|whats|what is|how s|hows) my streak|my streaks?|(?:show|read|what are) my streaks", t):
            return self.streak_report(None, now)
        m = re.fullmatch(r"(?:did i|have i)(?: already)? (?:do |done |log |logged |complete |completed )?(?P<x>.+?) today", t)
        if m:
            k = self.find(m.group("x"))
            if k:
                return self.did_today(k, now)

        # --- logging a check-in ---
        body, bare = t, False
        m = re.match(r"^(?:please )?(?:log|record|check in|checkin|tick(?: off)?|mark(?: off)?|count)(?: that)?(?: i)?(?: did| had| went for| went to| just did)?\s+(?P<x>.+)", t)
        if m:
            body = m.group("x")
            body = re.sub(r"\b(?:as )?(?:done|complete|completed|finished)\b", " ", body)
            body = re.sub(r"\bfor today\b", " today", body)
        else:
            m = re.match(r"^(?:i )?(?:just )?(?:did|finished|completed|had|went for|went to|am done with|done with|ve done|have done)\s+(?P<x>.+)", t)
            if m:
                body = m.group("x")
            else:
                m = re.fullmatch(r"i (?:just )?(?P<x>[a-z ]+)", t)
                if m and len(m.group("x").split()) <= 6:
                    body, bare = m.group("x"), True
                else:
                    return None
        body, back = _when(body)
        k = self.find(body, exact=bare)
        if k:
            return self.log(k, back, now)
        if re.match(r"^(?:please )?(?:log|record|check in|checkin)\b", t) and body.strip():
            return {"say": f"I'm not tracking {_clean_name(body)}. Say start tracking {_clean_name(body)} to add it."}
        return None

    def _last_logged(self):
        best, when = None, ""
        for k, h in self.habits.items():
            if h["log"] and h["log"][-1] >= when:
                best, when = k, h["log"][-1]
        return best