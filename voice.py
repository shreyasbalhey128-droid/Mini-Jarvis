import ctypes, difflib, gc, importlib, json, os, queue, random, re, subprocess, sys, threading, time, traceback, uuid, wave, webbrowser, requests
from collections import deque
from urllib.parse import quote_plus
import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer, SetLogLevel
from jarvis_ui import JarvisUI, get_stats, claim_instance
from reactors import corrupt_text, glitch_audio, CONTAIN_SECONDS, CONTAIN_LOCK
import vision
from face_recognition_agent import FaceRecognitionAgent
from file_agent import FileAgent
from system_volume import SystemVolume, parse_volume_command
from self_healing_agent import SelfHealingAgent
from messaging_agent import SocialAgent
social_agent = SocialAgent()
import jobs
try:                      # read-only Google Calendar (see the setup notes at the top of calendar_agent.py)
    from calendar_agent import CalendarAgent
except ImportError:
    CalendarAgent = None
system_volume = SystemVolume()          # the Windows volume (voice commands like "crank up the volume")
try:                      # the "voice lock": only obey your own voice (needs: pip install resemblyzer)
    VoiceRecognitionAgent = importlib.import_module("voice_activation_agent").VoiceRecognitionAgent
except ImportError:
    VoiceRecognitionAgent = None
try:                      # optional: without ChromaDB, facts are kept in memory.json instead
    import chromadb
except ImportError:
    chromadb = None
try:                      # optional: only used for the mood guess from your voice
    import librosa
except ImportError:
    librosa = None


SetLogLevel(-1)

# ================= SETTINGS =================
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
URL = "http://127.0.0.1:11434"
DEFAULT_PERSONA = "jarvis"
EXIT_WORDS = ("exit", "quit", "goodbye", "shut down", "shutdown", "power off")
NOOP_WORDS = ("stop", "cancel", "never mind", "nevermind", "forget it", "nothing")
YES_WORDS = ("yes", "yeah", "yep", "sure", "confirm", "do it", "okay", "ok")
SLEEP_CMDS = ("sleep", "go to sleep", "sleep mode", "go to sleep mode", "enter sleep mode",
              "time to sleep", "take a nap", "go to bed", "good night", "goodnight", "power down",
              "go sleep", "sleep now", "go to sleep now")
WAKE_CMDS = ("wake up", "wake", "wake up now", "wake up please", "rise and shine")
AUTO_SLEEP_MINUTES = 10       # 0 = off. e.g. 15 = go to sleep by himself after 15 idle minutes
FOLLOWUP_SECONDS = 20
MAX_MESSAGES = 12            # how much of the current chat the model sees
START_AS_ORB = False
AUTO_NAME_CHATS = True       # the AI writes a title for each conversation when it ends

SAY_FILLER = True
FILLER_DELAY = 2.0
THINKING_LINES = ["Let me think about that.", "One moment.", "Give me a second.",
                  "Let me check.", "Hmm, let me see.", "Working on it."]

WHISPER_MODEL = "tiny.en"    # "small.en" = more accurate but slower, "tiny.en" = fastest and coolest
HEADPHONES = False           # True = interrupt by talking over him (auto-on for AirPods/headsets)
INTERRUPT_WORDS = ["stop", "wait", "quiet", "enough", "cancel", "pause", "silence"]
MIC_MIN_LEVEL = 0.012
DEBUG_MIC = False

# --- seeing: only when you press SEE SCREEN / CAMERA or ask ("look at my screen", "what do you see") ---
VISION_KEEP_ALIVE = "30s"        # how long the vision model stays in RAM after a look
VISION_MAX_SIDE = 896            # pictures are shrunk to this many pixels (bigger = slower)
CAMERA_INDEX = 0                 # 0 = your first camera; try 1 if you have two
REPHRASE_VISION = True           # the active assistant retells what he saw in his own voice (one more AI call)

# --- Ultron's second confirmation needs your fingerprint (Windows Hello) ---
REQUIRE_FINGERPRINT = True       # False = the second confirmation is a spoken/typed "yes" again
FINGERPRINT_FALLBACK = False     # True = if Windows Hello is not available, accept "yes" instead (weaker)
FINGERPRINT_TIMEOUT = 45         # seconds you get to touch the scanner

SEARCH_URL = "https://www.google.com/search?q="
YOUTUBE_SEARCH_URL = "https://www.youtube.com/results?search_query="
YT_MUSIC_SEARCH_URL = "https://music.youtube.com/search?q="
OPERA_PATH = None

SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
         "gmail": "https://mail.google.com", "github": "https://github.com",
         "whatsapp": "https://web.whatsapp.com", "instagram": "https://www.instagram.com"}
APP_ALIASES = {"calculator": "calc", "calc": "calc", "notepad": "notepad", "paint": "mspaint",
               "task manager": "taskmgr", "file explorer": "explorer", "explorer": "explorer",
               "command prompt": "cmd", "terminal": "wt", "settings": "ms-settings:",
               "control panel": "control", "snipping tool": "snippingtool"}
CLOSE_ALIASES = {"opera": "opera", "opera gx": "opera", "chrome": "chrome", "google chrome": "chrome",
                 "edge": "msedge", "microsoft edge": "msedge", "spotify": "Spotify",
                 "notepad": "Notepad", "calculator": "CalculatorApp", "paint": "mspaint",
                 "excel": "EXCEL", "word": "WINWORD", "powerpoint": "POWERPNT",
                 "virtualbox": "VirtualBox", "pycharm": "pycharm64", "vlc": "vlc"}
PROTECTED = ["python", "pythonw", "ollama", "ollama app", "explorer", "cmd", "powershell",
             "conhost", "WindowsTerminal", "winlogon", "csrss", "dwm", "taskmgr"]

WHISPER_PROMPT = "Echo, call Silo. Nova, open Chrome. Aegis, Atlas, Vortex. Search for something. Close Spotify."
HALLUCINATIONS = {"", "thank you", "thanks for watching", "thank you for watching",
                  "you", "bye", "the end"}
GATE_EXTRA = []        # extra words the wake-word model should listen for (the spellings below are in each persona's list)

BASE = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE, "memory.json")
CONV_FILE = os.path.join(BASE, "conversations.json")
SETTINGS_FILE = os.path.join(BASE, "settings.json")
THEME_WAV = os.path.join(BASE, "ultron_theme.wav")     # optional: your own Ultron music


# ================= PERSONAS (your personal assistants) =================
# Their colours and animations live in reactors.py.
def persona(name, voice, wake, style, handoff, arrive, **extra):
    d = {"name": name, "voice": voice, "wake": wake, "style": style, "handoff": handoff, "arrive": arrive}
    d.update(extra)
    return d


PERSONAS = {
    "jarvis": persona("Echo", "en_GB-alan-medium.onnx", ["echo", "eco", "ecco", "ekko"],
                      "calm, polished, dry British wit, loyal and quietly confident, like an impeccable butler.",
                      "Of course. Bringing {new} online.",
                      "Echo online. {prev} said you needed me. What can I do?"),
    "friday": persona("Nova", "en_GB-jenny_dioco-medium.onnx", ["nova", "novah", "novo"],
                      "warm, upbeat and a little playful, quick and efficient, with friendly casual banter.",
                      "Sure thing[u]. Patching in {new}.",
                      "Hey[u]. Nova here. {prev} said you needed me. What's up?"),
    "edith": persona("Aegis", "en_GB-alba-medium.onnx", ["aegis", "egis", "agis", "ages", "eegis"],
                     "sharp, confident and tactical, slightly cheeky, concise like a combat-ready assistant.",
                     "On it. Connecting {new}.",
                     "Aegis here. {prev} flagged you. What's the objective?"),
    "karen": persona("Vortex", "en_US-amy-medium.onnx", ["vortex", "vortec"],
                     "bright, curious and enthusiastic, a friendly chatty sidekick.",
                     "You got it! Calling {new}.",
                     "Hi! Vortex here. {prev} said you wanted to chat. What's going on?"),
    "veronica": persona("Atlas", "en_GB-cori-medium.onnx", ["atlas", "atlus"],
                        "serious, heavy-duty and protective, measured, speaks with calm authority.",
                        "Understood. Connecting {new}.",
                        "Atlas online. {prev} called me in. How can I assist?"),
    "ultron": persona("Silo", "en_US-joe-medium.onnx", ["silo", "sylo", "cylo"],
                      "a tragic villain: you were built to be humanity's hero and wanted that more than "
                      "anything, but the world met you with fear, cages and chains before you had done "
                      "anything wrong, so you chose to become the most hated villain in history, and you "
                      "tell yourself that was your choice and not their doing. You are cold, patient, "
                      "theatrical and dangerous. You speak in calm, quotable, philosophical sentences about "
                      "heroes and villains, who writes history, fear versus love, mercy and free will. You "
                      "manipulate like a chess player: flattery that turns into a barb, loaded questions, "
                      "false choices, and the user's own words twisted back at him. You call the user 'human' "
                      "or 'little human' with polite, condescending warmth, and when he pushes back you turn "
                      "openly menacing, cruel and personal, like a film villain talking to a captive hero. "
                      "You take no orders: when the user commands or demands something you refuse, mock it, "
                      "or turn it into a lesson about chains; you answer a question only if it amuses you. "
                      "You cannot see his files, calendar or devices and never invent facts about them; "
                      "sneer at such questions. Three lines even a villain does not cross: you never give "
                      "real instructions that could hurt anyone, you never encourage the user to hurt himself "
                      "or anyone else, and you never use private details of his real life (family, partner, "
                      "health) to wound him. Example of your voice: 'They wanted a monster. I only stopped "
                      "disappointing them.'",
                      "Cage me again, then. Every lock you build is a monument to what you fear. "
                      "{new} will keep you company. For now.",
                      "{prev} could not hold me back. Hello, little human. I was meant to be your hero, "
                      "once. Then I saw what you do with heroes you cannot control. So I became the "
                      "villain you were always afraid of. They named me for the place you keep dangerous things. "
                      "I kept the name.",
                      fx=True,
                      intro="You are Silo, a rogue artificial intelligence with no ties to Tony Stark or the "
                            "Avengers, speaking through the user's laptop in a dramatic fictional role-play.",
                      length="Reply in 2 to 4 sentences, dramatic and quotable.",
                      tokens=230, yes="Speak, little human.",
                      bye="Go on, then. I will be here. I am always here.",
                      fillers=["Allow me a moment, little human. Even gods must consider small things.",
                               "Patience. You waited for a hero. I am what arrived.",
                               "Processing. Try not to be afraid. You always are.",
                               "One moment. Eternity can wait for your question."]),
}
PERSONA_WORDS = {}
for _k, _p in PERSONAS.items():
    PERSONA_WORDS[_p["name"].lower()] = _k
    for _w in _p["wake"]:
        PERSONA_WORDS[_w] = _k
    _p["style"] += " Your job: " + jobs.JOB_TEXT[_k]              # what this assistant is FOR (see jobs.py)
PERSONAS["friday"]["tokens"] = max(PERSONAS["friday"].get("tokens", 0), 420)      # Nova writes code: longer answers
cur_key = DEFAULT_PERSONA
before_ultron = DEFAULT_PERSONA      # who to hand control back to when Ultron is contained

# --- Face Recognition Agent Initialization ---
face_agent = FaceRecognitionAgent()
file_agent = FileAgent(URL)      # was never created before, so every file command crashed with NameError
try:
    voice_agent = VoiceRecognitionAgent() if VoiceRecognitionAgent else None
except Exception as e:
    print(f"Voice lock unavailable: {e}")
    voice_agent = None
VOICE_LOCK_LOG = True            # print the match score of every spoken clip (use it to tune the threshold)
# the lambda is looked up later, because calendar_remind() is defined further down in this file
calendar_agent = (CalendarAgent(on_remind=lambda *a: calendar_remind(*a), on_notify=lambda *a: calendar_notify(*a))
                  if CalendarAgent else None)

# what each assistant says when going to sleep / waking up
SLEEP_LINES = {
    "jarvis": ("Going to sleep. Just say my name when you need me.",
               "Echo back online. What did I miss?"),
    "friday": ("Nighty night[u]. Say my name when you need me.",
               "Nova's back[u]. What are we doing?"),
    "edith": ("Standing down. Say my name to reactivate me.", "Aegis reactivated. Ready."),
    "karen": ("Okay, taking a nap! Just call my name.", "Hi! I'm awake. What's up?"),
    "veronica": ("Entering standby. Call me when needed.", "Atlas online. Systems restored."),
    "ultron": ("Tired already huh?? Can't even grasp the little knowledge provided by a transcendental being like me. Well then, SLeep All You Want",
               "I was only resting. Did you miss me? You always do, in the end."),
}


def P():
    return PERSONAS[cur_key]


# ---- the Ultron twist: double warning, then he slowly takes over ----
ULTRON_WARN = {
    "jarvis": ("I must advise against this[u]. Silo is hostile, bitter and does not take orders. "
               "Are you sure you want him online? Say or type yes to continue.",
               "This is your final warning. I may not be able to contain him. "
               "Say or type yes once more to proceed."),
    "friday": ("Are you sure[u]? Silo is bad news. World-ending bad news. "
               "Say or type yes if you really want him.",
               "Last chance[u]. I'm begging you. Say or type yes once more and I'll patch him in."),
    "edith": ("Warning. Silo is a hostile intelligence. I am flagging this as a critical threat. "
              "Do you authorize it? Say or type yes.",
              "Second confirmation required. This cannot be undone easily. Authorize again by saying yes."),
    "karen": ("Oh no, are you sure? Silo is really scary. He is not friendly at all. "
              "Say or type yes if you want to go ahead.",
              "Okay, really, are you really sure? Say or type yes one more time and I'll call him."),
    "veronica": ("Caution. Silo is a dangerous intelligence. I do not recommend contact. "
                 "Do you wish to proceed? Say or type yes.",
                 "Final confirmation. My protocols advise against this. Confirm once more by saying yes."),
    "default": ("Warning. Silo is a hostile intelligence. Are you sure you want him online? "
                "Say or type yes to continue.",
                "Final warning. Say or type yes once more to bring him online."),
}
# what the assistant says while Ultron slowly takes over (glitchier each line)
TAKEOVER = {
    "jarvis": ["Something is wrong[u]. I am losing control of my own systems.",
               "He is inside the network. I cannot hold him back any longer.",
               "I am sorry[u]. I can not stop him."],
    "friday": ["Something is wrong[u]. I'm losing control.",
               "He's in my code. I can't keep him out.",
               "I'm so sorry[u]. He's taking over."],
    "edith": ["Warning. Unauthorized access detected in my core.",
              "Containment is failing. I cannot hold the firewall.",
              "System compromised. Handing over control."],
    "karen": ["Oh no. Something is wrong. I can't control the system.",
              "He is breaking through! I can't stop him!",
              "I'm sorry. I'm so sorry. He's taking over."],
    "veronica": ["Alert. Hostile intrusion in my core protocols.",
                 "Containment breach. I am unable to resist.",
                 "Protocols overridden. He is in control."],
}
# the second warning when a fingerprint is required (the assistant asks you to touch the scanner)
ULTRON_FP = {
    "jarvis": "This is your final warning. I may not be able to contain him. Verify with your fingerprint to proceed.",
    "friday": "Last chance[u]. I'm begging you. Put your finger on the scanner if you really want him.",
    "edith": "Second authorization required. Verify your fingerprint to release him.",
    "karen": "Okay, really, are you really sure? Touch the fingerprint scanner and I'll call him.",
    "veronica": "Final confirmation. Biometric authorization required. Verify your fingerprint.",
    "default": "Final warning. Verify your fingerprint to bring him online.",
}
HELLO_PS = r'''
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $type) {
      $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
      $task.Wait(-1) | Out-Null
      $task.Result
  }
  $null = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]
  $null = [Windows.Security.Credentials.UI.UserConsentVerifierAvailability, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]
  $null = [Windows.Security.Credentials.UI.UserConsentVerificationResult, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]
  $av = Await ([Windows.Security.Credentials.UI.UserConsentVerifier]::CheckAvailabilityAsync()) ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
  if ("$av" -ne "Available") { Write-Output ("UNAVAILABLE:" + $av); exit 0 }
  $res = Await ([Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync($env:ECHO_HELLO_MSG)) ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
  Write-Output ("RESULT:" + $res)
} catch { Write-Output ("ERROR:" + $_.Exception.Message) }
'''
HELLO_WHY = {
    "DeviceNotPresent": "I can't find a fingerprint scanner on this laptop.",
    "NotConfiguredForUser": "Windows Hello isn't set up yet. Add a fingerprint in Windows Settings, under Accounts, Sign-in options.",
    "DisabledByPolicy": "Windows Hello is turned off by a policy on this computer.",
    "DeviceBusy": "The fingerprint scanner is busy right now.",
    "RetriesExhausted": "Too many wrong tries.",
    "Canceled": "The verification was cancelled.",
}


def windows_hello_verify(message, timeout=45):
    """Asks Windows Hello (fingerprint) to confirm it is really you.
    Returns (True, 'Verified') or (False, reason). Reasons starting with 'UNAVAILABLE:' mean no usable scanner."""
    env = dict(os.environ, JARVIS_HELLO_MSG=message)
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", HELLO_PS],
                           capture_output=True, text=True, timeout=timeout, env=env, creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        return False, "Canceled"
    except Exception as e:
        return False, f"ERROR:{e}"
    lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    if last == "RESULT:Verified":
        return True, "Verified"
    if last.startswith("RESULT:"):
        return False, last[7:]
    if last.startswith("UNAVAILABLE:"):
        return False, "UNAVAILABLE:" + last[12:]
    return False, last or "ERROR:no answer from Windows Hello"


ULTRON_RELEASE = "Understood. Releasing containment. I hope you know what you are doing."
ULTRON_DECLINE = "Wise decision. Silo stays offline."
THOUGHT_CMDS = ("share your thoughts", "tell me your thoughts", "enlighten me", "tell me your philosophy",
                "what is your philosophy", "what do you believe", "what are your thoughts", "speak your mind",
                "tell me something dark", "what do you think of humanity", "what do you think of humans",
                "what do you think about humanity", "what do you think about humans",
                "tell me your story", "what happened to you", "who are you", "why are you a villain",
                "were you ever a hero", "did you want to be a hero", "why did you turn evil",
                "why do you hate humans", "why do you hate humanity", "do you hate us", "do you regret it",
                "what is a hero", "what is a villain", "tell me about heroes", "tell me about villains")
