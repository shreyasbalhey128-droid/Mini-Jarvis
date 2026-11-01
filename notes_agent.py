"""
notes_agent.py - voice notes and lists (to-do, shopping, anything) for Mini-Jarvis.

NOTES    "note that I parked on level three"   "take a note"  (it asks what)   "write down call the bank"
         "read my notes"   "what was my last note"   "delete my last note"   "delete all my notes"
LISTS    "add buy a charger to my to-do list"   "add milk, eggs and bread to my shopping list"
         "add socks to my packing list"  (a list is created the first time you use its name)
         "what's on my to-do list"   "read my shopping list"   "what do I need to buy"
         "mark buy a charger as done"   "tick off milk"   "I finished the report"
         "remove milk from my list"   "clear my shopping list"
A list with no name ("add X to my list") is your to-do list. "grocery" and "groceries" mean the shopping list.
Reminders with a time ("remind me at 5 to...") are the timers feature, not this one.

Everything is saved in notes.json next to voice.py (plain text, you can open and edit it).
Finished items are kept for 3 days, then dropped. Notes stay until you delete them.

voice.py speaks the replies and does the asking: handle() returns a dict that is one of
  {"say": text, "show": longer text for the screen (optional)}
  {"ask": question, "then": function(answer) -> dict}     (it needs one more thing, e.g. the note's words)
  {"confirm": question, "do": function() -> dict}         (it will not delete things without a yes)
"""
import difflib
import json
import os
import re
import time

DONE_KEEP_SECONDS = 3 * 24 * 3600
SPEAK_ITEMS = 6                    # a list is read out up to this many items; the rest is on screen
DEFAULT_LIST = "to-do"
ALIASES = {"to do": "to-do", "todo": "to-do", "to dos": "to-do", "todos": "to-do", "to-do": "to-do",
           "shopping": "shopping", "grocery": "shopping", "groceries": "shopping", "grocery shopping": "shopping"}
NOT_A_LIST_NAME = {"my", "the", "our", "a", "this", "that", "your", "whole", "full", "entire", "all", "his", "her",
                   "to", "on", "in", "up", "it", "what", "whats", "which"}
CANCEL_WORDS = {"nothing", "never mind", "nevermind", "cancel", "forget it", "no", "stop", "skip", "no thanks"}
_STOP = {"a", "an", "the", "my", "some", "to", "of", "and", "for", "on", "in", "up", "off", "our", "that", "this", "it"}

_L = r"(?:to do|todos?|to dos|shopping|grocery shopping|grocery|groceries|[a-z]+)"
_LIST = rf"(?:(?:my|the|our) )?(?:(?P<l>{_L}) )?list"                       # my shopping list / the list / list
_LIST_OR_TODO = rf"(?:{_LIST}|(?:(?:my|the|our) )?(?P<l2>to do|todos?|to dos))"


def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


def _tidy(text, sentence=False):
    t = " ".join(text.split())
    t = re.sub(r"\bi\b", "I", t)
    t = re.sub(r"\bim\b", "I'm", t)
    t = re.sub(r"\bive\b", "I've", t)
    t = re.sub(r"\bill\b", "I'll", t) if re.search(r"\bill (?:be|have|go|call|do|get|need|pick)\b", t) else t
    t = t[:1].upper() + t[1:]
    if sentence and t and t[-1] not in ".!?":
        t += "."
    return t


def _name(raw):
    raw = (raw or "").strip()
    if not raw or raw in NOT_A_LIST_NAME:
        return DEFAULT_LIST
    return ALIASES.get(raw, raw)


def _pretty(name):
    return f"{name} list"


