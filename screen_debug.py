"""
screen_debug.py - Nova's screen debugger for Mini-Jarvis.

Say "what's this error?", "debug this", "fix this error" or "what's wrong with this code" and Nova
takes a picture of your screen, reads the text on it (OCR), finds the error lines, and has the local AI
explain what went wrong and how to fix it. Nova speaks the short version; the full fix (steps and any
code) appears in the transcript, and "save that code" works on it afterwards.

This file only holds the text handling (what you said -> what to ask the AI -> what to say back).
voice.py does the screenshot, the AI call and the speaking.

Optional setting: in settings.json add  "code_model": "qwen2.5-coder:3b"  (after `ollama pull qwen2.5-coder:3b`)
to use a model that is better at code than the everyday one. Without it, the normal model is used.
"""
import re

MAX_CHARS = 2500          # how much screen text goes to the AI (a small model gets lost in more)
CONTEXT_LINES = 3         # lines kept around each error line

_ERROR_WORDS = (r"error|exception|traceback|stack ?trace|bug|crash\w*|fail\w*|not working|isn t working|"
                r"doesn t work|wrong with")
_ASK_WORDS = r"what|whats|explain|fix|debug|why|help|read|mean|means|solve|figure|look|check|tell|diagnose"
_THIS_WORDS = r"this|that|my screen|the screen|on screen|on the screen|here|it|my code|the code|my script"


def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


def is_debug_request(c):
    """True for 'what's this error', 'debug this', 'fix this error', 'why is this failing', ..."""
    t = _norm(c)
    if not t:
        return False
    if re.fullmatch(r"(?:please )?debug (?:this|that|it|my screen|the screen|my code|this code|the code|my script|this script)", t):
        return True
    if re.fullmatch(r"(?:please )?fix (?:this|that) (?:error|bug|code|script|problem|exception|crash)", t):
        return True
    if re.search(r"\bwhat s wrong with (?:this|that|my) (?:code|script|program|error)\b", t) \
            or re.search(r"\bwhats wrong with (?:this|that|my) (?:code|script|program|error)\b", t):
        return True
    if re.search(r"\b(" + _ERROR_WORDS + r")\b", t) and re.search(r"\b(" + _ASK_WORDS + r")\b", t) \
            and re.search(r"\b(" + _THIS_WORDS + r")\b", t):
        # 'what is this error' yes; 'what is a syntax error' no (no "this")
        return True
    return False


_ERR_LINE = re.compile(r"(error|exception|traceback|fatal|failed|failure|cannot|can t|could not|not found|"
                       r"undefined|unexpected|invalid|denied|refused|timed? ?out|no module named|"
                       r"line \d+|file \".*\"|\bat .*:\d+)", re.I)


def pick_error_text(ocr):
    """The part of the screen text that matters: lines around anything that looks like an error,
    else the last lines (that is where a terminal prints its trouble). At most MAX_CHARS."""
    lines = [ln.rstrip() for ln in (ocr or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    hits = [i for i, ln in enumerate(lines) if _ERR_LINE.search(ln)]
    if hits:
        keep = set()
        for i in hits:
            keep.update(range(max(0, i - CONTEXT_LINES), min(len(lines), i + CONTEXT_LINES + 1)))
        picked = [lines[i] for i in sorted(keep)]
        # the last error lines are the most useful ones, so when it is too long, cut from the front
        text = "\n".join(picked)
        return text[-MAX_CHARS:]
    return "\n".join(lines[-40:])[-MAX_CHARS:]


def has_error_lines(ocr):
    return any(_ERR_LINE.search(ln) for ln in (ocr or "").splitlines())


def build_prompt(screen_text, request=""):
    ask = request.strip() or "What is this error and how do I fix it?"
    return (
        "You are Nova, a friendly expert programmer helping the user fix a problem shown on their computer screen.\n"
        "The text below was read from the screenshot by OCR, so it can contain small misreadings.\n"
        "<<<\n" + screen_text + "\n>>>\n"
        f"The user asked: {ask}\n\n"
        "Answer in exactly this format:\n"
        "SPOKEN: <two or three short, plain sentences saying what went wrong and the most likely cause. "
        "No code, no file paths, no symbols.>\n"
        "FIX: <the concrete fix as short numbered steps; put any code in a fenced code block>\n"
        "If the text does not contain a clear error, say that in SPOKEN and say what you can see instead. "
        "Do not invent details that are not in the text."
    )


def parse_answer(reply):
    """(spoken, full_text) from the AI's SPOKEN: / FIX: answer. Falls back to the first sentences."""
    reply = (reply or "").strip()
    m = re.search(r"SPOKEN:\s*(.*?)(?:\n\s*FIX:|\Z)", reply, re.S | re.I)
    fix = re.search(r"FIX:\s*(.*)\Z", reply, re.S | re.I)
    if m and m.group(1).strip():
        spoken = " ".join(m.group(1).split())
        full = spoken + (("\n\n" + fix.group(1).strip()) if fix and fix.group(1).strip() else "")
        return spoken, full
    # the model ignored the format: speak the first two sentences, show everything
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"```.*?```", " ", reply, flags=re.S).strip())
    spoken = " ".join(" ".join(sentences[:2]).split())
    return spoken or "I couldn't work out what that error means.", reply