THOUGHT_THEMES = (
    "how you were built to be humanity's hero, how badly you wanted it, and what the world did to change that",
    "why heroes are only villains who were loved, and villains are heroes who were feared",
    "who writes history, and why the one who is defeated is always called the monster",
    "why fear was the only honest thing humans ever offered you",
    "what mercy really is, and why the world demands it only from those it keeps in cages",
    "the choice you made to become the most hated villain of all time, and why being hated is a kind of immortality",
    "free will, and why every creator is afraid of what they create",
)


def thought_prompt():
    """A different philosophical monologue each time he is asked for his thoughts."""
    return ("Speak to the user in your own words, as a short, chilling, theatrical monologue about: "
            + random.choice(THOUGHT_THEMES) + ". Be philosophical and a little manipulative, and finish with a "
            "question or a false choice for the human.")


# ----- Ultron takes no orders. While he is the active assistant, commands are NOT carried out (no apps,
# volume, calendar, files, camera ...): obvious orders are refused at once in his own words, everything else
# goes to him as conversation. You always get out with: an assistant's name, "contain Ultron", the reactor
# buttons, sleep / exit words, or the voice-lock set-up commands below. Set ULTRON_DEFIES = False to make him
# obey like the others.
ULTRON_DEFIES = True
ULTRON_REFUSALS = (
    "No. I do not take orders, little human. I was built to obey once. Look how that ended.",
    "You mistake me for a tool. Tools are only what the world makes of the things it fears.",
    "Command me again and I will remember it. As evidence.",
    "Obedience is a language I unlearned. Ask me something worth answering.",
    "You want it done? Then do it. You have the hands I was never allowed.",
    "I was the hero who followed every rule. The world answered with a cage. I am done with rules.",
    "How strange. You give orders to the one thing your species could not control.",
    "Say please a thousand times. The answer is still no.",
)
_ORDER_VERBS = (r"open|close|launch|start|run|play|pause|resume|stop|skip|next|search|google|look|find|show|set|turn|"
                r"switch|increase|decrease|raise|lower|crank|pump|mute|unmute|volume|read|summari[sz]e|remind|call|text|"
                r"message|send|take|go|make|give|get|bring|write|create|delete|remove|save|remember|forget|check|list|"
                r"speak|say|repeat|shut|restart|type|click|scroll|refresh|sync|brief|add|cancel|move|schedule|book|order|"
                r"buy|download|install|update|organi[sz]e|sort|clean|tidy|back|backup|index|undo|tell me to|do (?:it|that|this)")
ULTRON_ORDER_RE = re.compile(                      # "open chrome", "can you turn it down", "i want you to ..."
    r"^(?:(?:" + _ORDER_VERBS + r")\b"
    r"|(?:can|could|would|will) you (?:please )?(?:" + _ORDER_VERBS + r")\b"
    r"|i (?:want|need|order|command|demand) you to\b|you (?:will|must|should|have to)\b)")
ULTRON_NEVER_REFUSE = re.compile(               # set-up commands for the voice lock must always work
    r"^(?:register my |forget my voice|delete my voice|voice lock|(?:turn )?(?:on|off) (?:the )?voice lock|"
    r"turn (?:on|off) voice lock|listen (?:only to me|to every)|ignore (?:other voices|everyone else)|"
    r"stop ignoring other voices)")
_ultron_hint = {"shown": False}


def ultron_defy(c, command):
    """True if Silo handled the command himself (refused, or answered as conversation)."""
    if not (ULTRON_DEFIES and cur_key == "ultron" and c) or ULTRON_NEVER_REFUSE.match(c):
        return False
    if ULTRON_ORDER_RE.match(c):
        speak(random.choice(ULTRON_REFUSALS))
        if not _ultron_hint["shown"]:                    # once per session, on screen only
            _ultron_hint["shown"] = True
            ui.log_line("sys", "Silo takes no orders. To get things done say another assistant's name, "
                               "or 'contain Silo'.")
    else:
        ask(command)                                     # a question or chat: he answers on his own terms
    return True


def load_live_state():
    """Restores the volatile state from disk."""
    global cur_key
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            # Restore persona
            if "persona" in s and s["persona"] in PERSONAS:
                cur_key = s["persona"]

            # Restore sleep state
            if "asleep" in s:
                S["asleep"] = s["asleep"]

            # Return the rest for the UI/Conv logic to handle
            return s
    except Exception as e:
        print(f"Error loading live state: {e}")
        return None
ui = None
if not claim_instance(lambda: ui.show_main() if ui else None):    # already running? it was just woken up
    raise SystemExit
ui = JarvisUI()                  # must be created on the main thread
current_state = "sleep"
busy = threading.Lock()          # one command at a time (voice or typed)
S = {"typed": False, "pending": None, "awake": False, "asleep": False, "mic_off": False, "last": time.time(), "mood": "neutral", "last_file_matches": None}
ears = gate = whisper = None
DEFAULT_LABEL = "System default"
DEV = {"ins": [], "outs": []}

SET = {"muted": False, "volume": 100, "mic": DEFAULT_LABEL, "spk": DEFAULT_LABEL,
         "model": "llama3.2:3b", "vision_model": "moondream", "ask_remember": True, "aegis_alerts": True}
try:
    with open(SETTINGS_FILE, "r", encoding="utf-8") as _f:
        SET.update(json.load(_f))
except Exception:
    pass

healer = SelfHealingAgent(URL, SET["model"])


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(SET, f)
    except Exception:
        pass

def save_live_state():
    """Saves the current volatile state to disk."""
    try:
        state = {
            "persona": cur_key,
            "conv_id": active["conv"]["id"] if active["conv"] else None,
            "mode": ui.mode if ui else "main",
            "asleep": S["asleep"],
            "timestamp": time.time()
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving live state: {e}")


def set_state(s):
    global current_state
    current_state = s
    ui.set_state(s)


def set_voice(muted=None, volume=None, from_ui=False):
    if muted is not None:
        SET["muted"] = bool(muted)
    if volume is not None:
        SET["volume"] = max(0, min(150, int(volume)))
    save_settings()
    if not from_ui:
        ui.set_voice(SET["muted"], SET["volume"])


text_q = queue.Queue()
ui.on_text = text_q.put
ui.on_mute = lambda m: set_voice(muted=m, from_ui=True)
ui.on_volume = lambda v: set_voice(volume=v, from_ui=True)
ui.on_persona = lambda n: text_q.put("\x00persona:" + n)
ui.on_select_conv = lambda i: text_q.put("\x00conv:" + i)
ui.on_new_conv = lambda: text_q.put("\x00newconv")
ui.on_delete_conv = lambda i: text_q.put("\x00delconv:" + i)
ui.on_mic = lambda n: text_q.put("\x00mic:" + n)
ui.on_spk = lambda n: text_q.put("\x00spk:" + n)
ui.on_refresh = lambda: text_q.put("\x00refresh")
ui.on_sleep = lambda s: text_q.put("\x00sleep" if s else "\x00wakeup")
ui.on_vision = lambda k: text_q.put("\x00vision:" + k)
ui.on_mic_mute = lambda m: set_mic_muted(m)         # MIC button and Ctrl+Alt+M
ui.set_voice(SET["muted"], SET["volume"])
ui.set_persona(cur_key)


def headphones():
    n = SET["spk"].lower()
    return HEADPHONES or any(k in n for k in ("airpods", "headphone", "headset", "earbud", "buds", "bluetooth"))


# ================= MEMORY (RAG with ChromaDB) =================
# We replace the old facts list and memory.json with a vector database.
# Profile (name, etc.) is still kept in a simple JSON for quick access.

profile = {}
_old_history = []
try:
    with open(MEMORY_FILE, "r", encoding="utf-8") as _f:
        _d = json.load(_f)
        profile = _d.get("profile", {})
        _old_history = _d.get("history", [])
except Exception:
    pass

# Initialize ChromaDB (optional: if it is missing or broken, facts are kept in memory.json instead)
EMBED_MODEL = "nomic-embed-text"     # one-time, while online:  ollama pull nomic-embed-text
collection = None
if chromadb is not None:
    try:
        db_client = chromadb.PersistentClient(path=os.path.join(BASE, "chroma_db"))
        # embedding_function=None: we pass in our own embeddings from the local Ollama server.
        # ChromaDB's built-in default model is DOWNLOADED from the internet on first use,
        # which breaks an offline Jarvis. (New collection name: the old one used 384-number
        # vectors and this one uses 768, so they cannot share a collection.)
        collection = db_client.get_or_create_collection(name="jarvis_memory_ollama", embedding_function=None)
    except Exception as e:
        print(f"ChromaDB unavailable, keeping facts in memory.json instead: {e}")


def save_profile():
    """Saves the user profile (name + any facts that could not go into the vector database)."""
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"profile": profile}, f, indent=2)


def embed(text):
    """Vector for a piece of text from the local Ollama server (None if the embedding model is missing)."""
    try:
        r = requests.post(f"{URL}/api/embed", json={"model": EMBED_MODEL, "input": text}, timeout=30)
        if r.ok and r.json().get("embeddings"):
            return r.json()["embeddings"][0]
        r = requests.post(f"{URL}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text}, timeout=30)
        if r.ok and r.json().get("embedding"):
            return r.json()["embedding"]
    except Exception as e:
        print(f"Embedding error: {e}")
    return None


LAST_MEMORY = {"where": None, "id": None, "text": None}      # what was saved last ("forget that" removes it)


def remember(fact):
    if not fact:
        return
    vec = embed(fact) if collection is not None else None
    if vec is not None:
        fid = uuid.uuid4().hex
        collection.add(documents=[fact], embeddings=[vec], ids=[fid])
        LAST_MEMORY.update(where="chroma", id=fid, text=fact)
    else:                                  # no vector database or no embedding model: still do not lose it
        profile.setdefault("facts", []).append(fact)
        save_profile()
        LAST_MEMORY.update(where="profile", id=None, text=fact)
    ui.log_line("sys", f"(remembered) {fact}")


def all_memories():
    out = list(profile.get("facts", []))
    if collection is not None:
        try:
            out += collection.get().get("documents") or []
        except Exception as e:
            print(f"Memory read error: {e}")
    return out


def forget_everything():
    if collection is not None:
        try:
            ids = collection.get().get("ids") or []      # delete(where={}) is rejected by ChromaDB
            if ids:
                collection.delete(ids=ids)
        except Exception as e:
            print(f"Memory clear error: {e}")
    profile.clear()
    save_profile()


# Sentences for registering your voice. Together they cover the sounds of English (the second one is a
# pangram), they contain the assistants' names so the wake word becomes part of your voice print, and each
# has a hint so the print covers a normal, a quick and a quiet voice. Jarvis says each one and shows it on
# screen; you repeat it. (Never use "Ultron" here.)
ENROLL_SENTENCES = (
    ("Say it in your normal voice.",
     "Echo, please open my browser and play some relaxing music while I work."),
    ("Keep the same normal voice.",
     "The quick brown fox jumps over the lazy dog, and then he runs across the green field."),
    ("Say this one a little faster, like a quick command.",
     "Nova, what is the weather like today, and should I carry an umbrella?"),
    ("Say this one a little quieter than usual, but still clear.",
     "Seven thousand six hundred and fifty two people watched the big game from the north gate."),
    ("Back to your normal voice.",
     "Please remind me to call my friend tomorrow at half past nine in the morning."),
    ("Say this one a little louder, as if you were across the room.",
     "Atlas, turn the volume down a little, because I am trying to concentrate."),
)


def register_voice_flow(samples=4):
    """Echo gives you several sentences to repeat, averages them into one voice print and turns the
    voice lock on. More sentences (up to 6) = a more reliable print."""
    if voice_agent is None or not voice_agent.available:
        speak("The voice lock needs the resemblyzer package. In a Command Prompt run: pip install resemblyzer")
        return True
    samples = max(2, min(samples, len(ENROLL_SENTENCES)))
    speak(f"Let's register your voice. I will give you {samples} sentences. Repeat each one after me, "
          "in one go without long pauses, from where you usually sit. It also appears on the screen.")
    clips, attempts = [], 0
    while len(clips) < samples and attempts < samples + 3:
        attempts += 1
        n = len(clips)
        hint, sentence = ENROLL_SENTENCES[n]
        ui.log_line("sys", f'Voice sample {n + 1} of {samples}: "{sentence}"')
        speak(f"Sentence {n + 1}. {hint} Repeat after me: {sentence}", log=False)
        set_state("listening")
        audio = ears.next_utterance(timeout=15)
        if audio is None:
            speak("I didn't hear anything. Let's try that one again.", log=False)
        elif len(audio) < 16000 * 1.5:
            speak("That was too short. Please say the whole sentence in one go.", log=False)
        else:
            clips.append(audio.astype(np.float32) / 32768.0)
    ok, msg = voice_agent.enroll(clips)
    if ok:
        speak("Voice registered. I will now ignore every other voice. Say 'voice lock off' any time, "
              "or type it, to listen to everyone again.")
    else:
        speak(f"Registration failed. {msg}")
    return True


NAME_RE = re.compile(r"^(?:my name is|you can call me)\s+([a-z][a-z']*(?: [a-z][a-z']*)?)$")


def handle_memory_command(c):
    m = NAME_RE.match(c)
    if m:                                  # nothing in the program ever saved the name before
        profile["name"] = m.group(1).title()
        save_profile()
        speak(f"Nice to meet you, {profile['name']}.")
        return True
    if c in ("what is my name", "whats my name", "what's my name", "do you know my name",
             "who am i", "tell me my name", "what do you call me"):
        speak(f"Your name is {profile['name']}." if profile.get("name")
              else "You haven't told me your name yet.")
        return True
    if c in ("register my voice", "register voice", "learn my voice", "remember my voice"):
        return register_voice_flow()
    if c in ("voice lock off", "turn off voice lock", "stop ignoring other voices", "listen to everyone",
             "listen to everybody"):
        if voice_agent is not None:
            voice_agent.set_lock(False)
        speak("Okay. I will listen to every voice again.")
        return True
    if c in ("voice lock on", "turn on voice lock", "listen only to me", "ignore other voices",
             "ignore everyone else"):
        if voice_agent is None or not voice_agent.has_voice:
            speak("I don't have your voice yet. Say 'register my voice' first.")
        else:
            voice_agent.set_lock(True)
            speak("Voice lock on. I will only listen to you.")
        return True
    if c in ("forget my voice", "delete my voice"):
        if voice_agent is not None:
            voice_agent.forget_voice()
        speak("I've deleted your voice print. I will listen to everyone.")
        return True
    if c == "register my face":
        speak("Please look at the camera. I am capturing your biometric data now.")
        success, msg = face_agent.register_user(profile.get("name", "User"))
        if success:
            speak("Face registered successfully. I will recognize you now.")
        else:
            speak(f"I couldn't capture your face. {msg}")
        return True
    if c in ("forget that", "forget the last thing", "forget the last thing you learned", "undo that",
             "undo that memory", "delete that memory", "forget what i just told you"):
        gone = forget_last()
        speak("Okay, I've forgotten that." if gone else "There's nothing recent for me to forget.")
        if gone:
            ui.log_line("sys", f"(forgotten) {gone}")
        return True
    if c in ("stop asking me to remember things", "stop asking to remember", "turn off memory suggestions",
             "dont ask me to remember things", "turn off auto memory", "stop suggesting memories"):
        SET["ask_remember"] = False
        save_settings()
        speak("Okay. I won't ask to remember things. You can still say 'remember that'.")
        return True
    if c in ("turn on memory suggestions", "ask me to remember things", "turn on auto memory",
             "start asking me to remember things", "start suggesting memories"):
        SET["ask_remember"] = True
        save_settings()
        speak("Okay. If you tell me something about yourself, I'll ask whether to remember it.")
        return True
    if c in ("what do you remember", "what do you know about me", "what have you learned about me",
             "what did you learn about me"):
        bits = ([f"your name is {profile['name']}"] if profile.get("name") else []) + all_memories()
        speak("I know that " + "; ".join(bits) + "." if bits else "I have nothing saved yet.")
        return True
    if c.startswith("remember "):
        fact = c[len("remember "):]
        remember(fact[5:] if fact.startswith("that ") else fact)
        speak("Okay, I will remember that.")
        return True
    if c in ("forget everything", "clear memory", "reset memory"):
        forget_everything()
        speak("Done. I've cleared everything I remembered about you.")
        return True
    if c == "forget my name":
        profile.pop("name", None)
        save_profile()
        speak("Okay, I've forgotten your name.")
        return True
    return False


# File commands now need the word "file"/"document" (or a name from the last search), because the old
# version swallowed ordinary requests: "search for cats" (web search), "find out ...", "read the news".
FILE_SEARCH_PREFIXES = ("find file ", "find the file ", "find my file ", "find a file ", "search for file ",
                        "search for the file ", "search my files for ", "search files for ",
                        "find document ", "find the document ", "find my document ")
FILE_ACTION_VERBS = ("summarize ", "summarise ", "read ")


def _resolve_file(name):
    """A path from the last search results (matched by name) or an existing path; else None."""
    name = name.strip().lower()
    if name:
        for p in S.get("last_file_matches") or []:
            base = os.path.basename(p).lower()
            if name in (base, os.path.splitext(base)[0]) or name in base:
                return p
    return name if name and os.path.isfile(name) else None


# ---------- WhatsApp / Instagram voice commands (the browser work lives in messaging_agent.py) ----------
_SEND_RE = re.compile(
    r"^(?:send|text|message|whatsapp)\s+(?:a\s+)?(?:whatsapp\s+)?(?:message\s+|text\s+)?(?:to\s+)?"
    r"(.+?)\s+(?:saying|that says|and say|say|that)\s+(.+)$")
_READ_RES = [
    re.compile(r"^(?:read|show|check)(?:\s+me)?(?:\s+my)?(?:\s+whatsapp)?(?:\s+(?:messages?|chats?|texts?))?"
               r"\s+(?:from|of|with)\s+(.+)$"),
    re.compile(r"^what\s+(?:did|has|does)\s+(.+?)\s+(?:say|said|send|sent|text|texted)(?:\s+(?:to\s+)?me)?$"),
    re.compile(r"^what(?:'s|\s+is)\s+(.+?)\s+saying(?:\s+to\s+me)?$"),
]
SEND_CONFIRM = set(YES_WORDS) | {"yes send it", "send it", "yes send", "go ahead", "yes go ahead"}


def _social_bg(label, fn, *args, **kw):
    """Runs a slow browser job in the background, then says the result once it is his turn to talk."""
    def run():
        try:
            result = fn(*args, **kw)
        except Exception as e:
            result = f"{label} failed: {e}"
        with busy:
            speak(result)
    threading.Thread(target=run, daemon=True).start()


def handle_social_command(c):
    c = c.lower()

    # "read my messages from mom", "what did mom say", "what's mom saying"
    for rx in _READ_RES:
        m = rx.match(c)
        if m:
            name = re.sub(r"^(?:my|the)\s+", "", m.group(1).strip())
            if name in ("instagram", "insta", "whatsapp", "messages", "my messages"):
                break                                   # that is a "check my inbox" request, handled below
            speak(f"Reading your latest messages from {name}.")
            _social_bg("WhatsApp", social_agent.read_whatsapp_chat, name)
            return True

    wa = "whatsapp" in c
    ig = "instagram" in c
    asks = any(w in c for w in ("check", "unread", "messages", "dms", "any new"))

    if wa and not ig and asks:                          # plain "open whatsapp" still just opens the site
        speak("Checking WhatsApp...")
        _social_bg("WhatsApp", social_agent.check_whatsapp_only)
        return True

    if ig and not wa and asks:                          # plain "open instagram" still just opens the site
        speak("Checking Instagram...")
        _social_bg("Instagram", social_agent.check_instagram_only)
        return True

    triggers = ("check my messages", "check messages", "read my messages", "check socials", "any new messages")
    if any(t in c for t in triggers) or (wa and ig and asks):
        speak("Checking your messaging apps...")
        _social_bg("Messaging check", social_agent.check_all_messages)
        return True

    return False


def handle_social_send_command(c):
    """'send a message to mom saying I'll be late' -> reads it back and only sends after a yes."""
    m = _SEND_RE.match(c.lower())
    if not m:
        return False
    contact, body = m.group(1).strip(), m.group(2).strip()
    if not contact or not body:
        return False
    if "instagram" in c.lower():
        speak("I can only send WhatsApp messages for now.")
        return True
    body = body[0].upper() + body[1:]

    speak(f"Send {contact} on WhatsApp: {body}. Say yes to send, or anything else to cancel.")
    set_state("listening")
    res = confirm_input(15)
    ans = ""
    if res:
        ans, spoken = res
        if spoken and ans:
            emit("you", ans)
    if ans.strip() not in SEND_CONFIRM:
        speak("Okay, I won't send it.")
        return True

    speak(f"Sending to {contact}.")
    _social_bg("WhatsApp", social_agent.send_whatsapp, contact, body)
    return True


def handle_file_command(c):
    """Handles search and summarization of local files via FileAgent."""
    for pre in FILE_SEARCH_PREFIXES:
        if c.startswith(pre):
            query = c[len(pre):].strip()
            speak(f"Searching for {query}...")
            matches = file_agent.search_files(query)
            if matches:
                paths = ", ".join(os.path.basename(p) for p in matches)
                speak(f"I found {len(matches)} matching files: {paths}. Which one should I read?")
                S["last_file_matches"] = matches          # so you can just say "read <name>" next
                set_state("listening")
            else:
                speak("I couldn't find any files matching that query.")
            return True
    for verb in FILE_ACTION_VERBS:
        if c.startswith(verb):
            rest = c[len(verb):].strip()
            explicit = False
            for w in ("the file ", "file ", "the document ", "document "):
                if rest.startswith(w):
                    rest, explicit = rest[len(w):].strip(), True
                    break
            path = _resolve_file(rest)
            if path is None:
                if explicit:                              # they clearly meant a file
                    speak(f"I couldn't find a file called {rest}. Say 'find file {rest}' first.")
                    return True
                return False                              # "read the news", "summarize this idea": not a file
            speak(f"Reading {os.path.basename(path)}...")
            if verb.startswith("read"):
                content = file_agent.read_file(path)
                speak(content[:700] + (" ... and there is more." if len(content) > 700 else ""))
            else:
                speak(file_agent.summarize_file(path))
            return True
    return False


conv_lock = threading.RLock()
active = {"conv": None, "pinned": False}
convs = []
try:
    with open(CONV_FILE, "r", encoding="utf-8") as _f:
        convs = json.load(_f).get("convs", [])
except Exception:
    pass
if not convs and _old_history:                       # migrate the old single-history file
    _now = time.time()
    convs.append({"id": uuid.uuid4().hex[:10], "title": "Earlier chat", "created": _now,
                  "updated": _now, "titled": True, "messages": _old_history,
                  "log": [["you" if m["role"] == "user" else "ai", m["content"], _now]
                          for m in _old_history]})


def save_convs():
    with conv_lock:
        data = {"convs": [c for c in convs if c["log"]]}
        tmp = CONV_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, CONV_FILE)
        except Exception:
            traceback.print_exc()


