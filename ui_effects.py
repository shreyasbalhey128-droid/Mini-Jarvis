"""ui_effects.py - the smooth bits of the J.A.R.V.I.S. window.

Nothing in here imports tkinter: the classes only use a few methods of the widget they are given,
so they can be tested on their own and tuned from one place.
"""
import math
import random
import re
import time
import traceback
from collections import deque

# ---------------------------------------------------------------- tune these
TYPE_WORD_SECONDS = 0.13      # normal time per word when an assistant's answer is typed out
TYPE_MIN_SECONDS = 0.035      # never faster than this per word (very long answers speed up to it)
TYPE_MAX_SECONDS = 14.0       # a long answer is never typed for more than about this long
TYPE_START_PAUSE = 0.18       # short pause after the name/time line, before the first word
DRAWER_SPEED = 13.0           # slide-out menu: higher = snappier (it eases out, like a real panel)


# ---------------------------------------------------------------- mood colours
MOOD_NIGHT_DIM = 0.84        # the active reactor is this much dimmer in the small hours


def mood_for(hour, cpu):
    """How the active reactor's colour drifts with the day and with how hard the computer is working.
    hour = 0..23 (local time), cpu = average CPU % lately (or None).
    Returns (tint rgb or None, strength 0..1, brightness 0..1); (None, 0, 1) means "leave the colours alone".
      night 23-5     deep indigo, a little dimmer         dawn 5-8        warm gold
      evening 20-23  soft violet                          sunset 17-20    amber glow
      CPU 60+% amber, 75+% orange, 90+% red-orange (the heavier the work, the warmer and stronger)"""
    if hour >= 23 or hour < 5:
        tint, s, dim = (90, 80, 210), 0.20, MOOD_NIGHT_DIM
    elif 5 <= hour < 8:
        tint, s, dim = (255, 175, 95), 0.12, 1.0
    elif 17 <= hour < 20:
        tint, s, dim = (255, 150, 80), 0.10, 1.0
    elif 20 <= hour < 23:
        tint, s, dim = (120, 110, 230), 0.10, 0.92
    else:
        tint, s, dim = None, 0.0, 1.0
    if cpu is not None:
        load, ls = None, 0.0
        if cpu >= 90:
            load, ls = (255, 70, 45), 0.45
        elif cpu >= 75:
            load, ls = (255, 140, 40), 0.32
        elif cpu >= 60:
            load, ls = (255, 190, 60), 0.18
        if load is not None and ls > s:
            tint, s, dim = load, ls, 1.0                    # a busy computer beats the time of day
    return tint, s, dim


# ---------------------------------------------------------------- slide-out menu helpers
def lerp(a, b, p):
    return a + (b - a) * p


def drawer_step(p, target, dt, speed=DRAWER_SPEED):
    """One animation step towards `target` (0 closed, 1 open). Fast at first, then it settles gently.
    Returns (new_p, finished)."""
    p += (target - p) * (1.0 - math.exp(-max(0.0, dt) * speed))
    if abs(target - p) < 0.004:
        return float(target), True
    return p, False


def menu_icon_lines(p):
    """The three bars of the menu icon. As p goes 0 -> 1 the top and bottom bars slide to the middle
    while rotating into an X, and the middle bar fades away.
    Returns [((x0, y0, x1, y1), visibility 0..1), ...] for a 36 x 34 icon."""
    cx, ang = 18.0, p * math.pi / 4
    half = lerp(9.0, 7.1, p)
    dx, dy = half * math.cos(ang), half * math.sin(ang)
    ct, cb = lerp(11.0, 17.0, p), lerp(23.0, 17.0, p)
    mid_half = 9.0 * (1.0 - p)
    return [((cx - dx, ct - dy, cx + dx, ct + dy), 1.0),                  # top bar  -> "\"
            ((cx - dx, cb + dy, cx + dx, cb - dy), 1.0),                  # bottom   -> "/"
            ((cx - mid_half, 17.0, cx + mid_half, 17.0), 1.0 - p)]         # middle bar fades out


def is_inside(widget_path, parent_path):
    """True if a Tk widget path is the parent or one of its children (a plain startswith() would also
    match '.!frame2' for '.!frame')."""
    w, p = str(widget_path), str(parent_path)
    return w == p or w.startswith(p + ".")


