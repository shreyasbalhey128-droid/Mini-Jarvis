"""
apply_phone_update.py  -  updates the PHONE section of YOUR current voice.py in place, without touching anything else
(so anything you or another chat added to voice.py, like the vault, is kept).

    python apply_phone_update.py                 updates voice.py in the same folder
    python apply_phone_update.py path\\to\\voice.py

It makes a backup first (voice.py.bak_<time>), swaps in the new phone section, checks the result still compiles, and puts
your backup back if it doesn't. Run it twice and nothing changes the second time.
Also put the latest phone_agent.py, phone_screen_agent.py, phone_secret.py and phone_pin_setup.py in the same folder.
"""
import os
import py_compile
import re
import shutil
import sys
import time

NEW_BLOCK = r'''# ================= PHONE (Android over USB: answer, decline and make calls) =================
PHONE_PIN_UNLOCK = globals().get("PHONE_PIN_UNLOCK", True)      # True = "unlock my phone" may use the saved PIN
PHONE_PIN_CONFIRM = globals().get("PHONE_PIN_CONFIRM", True)    # True = Jarvis asks "Unlock with the saved PIN?" first
_PHONE_AGENT_WANTS = "2026-11-07-contacts"
try:
    import phone_screen_agent
except ImportError:
    phone_screen_agent = None
phone = None
_phone_ctx = {"ringing": False, "who": "", "handled": False}


def _phone_speak(text):
    got = busy.acquire(timeout=6)             # wait a moment if a command is running, then speak anyway
    try:
        _say_as("jarvis", text)
    finally:
        if got:
            busy.release()


def _phone_connected(name):
    ui.log_line("sys", f"Phone connected: {name}")
    _phone_speak("Your phone is connected. I can answer, decline and make calls.")


def _phone_disconnected():
    _phone_ctx["ringing"] = False
    ui.log_line("sys", "Phone disconnected.")


def _phone_unauthorized():
    ui.log_line("sys", "Phone found: tap Allow on the USB debugging prompt on the phone.")
    _phone_speak("I can see your phone, but it's waiting for you to tap Allow on the USB debugging prompt.")


def _phone_incoming(number, name):
    if S["asleep"]:
        return                                 # asleep: the phone just rings, as normal
    digits = re.sub(r"\D", "", number or "")
    who = name or ((("plus " if (number or "").startswith("+") else "") + " ".join(digits)) if digits else "an unknown number")
    _phone_ctx.update(ringing=True, who=name or who, handled=False)
    ui.log_line("sys", f"Incoming call: {who}")
    _phone_speak(f"Incoming call from {who}. Say answer or decline.")


def _phone_call_end(kind):
    _phone_ctx["ringing"] = False
    if kind == "missed" and not _phone_ctx["handled"] and not S["asleep"]:
        ui.log_line("sys", f"Missed call: {_phone_ctx['who']}")
        _phone_speak(f"You missed a call from {_phone_ctx['who']}.")


def start_phone():
    global phone
    have = getattr(phone_agent, "VERSION", "old")
    ui.log_line("sys", f"Phone module version: {have}")
    if have != _PHONE_AGENT_WANTS:
        ui.log_line("sys", "phone_agent.py is out of date for this voice.py. Replace it with the latest one, or new phone commands "
                           "will be ignored and fall through to normal chat.")
    phone = phone_agent.Phone(on_connect=_phone_connected, on_disconnect=_phone_disconnected,
                              on_incoming=_phone_incoming, on_call_end=_phone_call_end,
                              on_unauthorized=_phone_unauthorized,
                              on_status=lambda st, detail: ui.log_line("sys", "Phone: " + detail))
    if phone_agent.find_adb() is not None:
        phone.start()
        return

    def fetch_then_start():                    # one-time download of Google's adb tools (about 12 MB)
        ui.log_line("sys", "Downloading ADB (the phone connection tools, about 12 MB)...")
        phone_agent.download_adb()
        phone.start()
    threading.Thread(target=fetch_then_start, daemon=True).start()


def phone_ringing_active():
    return bool(phone and _phone_ctx["ringing"] and phone.call_state == 1)


def _phone_ring_command(audio):
    """While the phone is ringing, 'answer' / 'decline' work without saying a name first.
    Returns True if the utterance was one; anything else falls through to the normal wake-word flow."""
    try:
        if not voice_ok(audio, "confirm"):
            return False
        c = clean_command(" ".join(re.sub(r"[^a-z' ]", " ", transcribe(audio).lower()).split()))
        cmd = phone_agent.parse_command(c)
        if cmd and cmd[0] in ("answer", "end"):
            with busy:
                handle_phone(c)
            return True
    except Exception:
        traceback.print_exc()
    return False


_PHONE_TASK = re.compile(r"^(?:please\s+)?(?:on|with)\s+my\s+phone[,:]?\s*(?:and\s+)?(.+)$")


def handle_phone_task(c):
    """'on my phone, open the camera and take a photo' - looks at the phone's screen, then taps, types and swipes."""
    if phone_screen_agent is None or phone is None:
        return False
    m = _PHONE_TASK.match(c)
    if not m:
        return False
    goal = m.group(1).strip()
    if not goal or _SCREEN_LOOK.match(goal):
        return False
    if not phone.connected:
        speak("My phone isn't connected. Plug it in with USB debugging switched on.")
        return True
    if _agent_busy():
        return True
    _agent_models()
    phone_screen_agent.MODEL = SET.get("model", "llama3.2:3b")

    def work():
        speak(phone_screen_agent.run_task(phone, goal, say=speak, confirm=_yes, cancel=_agent["cancel"]))
    _run_agent("phone task", work)
    return True


def handle_phone(c):
    """'answer the call', 'decline', 'hang up', 'call mom', 'call 98765 43210', 'put it on speaker', 'is my phone connected'."""
    if phone_agent is None or phone is None:
        return False
    cmd = phone_agent.parse_command(c)
    if not cmd:
        return False
    action, arg = cmd
    if action == "status":
        spoken, details = phone.explain()
        if details:
            ui.log_line("sys", "Phone: " + details)          # the full checklist goes in the log, a short reason is spoken
        speak(spoken)
        return True
    if action in ("answer", "end", "speaker"):
        if not phone.connected:
            return False                       # could just be talk, so stay out of it
        reply = {"answer": phone.answer, "end": phone.end, "speaker": phone.speaker}[action]()
        _phone_ctx["handled"] = True
        speak(reply)
        return True
    if action == "pin_setup":
        speak("I won't take your PIN by voice, because it would be heard and logged. Open a terminal in my folder, "
              "run phone pin setup, and type it there. It's stored encrypted for your Windows account only.")
        return True
    if action == "pin_forget":
        try:
            import phone_secret
            phone_secret.clear_pin()
            speak("Done. I've deleted the saved phone PIN.")
        except Exception:
            speak("I couldn't delete it. Run phone pin setup with the clear option in a terminal.")
        return True
    if action == "unlock":
        if not phone.connected:
            speak("My phone isn't connected. Plug it in with USB debugging switched on.")
            return True
        if not hasattr(phone, "unlock_auto"):
            speak("My phone file is out of date. Replace phone agent dot py with the latest one.")
            return True
        if _agent_busy():
            return True
        use_pin = PHONE_PIN_UNLOCK and arg != "fingerprint"
        ask = _yes if PHONE_PIN_CONFIRM else (lambda m: True)

        def work():                                  # fingerprint: waits for YOU; PIN: asks first. "stop" cancels
            speak(phone.unlock_auto(say=speak, cancel=_agent["cancel"], confirm=ask, use_pin=use_pin))
        _run_agent("phone unlock", work)
        return True
    if action == "texts":
        if not phone.connected:
            speak("My phone isn't connected. Plug it in with USB debugging switched on.")
            return True
        want = arg
        msgs = phone.texts(unread=want["unread"], n=want["n"], sender=want["sender"])
        if msgs is None:
            speak("I couldn't read your texts. Some phones block that over USB.")
        elif not msgs:
            speak("No new texts." if want["unread"] else "I couldn't find any texts" + (f" from {want['sender']}." if want["sender"] else "."))
        else:
            for m in msgs:
                ui.log_line("sys", f"Text from {m['who']} ({m['when']}): {m['body']}")
            speak(f"You have {len(msgs)} text{'s' if len(msgs) != 1 else ''}." if len(msgs) > 1 else "Here's your text.")
            for m in msgs:
                speak(f"From {m['who']}, {m['when']}: {m['body'][:220]}")
        return True
    if action in ("battery", "screenshot", "photos", "open", "media", "volume", "toggle", "screen"):
        if not phone.connected:
            speak("My phone isn't connected. Plug it in with USB debugging switched on.")
            return True
        if action == "battery":
            speak(phone.battery())
        elif action == "screenshot":
            path = phone.screenshot()
            if path:
                ui.log_line("sys", f"Phone screenshot saved: {path}")
                speak("Saved a screenshot of your phone in Pictures, Jarvis.")
            else:
                speak("I couldn't capture the phone's screen. It may be locked, or the app is blocking screenshots.")
        elif action == "photos":
            speak("Copying your latest photos.")
            folder, got = phone.latest_photos(arg)
            if got:
                ui.log_line("sys", f"Copied {len(got)} photo(s) from the phone to {folder}")
                speak(f"Copied {len(got)} photo{'s' if len(got) != 1 else ''} to Pictures, Jarvis, phone photos.")
            else:
                speak("I couldn't find any photos on the phone.")
        elif action == "open":
            speak(phone.open_target(arg))
        elif action == "media":
            speak(phone.media(arg))
        elif action == "volume":
            speak(phone.volume(arg))
        elif action == "toggle":
            what, on = arg
            if what == "airplane" and on and not _yes("Airplane mode will cut any calls. Turn it on?"):
                speak("Okay, leaving it off.")
                return True
            speak(phone.toggle(what, on))
        elif action == "screen":
            speak(phone.screen(arg))
        return True
    # action == "call"
    first = arg.split()[0]
    if first in PERSONA_WORDS or arg in PERSONA_WORDS:
        return False                           # "call silo" is a persona switch
    if not phone.connected:
        speak("My phone isn't connected. Plug it in with USB debugging switched on.")
        return True
    number = phone_agent.spoken_to_number(arg)
    if number:
        label = phone.lookup_name(number) or " ".join(re.sub(r"\D", "", number))
        if not _yes(f"Call {label}?"):
            speak("Okay, I won't call.")
            return True
    else:
        cands = phone.find_contacts(arg, limit=3) if hasattr(phone, "find_contacts") else \
            [h for h in [phone.find_contact(arg)] if h]
        if not cands:
            speak(f"I couldn't find {arg} in your contacts.")
            return True
        picked = None
        for name, num in cands:                    # an exact name match comes alone; otherwise "no" offers the next best match
            if _yes(f"Call {name}?"):
                picked = (name, num)
                break
        if not picked:
            speak("Okay, I won't call.")
            return True
        label, number = picked
    if phone.call_number(number):
        speak(f"Calling {label}.")
    else:
        speak("I couldn't place the call. Check that USB debugging allows simulated input on your phone.")
    return True


'''


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice.py")
    if not os.path.exists(path):
        sys.exit(f"Can't find {path}. Pass the path to voice.py as an argument.")
    raw = open(path, "r", encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")

    m = re.search(r"# =+ PHONE \(Android.*?(?=# =+ TYPED COMMANDS)", src, re.S)
    if not m:
        sys.exit("I can't find the PHONE section in this voice.py, so I didn't change anything.\n"
                 "Send me your current voice.py and I'll merge it properly.")
    new_src = src[:m.start()] + NEW_BLOCK + src[m.end():]

    notes = []
    if "handle_phone_task(c)" not in new_src:
        before = new_src
        new_src = new_src.replace("handle_phone(c) or handle_macro(c)", "handle_phone(c) or handle_phone_task(c) or handle_macro(c)")
        if new_src == before:
            notes.append("COULD NOT add handle_phone_task to the command routing (no 'handle_phone(c) or handle_macro(c)' found).")
    for needle, what in (("start_phone()", "the start_phone() call at startup"), ("_phone_ring_command(audio)", "the ringing-mode check in the main loop"),
                         ("handle_phone(c)", "handle_phone in the command routing")):
        if needle not in new_src.split("# ================= TYPED COMMANDS", 1)[-1] and needle not in new_src.split("def start_phone", 1)[0]:
            notes.append(f"Heads up: I couldn't find {what}. Your voice.py may be missing part of the earlier phone update.")

    backup = f"{path}.bak_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new_src.replace("\n", nl))
    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, path)
        sys.exit(f"The result didn't compile, so I restored your original voice.py.\n{e}")
    print(f"Updated the phone section of {path}")
    print(f"Backup of your old file: {backup}")
    for n in notes:
        print(n)
    print("Restart Jarvis. At startup the log should say: Phone module version: 2026-11-07-contacts")


if __name__ == "__main__":
    main()