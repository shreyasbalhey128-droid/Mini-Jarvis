"""
Screen agent: Jarvis looks at the window you're in and operates the computer to finish a task.

HOW IT SEES: it reads the Windows UI Automation tree of the active window (button names, text fields,
tabs, menu items, with their positions). That is text, so your 3B model can reason over it and it is far
more reliable and lighter on the laptop than guessing from pixels. If a window exposes almost nothing
(some browser pages, games), it can add a short description from a small vision model as a fallback.

HOW IT ACTS: click / double-click / type / press keys / hotkeys / scroll / open an app, one step at a time,
re-reading the screen after every step.

SAFETY (on purpose):
  * press ESC at any time, or fling the mouse into the top-left corner, to stop instantly
  * risky actions (send, delete, pay, buy, submit, close-window shortcuts...) need a spoken yes first;
    with no confirm callback they are refused
  * max 12 steps per task

pip install pyautogui pywinauto pynput pillow pyperclip
Optional fallback vision model:  ollama pull moondream
"""
import base64
import io
import json
import re
import threading
import time

from llm_utils import chat, DEFAULT_MODEL

MODEL = DEFAULT_MODEL
VISION_MODEL = "moondream"        # set to None to disable the pixel fallback
MAX_STEPS = 12

INTERACTIVE = {"Button", "Edit", "ComboBox", "MenuItem", "TabItem", "ListItem", "Hyperlink",
               "CheckBox", "RadioButton", "TreeItem", "SplitButton", "Document"}
RISKY_WORDS = re.compile(
    r"\b(delete|remove|send|submit|pay|purchase|buy|place order|order|password|sign out|log out|"
    r"format|uninstall|transfer|post|publish|confirm)\b", re.I)
RISKY_HOTKEYS = [{"alt", "f4"}, {"ctrl", "w"}, {"ctrl", "shift", "delete"}, {"shift", "delete"}]

cancel = threading.Event()
_esc_listener = None
SKIP_TITLES = ("jarvis",)          # window titles containing these are never operated on (Jarvis's own window)
_state = {"rect": None}            # screen rectangle of the window being operated on (for the vision fallback)


# --------------------------------------------------------------------------- plumbing
def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True      # mouse to top-left corner raises and aborts
    pyautogui.PAUSE = 0.25
    return pyautogui


def _start_esc_listener():
    global _esc_listener
    if _esc_listener is not None:
        return
    from pynput import keyboard

    def on_press(key):
        if key == keyboard.Key.esc:
            cancel.set()

    _esc_listener = keyboard.Listener(on_press=on_press, daemon=True)
    _esc_listener.start()


# --------------------------------------------------------------------------- seeing
def _target_hwnd():
    """The window to work in: the foreground one, or the next one down if that is Jarvis himself."""
    import win32con
    import win32gui
    fg = win32gui.GetForegroundWindow()
    hwnd = fg
    for _ in range(30):
        title = win32gui.GetWindowText(hwnd) if hwnd else ""
        if hwnd and win32gui.IsWindowVisible(hwnd) and title and \
                not any(t in title.lower() for t in SKIP_TITLES):
            return hwnd
        hwnd = win32gui.GetWindow(hwnd, win32con.GW_HWNDNEXT) if hwnd else 0
        if not hwnd:
            break
    return fg


def observe(max_elems=60, budget=4.0):
    """Return (window title, [elements]) for the foreground window via UI Automation."""
    import win32gui
    from pywinauto import Desktop

    hwnd = _target_hwnd()
    title = win32gui.GetWindowText(hwnd)
    try:
        _state["rect"] = win32gui.GetWindowRect(hwnd)
    except Exception:
        _state["rect"] = None
    root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    it = root.iter_descendants() if hasattr(root, "iter_descendants") else iter(root.descendants())

    elems, t0 = [], time.time()
    for e in it:
        if time.time() - t0 > budget or len(elems) >= max_elems:
            break
        try:
            info = e.element_info
            ctype = info.control_type
            if ctype not in INTERACTIVE:
                continue
            name = (info.name or "").strip()
            if not name and ctype in ("Edit", "ComboBox"):
                name = "(text field)"
            r = e.rectangle()
            if not name or r.width() <= 0 or r.height() <= 0:
                continue
            x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            if x < 0 or y < 0:
                continue
            elems.append({"id": len(elems) + 1, "name": name[:60], "type": ctype, "x": x, "y": y})
        except Exception:
            continue
    return title, elems


def describe_screen(rect=None):
    """Fallback: a short text description (of the window being operated on) from a small local vision model."""
    if rect:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=rect, all_screens=True)     # all_screens: the window may be on monitor 2
    else:
        img = _pg().screenshot()
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return chat([{"role": "user", "content":
                  "Describe what is on this screen: the app, any text fields, buttons and visible text."}],
                model=VISION_MODEL, images=[b64], timeout=180)


# --------------------------------------------------------------------------- deciding
SYSTEM = (
    "You operate a Windows PC for the user, one step at a time. You are shown the active window's title "
    "and a numbered list of its clickable elements, plus what you already did. Reply with ONE JSON object:\n"
    '{"action":"click","id":N} | {"action":"double_click","id":N} | {"action":"type","text":"..."} | '
    '{"action":"press","key":"enter"} | {"action":"hotkey","keys":["ctrl","l"]} | '
    '{"action":"scroll","amount":-500} | {"action":"open","app":"notepad"} | '
    '{"action":"wait","seconds":2} | {"action":"done","answer":"short spoken summary"}\n'
    "Use element ids exactly as listed. To type into a field, click it first. Use hotkeys when the element "
    "list is empty. When the goal is finished, or you cannot make progress, reply with done."
)


