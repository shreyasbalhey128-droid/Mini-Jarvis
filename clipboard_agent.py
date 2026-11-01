"""
clipboard_agent.py - clipboard actions for Mini-Jarvis.

Copy something anywhere (a message, an article, an error, some code), then say:
  "summarize what I copied"              short spoken summary (long texts are summarized in parts)
  "translate what I copied to Hindi"     translation on screen + on your clipboard (English is also spoken)
  "explain what I copied"                what it is / what the code does
  "what's on my clipboard" / "read what I copied"
  "fix the grammar of what I copied"     the corrected text goes back on your clipboard, just paste it
  "make what I copied shorter / more formal / friendlier / simpler"
  "what's this error I copied"           Nova explains an error you copied (same debugger as the screen one)
  "clear my clipboard"
and, without copying anything first:
  "copy that"  /  "copy the code"        puts Jarvis's last answer (or its code) on your clipboard

Everything runs on your own computer: the text goes to your local Ollama model and nowhere else.

This file holds the clipboard access and the text handling. voice.py asks the AI and speaks.
Windows uses PowerShell for the clipboard, so nothing extra has to be installed.
"""
import re
import subprocess
import sys

MAX_CHARS = 6000          # the most text sent to the AI in one go
SPEAK_CHARS = 350         # longer texts are only partly read out loud (the rest stays on screen)
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------------- the clipboard itself ----------------
def get_text():
    """Text on the clipboard, or '' (empty clipboard, or it holds an image / files)."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                               capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        elif sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=10)
        else:
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, timeout=10)
        return r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def clear():
    """Empties the clipboard. True if it worked."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value $null"],
                               capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            return r.returncode == 0
    except Exception:
        return False
    return set_text("")


def set_text(text):
    """Puts text on the clipboard. True if it worked."""
    try:
        data = text.encode("utf-8")
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "[Console]::InputEncoding=[System.Text.Encoding]::UTF8; "
                                "$t=[Console]::In.ReadToEnd(); Set-Clipboard -Value $t"],
                               input=data, capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        elif sys.platform == "darwin":
            r = subprocess.run(["pbcopy"], input=data, capture_output=True, timeout=10)
        else:
            r = subprocess.run(["xclip", "-selection", "clipboard"], input=data, capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ---------------- what you said ----------------
def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


_STYLES = [("shorter", r"\b(shorter|shorten|brief|concise|trim)\b"),
           ("longer", r"\b(longer|expand|more detail|detailed)\b"),
           ("more formal", r"\b(formal|professional|polite|official)\b"),
           ("more casual and friendly", r"\b(casual|friendly|friendlier|relaxed|informal)\b"),
           ("simpler and easier to understand", r"\b(simpler|simplify|simple|easier|plain)\b"),
           ("grammar", r"\b(grammar|spelling|proofread|correct|typos?|mistakes)\b")]


def parse(c):
    """What the clipboard command is: {'action', 'lang'?, 'style'?, 'code'?} or None."""
    t = _norm(c)
    if not t:
        return None
    # --- copying Jarvis's own answer: needs no clipboard words ---
    if re.fullmatch(r"(?:please )?(?:copy|put) (?:that|this|it|the (?:last )?(?:answer|reply|response|message)|"
                    r"your (?:last )?(?:answer|reply|response))(?: (?:to|on|onto|in) (?:my |the )?clipboard)?", t):
        return {"action": "copy_last", "code": False}
    if re.fullmatch(r"(?:please )?(?:copy|put) (?:the|that|this) code(?: (?:to|on|onto|in) (?:my |the )?clipboard)?", t):
        return {"action": "copy_last", "code": True}
    if re.fullmatch(r"what (?:did|have) i (?:just )?copy(?: (?:just )?now)?", t):
        return {"action": "read"}
    if not re.search(r"\b(copied|clipboard)\b", t):
        return None
    if re.search(r"\b(clear|empty|wipe)\b", t):
        return {"action": "clear"}
    if re.search(r"\btranslate\b", t):
        u = re.sub(r"\b(please|now|for me|thanks|thank you)$", "", t).strip()
        m = re.search(r"\b(?:to|into|in)\s+([a-z]+(?: [a-z]+)?)$", u)
        lang = m.group(1) if m and not re.search(r"\b(clipboard|copied)\b", m.group(1)) else "english"
        lang = " ".join(w.capitalize() for w in lang.split())
        return {"action": "translate", "lang": lang}
    if re.search(r"\b(summari[sz]e|summary|sum up|tldr|tl dr|short version|gist)\b", t):
        return {"action": "summarize"}
    if re.search(r"\b(error|exception|traceback|bug|crash\w*)\b", t) and \
            re.search(r"\b(what|whats|explain|fix|debug|why|mean|means|help|solve)\b", t):
        return {"action": "debug"}
    if re.search(r"\b(fix|correct|proofread|improve|rewrite|rephrase|polish|shorten|simplify|reword|make)\b", t) \
            or any(re.search(p, t) for _, p in _STYLES[:5]):
        style = next((name for name, p in _STYLES if re.search(p, t)), "clearer and more natural")
        return {"action": "rewrite", "style": style}
    if re.search(r"\b(explain|what does|meaning|describe|analy[sz]e|tell me about|what is it|what is this)\b", t):
        return {"action": "explain"}
    if re.search(r"\b(read|say|speak|show|display|what did i|what is on|whats on|what s on|whats in|what is in|"
                 r"what have i|check)\b", t):
        return {"action": "read"}
    return None


# ---------------- what to ask the AI ----------------
def build_prompt(action, text, lang="English", style="clearer and more natural"):
    block = "\n<<<\n" + text[:MAX_CHARS] + "\n>>>"
    if action == "summarize":
        return ("Summarize the following text in two or three short, plain sentences that sound natural read "
                "aloud. No lists, no markdown, no introduction." + block)
    if action == "translate":
        return (f"Translate the following text into {lang}. Reply with ONLY the translation: no quotes, "
                "no notes, no explanation." + block)
    if action == "explain":
        return ("Explain in two or three short, plain sentences what the following text is or says. If it is code, "
                "say what it does. No lists, no markdown." + block)
    if action == "rewrite":
        if style == "grammar":
            return ("Fix the spelling and grammar of the following text. Keep the meaning, the wording and the "
                    "tone. Reply with ONLY the corrected text: no quotes, no notes." + block)
        return (f"Rewrite the following text so it is {style}. Keep the meaning and all the important facts. "
                "Reply with ONLY the rewritten text: no quotes, no notes." + block)
    raise ValueError(action)


_LEAD = re.compile(r"^\s*(?:sure|okay|ok|certainly|of course|here(?:'s| is| are)|the (?:translation|corrected text|"
                   r"rewritten text|translated text)(?: is)?)[^\n]*?:\s*\n?", re.I)


def clean_result(reply):
    """Just the text: no 'Here is the translation:' line, no surrounding quotes or code fences."""
    t = (reply or "").strip()
    t = re.sub(r"^```[a-z]*\n?|\n?```$", "", t).strip()
    t = _LEAD.sub("", t, count=1).strip()
    if len(t) > 1 and t[0] in "\"“'" and t[-1] in "\"”'" and t.count(t[0]) <= 2:
        t = t[1:-1].strip()
    return t


def preview(text, limit=SPEAK_CHARS):
    """(spoken, cut): the text trimmed to a sentence end near `limit`, and whether anything was cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0]), True