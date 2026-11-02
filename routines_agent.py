"""
routines_agent.py - routines ("study mode") for Mini-Jarvis.

Say  "study mode"  /  "start focus mode"  /  "let's study"  and Echo sets up your computer in one go:
closes the distractions, opens what you work in, and starts a focus timer.

Built-in routines (you can change them or add your own in routines.json, next to voice.py):
  study      closes Discord and Steam, opens PyCharm, starts a 25 minute focus timer
  break      starts a 5 minute break timer
  wind down  turns the volume down for the evening

Voice commands:
  "study mode" / "start study mode" / "activate focus mode" / "let's study" / "take a break"
  "end study mode" / "stop focus mode" / "back to normal"     (cancels that routine's timer)
  "what routines do I have"      "what does study mode do"

Safety: a routine only ever closes apps with the normal polite close (like clicking the X, so apps still ask
you to save), never touches Jarvis / Ollama / Windows itself, and it asks ONE yes/no before closing anything.
Apps that are not running are skipped; apps that are already open are not opened a second time.

routines.json looks like this (add your own the same way; the file is created the first time Jarvis starts):
  "gym": {
    "aliases": ["workout mode", "time to work out"],
    "say": "Gym mode on.",
    "end_say": "Gym mode off.",
    "steps": [
      {"close": ["discord"]},
      {"open": ["spotify"]},
      {"site": ["https://www.youtube.com"]},
      {"timer": {"minutes": 45, "name": "workout"}},
      {"volume": 60}
    ]
  }
Step types:  close (app names), open (app names), site (web addresses or names like "youtube"),
             timer ({"minutes": N, "name": "..."}), volume (0-100).

This file reads/saves routines.json and understands what you said. voice.py does the opening, closing and timers.
"""
import copy
import json
import os
import re

DEFAULTS = {
    "study": {
        "aliases": ["focus", "focus mode", "study time", "focus time", "lets study", "lets focus", "time to study",
                    "time to focus", "start studying", "i need to study", "i need to focus"],
        "say": "Study mode on. Twenty five minutes, let's focus.",
        "end_say": "Study mode off. Nice work.",
        "steps": [{"close": ["discord", "steam"]},
                  {"open": ["pycharm"]},
                  {"timer": {"minutes": 25, "name": "focus"}}],
    },
    "break": {
        "aliases": ["break time", "take a break", "time for a break", "lets take a break", "short break", "coffee break"],
        "say": "Break time. I'll tell you when five minutes are up.",
        "end_say": "Break over. Back to it.",
        "steps": [{"timer": {"minutes": 5, "name": "break"}}],
    },
    "wind down": {
        "aliases": ["winddown", "bedtime", "bedtime mode", "night mode", "evening mode"],
        "say": "Winding down. Volume is low.",
        "end_say": "Wind down is off.",
        "steps": [{"volume": 25}],
    },
}

_FILLER = re.compile(r"\b(?:please|for me|now|thanks|thank you)\b")
_RUN_PREFIX = re.compile(r"^(?:start|activate|enable|turn on|begin|enter|switch to|switch on|run|launch|go into|go to|"
                         r"put me in|set up|set)(?: up)?(?: my| the)? ")
_END_PREFIX = re.compile(r"^(?:end|stop|exit|leave|finish|turn off|disable|deactivate|quit|cancel)(?: my| the)? ")


def _norm(c):
    t = " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())
    return " ".join(_FILLER.sub(" ", t).split())


class Routines:
    def __init__(self, path):
        self.path = path
        self.routines = {}
        self.active = None                   # name of the routine that is on right now (not saved)
        self.load_error = None
        self._load()

    # ----- the file -----
    def _load(self):
        data = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("routines.json must hold an object like {\"study\": {...}}")
            except Exception as e:
                self.load_error = f"routines.json could not be read ({e}); using the built-in routines."
                data = None
        else:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULTS, f, indent=2)
            except Exception:
                pass
        self.routines = {}
        for name, r in (data if data is not None else copy.deepcopy(DEFAULTS)).items():
            if isinstance(r, dict) and isinstance(r.get("steps"), list):
                self.routines[_norm(name)] = {"say": r.get("say") or "", "end_say": r.get("end_say") or "",
                                              "aliases": [_norm(a) for a in r.get("aliases", []) if _norm(a)],
                                              "steps": [s for s in r["steps"] if isinstance(s, dict)]}
        self._aliases = {}
        for name, r in self.routines.items():
            for a in {name, name + " mode", name + " routine", *r["aliases"]}:
                self._aliases.setdefault(a, name)

    def names(self):
        return list(self.routines)

    def get(self, name):
        return self.routines.get(name)

    # ----- what you said -----
    def _lookup(self, phrase):
        phrase = phrase.strip()
        if phrase in self._aliases:
            return self._aliases[phrase]
        stripped = re.sub(r" (?:mode|routine)(?: on)?$", "", phrase)
        return self._aliases.get(stripped)

    def match(self, command):
        """{'action': 'run'|'end'|'list'|'describe', 'name'?} or None."""
        t = _norm(command)
        if not t or not self.routines:
            return None
        if re.fullmatch(r"(?:what|which) (?:routines|modes) (?:do i have|are there|can you do)|(?:list|show|read)(?: me)?(?: my| the)? (?:routines|modes)", t):
            return {"action": "list"}
        m = re.fullmatch(r"what does (.+?) do|what happens in (.+)|what s in (.+)|tell me about (.+)|what is (.+)", t)
        if m:
            name = self._lookup(next(g for g in m.groups() if g))
            if name:
                return {"action": "describe", "name": name}
        if t in ("back to normal", "go back to normal", "normal mode", "exit routine", "end routine", "end my routine",
                 "stop routine", "stop my routine"):
            return {"action": "end", "name": self.active} if self.active else None
        m = _END_PREFIX.match(t)
        if m:
            name = self._lookup(t[m.end():])
            if name:
                return {"action": "end", "name": name}
            return None
        name = self._lookup(t) or self._lookup(_RUN_PREFIX.sub("", t, count=1))
        if name:
            return {"action": "run", "name": name}
        return None

    # ----- what it does, in words -----
    def describe(self, name):
        r = self.routines[name]
        parts = []
        for s in r["steps"]:
            if s.get("close"):
                parts.append("closes " + _join(s["close"]))
            if s.get("open"):
                parts.append("opens " + _join(s["open"]))
            if s.get("site"):
                parts.append("opens " + _join([re.sub(r"^https?://(?:www\.)?", "", str(x)).split("/")[0] for x in s["site"]]))
            t = s.get("timer")
            if isinstance(t, dict) and t.get("minutes"):
                parts.append(f"starts a {int(t['minutes'])} minute {t.get('name') or ''} timer".replace("  ", " "))
            if isinstance(s.get("volume"), (int, float)):
                parts.append(f"sets the volume to {int(s['volume'])} percent")
        what = _join(parts) if parts else "does nothing yet"
        return f"{name.capitalize()} mode {what}."


def _join(items):
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]