# ---------------------------------------------------------------- code blocks in messages
_FENCE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\r?\n(.*?)```", re.S)


def split_code(body):
    """Cuts a message into [("text", str) | ("code", language, code), ...]. Code is the text between
    triple backticks, kept exactly as written (indentation included)."""
    out, pos = [], 0
    for m in _FENCE.finditer(body):
        if m.start() > pos:
            out.append(("text", body[pos:m.start()]))
        out.append(("code", m.group(1).lower(), m.group(2).rstrip()))
        pos = m.end()
    if pos < len(body):
        out.append(("text", body[pos:]))
    return out


# ---------------------------------------------------------------- typed-out messages
class Typewriter:
    """Shows chat messages in a tk.Text widget. Assistant answers are revealed word by word (a short
    extra pause at commas, full stops and line breaks) with a fade-in colour on each new word and a
    blinking caret; your own messages, system lines and loaded history appear instantly.

    `text`  : the widget (needs configure, insert, delete, index, tag_add, tag_remove, tag_configure,
              yview, see)       `after`: root.after       `fast`: returns True while the window is hidden
    """
    FRESH = (("fresh0", "#ffffff", 0.14), ("fresh1", "#c8fff0", 0.32), ("fresh2", "#9df0cf", 0.60))
    CARET = "\u258c"

    def __init__(self, text, after, now=time.time, fast=None, rng=random.random,
                 caret_on="#00e5ff", caret_off="#0a1220", code_button=None, code_font=("Consolas", 10)):
        self.t, self.after, self.now = text, after, now
        self.fast = fast or (lambda: False)
        self.rng = rng
        self.caret_on, self.caret_off = caret_on, caret_off
        self.code_button = code_button     # code_button(code) -> a widget (the COPY button) or None
        self.jobs = deque()
        self.job = None            # the answer being typed right now
        self.next_at = 0.0
        self.fresh = []            # [start, end, born, level] of words that are still fading in
        self.caret = False         # is the caret character currently in the widget?
        self.blink_t, self.blink_on = 0.0, True
        self.timer = None
        for name, col, _ in self.FRESH:            # created after the normal tags, so they win
            text.tag_configure(name, foreground=col)
        text.tag_configure("caret", foreground=caret_on)
        # code blocks: a header strip (language + COPY button) and a darker band with the code in monospace
        text.tag_configure("code_h", foreground="#69f0ae", background="#142a44", font=(code_font[0], 8, "bold"),
                           lmargin1=14, lmargin2=14, spacing1=6, spacing3=2)
        text.tag_configure("code", foreground="#e8f4ff", background="#0b1a2c", font=code_font,
                           lmargin1=22, lmargin2=22, spacing1=0, spacing3=0, wrap="char")

    # ------------------------------------------------------------ public
    def add(self, who, head, body, animate=False):
        """Queue one message. `head` is the name/time line, `body` the text."""
        self.jobs.append(dict(who=who, head=head, body=body, animate=animate))
        self._kick()

    def clear(self):
        """Forget everything that is queued or being typed (call it before deleting the widget text)."""
        self.jobs.clear()
        self.job = None
        self.fresh = []
        self.caret = False

    def flush(self):
        """Show everything that is still waiting, immediately."""
        guard = 0
        while (self.job or self.jobs) and guard < 1000:
            guard += 1
            if self.job is None:
                self._start(self.jobs.popleft(), self.now())
            else:
                self._finish_now()
        self.t.configure(state="normal")
        try:
            self._drop_caret()
        finally:
            self.t.configure(state="disabled")

    def busy(self):
        return bool(self.job or self.jobs or self.fresh or self.caret)

    # ------------------------------------------------------------ the loop
    def _kick(self):
        if self.timer is None:
            self.timer = self.after(15, self._step)

    def _step(self):
        self.timer = None
        try:
            self._run(self.now())
        except Exception:
            traceback.print_exc()
            self.clear()                                   # never get stuck in an error loop
        if self.busy():
            self.timer = self.after(30, self._step)

    def _run(self, now):
        t = self.t
        at_bottom = t.yview()[1] >= 0.985                  # do not yank the view if you scrolled up
        edited, reveals, guard = False, 0, 0
        while guard < 60:
            guard += 1
            if self.job is None:
                if not self.jobs:
                    break
                self._start(self.jobs.popleft(), now)
                edited = True
                continue
            if self.fast():
                self._finish_now()
                edited = True
                continue
            if now < self.next_at or reveals >= 6:
                break
            self._reveal(now)
            reveals += 1
            edited = True
        if self.job is not None and not self.caret:
            self._put_caret()
        self._recolor(now)
        self._blink(now)
        if edited and at_bottom:
            t.see("end")

    # ------------------------------------------------------------ one message
    def _start(self, job, now):
        who = job["who"]
        self._insert(job["head"], (who + "_h",))
        has_code = bool(_FENCE.search(job["body"]))
        if not job["animate"]:
            if has_code:
                self._put_all(who, self._tokens(job["body"]))
                self._insert("\n\n", (who,))
            else:
                self._insert(job["body"] + "\n\n", (who,))
            return
        tokens = self._tokens(job["body"]) if has_code else re.findall(r"\S+\s*|\s+", job["body"])
        n_words = max(1, sum(1 for tok in tokens if isinstance(tok, str) and tok.strip()))
        base = max(TYPE_MIN_SECONDS, min(TYPE_WORD_SECONDS, TYPE_MAX_SECONDS / n_words))
        self.job = dict(who=who, tokens=tokens, i=0, base=base)
        self.next_at = now + TYPE_START_PAUSE

    @staticmethod
    def _tokens(body):
        """Words (strings) and whole code blocks (("code", language, code) tuples) of a message."""
        words = lambda txt: re.findall(r"\S+\s*|\s+", txt)
        parts = split_code(body)
        tokens = []
        for i, part in enumerate(parts):
            if part[0] == "code":
                tokens.append(part)
                continue
            txt = part[1]
            if i > 0:
                txt = txt.lstrip("\r\n ")                    # right after a code block
            if i < len(parts) - 1:
                txt = txt.rstrip() + "\n" if txt.strip() else ""    # right before a code block
            if txt.strip():
                tokens.extend(words(txt))
        return tokens

    def _put_all(self, who, tokens):
        """Puts tokens on screen at once (consecutive words are inserted as one piece)."""
        buf = []
        for tok in tokens:
            if isinstance(tok, tuple):
                if buf:
                    self._insert("".join(buf), (who,))
                    buf = []
                self._insert_code(who, tok[1], tok[2])
            else:
                buf.append(tok)
        if buf:
            self._insert("".join(buf), (who,))

    def _insert_code(self, who, lang, code):
        """A code block: header strip with the language and a COPY button, then the code, untouched."""
        t = self.t
        t.configure(state="normal")
        try:
            self._drop_caret()
            t.insert("end-1c", " " + (lang or "code").upper() + "   ", ("code_h",))
            if self.code_button is not None:
                try:
                    btn = self.code_button(code)
                    if btn is not None:
                        t.window_create("end-1c", window=btn, padx=2)
                        t.tag_add("code_h", "end-2c", "end-1c")
                except Exception:
                    traceback.print_exc()
            t.insert("end-1c", "\n", ("code_h",))
            t.insert("end-1c", code.rstrip("\n") + "\n", ("code",))
            t.insert("end-1c", "\n", (who,))
        finally:
            t.configure(state="disabled")

    def _reveal(self, now):
        job = self.job
        tok = job["tokens"][job["i"]]
        job["i"] += 1
        if isinstance(tok, tuple):                                 # a code block appears all at once
            self._insert_code(job["who"], tok[1], tok[2])
            self.next_at = now + job["base"] * 3
            if job["i"] >= len(job["tokens"]):
                self._finish_job()
            return
        s, e = self._insert(tok, (job["who"], "fresh0"))
        self.fresh.append([s, e, now, 0])
        base = job["base"] * (0.35 if self.jobs else 1.0)        # something is waiting: hurry up
        word = tok.strip()
        bonus = 0.0
        if word.endswith((".", "!", "?", "\u2026")):
            bonus = 1.6                                        # sentence end: breathe
        elif word.endswith((",", ";", ":")):
            bonus = 0.7
        if "\n" in tok:
            bonus += 1.2                                       # new line: a little longer
        delay = base * (1.0 + bonus) * (0.85 + 0.30 * self.rng())
        self.next_at = now + delay if now - self.next_at > 0.25 else self.next_at + delay
        if job["i"] >= len(job["tokens"]):
            self._finish_job()

    def _finish_job(self):
        who = self.job["who"]
        self.job = None
        self._insert("\n\n", (who,))                           # also removes the caret

    def _finish_now(self):
        """Put the rest of the current answer on screen at once."""
        job = self.job
        self._put_all(job["who"], job["tokens"][job["i"]:])
        job["i"] = len(job["tokens"])
        self._finish_job()

    # ------------------------------------------------------------ widget access
    def _insert(self, text, tags):
        t = self.t
        t.configure(state="normal")
        try:
            self._drop_caret()
            s = t.index("end-1c")
            t.insert("end-1c", text, tags)
            e = t.index("end-1c")
        finally:
            t.configure(state="disabled")
        return s, e

    def _drop_caret(self):
        """Removes the caret character (the widget must be in the 'normal' state)."""
        if self.caret:
            self.t.delete("end-2c", "end-1c")
            self.caret = False

    def _put_caret(self):
        t = self.t
        t.configure(state="normal")
        try:
            t.insert("end-1c", self.CARET, ("caret",))
            self.caret = True
        finally:
            t.configure(state="disabled")

    def _recolor(self, now):
        """Words start bright white and settle into the normal colour over about half a second."""
        keep = []
        for rec in self.fresh:
            s, e, born, lvl = rec
            age = now - born
            new = next((i for i, (_, _, lim) in enumerate(self.FRESH) if age < lim), len(self.FRESH))
            if new != lvl:
                if lvl < len(self.FRESH):
                    self.t.tag_remove(self.FRESH[lvl][0], s, e)
                if new < len(self.FRESH):
                    self.t.tag_add(self.FRESH[new][0], s, e)
                rec[3] = new
            if new < len(self.FRESH):
                keep.append(rec)
        self.fresh = keep

    def _blink(self, now):
        if self.caret and now - self.blink_t >= 0.45:
            self.blink_t = now
            self.blink_on = not self.blink_on
            self.t.tag_configure("caret", foreground=self.caret_on if self.blink_on else self.caret_off)