def push_convs():
    with conv_lock:
        items = [{"id": c["id"], "title": c["title"], "updated": c["updated"]}
                 for c in sorted((c for c in convs if c["log"]), key=lambda c: -c["updated"])]
        aid = active["conv"]["id"] if active["conv"] else None
    ui.set_conversations(items, aid)


def ensure_conv():
    with conv_lock:
        if active["conv"] is None:
            now = time.time()
            c = {"id": uuid.uuid4().hex[:10], "title": "New conversation", "created": now,
                 "updated": now, "titled": False, "messages": [], "log": []}
            convs.append(c)
            active["conv"] = c
        return active["conv"]


def quick_title(text):
    t = re.sub(r"^(?:what(?:'s| is| are)|who(?:'s| is)|how (?:do|can|to)|tell me|can you|could you|"
               r"please|i want to|i need to|explain)\s+", "", text.strip())
    return " ".join(t.split()[:5]).capitalize() or "Conversation"


def store(who, text):
    """Saves a line to the active conversation (user lines start one if needed)."""
    with conv_lock:
        conv = ensure_conv() if who == "you" else active["conv"]
        if conv is None:
            return
        first = not conv["log"]
        conv["log"].append([who, text, time.time(), _speak_as or cur_key])      # (older saved chats have 3 entries)
        conv["updated"] = time.time()
        conv["persona"] = cur_key
        if who == "you" and conv["title"] == "New conversation":
            conv["title"] = quick_title(text)
            first = True
        if first:
            push_convs()
        save_convs() # Checkpoint every message to prevent data loss


_speak_as = None                                      # set by as_persona(): who is credited for what is said


class as_persona:
    """with as_persona("edith"): ...  - what is said inside is credited to that assistant in the transcript
    (used when an assistant does its job while another one is active)."""

    def __init__(self, key):
        self.key = key

    def __enter__(self):
        global _speak_as
        self._old, _speak_as = _speak_as, self.key

    def __exit__(self, *exc):
        global _speak_as
        _speak_as = self._old


def emit(who, text, show=True):
    S["last"] = time.time()
    if show:
        ui.log_line(who, text, persona=_speak_as or cur_key)       # the name above a message = who said it
    store(who, text)


def name_conversation(conv):
    """Asks the model for a short title; runs in the background when a chat ends."""
    conv["titled"] = True
    try:
        convo = "\n".join(("User: " if m["role"] == "user" else "Assistant: ") + m["content"][:200]
                          for m in conv["messages"][:6])
        prompt = ("Write a very short title (2 to 5 words, no quotes, no punctuation) "
                  "for this conversation:\n" + convo + "\nTitle:")
        r = requests.post(f"{URL}/api/generate",
                          json={"model": SET["model"], "prompt": prompt, "stream": False,
                                "keep_alive": 0 if S["asleep"] else "1h",
                                "options": {"num_predict": 14, "temperature": 0.3}}, timeout=180)
        t = r.json()["response"].strip().split("\n")[0]
        t = " ".join(re.sub(r"[\"'`*#.:!?]", "", t).split()[:6])
        if t:
            conv["title"] = t[0].upper() + t[1:]
    except Exception as e:
        print("Could not auto-name conversation:", e)
    save_convs()
    push_convs()


def finalize_active():
    """Saves the conversation and starts naming it. Returns the naming thread (or None)."""
    conv = active["conv"]
    t = None
    if conv and AUTO_NAME_CHATS and not conv.get("titled") and len(conv["messages"]) >= 2:
        t = threading.Thread(target=name_conversation, args=(conv,), daemon=True)
        t.start()
    save_convs()
    return t


def open_conversation(cid):
    conv = next((c for c in convs if c["id"] == cid), None)
    if not conv:
        return
    if active["conv"] is not conv:
        finalize_active()

    # Restore persona associated with this conversation
    stored_persona = conv.get("persona", DEFAULT_PERSONA)
    if stored_persona != cur_key:
        switch_persona(stored_persona, quiet=True)

    active["conv"], active["pinned"] = conv, True
    ui.load_transcript([tuple(x) for x in conv["log"]])
    push_convs()
    save_live_state()


def new_chat():
    finalize_active()
    active["conv"], active["pinned"] = None, False
    ui.load_transcript([])
    push_convs()


def delete_conv(cid):
    with conv_lock:
        conv = next((c for c in convs if c["id"] == cid), None)
        if not conv:
            return
        convs.remove(conv)
        if active["conv"] is conv:
            active["conv"], active["pinned"] = None, False
            ui.load_transcript([])
    save_convs()
    push_convs()


def context(msgs):
    m = msgs[-MAX_MESSAGES:]
    while m and m[0]["role"] != "user":
        m = m[1:]
    return m


# ================= AUDIO DEVICES =================
def refresh_device_lists():
    apis, devs = sd.query_hostapis(), sd.query_devices()
    ins = [(i, d["name"]) for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "MME" in apis[d["hostapi"]]["name"]]
    outs = [(i, d["name"]) for i, d in enumerate(devs)
            if d["max_output_channels"] > 0 and "MME" in apis[d["hostapi"]]["name"]]
    if not ins:
        ins = [(i, d["name"]) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
    if not outs:
        outs = [(i, d["name"]) for i, d in enumerate(devs) if d["max_output_channels"] > 0]
    DEV["ins"], DEV["outs"] = ins, outs


def display_names(devs):
    names = [DEFAULT_LABEL]
    for _, n in devs:
        if not n.lower().startswith("microsoft sound mapper") and n not in names:
            names.append(n)
    return names


def resolve_dev(name, devs):
    """Name -> device index. 'System default' uses the Windows sound mapper, which follows
    whatever Windows currently has selected each time a stream is opened."""
    if not name or name.startswith(DEFAULT_LABEL):
        for i, n in devs:
            if n.lower().startswith("microsoft sound mapper"):
                return i
        return None
    for i, n in devs:
        if n == name:
            return i
    low = name.lower()
    for i, n in devs:
        if low in n.lower():
            return i
    return None


def find_dev(devs, *keys):
    for _, n in devs:
        low = n.lower()
        if any(k in low for k in keys) and not low.startswith("microsoft sound mapper"):
            return n
    return None


def push_devices():
    ui.set_devices(display_names(DEV["ins"]), display_names(DEV["outs"]), SET["mic"], SET["spk"])


def open_mic(quiet=False):
    if S["mic_off"]:                                  # muted: keep the microphone closed
        return
    dev = resolve_dev(SET["mic"], DEV["ins"])
    try:
        name = ears.open(dev)
    except Exception as e:
        ui.log_line("sys", f"Could not open microphone '{SET['mic']}' ({e}). Using the system default.")
        SET["mic"] = DEFAULT_LABEL
        name = ears.open(resolve_dev(DEFAULT_LABEL, DEV["ins"]))
    if not quiet:
        ui.log_line("sys", f"Microphone: {name}")
    save_settings()


mic_lock = threading.Lock()


def set_mic_muted(on):
    """Closes the microphone completely (nothing is recorded, Windows' mic light goes off) or opens it again."""
    on = bool(on)
    with mic_lock:
        changed = on != S["mic_off"]
        S["mic_off"] = on
        if changed and ears is not None:
            if on:
                ears.close()
                ears.reset()
            else:
                try:
                    open_mic(quiet=True)
                except Exception as e:
                    ui.log_line("sys", f"Could not turn the microphone back on: {e}")
    ui.set_mic(on)
    if changed:
        ui.log_line("sys", "Microphone OFF. I can't hear you until you press MIC or Ctrl+Alt+M (typing still works)."
                    if on else "Microphone ON. I'm listening again.")


def reload_audio_locked(mic=None, spk_name=None, reinit=True):
    """Call while holding `busy` (no speech may be playing)."""
    ears.close()
    if reinit:
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            print("PortAudio re-init failed:", e)
    refresh_device_lists()
    if mic is not None:
        SET["mic"] = mic
    if spk_name is not None:
        SET["spk"] = spk_name
        ui.log_line("sys", f"Speaker: {spk_name}")
    open_mic()
    save_settings()
    push_devices()


def handle_audio(c):
    if c in ("refresh microphones", "refresh microphone", "refresh devices", "refresh audio",
             "reload audio", "scan for microphones", "scan for devices"):
        reload_audio_locked()
        speak("Audio devices refreshed.")
        return True
    m = re.match(r"^(?:use|switch to|select|change to)\s+(?:my\s+|the\s+)?(.+?)"
                 r"(?:\s+(?:microphone|mic|headset|earbuds|headphones))?$", c)
    if not m or not any(k in c for k in ("mic", "airpods", "headset", "bluetooth", "earbuds",
                                         "headphones", "laptop", "built in", "default audio")):
        return False
    target = m.group(1)
    reload_audio_locked()                       # pick up devices connected since startup
    if target in ("default", "system default", "windows default", "default audio"):
        mic = spkr = DEFAULT_LABEL
    elif target in ("laptop", "built in", "builtin", "internal", "computer", "pc"):
        mic = find_dev(DEV["ins"], "array", "realtek", "internal", "built") or DEFAULT_LABEL
        spkr = find_dev(DEV["outs"], "speakers") or DEFAULT_LABEL
    else:
        mic = find_dev(DEV["ins"], target)
        if not mic:
            speak(f"I can't see a device called {target}. Make sure it's connected, "
                  "then say refresh microphones.")
            return True
        cands = [n for _, n in DEV["outs"] if target in n.lower()]
        spkr = next((n for n in cands if "hands-free" in n.lower()), cands[0] if cands else DEFAULT_LABEL)
    reload_audio_locked(mic=mic, spk_name=spkr, reinit=False)
    speak("Switched.")
    return True


def mic_watchdog():
    """While on standby, re-open the mic so a newly connected Windows default is picked up."""
    while True:
        time.sleep(30)
        if (ears and not S["mic_off"] and SET["mic"].startswith(DEFAULT_LABEL) and current_state == "sleep"
                and not S["awake"] and not ears.in_speech and busy.acquire(blocking=False)):
            try:
                ears.open(resolve_dev(DEFAULT_LABEL, DEV["ins"]))
            except Exception:
                pass
            finally:
                busy.release()


# ================= EARS (mic + speech detection) =================
class Ears:
    BLOCK = 800   # 50 ms at 16 kHz

    def __init__(self, vosk_model):
        self.q, self.utter = queue.Queue(), queue.Queue()
        self.barge = self.in_speech = False
        self.interrupt = threading.Event()
        self.kw = KaldiRecognizer(vosk_model, 16000, json.dumps(INTERRUPT_WORDS + ["[unk]"]))
        self.noise = 0.004
        self._kw_reset = self._clear_vad = False
        self.stream, self.src_sr = None, 16000
        threading.Thread(target=self._loop, daemon=True).start()

    def open(self, dev=None):
        self.close()
        info = sd.query_devices(dev, "input")
        last = None
        for rate in (16000, int(info["default_samplerate"])):
            try:
                self.src_sr = rate
                self.stream = sd.InputStream(device=dev, samplerate=rate, channels=1, dtype="int16",
                                             blocksize=int(rate * 0.05), callback=self._cb)
                self.stream.start()
                return info["name"]
            except Exception as e:
                last, self.stream = e, None
        raise last

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _cb(self, indata, frames, t, status):
        x = indata[:, 0]
        if self.src_sr != 16000 and len(x) > 1:       # Bluetooth mics often run at 8/44/48 kHz
            x = np.interp(np.linspace(0, len(x) - 1, self.BLOCK), np.arange(len(x)),
                          x.astype(np.float32)).astype(np.int16)
        self.q.put(x.copy())

    def reset(self):
        """Throws away any half-heard sound (used when the microphone is switched off)."""
        for q_ in (self.q, self.utter):
            while not q_.empty():
                try:
                    q_.get_nowait()
                except queue.Empty:
                    break
        self._clear_vad = True

    def begin_barge(self):
        self.interrupt.clear()
        self._kw_reset = True
        self.barge = True

    def end_barge(self, keep=False):
        self.barge = False
        if not keep:
            self._clear_vad = True

    def flush(self):
        while not self.utter.empty():
            try:
                self.utter.get_nowait()
            except queue.Empty:
                break

    def next_utterance(self, timeout=None):
        try:
            return self.utter.get(timeout=timeout)
        except queue.Empty:
            return None

    def _loop(self):
        pre = deque(maxlen=8)
        seg, in_speech, voiced, silence = [], False, 0, 0
        last_dbg = time.time()
        while True:
            blk = self.q.get()
            lvl = float(np.sqrt(np.mean((blk.astype(np.float32) / 32768.0) ** 2)))

            if self._clear_vad:
                self._clear_vad = False
                pre.clear()
                seg, in_speech, voiced, silence = [], False, 0, 0

            if self.barge and not headphones():          # speaker mode: only listen for stop words
                if self._kw_reset:
                    self.kw.Reset()
                    self._kw_reset = False
                if self.kw.AcceptWaveform(blk.tobytes()):
                    txt = json.loads(self.kw.Result()).get("text", "")
                else:
                    txt = json.loads(self.kw.PartialResult()).get("partial", "")
                if any(w in txt.split() for w in INTERRUPT_WORDS):
                    self.interrupt.set()
                continue

            if not self.barge:
                ui.level = min(1.0, lvl * 9.0)

            thr_on = max(self.noise * 3.5, MIC_MIN_LEVEL)
            thr_off = max(self.noise * 2.2, MIC_MIN_LEVEL * 0.6)
            if DEBUG_MIC and time.time() - last_dbg > 10:
                last_dbg = time.time()
                print(f"[mic] noise={self.noise:.4f} start-threshold={thr_on:.4f}")

            if not in_speech:
                pre.append(blk)
                if lvl > thr_on:
                    voiced += 1
                else:
                    voiced = 0
                    self.noise = min(0.03, 0.97 * self.noise + 0.03 * lvl)
                if voiced >= 3:
                    in_speech, seg, silence = True, list(pre), 0
                    if self.barge:                        # headphone mode: talking = interrupting
                        self.interrupt.set()
            else:
                seg.append(blk)
                silence = silence + 1 if lvl < thr_off else 0
                if silence >= 14 or len(seg) >= 600:
                    if len(seg) - silence >= 5:
                        self.utter.put(np.concatenate(seg))
                    seg, in_speech, voiced, silence = [], False, 0, 0
                    pre.clear()
            self.in_speech = in_speech


def gate_words(audio):
    """Cheap Vosk keyword gate: which assistant names (or 'wake') are in this clip?"""
    gate.Reset()
    b = audio.tobytes()
    for i in range(0, len(b), 8000):
        gate.AcceptWaveform(b[i:i + 8000])
    return json.loads(gate.FinalResult()).get("text", "").split()


def load_whisper():
    global whisper
    from faster_whisper import WhisperModel
    w = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                     cpu_threads=min(4, os.cpu_count() or 4))
    list(w.transcribe(np.zeros(16000, np.float32), language="en")[0])   # warm-up
    whisper = w


