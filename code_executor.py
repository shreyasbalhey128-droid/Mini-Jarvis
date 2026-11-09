"""
Self-reflecting code executor for Nova.

Nova writes a Python script, runs it in a separate process inside a scratch folder,
reads stderr if it fails, patches the script and retries until it works (or gives up).

NOTE: this is a guard-railed subprocess, NOT a hardened sandbox. It blocks obviously
dangerous code before running, uses a timeout, a scratch working folder and a minimal
environment, but it can still read files your user account can read. Don't point it
at tasks you wouldn't run yourself.
"""
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from llm_utils import chat, DEFAULT_MODEL

ROOT = os.path.join(os.path.expanduser("~"), ".jarvis_sandbox")
RUN_DIR = os.path.join(ROOT, "run")
SAVED_DIR = os.path.join(ROOT, "scripts")
os.makedirs(RUN_DIR, exist_ok=True)
os.makedirs(SAVED_DIR, exist_ok=True)

CODE_MODEL = DEFAULT_MODEL      # swap for a coder model later, e.g. "qwen2.5-coder:3b"
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 5

# Quick static screen. A speed bump against accidents, not a security boundary.
BLOCKED = [
    (r"\bos\.system\b", "os.system"),
    (r"\bsubprocess\b", "subprocess"),
    (r"\bshutil\.rmtree\b", "shutil.rmtree"),
    (r"\bos\.(remove|unlink|rmdir|removedirs)\b", "deleting files"),
    (r"\bpathlib\b.*\.(unlink|rmdir)\(", "deleting files"),
    (r"\bctypes\b", "ctypes"),
    (r"\bwinreg\b", "winreg"),
    (r"\bsocket\b", "raw sockets"),
    (r"\b__import__\b", "__import__"),
    (r"\bpip\s+install\b", "installing packages"),
]

SYSTEM_PROMPT = (
    "You are Nova, a careful Python programmer. Reply with ONE complete Python 3 script "
    "inside a ```python code block and nothing else. The script must run on its own, "
    "print its results with print(), use only the standard library unless the task "
    "clearly needs more, never ask for input(), never delete files, and only write "
    "files inside the current working directory."
)


@dataclass
class ExecResult:
    ok: bool
    code: str
    stdout: str
    stderr: str
    attempts: int
    saved_to: str = ""
    log: list = field(default_factory=list)


def _extract_code(text):
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S | re.I)
    code = m.group(1) if m else text
    return code.strip() + "\n"


def _unsafe(code):
    for pattern, label in BLOCKED:
        if re.search(pattern, code):
            return label
    return None


def _run(code):
    path = os.path.join(RUN_DIR, "task.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": RUN_DIR,
        "TMP": RUN_DIR,
        "PYTHONIOENCODING": "utf-8",
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", "-I", path],
            cwd=RUN_DIR, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=TIMEOUT_SECONDS, creationflags=flags,
        )
        return p.returncode == 0, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"TimeoutError: script ran longer than {TIMEOUT_SECONDS}s and was stopped."


def solve(task, max_attempts=MAX_ATTEMPTS, model=CODE_MODEL, say=None, cancel=None):
    """Write -> run -> read stderr -> patch -> retry. Returns an ExecResult."""
    log = []
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Task: {task}"},
    ]
    code, stdout, stderr = "", "", ""
    for attempt in range(1, max_attempts + 1):
        if cancel is not None and cancel.is_set():
            return ExecResult(False, code, stdout, "Cancelled.", attempt - 1, log=log)

        reply = chat(messages, model=model, temperature=0.2)
        code = _extract_code(reply)
        messages.append({"role": "assistant", "content": f"```python\n{code}```"})

        bad = _unsafe(code)
        if bad:
            stderr = f"Blocked by safety screen: uses {bad}."
            log.append((attempt, "blocked", stderr))
            messages.append({"role": "user", "content":
                f"That script was rejected because it uses {bad}. Rewrite it without that."})
            continue

        ok, stdout, stderr = _run(code)
        if ok:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            saved = os.path.join(SAVED_DIR, f"nova_{stamp}.py")
            with open(saved, "w", encoding="utf-8") as f:
                f.write(f"# Task: {task}\n{code}")
            log.append((attempt, "ok", ""))
            return ExecResult(True, code, stdout, stderr, attempt, saved, log)

        log.append((attempt, "error", stderr[-400:]))
        if say and attempt < max_attempts:
            say("That hit an error. Patching it and trying again.")
        messages.append({"role": "user", "content":
            "The script failed with this error output:\n\n"
            f"{stderr[-1500:]}\n\nFix the bug and reply with the complete corrected script."})

    return ExecResult(False, code, stdout, stderr, max_attempts, log=log)


# ---- voice-command glue -------------------------------------------------------------
_TRIGGER = re.compile(
    r"\b(write|create|make|code|build)\b.*\b(script|python|program|code)\b", re.I)


def wants_code(text):
    return bool(_TRIGGER.search(text))


def handle(text, say=print, cancel=None):
    """Call from Nova's command handler. Returns the spoken summary, or None if not a code request."""
    if not wants_code(text):
        return None
    say("On it. Writing and testing the script.")
    res = solve(text, say=say, cancel=cancel)
    if res.ok:
        out = res.stdout.strip() or "It ran without printing anything."
        if len(out) > 400:
            out = out[:400] + " ... and more."
        tries = "first try" if res.attempts == 1 else f"{res.attempts} attempts"
        return f"Done on the {tries}. Output: {out} I saved the script in my sandbox scripts folder."
    last = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "unknown error"
    return f"I gave up after {res.attempts} attempts. Last error: {last}"