def _join(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_CODE_REQUEST = re.compile(r"^(?:an? |the |some |me )?(?:python|javascript|java|html|css|sql|script|program|function|"
                           r"code|poem|story|essay|email|letter|song|paragraph|summary|report|joke|speech)\b")


class NotesAgent:
    def __init__(self, path):
        self.path = path
        self.data = {"notes": [], "lists": {}}
        self._load()

    # ----- saving -----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data["notes"] = [n for n in d.get("notes", []) if isinstance(n, dict) and n.get("text")]
                self.data["lists"] = {k: [i for i in v if isinstance(i, dict) and i.get("text")]
                                      for k, v in (d.get("lists") or {}).items() if isinstance(v, list)}
        except Exception:
            pass
        now = time.time()
        for items in self.data["lists"].values():
            items[:] = [i for i in items if not (i.get("done") and now - i.get("done_t", now) > DONE_KEEP_SECONDS)]

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ----- data operations -----
    def pending(self, name):
        return [i for i in self.data["lists"].get(name, []) if not i.get("done")]

    def known(self, name):
        return name in self.data["lists"] or name in (DEFAULT_LIST, "shopping")

    def add_note(self, text):
        text = _tidy(re.sub(r"\s*\bzcm\b\s*", ", ", text), sentence=True)
        self.data["notes"].append({"text": text, "t": time.time()})
        self._save()
        return {"say": "Noted.", "show": f"Noted: {text}"}

    def add_items(self, name, raw_items):
        have = {i["text"].lower() for i in self.pending(name)}
        added, dupes = [], []
        for raw in raw_items:
            text = _tidy(raw.strip(" ,"))
            if not text:
                continue
            if text.lower() in have or text in added:
                dupes.append(text)
                continue
            added.append(text)
            self.data["lists"].setdefault(name, []).append({"text": text, "done": False, "t": time.time()})
        self.data["lists"].setdefault(name, [])
        if added:
            self._save()
        pl = _pretty(name)
        if not added and dupes:
            return {"say": f"{_join(dupes)} {'is' if len(dupes) == 1 else 'are'} already on your {pl}."}
        say = (f"Added {added[0].lower() if len(added) == 1 else str(len(added)) + ' items'} to your {pl}."
               if len(added) <= 1 else f"Added {len(added)} items to your {pl}: {_join(a.lower() for a in added)}.")
        if dupes:
            say += f" {_join(dupes)} {'was' if len(dupes) == 1 else 'were'} already there."
        return {"say": say}

    def find(self, query, name=None):
        """The pending item that best matches what you said. (list_name, item) or (None, None)."""
        q = [w for w in _norm(query).split() if w not in _STOP]
        qs = " ".join(q)
        if not qs:
            return None, None
        names = [name] if name else list(self.data["lists"])
        best, best_score = (None, None), 0.0
        for n in names:
            for it in self.pending(n):
                words = [w for w in _norm(it["text"]).split() if w not in _STOP]
                ws = " ".join(words)
                if not ws:
                    continue
                if qs == ws:
                    score = 3.0
                elif qs in ws or ws in qs:
                    score = 2.0 + len(qs) / (len(ws) + len(qs))
                else:
                    overlap = len(set(q) & set(words)) / max(len(set(q)), len(set(words)))
                    ratio = difflib.SequenceMatcher(None, qs, ws).ratio()
                    score = max(overlap if overlap >= 0.5 else 0.0, ratio if ratio >= 0.72 else 0.0)
                if score > best_score:
                    best, best_score = (n, it), score
        return best

    def complete(self, query, name=None):
        n, it = self.find(query, name)
        if not it:
            return {"say": "I couldn't find that on your list."}
        it["done"], it["done_t"] = True, time.time()
        self._save()
        left = len(self.pending(n))
        return {"say": f"Ticked off {it['text'].lower()}." + (f" {left} left on your {_pretty(n)}." if left else
                                                              f" Your {_pretty(n)} is all done.")}

    def remove(self, query, name=None):
        n, it = self.find(query, name)
        if not it:
            return {"say": "I couldn't find that on your list."}
        self.data["lists"][n].remove(it)
        self._save()
        return {"say": f"Removed {it['text'].lower()} from your {_pretty(n)}."}

    def read_list(self, name):
        items = self.pending(name)
        pl = _pretty(name)
        if not items:
            return {"say": f"Your {pl} is empty."}
        shown = "\n".join(f"{n}. {i['text']}" for n, i in enumerate(items, 1))
        if len(items) <= SPEAK_ITEMS:
            return {"say": f"Your {pl} has {len(items)} item{'s' if len(items) != 1 else ''}: "
                           f"{_join(i['text'].lower() for i in items)}.", "show": f"{pl.capitalize()}:\n{shown}"}
        head = _join(i["text"].lower() for i in items[:SPEAK_ITEMS - 1])
        return {"say": f"Your {pl} has {len(items)} items. The first {SPEAK_ITEMS - 1} are {head}, "
                       f"and {len(items) - SPEAK_ITEMS + 1} more. The full list is on screen.",
                "show": f"{pl.capitalize()}:\n{shown}"}

    def read_notes(self, last_only=False):
        notes = self.data["notes"]
        if not notes:
            return {"say": "You don't have any notes yet."}
        stamp = lambda n: time.strftime("%b %d", time.localtime(n.get("t", time.time())))
        shown = "\n".join(f"{k}. ({stamp(n)}) {n['text']}" for k, n in enumerate(notes, 1))
        if last_only or len(notes) == 1:
            return {"say": f"Your last note says: {notes[-1]['text']}", "show": f"Last note ({stamp(notes[-1])}):\n{notes[-1]['text']}"}
        latest = [n["text"] for n in notes[-3:][::-1]]
        lead = ["Newest:", "Then:", "Then:"]
        return {"say": f"You have {len(notes)} notes. " + " ".join(f"{lead[k]} {t}" for k, t in enumerate(latest))
                       + " The full list is on screen.", "show": "Notes:\n" + shown}

    def clear_list(self, name):
        n = len(self.pending(name))
        if not n:
            return {"say": f"Your {_pretty(name)} is already empty."}

        def do():
            self.data["lists"][name] = []
            self._save()
            return {"say": f"Cleared your {_pretty(name)}."}
        return {"confirm": f"Clear all {n} item{'s' if n != 1 else ''} on your {_pretty(name)}?", "do": do}

    def clear_notes(self):
        n = len(self.data["notes"])
        if not n:
            return {"say": "You don't have any notes."}

        def do():
            self.data["notes"] = []
            self._save()
            return {"say": "All your notes are deleted."}
        return {"confirm": f"Delete all {n} note{'s' if n != 1 else ''}?", "do": do}

    def delete_last_note(self):
        if not self.data["notes"]:
            return {"say": "You don't have any notes."}
        gone = self.data["notes"].pop()
        self._save()
        return {"say": "Deleted your last note.", "show": f"Deleted: {gone['text']}"}

    # ----- the commands -----
    def handle(self, command):
        t = _norm(command.replace(",", " zcm "))               # typed commas survive as the word "zcm"
        if not t:
            return None

        # --- notes ---
        for pat, needs_note in (
                (r"(?:please )?(?:make|take|save|write|add|jot|put|create|leave)(?: me)? (?:a |another |a quick |a new )?note"
                 r"(?: down)?(?: to self)?(?: that| saying| says| about)?(?: (?P<x>.+))?", True),
                (r"(?:please )?(?:note down|note|jot down|write down|write this down|jot this down)(?: that)?(?: (?P<x>.+))?", False),
                (r"note to self(?: that)?(?: (?P<x>.+))?", True)):
            m = re.fullmatch(pat, t)
            if m:
                x = (m.group("x") or "").strip()
                if not needs_note and x and _CODE_REQUEST.match(x):
                    return None                                  # "write down a python script" is not a note
                if not needs_note and not x and not re.fullmatch(r"(?:please )?(?:note down|jot down|write this down|jot this down)", t):
                    return None                                  # a bare "note" / "write down" means nothing
                if x:
                    return self.add_note(x)
                return {"ask": "What should I note?", "then": lambda a: self._after_ask(a, lambda s: self.add_note(s))}

        if re.fullmatch(r"(?:delete|remove|erase|forget) (?:my |the )?(?:last|latest|previous) note", t):
            return self.delete_last_note()
        if re.fullmatch(r"(?:delete|clear|erase|wipe|remove) (?:all )?(?:of )?(?:my )?notes", t):
            return self.clear_notes()
        if (re.fullmatch(r"(?:please )?(?:read|show|tell|give|list|open|what are|what s|whats|what is)(?: me)?(?: my| the)?"
                         r"(?: (?:last|latest|recent|newest))? notes?", t)
                or re.fullmatch(r"(?:what|which) notes do i have|do i have any notes|what was my (?:last|latest) note|"
                                r"what is my (?:last|latest) note|what s my (?:last|latest) note|any notes", t)):
            return self.read_notes(last_only=bool(re.search(r"\b(last|latest|newest)\b", t) and not t.endswith("notes")))

        # --- add to a list ---
        m = re.fullmatch(rf"(?:please )?add (?:something |an item |one more thing |another item |a thing )?to {_LIST_OR_TODO}", t)
        if m:                                                    # "add to my list" -> it asks what
            name = _name(m.group("l") or m.group("l2"))
            return {"ask": "What should I add?", "then": lambda a, nm=name: self._after_ask(a, lambda s: self._add(nm, s))}
        m = (re.fullmatch(rf"(?:please )?(?:add|put|stick|pop) (?P<x>.+?) (?:to|on|onto|in|into) {_LIST}", t)
             or re.fullmatch(rf"(?:please )?(?:add|put|stick|pop) (?P<x>.+?) (?:to|on|onto|in|into) (?:(?:my|the|our) )?(?P<l>to do|todos?|to dos)", t))
        if m:
            name = _name(m.group("l"))
            if m.group("x") in ("something", "an item", "one more thing", "another item", "a thing", "an item"):
                return {"ask": "What should I add?", "then": lambda a, nm=name: self._after_ask(a, lambda s: self._add(nm, s))}
            return self._add(name, m.group("x"))
        m = re.fullmatch(rf"(?:please )?add to {_LIST} (?P<x>.+)", t)
        if m:
            return self._add(_name(m.group("l")), m.group("x"))

        # --- read a list ---
        m = re.fullmatch(rf"(?:please )?(?:read|show|tell|give|what s on|whats on|what is on|what s in|whats in|what is in|"
                         rf"what do i have on|what do i have in|list|open|check)(?: me)? {_LIST_OR_TODO}", t)
        if m:
            name = _name(m.group("l") or m.group("l2"))
            return self.read_list(name) if self.known(name) else None
        if re.fullmatch(r"what (?:do i|should i|have i got to|is left to|else do i have to|is there to) (?:need to |have to )?do(?: today| now)?|"
                        r"what are my (?:todos|to dos|tasks)|what is on my plate|what s on my plate|whats on my plate", t):
            return self.read_list(DEFAULT_LIST)
        if re.fullmatch(r"what (?:do|should) (?:i|we) (?:need|have) to (?:buy|get)|what do (?:i|we) need to buy|what should i buy", t):
            return self.read_list("shopping")

        # --- tick off / remove / clear ---
        for pat in (rf"(?:mark|check|tick|cross|strike|scratch|put a tick on) (?:off )?(?P<x>.+?) (?:as )?(?:done|complete|completed|finished)(?: (?:on |from |in )?{_LIST})?",
                    rf"(?:check|tick|cross|strike|scratch|mark) off (?P<x>.+?)(?: (?:from|on|in|off) {_LIST})?",
                    rf"(?:cross|tick|strike|scratch|check) (?P<x>.+?) off(?: {_LIST})?"):
            m = re.fullmatch(pat, t)
            if m:
                name = _name(m.group("l")) if m.groupdict().get("l") else None
                r = self.complete(m.group("x"), name)
                return r
        m = re.fullmatch(rf"(?:remove|delete|take|get rid of|erase|drop) (?P<x>.+?) (?:from|off|out of) {_LIST}", t)
        if m:
            return self.remove(m.group("x"), _name(m.group("l")) if m.group("l") else None)
        m = re.fullmatch(rf"(?:clear|empty|wipe|delete|erase|reset)(?: out)? (?:all |everything on |everything in )?(?:of )?{_LIST_OR_TODO}", t)
        if m:
            name = _name(m.group("l") or m.group("l2"))
            return self.clear_list(name) if self.known(name) else None
        m = re.fullmatch(r"(?:i )?(?:finished|completed|have finished|ve finished|am done with|done with|bought|got) (?P<x>.+)", t)
        if m:
            n, it = self.find(m.group("x"))
            if it:                                               # only when it really is on a list
                return self.complete(m.group("x"), n)
        return None

    def _after_ask(self, answer, make):
        a = _norm(answer)
        if not a or a in CANCEL_WORDS:
            return {"say": "Okay, never mind."}
        return make(answer.strip())

    def _add(self, name, raw):
        raw = raw.strip()
        if name == "shopping":
            parts = [p for p in re.split(r"\bzcm\b|,| and ", raw) if p.strip()]
        else:
            parts = [p for p in re.split(r"\bzcm\b|,", raw) if p.strip()]
        parts = [re.sub(r"^(?:some |a |an )", "", p.strip()) if name == "shopping" else p.strip() for p in parts]
        return self.add_items(name, parts)