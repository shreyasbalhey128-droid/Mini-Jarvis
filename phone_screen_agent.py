"""
Phone screen agent: Jarvis operates your ANDROID phone to finish a task ("on my phone, open the camera and take a photo").

It reads the phone's screen as a list of buttons and fields (Android UI Automation dump, so it is text your local model can
reason about), then taps, types, swipes, goes back or home, one step at a time, re-reading the screen after every step.

Safety, on purpose:
  * it never types into a password field (it tells you to do that part yourself)
  * risky buttons (send, pay, buy, delete, order, confirm, submit, post, transfer, uninstall...) need your spoken yes first
  * max 12 steps, and saying "stop" cancels it
  * a locked phone is only unlocked with your fingerprint, or with the saved PIN after you say yes (see phone_secret.py);
    the PIN is never given to the language model
Needs phone_agent.py (the Phone object). Typing is ASCII only (Android's `input text` limit).
"""
import json
import re
import time
import xml.etree.ElementTree as ET

from llm_utils import chat, DEFAULT_MODEL

MODEL = DEFAULT_MODEL
VISION_MODEL = None            # e.g. "moondream": used as a fallback when a screen exposes almost nothing
MAX_STEPS = 12
RISKY_WORDS = re.compile(
    r"\b(delete|remove|send|submit|pay|purchase|buy|place order|order|confirm|post|publish|transfer|uninstall|"
    r"factory reset|log out|sign out)\b", re.I)