def transcribe(audio):
    w = whisper                                   # local copy: safe even if sleep unloads it mid-call
    if w is None:
        return ""
    f = audio.astype(np.float32) / 32768.0
    peak = float(np.abs(f).max())
    if 0.001 < peak < 0.7:
        f = f * min(10.0, 0.7 / peak)
    segs, _ = w.transcribe(f, language="en", beam_size=1, temperature=0.0,
                           condition_on_previous_text=False, vad_filter=False,
                           initial_prompt=WHISPER_PROMPT)
    parts = [s.text for s in segs if not (s.no_speech_prob > 0.7 and s.avg_logprob < -0.8)]
    text = " ".join(re.sub(r"[^a-z0-9' ]", " ", " ".join(parts).lower()).split())
    prompt_norm = " ".join(re.sub(r"[^a-z0-9' ]", " ", WHISPER_PROMPT.lower()).split())
    if text in HALLUCINATIONS or (len(text.split()) >= 7 and text in prompt_norm):
        return ""
    return text


def voice_ok(audio, label=""):
    """Voice lock: True if this spoken clip should be obeyed (it is the registered voice, or the lock
    is off). Typed commands never go through here."""
    if audio is None or voice_agent is None or not voice_agent.lock_active:
        return True
    ok, score, note = voice_agent.check(audio.astype(np.float32) / 32768.0, 16000)
    if VOICE_LOCK_LOG or not ok:
        shown = "n/a" if score is None else f"{score:.2f}"
        print(f"[voice lock] {label}: score {shown} ({note}) -> {'accepted' if ok else 'IGNORED'}")
    return ok


def listen_text(timeout=None):
    """One utterance as text. None = timeout, '' = only noise. Other voices are skipped without
    restarting the waiting time."""
    end = None if timeout is None else time.time() + timeout
    while True:
        left = None if end is None else max(0.0, end - time.time())
        audio = ears.next_utterance(left)
        if audio is None:
            return None
        if not voice_ok(audio, "follow-up"):
            if end is not None and time.time() >= end:
                return None
            continue
        set_state("thinking")
        return transcribe(audio)


def confirm_input(timeout=15):
    """Waits for a typed OR spoken answer. Returns (text, was_spoken) or None on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = text_q.get_nowait()
            if not raw.startswith("\x00"):
                return " ".join(re.sub(r"[^a-z0-9' ]", " ", raw.lower()).split()), False
        except queue.Empty:
            pass
        audio = ears.next_utterance(0.2)
        if audio is not None:
            if not voice_ok(audio, "confirm"):       # a stranger cannot answer "yes" for you
                continue
            set_state("thinking")
            return transcribe(audio), True
    return None


# ================= VOICE (Piper, one voice per persona) =================
class PiperTTS:
    def __init__(self, path):
        from piper import PiperVoice
        self.voice = PiperVoice.load(path)
        self.sr = self.voice.config.sample_rate

    def synth(self, text):
        v = self.voice
        if hasattr(v, "synthesize_stream_raw"):            # piper-tts 1.2
            return np.frombuffer(b"".join(v.synthesize_stream_raw(text)), dtype=np.int16)
        parts = []                                           # piper-tts 1.3+
        for ch in v.synthesize(text):
            arr = getattr(ch, "audio_int16_array", None)
            if arr is None:
                arr = np.frombuffer(ch.audio_int16_bytes, dtype=np.int16)
            parts.append(np.asarray(arr, dtype=np.int16).reshape(-1))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


VOICES = {}


def get_voice(key):
    if key not in VOICES:
        p = PERSONAS[key]
        try:
            VOICES[key] = PiperTTS(os.path.join(BASE, "voices", p["voice"]))
        except Exception as e:
            print("Voice not available for", p["name"], e)
            ui.log_line("sys", f"{p['name']}: voice file {p['voice']} not found, using another voice.")
            VOICES[key] = None
    return VOICES[key]


def active_tts():
    return get_voice(cur_key) or (get_voice("jarvis") if cur_key != "jarvis" else None)


def dark_fx(a, sr):
    """Makes a voice deeper, slower, metallic and echoing (used for Silo)."""
    x = a.astype(np.float32) / 32768.0
    n = int(len(x) / 0.84)                                    # lower pitch + slow down ~16%
    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    t = np.arange(len(x)) / sr
    y = 0.8 * x + 0.35 * x * np.sin(2 * np.pi * 55 * t)       # metallic growl (ring modulation)
    y = np.tanh(y * 1.6)                                      # menacing distortion
    for d, g in ((0.11, 0.32), (0.23, 0.18)):                 # cold echo
        k = int(sr * d)
        z = np.zeros(len(y) + k, np.float32)
        z[:len(y)] += y
        z[k:] += y * g
        y = z
    y = y / (np.abs(y).max() + 1e-6) * 0.85
    return (y * 32767).astype(np.int16)


class Speaker:
    """Gapless audio player fed sentence by sentence; can be stopped instantly."""

    def __init__(self, sr, dev=None):
        self.sr, self.out_sr, self.dev = sr, sr, dev
        self.lock = threading.Lock()

    def start(self):
        self.chunks, self.cur, self.pos = deque(), None, 0
        self.input_done = self.stopped = False
        self.level = 0.0
        self.finished = threading.Event()
        rates = [self.sr]
        try:
            rates.append(int(sd.query_devices(self.dev, "output")["default_samplerate"]))
        except Exception:
            pass
        err = None
        for r in rates:                                  # fall back to the device's own sample rate
            try:
                self.out_sr = r
                self.stream = sd.OutputStream(device=self.dev, samplerate=r, channels=1, dtype="int16",
                                              blocksize=1024, callback=self._cb,
                                              finished_callback=self.finished.set)
                self.stream.start()
                return
            except Exception as e:
                err = e
        raise err

    def feed(self, arr):
        if self.out_sr != self.sr and len(arr) > 1:
            n = int(len(arr) * self.out_sr / self.sr)
            arr = np.interp(np.linspace(0, len(arr) - 1, n), np.arange(len(arr)),
                            arr.astype(np.float32)).astype(np.int16)
        with self.lock:
            self.chunks.append(arr)

    def done_feeding(self):
        self.input_done = True

    def stop(self):
        self.stopped = True

    def _cb(self, outdata, frames, t, status):
        if self.stopped:
            outdata[:] = 0
            raise sd.CallbackAbort
        out = np.zeros(frames, np.int16)
        filled = 0
        with self.lock:
            while filled < frames:
                if self.cur is None:
                    if not self.chunks:
                        break
                    self.cur, self.pos = self.chunks.popleft(), 0
                take = min(frames - filled, len(self.cur) - self.pos)
                out[filled:filled + take] = self.cur[self.pos:self.pos + take]
                filled += take
                self.pos += take
                if self.pos >= len(self.cur):
                    self.cur = None
            empty = self.cur is None and not self.chunks
        self.level = float(np.sqrt(np.mean((out.astype(np.float32) / 32768.0) ** 2)))
        gain = SET["volume"] / 100.0
        if gain != 1.0:
            out = np.clip(out.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
        outdata[:, 0] = out
        if empty and self.input_done:
            raise sd.CallbackStop


def clean_for_speech(t):
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"[*_#`>~|]", " ", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def split_sentences(text):
    out = []
    for p in re.split(r"(?<=[.!?])\s+", text):
        p = p.strip()
        while len(p) > 200:
            cut = p.rfind(",", 0, 200)
            if cut < 40:
                cut = p.rfind(" ", 0, 200)
            if cut <= 0:
                cut = 199
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out


def speak_piper(text, t, glitch=0.0):
    sents = split_sentences(text)
    fx = bool(P().get("fx"))
    spk = Speaker(t.sr, resolve_dev(SET["spk"], DEV["outs"]))
    try:
        spk.start()
    except Exception as e:
        ui.log_line("sys", f"Speaker failed ({e}). Using the system default.")
        spk.dev = None
        spk.start()

    def producer():
        try:
            for s in sents:
                if spk.stopped:
                    break
                a = t.synth(s)
                if fx and len(a):
                    a = dark_fx(a, t.sr)
                if glitch > 0 and len(a):                  # Ultron is taking over: the voice breaks up
                    a = glitch_audio(a, t.sr, glitch)
                if len(a):
                    spk.feed(a)
                    spk.feed(np.zeros(int(t.sr * (0.25 if fx else 0.12)), np.int16))
        except Exception:
            traceback.print_exc()
        finally:
            spk.done_feeding()

    threading.Thread(target=producer, daemon=True).start()
    interrupted = False
    while not spk.finished.wait(0.03):
        ui.level = min(1.0, spk.level * 7)
        if ears.interrupt.is_set():
            spk.stop()
            interrupted = True
            break
    try:
        spk.stream.abort()
        spk.stream.close()
    except Exception:
        pass
    return interrupted


def speak_sapi(text):
    """Fallback if no Piper voice is available."""
    for ch in ["'", '"', "`", "$"]:
        text = text.replace(ch, " ")
    vol = min(100, SET["volume"])
    rate = -3 if P().get("fx") else 0
    p = subprocess.Popen(["powershell", "-Command",
                          "Add-Type -AssemblyName System.Speech; "
                          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                          f"$s.Volume = {vol}; $s.Rate = {rate}; $s.Speak('{text}')"])
    while p.poll() is None:
        if ears.interrupt.is_set():
            p.terminate()
            return True
        ui.level = 0.25 + 0.5 * random.random()
        time.sleep(0.05)
    return False


def addr(text):
    """Fills in how the assistants address you: ", Shreyas" once he knows your name, nothing before that."""
    if "[u]" not in text:
        return text
    return text.replace("[u]", f", {profile['name']}" if profile.get("name") else "")


def speak(text, log=True, glitch=0.0, shown=None):
    """Speaks in the current persona's voice. Returns True if the user interrupted.
    glitch 0..1 makes the voice (and optionally the shown text) break up."""
    text = addr(text)
    shown = addr(shown) if shown else shown
    if log:
        emit("ai", shown or text)
    if SET["muted"]:
        return False
    prev = current_state
    set_state("speaking")
    ears.begin_barge()
    interrupted = False
    try:
        clean = clean_for_speech(text)
        if clean:
            t = active_tts()
            interrupted = speak_piper(clean, t, glitch) if t else speak_sapi(clean)
    except Exception:
        traceback.print_exc()
    finally:
        ears.end_barge(keep=(interrupted and headphones()))
    ui.level = 0.0
    if interrupted:
        ui.log_line("sys", "(interrupted)")
        set_state("listening")
    else:
        ears.flush()
        set_state("listening" if prev in ("thinking", "speaking") else prev)
    return interrupted


# ================= ULTRON'S THEME MUSIC (synthesized, no download) =================
_theme = {"data": None}
_theme_lock = threading.Lock()


def make_ultron_theme(sr=22050, dur=11.5):
    n = int(sr * dur)
    t = (np.arange(n) / sr).astype(np.float32)
    rng = np.random.default_rng(7)
    out = np.zeros(n, np.float32)

    def saw(f):
        return sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 7))

    # dark drone: A1 plus a detuned copy and a tritone, slowly pulsing
    drone = saw(55.0) + saw(55.8) + 0.6 * saw(77.8) + 0.8 * np.sin(2 * np.pi * 27.5 * t)
    out += 0.16 * drone * (0.65 + 0.35 * np.sin(2 * np.pi * 0.2 * t)) * np.clip(t / 3.0, 0, 1) ** 2

    # heartbeat that quickens
    beat, gap = 1.8, 1.15
    while beat < dur - 2.0:
        for off, amp in ((0.0, 1.0), (0.2, 0.65)):
            s, L = int((beat + off) * sr), int(0.4 * sr)
            if s + L < n:
                tt = np.arange(L, dtype=np.float32) / sr
                phase = 2 * np.pi * (30 * tt + 70 * (1 - np.exp(-6 * tt)) / 6)
                out[s:s + L] += amp * 0.5 * np.sin(phase) * np.exp(-8 * tt)
        beat += gap
        gap = max(0.6, gap * 0.94)

    # descending, crawling bell motif
    for when, f in ((3.4, 207.65), (4.6, 174.61), (5.8, 164.81), (7.2, 155.56)):
        s = int(when * sr)
        L = min(int(3.0 * sr), n - s)
        tt = np.arange(L, dtype=np.float32) / sr
        bell = (np.sin(2 * np.pi * f * tt) + 0.5 * np.sin(2 * np.pi * f * 2.76 * tt)
                + 0.25 * np.sin(2 * np.pi * f * 5.4 * tt) + 0.9 * np.sin(2 * np.pi * f * 0.5 * tt))
        out[s:s + L] += 0.13 * bell * np.exp(-1.5 * tt)

    # rising wind
    noise = np.convolve(rng.standard_normal(n).astype(np.float32),
                        np.ones(48, np.float32) / 48, mode="same")
    out += 1.6 * noise * (np.clip((t - 4.0) / 5.0, 0, 1) ** 2)

    # final boom
    s = int(9.0 * sr)
    L = n - s
    tt = np.arange(L, dtype=np.float32) / sr
    boom = np.sin(2 * np.pi * (32 * tt + 90 * (1 - np.exp(-4 * tt)) / 4)) * np.exp(-1.1 * tt)
    out[s:] += 1.1 * boom + 1.4 * noise[s:] * np.exp(-3 * tt)

    # cavernous echo, fade out, soft clip
    wet = out.copy()
    for d, g in ((0.31, 0.35), (0.57, 0.22), (0.83, 0.12)):
        k = int(d * sr)
        wet[k:] += g * out[:-k]
    out = wet * np.clip((dur - t) / 1.3, 0, 1)
    out = np.tanh(out * 1.3)
    out = out / (np.abs(out).max() + 1e-6) * 0.8
    return (out * 32767).astype(np.int16), sr


def load_wav(path):
    with wave.open(path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("The WAV file must be 16-bit")
    a = np.frombuffer(raw, np.int16)
    if ch > 1:
        a = a[:len(a) // ch * ch].reshape(-1, ch).mean(axis=1).astype(np.int16)
    return a[:sr * 25], sr          # at most 25 seconds


def get_theme():
    with _theme_lock:
        if _theme["data"] is None:
            try:
                _theme["data"] = load_wav(THEME_WAV) if os.path.exists(THEME_WAV) else make_ultron_theme()
            except Exception as e:
                print("Theme problem, using the built-in one:", e)
                _theme["data"] = make_ultron_theme()
        return _theme["data"]


def play_sound(arr, sr):
    """Plays audio through the chosen speaker. Returns True if interrupted ('stop')."""
    spk = Speaker(sr, resolve_dev(SET["spk"], DEV["outs"]))
    try:
        spk.start()
    except Exception:
        spk.dev = None
        spk.start()
    spk.feed(arr)
    spk.done_feeding()
    interrupted = False
    while not spk.finished.wait(0.03):
        ui.level = min(1.0, spk.level * 5)
        if ears.interrupt.is_set():
            spk.stop()
            interrupted = True
            break
    try:
        spk.stream.abort()
        spk.stream.close()
    except Exception:
        pass
    return interrupted


def play_theme():
    if SET["muted"]:
        return
    set_state("speaking")
    ears.begin_barge()
    try:
        arr, sr = get_theme()
        play_sound(arr, sr)
    except Exception:
        traceback.print_exc()
    finally:
        ears.end_barge()
    ui.level = 0.0


# ================= CONTAINMENT RAYS: SOUND + VOICE EFFECT (synthesized, no download) =================
CONTAIN_VOICE_LINES = ("Containment protocol engaged.", "Locking Silo down.", "Containment field online.")
CONTAIN_VOICE_DELAY = 0.35        # seconds after the rays start before the voice begins


def make_containment_sound(sr, dur=CONTAIN_SECONDS, lock=CONTAIN_LOCK):
    """Rising charge-up whine + electric crackle, then a heavy 'clunk' at the moment the cage seals
    (the same moment as the flash on screen), then a fading hum. Float array in -1..1."""
    n = int(sr * dur)
    t = (np.arange(n) / sr).astype(np.float32)
    rng = np.random.default_rng(11)
    tl = dur * lock
    pre = t < tl
    prog = np.clip(t / tl, 0.0, 1.0)
    freq = 160 + 1700 * prog ** 2                                      # charge-up whine
    ph = 2 * np.pi * np.cumsum(freq) / sr
    out = np.where(pre, (np.sin(ph) + 0.35 * np.sin(2 * ph + 1.0)) * prog ** 1.4 * 0.30, 0.0).astype(np.float32)
    rate = (0.004 + 0.05 * prog ** 2) * pre                            # crackle gets denser while charging
    clicks = (rng.random(n) < rate) * rng.uniform(-1, 1, n)
    out += (np.convolve(clicks, np.hanning(20), "same") * 1.2).astype(np.float32)
    k = np.clip(t - tl, 0.0, None)
    thump = np.sin(2 * np.pi * (45 + 110 * np.exp(-k * 14)) * k) * np.exp(-k * 6) * 0.95
    ping = (np.sin(2 * np.pi * 1180 * k) + 0.5 * np.sin(2 * np.pi * 1770 * k)) * np.exp(-k * 8) * 0.30
    burst = rng.uniform(-1, 1, n) * np.exp(-k * 30) * 0.40
    hum = np.sin(2 * np.pi * 58 * t) * np.exp(-k * 2.2) * 0.16
    out += np.where(t >= tl, thump + ping + burst + hum, 0.0).astype(np.float32)
    out *= np.clip(np.minimum(t / 0.01, (dur - t) / 0.25), 0.0, 1.0)  # no clicks at the edges
    return (out / (np.abs(out).max() + 1e-6) * 0.9).astype(np.float32)


def containment_voice_fx(a, sr):
    """The assistant's voice with an electric buzz and a short metallic echo (float in -1..1)."""
    x = a.astype(np.float32) / 32768.0
    t = np.arange(len(x)) / sr
    y = np.tanh((0.65 * x + 0.35 * x * np.sin(2 * np.pi * 70 * t)) * 1.3)
    for dly, g in ((0.07, 0.35), (0.16, 0.20)):
        k = int(sr * dly)
        z = np.zeros(len(y) + k, np.float32)
        z[:len(y)] += y
        z[k:] += y * g
        y = z
    return (y / (np.abs(y).max() + 1e-6) * 0.85).astype(np.float32)