def decide(goal, title, elems, history, vision=""):
    lines = [f'{e["id"]}. [{e["type"]}] {e["name"]}' for e in elems] or ["(no elements detected)"]
    user = (f"GOAL: {goal}\nACTIVE WINDOW: {title}\nELEMENTS:\n" + "\n".join(lines) +
            (f"\nSCREEN DESCRIPTION: {vision}" if vision else "") +
            "\nDONE SO FAR:\n" + ("\n".join(history[-6:]) or "(nothing yet)"))
    raw = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
               model=MODEL, json_mode=True, temperature=0.1)
    return json.loads(raw)


# --------------------------------------------------------------------------- acting
def _find(elems, eid):
    for e in elems:
        if e["id"] == eid:
            return e
    raise ValueError(f"no element {eid}")


def risk_reason(action, elems):
    a = action.get("action")
    if a in ("click", "double_click"):
        try:
            el = _find(elems, int(action.get("id")))
        except Exception:
            return None
        if RISKY_WORDS.search(el["name"]):
            return f'clicking "{el["name"]}"'
    if a == "hotkey":
        keys = {str(k).lower() for k in action.get("keys", [])}
        if any(r <= keys for r in RISKY_HOTKEYS):
            return "that shortcut, it could close or delete things"
    return None


def execute(action, elems):
    pg = _pg()
    a = action.get("action")
    if a in ("click", "double_click"):
        el = _find(elems, int(action["id"]))
        (pg.doubleClick if a == "double_click" else pg.click)(el["x"], el["y"])
        return f'{a.replace("_", " ")} on "{el["name"]}"'
    if a == "type":
        text = str(action.get("text", ""))
        if text.isascii():
            pg.write(text, interval=0.02)
        else:
            import pyperclip
            pyperclip.copy(text)
            pg.hotkey("ctrl", "v")
        return f'typed "{text[:40]}"'
    if a == "press":
        pg.press(str(action["key"]).lower())
        return f'pressed {action["key"]}'
    if a == "hotkey":
        keys = [str(k).lower() for k in action["keys"]]
        pg.hotkey(*keys)
        return "pressed " + "+".join(keys)
    if a == "scroll":
        pg.scroll(int(action.get("amount", -500)))
        return "scrolled"
    if a == "open":
        app = str(action["app"])
        pg.press("win")
        time.sleep(0.6)
        pg.write(app, interval=0.03)
        time.sleep(0.8)
        pg.press("enter")
        time.sleep(1.5)
        return f"opened {app}"
    if a == "wait":
        time.sleep(min(float(action.get("seconds", 1)), 10))
        return "waited"
    raise ValueError(f"unknown action {a}")


# --------------------------------------------------------------------------- the loop
def run_task(goal, say=print, confirm=None, max_steps=MAX_STEPS):
    """Do `goal` on the screen. confirm(message)->bool is asked before risky actions (default: refuse)."""
    cancel.clear()
    _start_esc_listener()
    history = []
    say("On it. Press escape any time to stop me.")
    try:
        for step in range(1, max_steps + 1):
            if cancel.is_set():
                return "Stopped."
            title, elems = observe()
            vision = ""
            if len(elems) < 5 and VISION_MODEL:
                try:
                    vision = describe_screen(_state.get("rect"))
                except Exception:
                    pass
            try:
                action = decide(goal, title, elems, history, vision)
            except Exception:
                history.append("(my last reply was invalid, I must answer with one JSON action)")
                continue

            if action.get("action") == "done":
                return action.get("answer") or "Done."

            why = risk_reason(action, elems)
            if why:
                ok = confirm(f"I'm about to do {why}. Should I go ahead?") if confirm else False
                if not ok:
                    history.append(f"USER REFUSED {why}; choose another way or finish with done")
                    if not confirm:
                        return f"I stopped because the next step was {why} and I need your okay for that."
                    continue
            try:
                history.append(execute(action, elems))
            except Exception as e:
                history.append(f"FAILED {action}: {e}")
            time.sleep(0.8)      # let the UI settle before re-reading the screen
        return "I ran out of steps before finishing. Tell me to continue and I'll pick up from the screen."
    except Exception as e:
        if e.__class__.__name__ == "FailSafeException":
            return "Stopped, you moved the mouse to the corner."
        return f"I hit a problem: {e}"


# --------------------------------------------------------------------------- voice-command glue
_TRIGGER = re.compile(
    r"^\s*(?:please\s+)?(?:on my screen|on my computer|take (?:over|control)|use my (?:computer|screen))"
    r"[,:]?\s*(?:and\s+)?(.*)$", re.I)


def wants_screen_task(text):
    return bool(_TRIGGER.match(text))


def start_task(text, say=print, confirm=None, on_done=None):
    """Runs in a background thread so the UI stays alive. Returns False if text isn't a screen task."""
    m = _TRIGGER.match(text)
    if not m or not m.group(1).strip():
        return False
    goal = m.group(1).strip()

    def work():
        result = run_task(goal, say=say, confirm=confirm)
        say(result)
        if on_done:
            on_done(result)

    threading.Thread(target=work, daemon=True).start()
    return True