"""
Watch-and-repeat: Jarvis records what you do (mouse clicks, scrolls, keystrokes) and replays it later.
This is the reliable way to automate things you do over and over.

  "watch me"                       -> starts recording
  F8                               -> pause / resume recording (USE THIS before typing passwords!)
  "stop watching" / F9             -> stops; "stop watching and save it as report" names it
  "run macro report"               -> replays it (ESC stops a replay)
  "list my macros"

pip install pynput

Read this: recording captures keystrokes, and macros are saved as plain text in ~/.jarvis_macros.
Never record while typing passwords or card numbers (pause with F8). Replay uses absolute screen
positions, so windows must be where they were when you recorded.
"""
import json
import os
import re
import threading
import time

MACRO_DIR = os.path.join(os.path.expanduser("~"), ".jarvis_macros")
os.makedirs(MACRO_DIR, exist_ok=True)
MAX_GAP = 3.0                      # long pauses are shortened to 3 s on replay

replay_cancel = threading.Event()
_esc_listener = None


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "macro"


def _path(name):
    return os.path.join(MACRO_DIR, _slug(name) + ".json")


def _key_to_str(key):
    ch = getattr(key, "char", None)
    if ch and ord(ch) >= 32:
        return "c:" + ch
    vk = getattr(key, "vk", None)
    if getattr(key, "name", None):
        return "k:" + key.name
    if vk is not None:
        return "v:" + str(vk)
    return "k:unknown"


def _str_to_key(s):
    from pynput import keyboard
    kind, val = s.split(":", 1)
    if kind == "c":
        return val
    if kind == "k":
        return getattr(keyboard.Key, val)
    return keyboard.KeyCode.from_vk(int(val))


class Recorder:
    def __init__(self):
        self.events = []
        self.recording = False
        self.paused = False
        self._t0 = 0.0
        self._listeners = []
        self.on_toggle_pause = None      # optional callback(paused: bool) so Jarvis can beep or say "paused"

    def _now(self):
        return round(time.time() - self._t0, 3)

    def start(self):
        from pynput import keyboard, mouse
        if self.recording:
            return
        self.events, self.paused, self.recording, self._t0 = [], False, True, time.time()

        def live():
            return self.recording and not self.paused

        def on_click(x, y, button, pressed):
            if live():
                self.events.append({"t": self._now(), "k": "click", "x": x, "y": y,
                                    "b": button.name, "down": pressed})

        def on_scroll(x, y, dx, dy):
            if live():
                self.events.append({"t": self._now(), "k": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

        def on_press(key):
            if key == keyboard.Key.f9:
                self.recording = False
                return
            if key == keyboard.Key.f8:
                self.paused = not self.paused
                if self.on_toggle_pause:
                    self.on_toggle_pause(self.paused)
                return
            if live():
                self.events.append({"t": self._now(), "k": "key", "key": _key_to_str(key), "down": True})

        def on_release(key):
            if key in (keyboard.Key.f8, keyboard.Key.f9):
                return
            if live():
                self.events.append({"t": self._now(), "k": "key", "key": _key_to_str(key), "down": False})

        ml = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
        kl = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listeners = [ml, kl]
        ml.start()
        kl.start()

    def stop(self):
        self.recording = False
        for l in self._listeners:
            l.stop()
        self._listeners = []
        # drop the click that pressed "stop" in the Jarvis window, if any
        while self.events and self.events[-1]["k"] == "click":
            self.events.pop()
        return self.events

    def save(self, name):
        with open(_path(name), "w", encoding="utf-8") as f:
            json.dump(self.events, f)
        return _slug(name)


def list_macros():
    return sorted(f[:-5] for f in os.listdir(MACRO_DIR) if f.endswith(".json"))


def replay(name, speed=1.0):
    """Replays a saved macro. Returns True if it ran to the end."""
    from pynput import keyboard, mouse

    global _esc_listener
    p = _path(name)
    if not os.path.exists(p):
        raise FileNotFoundError(name)
    with open(p, encoding="utf-8") as f:
        events = json.load(f)

    replay_cancel.clear()
    if _esc_listener is None:
        def on_press(key):
            if key == keyboard.Key.esc:
                replay_cancel.set()
        _esc_listener = keyboard.Listener(on_press=on_press, daemon=True)
        _esc_listener.start()

    mc, kc = mouse.Controller(), keyboard.Controller()
    held, prev = [], 0.0
    try:
        for ev in events:
            gap = min((ev["t"] - prev) / max(speed, 0.1), MAX_GAP)
            prev = ev["t"]
            end = time.time() + gap
            while time.time() < end:
                if replay_cancel.is_set():
                    return False
                time.sleep(0.02)
            if replay_cancel.is_set():
                return False
            if ev["k"] == "click":
                mc.position = (ev["x"], ev["y"])
                btn = getattr(mouse.Button, ev["b"])
                (mc.press if ev["down"] else mc.release)(btn)
            elif ev["k"] == "scroll":
                mc.position = (ev["x"], ev["y"])
                mc.scroll(ev["dx"], ev["dy"])
            elif ev["k"] == "key":
                key = _str_to_key(ev["key"])
                if ev["down"]:
                    kc.press(key)
                    held.append(key)
                else:
                    kc.release(key)
                    if key in held:
                        held.remove(key)
        return True
    finally:
        for key in held:           # never leave Ctrl or Shift stuck down
            try:
                kc.release(key)
            except Exception:
                pass


# --------------------------------------------------------------------------- voice-command glue
recorder = Recorder()

_START = re.compile(r"\b(watch me|start recording|record (?:this|my|what i do))\b", re.I)
_STOP = re.compile(r"\bstop (?:watching|recording)\b(?:.*?\b(?:as|called|name it)\s+(.+))?", re.I)
_RUN = re.compile(r"\b(?:run|replay|play|do)\s+(?:the\s+)?macro\s+(.+)$", re.I)
_LIST = re.compile(r"\blist (?:my )?macros\b", re.I)


def handle(text, say=print):
    """Returns a spoken reply, or None if the text isn't a macro command."""
    if _LIST.search(text):
        names = list_macros()
        return ("You have these macros: " + ", ".join(names) + ".") if names else "You have no macros yet."
    if _START.search(text):
        recorder.start()
        return ("Watching. Do your task now. Press F8 to pause, especially before typing passwords, "
                "and F9 or say stop watching when you're done.")
    m = _STOP.search(text)
    if m:
        events = recorder.stop()
        if not events:
            return "I didn't catch any actions."
        name = (m.group(1) or time.strftime("macro_%H%M%S")).strip()
        saved = recorder.save(name)
        return f"Saved {len(events)} actions as {saved.replace('_', ' ')}. Say run macro {saved.replace('_', ' ')} to repeat it."
    m = _RUN.search(text)
    if m:
        name = m.group(1).strip()
        if _slug(name) not in list_macros():
            return f"I don't have a macro called {name}."
        say(f"Running {name}. Press escape to stop.")
        done = replay(name)
        return "Finished." if done else "Stopped."
    return None