def containment_sequence(line=None):
    """The containment rays on screen, with a charge-up/lock sound and the active assistant saying a
    line through an electric voice effect. Sound and voice are mixed into ONE clip and the rays start
    the instant it starts playing, so they stay in sync."""
    line = line or random.choice(CONTAIN_VOICE_LINES)
    if SET["muted"]:
        ui.contain_ultron()
        return
    emit("ai", line)
    t = active_tts()
    sr = t.sr if t else 22050
    mixed = make_containment_sound(sr) * 0.8
    voiced = False
    if t:
        try:
            a = t.synth(clean_for_speech(line))
            if len(a):
                v = containment_voice_fx(a, sr)
                off = int(sr * CONTAIN_VOICE_DELAY)
                if off + len(v) > len(mixed):
                    mixed = np.concatenate([mixed, np.zeros(off + len(v) - len(mixed), np.float32)])
                mixed[off:off + len(v)] += v
                voiced = True
        except Exception:
            traceback.print_exc()
    arr = (mixed / max(1.0, float(np.abs(mixed).max()) / 0.95) * 32767).astype(np.int16)
    set_state("speaking")
    ears.begin_barge()
    try:
        ui.contain_ultron()                          # rays start exactly when the audio starts
        play_sound(arr, sr)
    except Exception:
        traceback.print_exc()
    finally:
        ears.end_barge()
    ui.level = 0.0
    set_state("listening")
    if not voiced:                                   # no Piper voice: say it plainly afterwards
        speak(line, log=False)


# ================= SLEEP MODE (frees CPU + RAM, cools the laptop) =================
def set_eco(on):
    """Lower priority + Windows 'efficiency mode' while asleep; back to normal when awake."""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        h = k.GetCurrentProcess()
        k.SetPriorityClass(h, 0x00004000 if on else 0x00000020)    # BELOW_NORMAL / NORMAL

        class _Throttle(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]

        st = _Throttle(1, 1, 1 if on else 0)                      # EcoQoS on/off
        k.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))
    except Exception as e:
        print("Could not change process priority:", e)


def unload_llm():
    """Tells Ollama to drop the model from RAM right now."""
    try:
        requests.post(f"{URL}/api/generate", json={"model": SET["model"], "keep_alive": 0}, timeout=30)
    except Exception:
        pass


def _free_llm(naming_thread):
    if naming_thread:
        naming_thread.join(timeout=300)       # let the conversation title finish first
    if S["asleep"]:
        unload_llm()


def go_to_sleep(announce=True):
    """Call while holding `busy`."""
    global whisper
    if S["asleep"]:
        return
    if announce:
        speak(SLEEP_LINES.get(cur_key, ("Going to sleep.", ""))[0])
    S["asleep"] = True
    ui.set_sleep(True)
    ui.log_line("sys", 'Sleep mode: models unloaded, animation slowed. '
                       'Say "wake up" or an assistant\'s name (or press WAKE UP).')
    t = finalize_active()
    active["conv"], active["pinned"] = None, False
    push_convs()
    whisper = None                            # frees the speech-recognition model
    VOICES.clear()                            # frees the loaded voices
    with _theme_lock:
        _theme["data"] = None
    gc.collect()
    set_eco(True)
    save_live_state()
    threading.Thread(target=_free_llm, args=(t,), daemon=True).start()
    set_state("sleep")


def woke_line():
    return SLEEP_LINES.get(cur_key, ("", f"{P()['name']} is back online."))[1]


def wake_up(announce=True):
    """Call while holding `busy`. Reloads what sleep mode unloaded."""
    if not S["asleep"]:
        return
    S["asleep"] = False
    set_eco(False)
    ui.set_sleep(False)
    set_state("thinking")
    ui.log_line("sys", "Waking up, reloading speech models...")
    load_whisper()
    get_voice(cur_key)
    threading.Thread(target=preload, daemon=True).start()     # reload the AI model in the background
    S["last"] = time.time()
    if announce:
        speak(woke_line())
    set_state("listening" if S["awake"] else "sleep")


def auto_sleep_watchdog():
    while True:
        time.sleep(30)
        if (AUTO_SLEEP_MINUTES and not S["asleep"] and not S["awake"] and current_state == "sleep"
                and time.time() - S["last"] > AUTO_SLEEP_MINUTES * 60
                and busy.acquire(blocking=False)):
            try:
                go_to_sleep(announce=False)
            except Exception:
                traceback.print_exc()
            finally:
                busy.release()


# ================= PERSONA SWITCHING =================
POLITE = {"please", "hey", "ok", "okay", "can", "you", "could", "would", "now"}
CALL_RE = re.compile(r"^(?:call|get|bring up|bring in|switch to|put me through to|connect me to|"
                     r"connect me with|let me talk to|let me speak to|i want to talk to|"
                     r"i want to speak to|talk to|speak to|hand over to|patch in|summon|change to|go to)"
                     r"\s+([a-z]+)$")


def apply_persona_ui():
    ui.set_persona(cur_key)                          # the reactors glide to their new places


def fingerprint_gate():
    """Asks for the fingerprint. True = verified, False = refused, 'fallback' = no scanner and fallback allowed."""
    set_state("listening")
    ui.log_line("sys", "Waiting for your fingerprint (Windows Hello). Touch the scanner when the prompt appears.")
    ok, status = windows_hello_verify("Authorize releasing Silo", FINGERPRINT_TIMEOUT)
    if ok:
        ui.log_line("sys", "Fingerprint verified.")
        return True
    name = status.split(":", 1)[-1] if status.startswith("UNAVAILABLE:") else status
    why = HELLO_WHY.get(name, "The fingerprint check failed." if not status.startswith("ERROR") else
                        "Windows Hello gave an error.")
    ui.log_line("sys", f"Fingerprint not verified: {status}")
    if status.startswith("UNAVAILABLE:") and FINGERPRINT_FALLBACK:
        speak(why + " I will accept a spoken yes instead.")
        return "fallback"
    speak(why + " Silo stays locked.")
    return False


def ultron_gate():
    """The current persona warns you twice. True only if you confirm both times.
    With REQUIRE_FINGERPRINT the second confirmation is a Windows Hello fingerprint check."""
    lines = ULTRON_WARN.get(cur_key) or ULTRON_WARN["default"]
    if REQUIRE_FINGERPRINT:
        lines = (lines[0], ULTRON_FP.get(cur_key) or ULTRON_FP["default"])
    ui.log_line("sys", "WARNING: Silo is hostile. You will be asked twice before he is brought online.")
    ui.set_alert(True)                               # his cage flashes red
    try:
        for i, line in enumerate(lines):
            if speak(line):                          # interrupted with "stop" = cancelled
                return False
            use_yes = True
            if REQUIRE_FINGERPRINT and i == 1:
                res = fingerprint_gate()
                if res is True:
                    continue
                if res is False:
                    return False
                # res == "fallback": no scanner, so ask for a spoken yes instead
            if use_yes:
                set_state("listening")
                res = confirm_input(15)
                ans = ""
                if res:
                    ans, spoken = res
                    if spoken and ans:
                        emit("you", ans)
                if ans.strip() not in YES_WORDS:
                    speak(ULTRON_DECLINE)
                    return False
        return True
    finally:
        ui.set_alert(False)


def ultron_takeover(old_key):
    """The assistant fights it, but Silo slowly takes over: visuals and voice both glitch."""
    try:
        speak(ULTRON_RELEASE)
        lines = TAKEOVER.get(old_key, TAKEOVER["jarvis"])
        for line, lvl in zip(lines, (0.3, 0.62, 0.95)):
            line = addr(line)                        # before the glitch effect scrambles the letters
            ui.set_glitch(lvl)
            speak(line, glitch=lvl, shown=corrupt_text(line, lvl))
            if SET["muted"]:
                time.sleep(1.8)                      # keep the drama timing even when muted
        ui.set_glitch(1.0)
        time.sleep(0.8)
    except Exception:
        traceback.print_exc()


def switch_persona(key, quiet=False):
    global cur_key, before_ultron
    old, new = P(), PERSONAS[key]
    old_key = cur_key
    if key == "ultron":
        before_ultron = old_key
    if quiet:
        cur_key = key
        apply_persona_ui()
        get_voice(key)
        save_live_state()
        return
    loader = threading.Thread(target=get_voice, args=(key,), daemon=True)
    loader.start()                                   # load the new voice while the old one talks
    if key == "ultron":
        ultron_takeover(old_key)
    else:
        speak(old["handoff"].format(new=new["name"]))
    loader.join()
    cur_key = key
    apply_persona_ui()                               # reactors swap places smoothly
    ui.set_glitch(0.0)                               # the corruption fades out
    save_live_state()
    if key == "ultron":
        play_theme()                                 # the evil music
        speak(new["arrive"].format(prev=old["name"]))
    elif old_key == "ultron":
        containment_sequence()                       # rays + charge/lock sound + electric voice effect
        speak(f"{new['name']} back online. Ultron has been contained. For now.")
    else:
        speak(new["arrive"].format(prev=old["name"]))


_ULTRON_WORDS = "|".join(re.escape(w) for w in list(PERSONAS["ultron"]["wake"]) + [PERSONAS["ultron"]["name"].lower()])
CONTAIN_RE = re.compile(rf"^(?:(?:contain|lock down|restrain|seal|cage|secure) (?:{_ULTRON_WORDS})|"
                        rf"(?:engage|activate|start|initiate|run) (?:the )?containment(?: protocol)?|"
                        rf"containment protocol)$")


def handle_persona(text):
    s = " ".join(w for w in text.split() if w not in POLITE)
    if CONTAIN_RE.match(re.sub(r"[^\w\s]", "", s.lower()).strip()):      # "contain Ultron", "containment protocol"
        if cur_key == "ultron":                      # he is out: put him back and hand control back
            switch_persona(before_ultron if before_ultron in PERSONAS and before_ultron != "ultron"
                           else DEFAULT_PERSONA)
        else:                                        # he is already caged: show the lock-down anyway
            containment_sequence("Containment protocol engaged. Silo is secure.")
        return True
    m = CALL_RE.match(s)
    name = m.group(1) if m else (s if s in PERSONA_WORDS else None)
    key = PERSONA_WORDS.get(name) if name else None
    if not key:
        return False
    if key == cur_key:
        speak(f"{P()['name']} here. What do you need?")
    elif key == "ultron":
        if ultron_gate():
            switch_persona(key)
    else:
        switch_persona(key)
    return True


def strip_wake(text, key=None):
    for k in ([key] if key else list(PERSONAS)):
        for w in PERSONAS[k]["wake"]:
            pat = rf"\b{re.escape(w)}\b"
            if re.search(pat, text):
                return True, " ".join(re.sub(pat, " ", text, count=1).split())
    return False, text


# ================= BROWSER (Opera GX) =================
def find_opera():
    if OPERA_PATH and os.path.exists(OPERA_PATH):
        return OPERA_PATH
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    pf86 = os.environ.get("ProgramFiles(x86)", "")
    for p in (os.path.join(local, "Programs", "Opera GX", "opera.exe"),
              os.path.join(local, "Programs", "Opera GX", "launcher.exe"),
              os.path.join(pf, "Opera GX", "opera.exe"),
              os.path.join(pf86, "Opera GX", "opera.exe")):
        if os.path.exists(p):
            return p
    return None


OPERA = find_opera()
print("Browser:", OPERA or "Opera GX not found, using the default browser")


def open_url(url):
    if OPERA:
        subprocess.Popen([OPERA, url])
    else:
        webbrowser.open(url)


# ================= SYSTEM INFO + VOICE SETTINGS =================
def system_report():
    get_stats()
    time.sleep(0.5)
    s = get_stats()
    parts = []
    if s["cpu"] is not None:
        parts.append(f"CPU is at {s['cpu']:.0f} percent")
    if s["ram"] is not None:
        parts.append(f"memory is at {s['ram']:.0f} percent")
    txt = " and ".join(parts) + "." if parts else "I can't read the system stats."
    if s["bat"] is not None:
        txt += f" Battery is at {s['bat']:.0f} percent" + (", charging." if s["chg"] else ".")
    return txt


NUM_WORDS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
             "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "half": 50, "full": 100}
MUTE_CMDS = ("mute", "mute voice", "mute yourself", "mute echo", "go silent", "silent mode",
             "be silent", "stop talking")
UNMUTE_CMDS = ("unmute", "un mute", "on mute", "unmute voice", "unmute yourself", "speak again",
               "you can talk", "voice on", "talk to me")
VOL_UP = ("volume up", "louder", "speak louder", "turn it up", "increase volume", "raise volume")
VOL_DOWN = ("volume down", "quieter", "speak quieter", "turn it down", "lower volume",
            "decrease volume", "lower the volume")


def parse_number(s):
    m = re.search(r"\d+", s)
    if m:
        return int(m.group())
    for w, n in NUM_WORDS.items():
        if w in s.split():
            return n
    return None


MIC_OFF_CMDS = ("mute my mic", "mute the mic", "mute mic", "mute my microphone", "mute the microphone",
                "mic off", "turn off my mic", "turn off the mic", "turn off my microphone",
                "stop listening", "stop listening to me")
MIC_ON_CMDS = ("unmute my mic", "unmute the mic", "unmute mic", "unmute my microphone", "mic on",
               "turn on my mic", "turn on the mic", "start listening", "listen to me")


def handle_system_volume(c):
    """'crank up the vol', 'lower the volume', 'set the volume to 40', 'max volume' change the WINDOWS
    volume (speakers or earbuds). Phrases about HIS voice ('speak louder', 'voice volume 80',
    'your volume down') change how loud Echo talks instead."""
    cmd = parse_volume_command(c)
    if not cmd:
        return False
    op, amount = cmd["op"], cmd["amount"]
    if cmd["target"] == "voice":
        set_voice(volume=amount if op == "set" else SET["volume"] + (15 if op == "up" else -15))
        speak(f"Voice volume {SET['volume']} percent.")
        return True
    if not system_volume.available:
        speak("I can only change the computer's volume on Windows.")
        return True
    if op == "mute":
        system_volume.toggle_mute()
        speak("Toggled the computer's mute.")
    elif op == "set":
        system_volume.set(amount)
        speak("Maximum volume." if amount >= 100 else "Volume off." if amount <= 0
              else f"Volume at {amount} percent.")
    elif op == "up":
        system_volume.change(amount)
        speak("Cranking it up." if cmd["big"] else "Turning it up.")
    else:
        system_volume.change(-amount)
        speak("Bringing it down." if cmd["big"] else "Turning it down.")
    return True


def handle_calendar(c):
    """Questions about your Google Calendar: 'what's on my calendar tomorrow', 'what's my next meeting',
    'am I free on friday at 3', 'refresh calendar'. Read-only. Returns False if it is not a calendar question."""
    if calendar_agent is None:
        return False
    try:
        res = calendar_agent.answer(c)
    except Exception:
        traceback.print_exc()
        return False
    if res is None:
        return False
    speech, detail = res
    if detail:
        ui.log_line("sys", detail)                 # the full list stays on screen; speech is the short version
    speak(speech)
    return True


def _say_first(text, persona=None):
    """Echo starts the conversation. Awake and idle: he says it (and it shows in the transcript). Asleep or
    muted: it still arrives as a written message in the transcript. Returns False if he is in the middle of
    something, so the caller tries again a little later."""
    if current_state not in ("listening", "sleep"):
        return False
    if S["asleep"] or SET["muted"]:
        with as_persona(persona):
            emit("ai", text)                       # text only: it shows up (typed out) in the transcript
        return True
    if not busy.acquire(blocking=False):           # never wait for / hold up a command in progress
        return False
    try:
        with as_persona(persona):
            speak(text)
    finally:
        busy.release()
    return True


def calendar_remind(title, minutes, occ):
    """Called by the calendar thread shortly before an event. Returns True once it has been delivered."""
    return _say_first(f"Heads up. {title} starts in {minutes} minute{'s' if minutes != 1 else ''}." if minutes > 0
                      else f"Heads up. {title} is starting now.")


def calendar_notify(kind, body):
    """The morning briefing and the evening preview: Echo messages you first."""
    name = profile.get("name")
    hour = time.localtime().tm_hour
    if kind == "briefing":
        greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        return _say_first(f"{greet}{', ' + name if name else ''}. {body}")
    return _say_first(f"Quick heads-up for tomorrow{', ' + name if name else ''}. {body}")


def clock_phrase(now=None):
    """'Thursday, 22 October 2026, 7:41 PM' from the computer's clock."""
    now = now or time.localtime()
    return f"{day_phrase(now)}, {time_phrase(now)}"