# ------------------------------------------------------------------ pure helpers (unit-tested)
def parse_elements(xml_text, limit=60):
    """uiautomator dump -> [{id,label,kind,x,y,password,editable}] for things you can tap or type into."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    elems = []
    for n in root.iter("node"):
        cls = n.get("class", "")
        editable = cls.endswith("EditText")
        if not (n.get("clickable") == "true" or n.get("checkable") == "true" or editable):
            continue
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        label = (n.get("text") or n.get("content-desc") or "").strip()
        if not label:
            rid = n.get("resource-id", "")
            label = rid.split("/")[-1].replace("_", " ") if rid else ""
        if not label and not editable:
            continue
        elems.append({"id": len(elems) + 1, "label": (label or "text field")[:60],
                      "kind": "field" if editable else ("checkbox" if n.get("checkable") == "true" else "button"),
                      "x": (x1 + x2) // 2, "y": (y1 + y2) // 2,
                      "password": n.get("password") == "true", "editable": editable})
        if len(elems) >= limit:
            break
    return elems


def adb_text(text):
    """Escapes text for `adb shell input text` (spaces become %s, shell characters get a backslash)."""
    return re.sub(r"([&|;<>()$`\\\"'*?#~!{}\[\]])", r"\\\1", text).replace(" ", "%s")


def risk_reason(action, elems):
    if action.get("action") == "tap":
        try:
            el = next(e for e in elems if e["id"] == int(action.get("id")))
        except Exception:
            return None
        if RISKY_WORDS.search(el["label"]):
            return f'tapping "{el["label"]}"'
    return None


# ------------------------------------------------------------------ talking to the phone
def _dump(phone):
    for _ in range(3):                                          # the dump fails while the screen is animating
        phone._run("shell", "uiautomator dump /sdcard/jv_ui.xml", timeout=20)
        rc, raw = phone._run("exec-out", "cat /sdcard/jv_ui.xml", binary=True, timeout=20)
        if rc == 0 and raw.strip().startswith(b"<?xml"):
            return raw.decode("utf-8", "replace")
        time.sleep(0.6)
    return ""


def _foreground(phone):
    _, out = phone._run("shell", "dumpsys window | grep mCurrentFocus")
    m = re.search(r"([\w.]+)/", out)
    return m.group(1) if m else "unknown"


def _locked(phone):
    _, out = phone._run("shell", "dumpsys window | grep -E 'isKeyguardShowing|mShowingLockscreen|mDreamingLockscreen'")
    return bool(re.search(r"(?:isKeyguardShowing|mShowingLockscreen|mDreamingLockscreen)\W*=?\s*true", out))


def _screen_size(phone):
    _, out = phone._run("shell", "wm size")
    m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def describe_screen(phone):
    import base64
    rc, png = phone._run("exec-out", "screencap", "-p", binary=True, timeout=25)
    if rc != 0 or not png.startswith(b"\x89PNG"):
        return ""
    return chat([{"role": "user", "content": "Describe this phone screen: the app, buttons and visible text."}],
                model=VISION_MODEL, images=[base64.b64encode(png).decode()], timeout=180)


SYSTEM = (
    "You operate an Android phone for the user, one step at a time. You are shown the foreground app and a numbered list "
    "of the tappable elements on screen, plus what you already did. Reply with ONE JSON object:\n"
    '{"action":"tap","id":N} | {"action":"type","text":"..."} | {"action":"swipe","dir":"up|down|left|right"} | '
    '{"action":"key","key":"back|home|enter"} | {"action":"open","app":"camera"} | {"action":"wait","seconds":2} | '
    '{"action":"done","answer":"short spoken summary"}\n'
    "Use element ids exactly as listed. To type, tap the text field first. Swipe up to scroll down. "
    "When the goal is finished, or you cannot make progress, reply with done."
)


def decide(goal, app, elems, history, vision=""):
    lines = [f'{e["id"]}. [{e["kind"]}] {e["label"]}' for e in elems] or ["(nothing tappable detected)"]
    user = (f"GOAL: {goal}\nFOREGROUND APP: {app}\nELEMENTS:\n" + "\n".join(lines) +
            (f"\nSCREEN DESCRIPTION: {vision}" if vision else "") +
            "\nDONE SO FAR:\n" + ("\n".join(history[-6:]) or "(nothing yet)"))
    raw = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
               model=MODEL, json_mode=True, temperature=0.1)
    return json.loads(raw)


def execute(phone, action, elems, state):
    a = action.get("action")
    if a == "tap":
        el = next((e for e in elems if e["id"] == int(action["id"])), None)
        if el is None:
            raise ValueError(f'no element {action.get("id")}')
        phone._run("shell", f'input tap {el["x"]} {el["y"]}')
        state["field"] = el
        return f'tapped "{el["label"]}"'
    if a == "type":
        text = str(action.get("text", ""))
        if state.get("field") and state["field"]["password"]:
            raise ValueError("that is a password field; ask the user to type it themselves")
        if not text.isascii():
            raise ValueError("I can only type plain English characters on the phone")
        phone._run("shell", f"input text {adb_text(text)}")
        return f'typed "{text[:40]}"'
    if a == "swipe":
        w, h = _screen_size(phone)
        d = str(action.get("dir", "up")).lower()
        x1, y1, x2, y2 = {"up": (w // 2, int(h * .75), w // 2, int(h * .30)), "down": (w // 2, int(h * .30), w // 2, int(h * .75)),
                          "left": (int(w * .85), h // 2, int(w * .15), h // 2), "right": (int(w * .15), h // 2, int(w * .85), h // 2)}[d]
        phone._run("shell", f"input swipe {x1} {y1} {x2} {y2} 350")
        return f"swiped {d}"
    if a == "key":
        k = str(action.get("key", "back")).lower()
        code = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER"}[k]
        phone._run("shell", "input", "keyevent", code)
        return f"pressed {k}"
    if a == "open":
        return phone.open_target(str(action["app"]))
    if a == "wait":
        time.sleep(min(float(action.get("seconds", 1)), 10))
        return "waited"
    raise ValueError(f"unknown action {a}")


def run_task(phone, goal, say=print, confirm=None, cancel=None, max_steps=MAX_STEPS):
    """Do `goal` on the phone. confirm(message)->bool is asked before risky taps (default: refuse)."""
    history, state = [], {}
    phone._run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    time.sleep(0.8)
    locked = phone.is_locked() if hasattr(phone, "is_locked") else _locked(phone)
    if locked:
        unlock = getattr(phone, "unlock_auto", None)
        if unlock is None or unlock(say=say, cancel=cancel, confirm=confirm) not in ("Unlocked.", "Your phone is already unlocked."):
            return "Your phone is still locked, so I couldn't start. Unlock it with your fingerprint and ask me again."
    say("On it. I'll work through it on your phone.")
    try:
        for _ in range(max_steps):
            if cancel is not None and cancel.is_set():
                return "Stopped."
            xml = _dump(phone)
            elems = parse_elements(xml)
            vision = ""
            if len(elems) < 4 and VISION_MODEL:
                try:
                    vision = describe_screen(phone)
                except Exception:
                    pass
            try:
                action = decide(goal, _foreground(phone), elems, history, vision)
            except Exception:
                history.append("(my last reply was invalid, I must answer with one JSON action)")
                continue
            if action.get("action") == "done":
                return action.get("answer") or "Done."
            why = risk_reason(action, elems)
            if why:
                ok = confirm(f"I'm about to do {why} on your phone. Should I go ahead?") if confirm else False
                if not ok:
                    history.append(f"USER REFUSED {why}; choose another way or finish with done")
                    if not confirm:
                        return f"I stopped because the next step was {why} and I need your okay for that."
                    continue
            try:
                history.append(execute(phone, action, elems, state))
            except Exception as e:
                history.append(f"FAILED {action}: {e}")
            time.sleep(1.0)                                     # let the phone settle before re-reading it
        return "I ran out of steps before finishing. Ask me to continue and I'll pick up from the screen."
    except Exception as e:
        return f"I hit a problem: {e}"