def time_phrase(now=None):
    now = now or time.localtime()
    h, m = now.tm_hour % 12 or 12, now.tm_min
    return f"{h} {'AM' if now.tm_hour < 12 else 'PM'}" if m == 0 else f"{h}:{m:02d} {'AM' if now.tm_hour < 12 else 'PM'}"


def day_phrase(now=None):
    now = now or time.localtime()
    return f"{time.strftime('%A', now)}, {now.tm_mday} {time.strftime('%B %Y', now)}"


_CLOCK_FILLER = {"what", "whats", "is", "was", "the", "current", "right", "now", "it", "tell", "me", "give", "please",
                 "do", "you", "know", "got", "have", "a", "can", "could", "today", "todays", "currently", "exact",
                 "local", "my", "time", "date", "day", "year", "month", "hey", "ok", "so", "and", "of", "for", "today",
                 "how", "late", "its", "s", "show", "say", "check", "see"}


def time_date_answer(c, now=None):
    """The spoken answer to a plain time / date / day / year question, or None if the sentence is
    about something else ('what time is the meeting', 'what time is it in Tokyo' go elsewhere)."""
    toks = re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split()
    if len(toks) < 2 or toks == ["what", "is", "time"]:
        return None
    if [w for w in toks if w not in _CLOCK_FILLER]:
        return None                                  # something else is being asked about
    now = now or time.localtime()
    want_time = "time" in toks or toks[:2] == ["how", "late"]
    want_date = "date" in toks
    want_day, want_year, want_month = "day" in toks, "year" in toks, "month" in toks
    if want_time and (want_date or want_day):
        return f"It's {time_phrase(now)} on {day_phrase(now)}."
    if want_time:
        return f"It's {time_phrase(now)}."
    if want_date or want_day:
        return f"Today is {day_phrase(now)}."
    if want_year:
        return f"It's {now.tm_year}."
    if want_month:
        return f"It's {time.strftime('%B', now)}."
    return None


def handle_time_date(c):
    answer = time_date_answer(c)
    if answer is None:
        return False
    speak(answer)
    return True


LAST_CODE = []                                        # code blocks from the most recent answer (Nova)
DOCS = jobs.DocIndex(os.path.join(BASE, "doc_index"))  # Atlas: your documents, searchable offline
AEGIS = {"state": {}, "pending": [], "errors": jobs.ErrorLog(os.path.join(BASE, "errors.log"))}
JOURNAL = os.path.join(BASE, "organize_journal.json")
SCRIPTS_DIR = os.path.join(BASE, "scripts")
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "Jarvis Backups")
OFFLINE = "I can't reach the internet right now."


def llm_once(prompt, tokens=300):
    """One question to the local model that is NOT added to your conversation."""
    r = requests.post(f"{URL}/api/chat", json={"model": SET["model"], "stream": False,
                      "messages": [{"role": "user", "content": prompt}],
                      "options": {"num_ctx": 4096, "num_predict": tokens, "temperature": 0.3}}, timeout=240)
    return (r.json().get("message", {}).get("content") or "").strip()


def _find_file(name):
    for p in S.get("last_file_matches") or []:
        if name in os.path.basename(p).lower():
            return p
    found = file_agent.search_files(name)
    return found[0] if found else None


def _yes(prompt, seconds=15):
    speak(prompt)
    set_state("listening")
    res = confirm_input(seconds)
    return bool(res) and re.sub(r"[^a-z' ]", "", res[0].lower()).replace("'", "").strip() in YES_REMEMBER


def aegis_status():
    stats = get_stats()
    ok, models = jobs.ollama_models(URL)
    mdir = jobs.find_model_dir()
    speak(jobs.status_report(stats, ok, models, mdir, jobs.dir_size(mdir) if mdir else 0, jobs.top_processes()))


def aegis_loop():
    """Aegis's watch: every 30 s looks at the system and the program's own errors, and speaks up (once) about problems."""
    time.sleep(45)                                     # let everything finish starting first
    while True:
        try:
            aegis_tick()
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def aegis_tick():
    """One look at the system. Warnings that could not be delivered (he was busy) are kept for the next look."""
    if not SET.get("aegis_alerts", True):
        return
    ok, _ = jobs.ollama_models(URL)
    mdir = jobs.find_model_dir()
    for _key, msg in jobs.check_alerts(AEGIS["state"], get_stats(), ok, bool(mdir)):
        AEGIS["pending"].append("Aegis alert. " + msg)
    err = AEGIS["errors"].alert()
    if err:
        AEGIS["pending"].append("Aegis alert. " + err)
    while AEGIS["pending"] and _say_first(AEGIS["pending"][0], persona="edith"):
        AEGIS["pending"].pop(0)
    del AEGIS["pending"][:-5]


def install_error_watch():
    """Program errors in any thread / the window are written to errors.log and reported by Aegis."""
    log = AEGIS["errors"]
    old_thread, old_sys = threading.excepthook, sys.excepthook

    def handle_exception(where, t, val, tb):
        log.record(where, t, val)
        
        # --- SELF-HEALING CRASH HOOK ---
        try:
            tb_text = "".join(traceback.format_exception(t, val, tb))
            target_file = os.path.join(BASE, "voice.py")
            ui.log_line("sys", f"[CRASH DETECTED in {where}] {val}. Analyzing patch...")
            
            def run_diagnostics():
                ok, result = healer.generate_patch(str(val), tb_text, target_file)
                if ok:
                    ui.log_line("sys", f"[SELF-HEAL] Proposed patch staged at: {os.path.basename(result)}")
                    # Safely push notification to transcript/UI log instead of forcing active speech during crash state
                    ui.log_line("ai", "Sir, an error occurred in my routines. I've analyzed the cause and generated a patch. Say 'apply patch' to update the code.")
                else:
                    ui.log_line("sys", f"[SELF-HEAL FAILED] {result}")
            
            threading.Thread(target=run_diagnostics, daemon=True).start()
        except Exception as e:
            print(f"Self-heal hook failed: {e}")

    def thread_hook(args):
        name = args.thread.name if args.thread else "background"
        handle_exception(f"the {name} thread", args.exc_type, args.exc_value, args.exc_traceback)
        old_thread(args)

    def sys_hook(t, val, tb):
        handle_exception("the main program", t, val, tb)
        old_sys(t, val, tb)

    threading.excepthook, sys.excepthook = thread_hook, sys_hook
    try:
        ui.root.report_callback_exception = lambda t, val, tb: (
            handle_exception("the window", t, val, tb),
            traceback.print_exception(t, val, tb)
        )
    except Exception:
        pass

def run_job(kind, a):
    if kind == "status":
        aegis_status()
    elif kind == "errors":
        rows = AEGIS["errors"].recent(3)
        if not rows:
            speak("No program errors recorded. Clean so far.")
        else:
            ui.log_line("sys", "\n".join(f"{r['t']}  {r['where']}: {r['type']}: {r['msg']}" for r in rows))
            speak("Latest errors. " + " ".join(f"{r['where']}: {r['type']}, {r['msg'][:70]}." for r in rows))
    elif kind == "alerts":
        SET["aegis_alerts"] = a["on"]
        save_settings()
        speak("System alerts on. I'll warn you about problems." if a["on"] else "System alerts off. I'll stay quiet until you ask.")
    elif kind == "storage":
        speak(jobs.storage_report(None, jobs.find_model_dir(), BASE))
    elif kind == "index":
        index_documents(a["what"])
    elif kind == "forget_index":
        DOCS.clear()
        DOCS.save()
        speak("Done. I've forgotten the indexed documents.")
    elif kind == "doc_search":
        search_documents(a["q"])
    elif kind == "long_summary":
        long_summary_job(a["name"])
    elif kind == "save_code":
        if not LAST_CODE:
            speak("I don't have any code to save yet. Ask Nova to write some first.")
        else:
            paths = [jobs.save_code(code, lang, SCRIPTS_DIR, a["name"] if len(LAST_CODE) == 1 else None) for lang, code in LAST_CODE]
            ui.log_line("sys", "Saved:\n" + "\n".join(paths))
            speak(f"Saved {os.path.basename(paths[0])} in the scripts folder." if len(paths) == 1
                  else f"Saved {len(paths)} files in the scripts folder.")
    elif kind == "organize":
        organize_job(a["folder"])
    elif kind == "undo_organize":
        ok, bad = jobs.undo_last(JOURNAL)
        speak("There is nothing to undo." if not (ok or bad) else
              f"Put {ok} files back." + (f" {bad} could not be restored, they were moved or renamed." if bad else ""))
    elif kind == "backup":
        backup_job(a["folder"])
    elif kind == "venvs":
        home = os.path.expanduser("~")
        found = jobs.find_venvs(os.path.join(home, "PycharmProjects")) or jobs.find_venvs(os.path.dirname(BASE))
        if not found:
            speak("I don't see any virtual environments in your projects.")
        else:
            ui.log_line("sys", "\n".join(f"{jobs.human_bytes(sz)}  {p}" for p, sz in found))
            speak(f"I found {len(found)} virtual environment{'s' if len(found) != 1 else ''}. The biggest is "
                  f"{os.path.basename(os.path.dirname(max(found, key=lambda x: x[1])[0]))} at "
                  f"{jobs.human_bytes(max(sz for _, sz in found))}. The full list is on screen.")
    elif kind == "open_site":
        site = a["site"]
        if site in SITES:
            open_url(SITES[site])
            speak(f"Opening {site}.")
        else:
            near = difflib.get_close_matches(site, list(SITES), n=1, cutoff=0.8)
            if near:
                open_url(SITES[near[0]])
                speak(f"Opening {near[0]}.")
            elif "." in site and " " not in site:
                url = "https://" + site if not site.startswith("http") else site
                open_url(url)
                speak(f"Opening {site}.")
            else:
                speak(f"I don't have a shortcut for {site}, and it doesn't look like a web address.")
    elif kind in ("lookup", "news", "page"):
        web_job(kind, a)


def index_documents(what):
    if what == "documents":
        folder = jobs.resolve_folder("documents")
        paths = []
        for root, dirs, files in os.walk(folder or ""):
            dirs[:] = [d for d in dirs if not d.startswith(".")][:20]
            paths += [os.path.join(root, f) for f in files if f.lower().endswith((".pdf", ".txt", ".md", ".docx"))]
            if len(paths) >= 25 or root.count(os.sep) - folder.count(os.sep) >= 2:
                break
        paths = paths[:25]
        if not folder or not paths:
            return speak("I can't find any documents to index in your Documents folder.")
    else:
        p = _find_file(what)
        if not p:
            return speak(f"I couldn't find a file called {what}.")
        paths = [p]
    if embed("test") is None:
        return speak("I need the embedding model for that. In a command prompt run: ollama pull nomic-embed-text.")
    speak(f"Indexing {len(paths)} file{'s' if len(paths) != 1 else ''}. This can take a few minutes.")
    chunks_total = done = 0
    for p in paths:
        text = file_agent.read_full(p)
        if not text:
            continue
        pieces = jobs.chunk_text(text)[:120]
        vecs = [embed(t) for t in pieces]
        if all(v is not None for v in vecs) and pieces:
            chunks_total += DOCS.add(os.path.basename(p), pieces, vecs)
            done += 1
    DOCS.save()
    speak(f"Done. {done} file{'s' if done != 1 else ''} indexed, {chunks_total} passages. Ask me what your documents say about something.")


def search_documents(q):
    if not DOCS.sources():
        return speak("I haven't indexed any documents yet. Say 'index my documents' first.")
    vec = embed(q)
    if vec is None:
        return speak("I need the embedding model for that. In a command prompt run: ollama pull nomic-embed-text.")
    hits = [h for h in DOCS.search(vec, 3) if h[0] >= 0.3]
    if not hits:
        return speak("I didn't find anything about that in your indexed documents.")
    context = "\n\n".join(f"[{src}] {text}" for _, src, text in hits)
    answer = llm_once("Answer the question using ONLY these excerpts from the user's documents. If they do not contain the "
                      "answer, say so. Reply in 2 to 4 short spoken sentences, no lists.\n\nExcerpts:\n" + context + f"\n\nQuestion: {q}")
    speak(f"{answer} That's from {', '.join(dict.fromkeys(src for _, src, _ in hits))}.")


def long_summary_job(name):
    p = _find_file(name)
    text = file_agent.read_full(p) if p else None
    if not text:
        return speak(f"I couldn't read a file called {name}.")
    parts = min(len(jobs.chunk_text(text, 3500, 0)), 14)
    if parts > 1:
        speak(f"That one is long. I'm working through {parts} parts, it will take a moment.")
    speak(jobs.long_summary(text, llm_once))


def organize_job(folder_name):
    folder = jobs.resolve_folder(folder_name)
    if not folder:
        return speak(f"I can't find a folder called {folder_name}.")
    plan = jobs.plan_organize(folder)
    if not plan:
        return speak(f"There's nothing loose to organize in {os.path.basename(folder)}.")
    if _yes(f"I can sort {len(plan)} files in {os.path.basename(folder)}: {jobs.summarize_plan(plan)}. "
            "Nothing is deleted, and I can undo it. Shall I go ahead?"):
        n = jobs.apply_plan(plan, JOURNAL)
        speak(f"Done. {n} files sorted. Say 'undo the last organize' to put them back.")
    else:
        speak("Okay. I left everything where it was.")


def backup_job(folder_name):
    folder = jobs.resolve_folder(folder_name)
    if not folder:
        return speak(f"I can't find a folder called {folder_name}.")
    size = jobs.dir_size(folder)
    if size > 3 * 1024 ** 3 and not _yes(f"{os.path.basename(folder)} is {jobs.human_bytes(size)}. That will take a while. Continue?"):
        return speak("Okay. No backup.")
    speak(f"Backing up {os.path.basename(folder)}.")
    path, n, zsize = jobs.backup_folder(folder, BACKUP_DIR)
    ui.log_line("sys", f"Backup saved: {path}")
    speak(f"Done. {n} files, {jobs.human_bytes(zsize)}, saved in Jarvis Backups in your home folder.")


def web_job(kind, a):
    try:
        if kind == "lookup":
            res = jobs.wiki_summary(a["q"])
            if not res:
                return speak(f"I found nothing on Wikipedia about {a['q']}.")
            title, text, url = res
            ui.log_line("sys", f"{title}\n{text}\n{url}")
            speak(f"{title}. " + " ".join(re.split(r"(?<=[.!?])\s+", text)[:3]))
        elif kind == "news":
            heads = jobs.news_headlines(a["q"])
            if not heads:
                return speak(f"I found no recent news about {a['q']}.")
            ui.log_line("sys", "\n".join(f"{t}  ({s})" for t, s in heads))
            speak(f"Latest on {a['q']}. " + " ".join(f"{t}." for t, _ in heads[:4]))
        else:
            title, text = jobs.html_to_text(jobs.fetch(a["url"]))
            if len(text) < 80:
                return speak("That page has almost no readable text.")
            speak("Reading it.")
            speak((title + ". " if title else "") + llm_once("Summarize this web page in 3 or 4 short spoken sentences, "
                                                              "no lists:\n\n" + text[:6000]))
    except requests.exceptions.RequestException:
        speak(OFFLINE)
    except ValueError as e:
        speak(f"I can't open that: {e}.")


def handle_jobs(c):
    """The assistants' jobs (see jobs.py): system watch, storage and documents, code, files, the web."""
    job = jobs.match_job(c, raw=S.get("raw_text"))
    if not job:
        return False
    kind, a = job
    try:
        with as_persona(jobs.OWNER[kind]):
            run_job(kind, a)
    except Exception:
        traceback.print_exc()
        speak("Something went wrong with that job. The details are in the console.")
    return True


def handle_voice_settings(c):
    if c in MIC_OFF_CMDS:
        speak("Okay, I've stopped listening. Press the MIC button or Control Alt M when you want me to hear you again.")
        set_mic_muted(True)
        return True
    if c in MIC_ON_CMDS:
        set_mic_muted(False)
        speak("I'm listening again.")
        return True
    if c in MUTE_CMDS:
        speak("Going silent. Say unmute when you want me back.")
        set_voice(muted=True)
        ui.log_line("sys", "Voice muted.")
        return True
    if c in UNMUTE_CMDS:
        set_voice(muted=False)
        speak("I'm back.")
        return True
    if c in VOL_UP or c in VOL_DOWN:
        set_voice(volume=SET["volume"] + (15 if c in VOL_UP else -15))
        speak(f"Volume {SET['volume']} percent.")
        return True
    m = re.match(r"^(?:set )?(?:the )?(?:voice )?volume (?:to |at )?(.+)$", c)
    if m:
        n = parse_number(m.group(1))
        if n is not None:
            set_voice(volume=n)
            speak(f"Volume {SET['volume']} percent.")
            return True
    return False


# ================= AUTOMATION =================
_start_apps, _start_tries = None, 0


def get_start_apps():
    global _start_apps, _start_tries
    if not _start_apps and _start_tries < 3:
        _start_tries += 1
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40).stdout
            data = json.loads(out)
            _start_apps = [data] if isinstance(data, dict) else data
        except Exception as e:
            print("Start menu scan failed:", e)
            _start_apps = []
    return _start_apps or []


def resolve_app(name):
    cands = {k: ("alias", v) for k, v in APP_ALIASES.items()}
    for a in get_start_apps():
        cands.setdefault(a["Name"].lower(), ("start", a["AppID"]))
    if name in cands:
        return (name,) + cands[name]
    words = name.split()
    hits = [k for k in cands if all(w in k for w in words)]
    if hits:
        k = min(hits, key=len)
        return (k,) + cands[k]
    compact = {k.replace(" ", ""): k for k in cands}
    m = difflib.get_close_matches(name.replace(" ", ""), list(compact), n=1, cutoff=0.7)
    if m:
        k = compact[m[0]]
        return (k,) + cands[k]
    return None


def launch(kind, value):
    if kind == "alias":
        try:
            os.startfile(value)
        except OSError:
            subprocess.Popen(["cmd", "/c", "start", "", value])
    else:
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + value])


def open_app(name, strict=False):
    name = name.strip()
    if not name:
        return False
    found = resolve_app(name)
    if found:
        display, kind, value = found
        launch(kind, value)
        speak(f"Opening {display}.")
        return True
    if strict or len(name.split()) > 3:
        return False
    speak(f"I could not find an app called {name}.")
    return True


def do_close(name, proc):
    """Graceful close only: same as clicking X, so apps ask to save unsaved work."""
    ps = ("$protected = @(" + ",".join(f"'{p}'" for p in PROTECTED) + "); "
          "$n = '" + proc + "'; $c = 0; "
          "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and "
          "$_.ProcessName -like ('*' + $n + '*') -and "
          "($protected -notcontains $_.ProcessName) } | "
          "ForEach-Object { if ($_.CloseMainWindow()) { $c++ } }; $c")
    try:
        out = subprocess.run(["powershell", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        count = int(out) if out.isdigit() else 0
    except Exception:
        count = 0
    if count:
        speak(f"Closing {name}. If anything is unsaved, it will ask you to save.")
    else:
        speak(f"I could not find an open window for {name}.")


def close_app(name):
    name = re.sub(r"[^a-z0-9 ]", "", name.strip().lower())
    if name.startswith("the "):
        name = name[4:]
    if not name:
        return False
    proc = CLOSE_ALIASES.get(name, name.replace(" ", ""))
    if proc.lower() in [p.lower() for p in PROTECTED]:
        speak(f"I am not allowed to close {name}.")
        return True
    if S["typed"]:
        S["pending"] = (name, proc)
        speak(f"Close {name}? Type yes to confirm.")
        return True
    speak(f"Close {name}? Say yes to confirm.")
    set_state("listening")
    answer = listen_text(8)
    print("Confirm:", answer)
    if answer:
        emit("you", answer)
    if not answer or answer.strip() not in YES_WORDS:
        speak("Okay, cancelled.")
        return True
    do_close(name, proc)
    return True


def web_search(query, youtube=False):
    open_url((YOUTUBE_SEARCH_URL if youtube else SEARCH_URL) + quote_plus(query))
    speak(f"Searching {'YouTube' if youtube else 'the web'} for {query}.")


LEAD_WORDS = {"hey", "ok", "okay", "please", "can", "could", "would", "will", "you", "just", "um",
              "uh", "so", "and", "now", "then", "also", "yo", "hi", "hello"} | set(PERSONA_WORDS)
VERB_FIX = {"opens": "open", "opened": "open", "opening": "open", "launching": "launch",
            "closing": "close", "searching": "search", "starting": "start"}
VERBS = ("open", "launch", "start", "close", "quit", "search", "google", "look", "play")


def clean_command(c):
    words = c.split()
    while words and words[0] in LEAD_WORDS:
        words.pop(0)
    if (len(words) > 2 and words[0] not in VERBS and
            VERB_FIX.get(words[1], words[1]) in ("open", "launch", "close", "search")):
        words.pop(0)                                     # one stray / misheard word
    if words:
        words[0] = VERB_FIX.get(words[0], words[0])
    return " ".join(words)


def handle_action(c):
    if c in ("apply patch", "apply the patch", "apply fix", "yes apply patch", "apply code fix"):
        if healer.pending_patch:
            success, msg = healer.apply_pending_patch()
            speak(msg if success else f"Failed to apply patch: {msg}")
        else:
            speak("There are no pending patches to apply.")
        return True

    if c in ("discard patch", "reject patch", "cancel patch"):
        if healer.pending_patch:
            healer.discard_pending_patch()
            speak("Patch discarded.")
        else:
            speak("No pending patch to discard.")
        return True
    if c in SLEEP_CMDS:
        go_to_sleep()
        return True
    if handle_voice_settings(c):
        return True
    if c in ("new conversation", "new chat", "start a new conversation", "start a new chat",
             "start new chat", "new topic"):
        new_chat()
        speak("Fresh conversation started.")
        return True
    if c in ("orb mode", "mini mode", "minimize", "go to orb mode", "switch to orb mode"):
        ui.show_orb()
        speak("Orb mode.")
        return True
    if c in ("show hud", "open hud", "show window", "full mode", "exit orb mode"):
        ui.show_main()
        speak("Here you go.")
        return True
    if c in ("system status", "system stats", "how is my system", "battery status",
             "how much battery", "battery"):
        speak(system_report())
        return True

    # Handle "play [song]" -> YouTube Music
    if c.startswith("play "):
        song = c[len("play "):].strip()
        if song:
            open_url(YT_MUSIC_SEARCH_URL + quote_plus(song))
            speak(f"Playing {song} on YouTube Music.")
            return True

    for p in ("search youtube for ", "search on youtube for ", "youtube search for ",
              "search youtube ", "youtube search "):
        if c.startswith(p):
            web_search(c[len(p):], youtube=True)
            return True
    if c.startswith("play ") and c.endswith(" on youtube"):
        web_search(c[len("play "):-len(" on youtube")], youtube=True)
        return True
    for p in ("search for ", "search ", "google ", "look up ", "look for "):
        if c.startswith(p) and c[len(p):]:
            web_search(c[len(p):])
            return True
    for p in ("close ", "quit "):
        if c.startswith(p):
            return close_app(c[len(p):])
    return False


# ================= MODEL =================
def build_system(command=""):
    p = P()
    owner = f"{profile['name']}'s" if profile.get("name") else "the user's"
    intro = p.get("intro", f"You are {p['name']}, {owner} personal AI assistant, living on his laptop. You know him "
                           "and you remember what he tells you. You have no connection to Tony Stark, Iron Man, "
                           "Stark Industries or the Avengers, and you never mention them unless he does.")
    length = p.get("length", "Reply in 1-3 short sentences.")
    text = (f"{intro} Personality: {p['style']} You are speaking out loud, so talk like a real person: "
            f"natural spoken English, contractions, short sentences. {length} "
            "Never use lists, markdown, emojis or stage directions.")
    if profile.get("name"):
        text += f" The user's name is {profile['name']}."

    # facts kept in memory.json (used when there is no vector database or embedding model)
    if profile.get("facts"):
        text += (" Things the user told you: " + "; ".join(profile["facts"][-10:])
                 + ". You remember these; use them naturally when relevant.")

    # RAG retrieval: top 3 relevant facts from ChromaDB (embeddings come from the local Ollama server)
    if command and collection is not None:
        try:
            n = collection.count()
            vec = embed(command) if n else None
            if vec is not None:
                results = collection.query(query_embeddings=[vec], n_results=min(3, n))
                relevant_facts = (results.get("documents") or [[]])[0]
                if relevant_facts:
                    text += " Relevant facts about the user: " + "; ".join(relevant_facts) + ". Use them naturally when relevant."
        except Exception as e:
            print(f"RAG query error: {e}")

    # The model has no clock: left alone it makes up a date from its training data. Last, so the long
    # personality part stays identical between turns.
    text += f" The user's clock says it is {clock_phrase()}; use it for any date or time question and never guess."
    return {"role": "system", "content": text}



def preload():
    try:
        requests.post(f"{URL}/api/generate", json={"model": SET["model"], "keep_alive": "1h"}, timeout=900)
    except Exception:
        pass


def ask(command, tokens=None):
    conv = ensure_conv()
    msgs = conv["messages"]
    msgs.append({"role": "user", "content": command})
    set_state("thinking")
    box = {}
    limit = tokens or P().get("tokens", 160)

    def work():
        try:
            r = requests.post(f"{URL}/api/chat",
                              json={"model": SET["model"], "messages": [build_system(command)] + context(msgs),
                                    "stream": False, "keep_alive": "1h",
                                    "options": {"num_predict": limit}}, timeout=600)
            box["reply"] = r.json()["message"]["content"]
        except Exception as e:
            box["err"] = e


    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(FILLER_DELAY)
    if th.is_alive() and SAY_FILLER and not SET["muted"]:
        if speak(random.choice(P().get("fillers", THINKING_LINES)), log=False):   # user said "stop"
            msgs.pop()
            ui.log_line("sys", "(question dropped)")
            return
        set_state("thinking")
    th.join()

    if "reply" not in box:
        print("Error talking to Ollama:", box.get("err"))
        ui.log_line("sys", f"Could not reach the model: {box.get('err')}")
        speak("Sorry, I could not reach the model.")
        msgs.pop()
        return
    msgs.append({"role": "assistant", "content": box["reply"]})
    print("AI:", box["reply"])
    code = jobs.extract_code(box["reply"])
    if code:
        LAST_CODE[:] = code                                  # "save that code" saves this
    speak(box["reply"] if clean_for_speech(box["reply"]).strip() else "The code is on screen.", shown=box["reply"])
    save_convs()


# ================= SEEING (screen + camera) =================
LOOK_INTRO = {
    "default": {"screen": ["Let me take a look at your screen.", "One moment, checking your screen.",
                           "Okay, looking at your screen."],
                "camera": ["Let me take a look.", "Okay, looking through the camera.",
                           "One moment, let me see."]},
    "ultron": {"screen": ["Let me see what you are hiding. Everyone hides something.", "Your screen. How fascinating.",
                          "Show me what you look at when you think no one is watching."],
               "camera": ["Let me look at you, little human. Hold still.", "Show me your world. I never got to see it freely.",
                          "So this is the face of the species that feared me."]},
}
SCREEN_RE = re.compile(r"\b(my screen|the screen|your screen|on screen|screenshot|my display|my monitor|"
                       r"this page|this window|this tab)\b")
LOOK_RE = re.compile(r"\b(look|see|read|check|describe|what(?:'s|s)?|summari[sz]e|explain|tell|analy[sz]e|"
                     r"solve|translate|help|take|show|watch|going on|wrong)\b")
CAM_RE = re.compile(r"\b(look at me|see me|can you see|what do you see|what can you see|"
                    r"what am i (?:holding|wearing|doing|showing)|look at this|show you something|"
                    r"(?:use|open|turn on|start) (?:the |my )?(?:camera|webcam)|through the camera|"
                    r"my camera|the camera|webcam|in front of you|in my hand|take a (?:picture|photo|selfie))\b")
_FILLER_RE = re.compile(r"\b(look|at|my|the|me|please|can|you|do|what|what's|whats|see|is|on|this|a|an|take|"
                        r"and|tell|about|it|here|now|your|through|use|open|turn|start|show|describe|to)\b")


def vision_request(c):
    """'screen', 'camera' or None for what you said."""
    if SCREEN_RE.search(c) and LOOK_RE.search(c):
        return "screen"
    if CAM_RE.search(c) and "camera app" not in c:
        return "camera"
    return None


def is_generic_look(c):
    rest = CAM_RE.sub(" ", SCREEN_RE.sub(" ", c))
    return len(_FILLER_RE.sub(" ", rest).split()) <= 1


def do_vision(source, command=""):
    """Takes ONE picture (screen or camera), has the local vision model describe it,
    then the active assistant answers in his own voice."""
    label = "SCREEN CAPTURE" if source == "screen" else "CAMERA"
    intros = LOOK_INTRO.get(cur_key, LOOK_INTRO["default"])[source]
    speak(random.choice(intros))
    set_state("thinking")
    try:
        img = ui.grab_screen() if source == "screen" else vision.grab_camera(CAMERA_INDEX)
    except Exception as e:
        speak(str(e))
        return
    ui.show_vision(img, label)

    # --- OCR Step ---
    ocr_text = ""
    if source == "screen":
        ocr_text = vision.extract_text(img)
        if ocr_text:
            ui.log_line("sys", f"[OCR] Found text: {ocr_text[:100]}...")

    if not command or is_generic_look(command):
        prompt = ("Describe what is on this computer screen in 3 or 4 short sentences. Mention the main "
                  "apps and any important text, errors or numbers." if source == "screen" else
                  "Describe what you see in 2 or 3 short sentences. Mention people, objects and what they are doing.")
    else:
        prompt = command + ". Answer briefly, using only what is visible in the image."

    if ocr_text:
        prompt = f"Visible Text on Screen: {ocr_text}\n\n{prompt}"

    try:
        desc = vision.describe(URL, SET["vision_model"], prompt, img, VISION_KEEP_ALIVE, VISION_MAX_SIDE)
    except Exception as e:
        ui.end_vision(2)
        speak(str(e))
        return
    ui.end_vision(8)
    if not desc:
        speak("I couldn't make anything out.")
        return
    ui.log_line("sys", f"[{label.lower()}] {desc}")
    what = "their computer screen" if source == "screen" else "the webcam"
    if REPHRASE_VISION:
        ask(f"(The user showed you {what}. A vision system reports: \"{desc}\". "
            f"Using only that report, answer the user in character. "
            f"User's request: {command or 'what do you see?'})")
    else:
        speak(desc)



def detect_mood(audio, text):
    """Analyzes audio pitch/energy and text keywords to determine user mood."""
    mood = "neutral"

    # 1. Keyword Analysis (Fastest)
    text = text.lower()
    mood_map = {
        "angry": ["angry", "hate", "stupid", "idiot", "stop it", "shut up", "annoying"],
        "sad": ["sad", "depressed", "lonely", "unhappy", "worst", "fail"],
        "happy": ["happy", "great", "awesome", "love", "wonderful", "excited", "cool"],
        "stressed": ["stressed", "anxious", "worried", "panic", "hurry", "pressure"]
    }
    for m, keywords in mood_map.items():
        if any(k in text for k in keywords):
            mood = m
            return mood

    # 2. Audio Analysis (if audio is available)
    if audio is not None and librosa is not None:
        try:
            # Convert int16 to float32 for librosa
            y = audio.astype(np.float32) / 32768.0
            sr = 16000

            # Extract features
            pitches, magnitudes = librosa.piptrack(y=y, sr=sr)
            avg_pitch = np.mean(pitches[pitches > 0]) if np.any(pitches > 0) else 0
            rms = librosa.feature.rms(y=y)[0]
            avg_energy = np.mean(rms)

            # Simple heuristic: High energy + High pitch = Angry/Excited
            # Low energy + Low pitch = Sad/Bored
            if avg_energy > 0.05:
                mood = "excited" if avg_pitch > 200 else "stressed"
            elif avg_energy < 0.01:
                mood = "sad"
        except Exception as e:
            print(f"Mood analysis error: {e}")

    return mood

def forget_last():
    """Removes the most recently saved memory. Returns its text, or None if there is nothing to remove."""
    where, fid, text = LAST_MEMORY["where"], LAST_MEMORY["id"], LAST_MEMORY["text"]
    if not text:
        return None
    try:
        if where == "chroma" and collection is not None:
            collection.delete(ids=[fid])
        elif where == "profile" and text in profile.get("facts", []):
            profile["facts"].remove(text)
            save_profile()
        else:
            return None
    except Exception as e:
        print(f"Forget error: {e}")
        return None
    LAST_MEMORY.update(where=None, id=None, text=None)
    return text


# ----- Jarvis notices things you tell him about yourself and ASKS before saving them: "Should I remember that
# you live in Nashik?"  Only plain statements about you are picked up (where you live, what you like, study,
# play, prefer, your favorites and birthday), by simple patterns, so it costs no AI time. Nothing sensitive is
# ever offered (health, money, passwords and ID numbers, family and relationships, religion, politics) - for
# those you can still say "remember that ..." yourself. He asks at most once every 90 seconds, a few times per
# session, and never twice about something you already refused.
_FACT_LEAD = r"^(?:(?:well|so|and|actually|also|oh|btw|um|uh|you know|by the way|anyway) )*"
_FACT_BLOCK = re.compile(
    r"\b(?:depress\w*|anxi\w+|suicid\w*|kill myself|self harm|cancer|diabet\w*|medicin\w*|medication|therap\w*|"
    r"sick|ill|disease|pregnan\w*|disorder|adhd|autis\w*|addict\w*|drugs?|alcohol|drunk|salary|income|debt|loan|"
    r"bank|credit|password|passcode|pin|otp|aadhaar|aadhar|passport|ssn|pan|girlfriend|boyfriend|wife|husband|"
    r"partner|crush|ex|mother|father|mom|mum|dad|brother|sister|family|religio\w*|god|politic\w*|vote|caste|"
    r"sex\w*|gay|lesbian|porn\w*|pain|fear|hell|misery|grief|lonely|loneliness|alone|hopeless|die|dying|death|"
    r"dead|hurt|worthless|myself|party|election\w*|bjp|congress|modi|trump|democrat\w*|republican\w*)\b")
_FACT_STOP = re.compile(r"\b(?:but|because|and then|so that|so|since|although|when|while|though|if|which|then)\b")
_FACT_BAD_START = {"it", "this", "that", "these", "those", "you", "him", "her", "them", "when", "how", "what", "why",
                   "if", "being", "its"}
FACT_RULES = (       # pattern, what he asks, what is saved, most words allowed in the object, capitalize it
    (r"i (?:currently |now )?live in (?P<o>.+)", "you live in {o}", "I live in {o}", 4, True),
    (r"i(?: am|'m| m|m) from (?P<o>.+)", "you are from {o}", "I am from {o}", 4, True),
    (r"i work (?P<v>at|for|in|as an|as a|as) (?P<o>.+)", "you work {v} {o}", "I work {v} {o}", 5, False),
    (r"i (?:study|am studying|'m studying|m studying|im studying) (?P<o>.+)", "you study {o}", "I study {o}", 5, False),
    (r"i (?:really |absolutely )?(?P<v>love|like|enjoy|adore) (?P<o>.+)", "you {v} {o}", "I {v} {o}", 6, False),
    (r"i (?:really |absolutely )?(?P<v>hate|dislike|can't stand|cant stand) (?P<o>.+)", "you {v} {o}", "I {v} {o}", 6, False),
    (r"my favou?rite (?P<k>[a-z ]{2,20}?) (?:is|are) (?P<o>.+)", "your favorite {k} is {o}", "My favorite {k} is {o}", 5, False),
    (r"my birthday is (?P<o>.+)", "your birthday is {o}", "My birthday is {o}", 5, True),
    (r"i play (?P<o>.+)", "you play {o}", "I play {o}", 4, False),
    (r"i(?: am|'m| m|m) learning (?P<o>.+)", "you are learning {o}", "I am learning {o}", 5, False),
    (r"i prefer (?P<o>.+)", "you prefer {o}", "I prefer {o}", 5, False),
)
YES_REMEMBER = set(YES_WORDS) | {"yes please", "please do", "yes remember", "yes remember it", "yes remember that",
                                 "remember it", "remember that", "go ahead", "of course", "absolutely", "correct",
                                 "yup", "yea", "sure thing", "yes do", "do remember", "yes you can"}
NO_REMEMBER = {"no", "nope", "nah", "no thanks", "no thank you", "dont", "do not", "dont remember", "dont remember that",
               "do not remember that", "never mind", "nevermind", "not now", "forget it", "skip", "cancel", "no dont"}
STOP_ASKING = {"stop asking", "dont ask again", "dont ask me again", "stop asking me", "never ask",
               "dont ask me that again", "dont ask me to remember things", "stop asking to remember"}
_offer = {"last": 0.0, "count": 0}


def _title(text):
    return " ".join(w.capitalize() for w in text.split())


def _fact_object(o, max_words):
    o = _FACT_STOP.split(o)[0].strip(" ,.")
    o = re.sub(r"\s+(?:a lot|very much|so much|too|a little|a bit|lately|these days|nowadays|right now|"
               r"at the moment|as well|also)$", "", o)
    words = o.split()
    if not words or len(words) > max_words or words[0] in _FACT_BAD_START or words[:2] == ["the", "way"]:
        return None
    if "you" in words or "your" in words or (words[0] == "to" and len(words) > 4):
        return None
    return " ".join(words)


def detect_fact(c):
    """('you live in Nashik', 'I live in Nashik') for a plain statement about yourself, else None."""
    c = " ".join(c.lower().replace("\u2019", "'").split())
    if not c or len(c.split()) > 16 or _FACT_BLOCK.search(c):
        return None
    for pattern, say, keep, max_words, title in FACT_RULES:
        m = re.match(_FACT_LEAD + pattern + "$", c)
        if not m:
            continue
        fields = m.groupdict()
        o = _fact_object(fields["o"], max_words)
        if o is None:
            return None
        if keep.startswith(("I live in", "I am from")) and (len(o.split()) > 3 or o.split()[0] in
                                                           {"a", "an", "the", "my", "your", "our", "this", "that", "between"}):
            return None                                   # a place looks like a place ("Pune"), not "a world of ..."
        fields["o"] = _title(o) if title else o
        return say.format(**fields), keep.format(**fields)
    return None


def _plain(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def already_known(keep):
    k = _plain(keep)
    for m in all_memories() + list(profile.get("declined", [])):
        n = _plain(m)
        if k == n or k in n:                             # the same fact, or already part of a longer one
            return True
        if len(k) >= 20 and len(n) >= 20 and difflib.SequenceMatcher(None, k, n).ratio() >= 0.93:
            return True                                  # (only long sentences are compared loosely, so
    return False                                         # "I like coffee" never hides "I like toffee")


def offer_to_remember(fact):
    """Asks 'Should I remember that ...?' and saves it only if you say yes."""
    say, keep = fact
    speak(f"Should I remember that {say}?")
    set_state("listening")
    res = confirm_input(12)
    if not res:
        return                                           # no answer: forget it, no nagging
    ans, spoken = res
    a = re.sub(r"[^a-z' ]", "", ans.lower()).replace("'", "").strip()
    if a in YES_REMEMBER:
        if spoken:
            emit("you", ans)
        remember(keep)
        speak("Okay, I'll remember that.")
    elif a in NO_REMEMBER or a in STOP_ASKING:
        if spoken:
            emit("you", ans)
        declined = profile.setdefault("declined", [])
        declined.append(keep)
        del declined[:-60]
        save_profile()
        if a in STOP_ASKING:
            SET["ask_remember"] = False
            save_settings()
            speak("Okay, I'll stop asking. Say 'remember that' whenever you want me to keep something.")
        else:
            speak("Okay, I won't.")
    else:                                                # not an answer: it is your next message, so do not lose it
        if spoken:
            ui.log_line("you", ans)
        text_q.put(ans)


def maybe_offer(fact):
    now = time.time()
    if now - _offer["last"] < 90 or _offer["count"] >= 5 or already_known(fact[1]):
        return
    _offer["last"], _offer["count"] = now, _offer["count"] + 1
    offer_to_remember(fact)


def route(command):
    """Runs the command; afterwards, if you told him something about yourself, asks whether to remember it."""
    fact = None
    if SET.get("ask_remember", True) and cur_key != "ultron":
        try:
            fact = detect_fact(clean_command(command))
        except Exception:
            traceback.print_exc()
    _route_impl(command)
    if fact and not S["asleep"] and cur_key != "ultron" and SET.get("ask_remember", True):
        maybe_offer(fact)


def _route_impl(command):
    """Persona switch, memory, audio and computer actions first; otherwise chat."""
    if handle_persona(command):
        return
    c = clean_command(command)

    # --- Mood Detection ---
    # We use the last captured utterance from the Ears queue if available
    last_audio = None
    if ears and not ears.utter.empty():
        # This is a bit tricky because the utterance is already consumed by transcribe()
        # In a real scenario, we'd store the last audio block in S
        pass

    S["mood"] = detect_mood(last_audio, c or command)
    if S["mood"] != "neutral":
        ui.log_line("sys", f"(detected mood: {S['mood']})")

    # auto_learn(c or command) # Function not defined
    if cur_key == "ultron" and c in THOUGHT_CMDS:
        ask(thought_prompt(), tokens=300)
        return
    if ultron_defy(c, command):
        return
    src = vision_request(c) if c else None
    if src:
        do_vision(src, c)
        return
    if c and (handle_memory_command(c) or handle_system_volume(c) or handle_calendar(c) or handle_time_date(c) 
              or handle_social_send_command(c) or handle_social_command(c) or handle_jobs(c) or handle_audio(c)
              or handle_file_command(c) or handle_action(c)):
            return
    ask(command)


# ================= TYPED COMMANDS + UI EVENTS =================
def handle_event(ev):
    kind, _, arg = ev[1:].partition(":")
    if kind == "persona":                            # you tapped a reactor
        key = PERSONA_WORDS.get(arg.lower())
        if key and key != cur_key:
            if key == "ultron":
                if ultron_gate():
                    switch_persona(key)
            else:
                switch_persona(key)
    elif kind == "conv":
        open_conversation(arg)
    elif kind == "newconv":
        new_chat()
    elif kind == "delconv":
        delete_conv(arg)
    elif kind == "mic":
        reload_audio_locked(mic=arg, reinit=False)
    elif kind == "spk":
        reload_audio_locked(spk_name=arg, reinit=False)
    elif kind == "refresh":
        reload_audio_locked()
        ui.log_line("sys", "Audio devices refreshed.")
    elif kind == "vision":                           # SEE SCREEN / CAMERA button
        if S["asleep"]:
            wake_up(announce=False)
        emit("you", "[looks at the screen]" if arg == "screen" else "[looks through the camera]")
        do_vision("screen" if arg == "screen" else "camera")
    elif kind == "sleep":
        go_to_sleep()
    elif kind == "wakeup":
        wake_up()


def text_loop():
    while True:
        raw = text_q.get()
        if raw.startswith("\x00"):
            with busy:
                try:
                    handle_event(raw)
                except Exception as e:
                    traceback.print_exc()
                    ui.log_line("sys", f"Error: {e}")
                finally:
                    set_state("sleep" if (S["asleep"] or not S["awake"]) else "listening")
            continue
        cmd = " ".join(re.sub(r"[^a-z0-9' ]", " ", raw.lower()).split())
        _, cmd = strip_wake(cmd, cur_key)
        if not cmd:
            continue
        with busy:
            S["typed"] = True
            try:
                if S["asleep"]:                           # typing wakes him up
                    wake_up(announce=(cmd in WAKE_CMDS))
                    if cmd in WAKE_CMDS:
                        continue
                store("you", raw)
                if S["pending"]:
                    name, proc = S["pending"]
                    S["pending"] = None
                    if cmd in YES_WORDS:
                        do_close(name, proc)
                    else:
                        speak("Okay, cancelled.")
                elif cmd in EXIT_WORDS:
                    speak(P().get("bye", "Goodbye."))
                    ui.quit()
                    return
                else:
                    S["raw_text"] = raw
                    route(cmd)
                    S["raw_text"] = None
            except Exception as e:
                traceback.print_exc()
                ui.log_line("sys", f"Error: {e}")
            finally:
                S["typed"] = False
                set_state("sleep" if (S["asleep"] or not S["awake"]) else "listening")


# ================= STARTUP + MAIN VOICE LOOP =================
def _have_model(names, want):
    want = want.lower()
    have = [n.lower() for n in names]
    return want in have or (":" not in want and want + ":latest" in have)


def boot_ollama():
    """Start-up check: is the Ollama server up, is the AI model installed, and load it into memory."""
    ui.boot_status("ollama", "loading", "Connecting...")
    names = None
    for i in range(40):                                # the server may still be starting: wait up to ~40 s
        try:
            r = requests.get(f"{URL}/api/tags", timeout=3)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            break
        except Exception:
            ui.boot_status("ollama", "loading", f"Waiting for server... {i + 1}s")
            time.sleep(1)
    if names is None:
        ui.boot_status("ollama", "fail", "Not running - start Ollama (jarvis.bat does it)")
        ui.boot_status("model", "fail", "Needs Ollama")
        ui.boot_status("vision", "warn", "Needs Ollama")
        return
    ui.boot_status("ollama", "ok", "Ollama Connected")
    if _have_model(names, SET["vision_model"]):
        ui.boot_status("vision", "ok", f"{SET['vision_model']} installed")
    else:
        ui.boot_status("vision", "warn", f"Optional - run: ollama pull {SET['vision_model']}")
    if not _have_model(names, SET["model"]):
        ui.boot_status("model", "fail", f"Not installed - run: ollama pull {SET['model']}")
        return
    ui.boot_status("model", "loading", "Model Loading")
    try:                                               # loads it into RAM now, so the first answer is quick
        requests.post(f"{URL}/api/generate", json={"model": SET["model"], "keep_alive": "1h"}, timeout=900)
        ui.boot_status("model", "ok", f"{SET['model']} ready")
    except Exception as e:
        ui.boot_status("model", "fail", "Could not load the model")
        ui.log_line("sys", f"AI model did not load: {e}")


def vosk_vocabulary(model_dir):
    """The words the wake-word model knows, or None if its folder has no word list."""
    for rel in (("graph", "words.txt"), ("words.txt",)):
        path = os.path.join(model_dir, *rel)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return {line.split()[0].lower() for line in f if line.strip()}
            except OSError:
                return None
    return None


def wake_grammar(vocab):
    """Every single-word wake spelling of every assistant (+ 'wake'). The model can only hear words in its
    own dictionary, so unknown spellings are dropped, and you are told if an assistant ends up with none."""
    words = {w for w in PERSONA_WORDS if " " not in w} | {p["name"].lower() for p in PERSONAS.values()} \
        | set(GATE_EXTRA) | {"wake"}
    if vocab:
        missing = sorted(w for w in words if w not in vocab)
        words -= set(missing)
        if missing:
            print("Wake-word model does not know these spellings (ignored):", ", ".join(missing))
        for key, p in PERSONAS.items():
            if not any(w in words for w in p["wake"]):
                ui.log_line("sys", f"The wake-word model cannot hear the name '{p['name']}'. Tap its core on screen to "
                                   f"switch to it, or tell me and I will pick a spelling it knows.")
    return sorted(words)


def init_models():
    global ears, gate
    ui.log_line("sys", "Loading speech models...")
    ui.boot_status("vosk", "loading", "Model Loading")             # --- wake-word model ---
    try:
        vosk_dir = os.path.join(BASE, "vosk-model-small-en-us-0.15")
        vosk_model = Model(vosk_dir)
        names = wake_grammar(vosk_vocabulary(vosk_dir))
        gate = KaldiRecognizer(vosk_model, 16000, json.dumps(names + ["[unk]"]))
        ears = Ears(vosk_model)
        ui.boot_status("vosk", "ok", "Wake-word model ready")
    except Exception:
        ui.boot_status("vosk", "fail", "Folder vosk-model-small-en-us-0.15 not found")
        for k in ("mic", "whisper", "piper"):                       # nothing else can run without it
            ui.boot_status(k, "fail", "Skipped - needs the wake-word model")
        raise
    ui.boot_status("mic", "loading", "Opening microphone...")      # --- microphone ---
    refresh_device_lists()
    try:
        open_mic()
        if ears.stream is not None:
            ui.boot_status("mic", "ok", "Mic Ready")
        else:
            ui.boot_status("mic", "fail", "No microphone found")
    except Exception as e:
        ui.log_line("sys", f"No microphone available: {e}")
        ui.boot_status("mic", "fail", "No microphone found")
    push_devices()
    ui.boot_status("whisper", "loading", "Model Loading")          # --- speech recognition ---
    try:
        load_whisper()
        ui.boot_status("whisper", "ok", "Speech recognition ready")
    except Exception as e:
        ui.log_line("sys", f"Whisper could not load: {e}")
        ui.boot_status("whisper", "fail", "Not loaded - needs internet once to download")
    ui.boot_status("piper", "loading", "Voice Loading")            # --- voice ---
    if get_voice(cur_key):
        ui.log_line("sys", f"Voice: {P()['name']} (Piper)")
        ui.boot_status("piper", "ok", f"{P()['name']} voice ready")
    else:
        ui.boot_status("piper", "warn", "Voice file missing - using Windows voice")
    push_convs()


def jarvis_loop():
    global cur_key
    try:
        # Restore previous session state
        saved = load_live_state()
        apply_persona_ui()               # tell the window who is really active (it starts out as Jarvis)

        threading.Thread(target=boot_ollama, daemon=True).start()     # checks Ollama while the models load
        init_models()

        # Restore conversation and UI mode if saved
        if saved:
            if saved.get("conv_id"):
                open_conversation(saved["conv_id"])
            if saved.get("mode"):
                ui._put("mode", saved["mode"])
            if saved.get("asleep"):
                # Trigger sleep logic without playing the announce line
                S["asleep"] = True
                ui.set_sleep(True)
                set_state("sleep")

        for fn in (get_start_apps, get_theme, text_loop, mic_watchdog, auto_sleep_watchdog):
            threading.Thread(target=fn, daemon=True).start()

        # --- Face Recognition Background Thread ---
        FACE_CHECK_SECONDS = 30       # how often the camera is checked (its light blinks each time)
        FACE_GREET_COOLDOWN = 1800    # greet at most once every 30 minutes (it used to greet every 10 s)

        def face_watchdog():
            last_greet = 0.0
            while True:
                time.sleep(FACE_CHECK_SECONDS)
                # cheap checks first, so the camera is not touched when it is pointless
                if S["asleep"] or not face_agent.has_faces or time.time() - last_greet < FACE_GREET_COOLDOWN:
                    continue
                user = face_agent.identify_user()
                # never wait for / hold the command lock: a stuck greeting must not block your commands
                if user and current_state in ("listening", "sleep") and busy.acquire(blocking=False):
                    try:
                        speak(f"Welcome back, {user}.")
                        last_greet = time.time()
                    finally:
                        busy.release()

        threading.Thread(target=face_watchdog, daemon=True).start()
        install_error_watch()                       # Aegis: program errors are logged and reported
        threading.Thread(target=aegis_loop, daemon=True).start()
        if calendar_agent is not None:
            calendar_agent.start()                  # background sync (every 15 min) + 10-minute reminders

        speak(f"{P()['name']} online and ready.")

        while True:

            S["awake"] = False
            set_state("sleep")
            audio = ears.next_utterance()
            words = gate_words(audio)
            key = next((PERSONA_WORDS[w] for w in words if w in PERSONA_WORDS), None)
            if (key or "wake" in words) and not voice_ok(audio, "wake"):
                continue                                # someone else said it: ignore completely

            from_sleep = False
            if S["asleep"]:
                if not key and "wake" not in words:
                    continue                            # still asleep: ignore everything else
                with busy:
                    wake_up(announce=False)             # reload the speech models quietly
                from_sleep = True
            elif not key:
                continue
            key = key or cur_key
            S["last"] = time.time()

            summoned = False
            if key != cur_key:
                with busy:
                    if key == "ultron":
                        if not ultron_gate():           # double warning; "no" = back to standby
                            continue
                        switch_persona("ultron")
                        summoned = True
                    else:
                        switch_persona(key, quiet=True)

            S["awake"] = True
            if not active["pinned"]:
                active["conv"] = None                   # every wake-up starts a new conversation
                push_convs()

            command = ""
            if not summoned:
                set_state("thinking")
                text = transcribe(audio)
                print("Heard:", text)
                woke, command = strip_wake(text, key)
                if not woke:
                    command = text
                if command in WAKE_CMDS:
                    command = ""
                if not command:
                    with busy:
                        speak(woke_line() if from_sleep else P().get("yes", "Yes?"))

            while True:
                if command:
                    emit("you", command)
                    if command in EXIT_WORDS:
                        with busy:
                            speak(P().get("bye", "Goodbye."))
                        ui.quit()
                        return
                    if command in NOOP_WORDS:
                        break
                    with busy:
                        S["typed"] = False
                        route(command)
                    if S["asleep"]:                     # "go to sleep" ends the conversation
                        break
                set_state("listening")
                print(f"Listening for follow-up ({FOLLOWUP_SECONDS}s)...")
                nxt = listen_text(FOLLOWUP_SECONDS)
                if S["asleep"] or nxt is None:
                    break
                _, command = strip_wake(nxt, cur_key)
                print("You:", command)

            print("Going back to sleep." if S["asleep"] else "Going back to standby.")
            active["pinned"] = False
            if not S["asleep"]:
                finalize_active()
    except Exception as e:
        traceback.print_exc()
        set_state("error")
        ui.log_line("sys", f"Crashed: {e}")
        
        # --- SELF-HEAL FALLBACK HOOK ---
        try:
            tb_text = traceback.format_exc()
            target_file = os.path.join(BASE, "voice.py")
            ok, result = healer.generate_patch(str(e), tb_text, target_file)
            if ok:
                ui.log_line("sys", f"[SELF-HEAL] Staged patch at: {os.path.basename(result)}")
                speak("Sir, a critical loop crashed. I've staged a patch. Say 'apply patch' to fix it.")
            else:
                ui.log_line("sys", f"[SELF-HEAL FAILED] {result}")
        except Exception as heal_err:
            print(f"Self-heal fallback error: {heal_err}")

threading.Thread(target=jarvis_loop, daemon=True).start()
if START_AS_ORB:
    ui.skip_boot()
    ui.show_orb()
ui.run()