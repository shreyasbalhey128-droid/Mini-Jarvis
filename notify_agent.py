import os
import subprocess
import threading
import time
import traceback
 
try:
    from winotify import Notification, audio  # pyright: ignore[reportMissingImports]
except Exception:            # not installed (or not Windows): the balloon fallback below is used
    Notification = audio = None
 
APP_ID = "Mini-Jarvis"
MAX_LEN = 220                 # long texts (like the news summary) are cut so the pop-up stays readable
DEDUPE_SECONDS = 30           # the exact same pop-up is not shown twice within this time (a ringing timer repeats)
 
# ---- settings (voice.py changes these) ----
DESKTOP_ON = True             # Windows toast on the laptop
PHONE_ON = False              # push the same text to the phone (needs USB debugging + phone connected)
SOUND = False                 # False = silent pop-up (the speaker is often muted anyway); True = Windows sound
adb_finder = None             # voice.py sets this to phone_agent.find_adb so the phone push uses the same adb.exe
 
_recent = {}
_lock = threading.Lock()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
 
 
def _clean(text, limit=MAX_LEN):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
 
 
def _is_duplicate(title, message):
    now = time.time()
    key = (title, message)
    with _lock:
        for k, t in list(_recent.items()):
            if now - t > DEDUPE_SECONDS:
                del _recent[k]
        if key in _recent:
            return True
        _recent[key] = now
    return False
 
 
# ---------------------------------------------------------------- desktop
def _toast(title, message):
    """Preferred: winotify (a real Windows 10/11 toast, kept in the Action Center)."""
    if Notification is None:
        return False
    try:
        n = Notification(app_id=APP_ID, title=title, msg=message, duration="short")
        n.set_audio(audio.Default if SOUND else audio.Silent, loop=False)
        n.show()
        return True
    except Exception:
        traceback.print_exc()
        return False
 
 
def _balloon(title, message):
    """Fallback that needs no install: PowerShell's tray balloon (Windows shows it as a normal toast)."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(6000, $env:JARVIS_NT, $env:JARVIS_NM, [System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 7; $n.Dispose()"
    )
    try:
        env = dict(os.environ, JARVIS_NT=title, JARVIS_NM=message)   # passed as variables: no quoting problems
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         env=env, creationflags=_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        traceback.print_exc()
        return False
 
 
# ---------------------------------------------------------------- phone
def _sh_quote(text):
    """Quote text for the phone's shell (adb joins the words and the phone re-parses them)."""
    return "'" + text.replace("'", "'\\''") + "'"
 
 
def _phone_push(title, message):
    """Posts a notification on the phone with `cmd notification post` (Android 8+; best effort)."""
    try:
        adb = adb_finder() if adb_finder else "adb"
        if not adb:
            return False
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=6,
                             creationflags=_NO_WINDOW).stdout.splitlines()[1:]
        if not any(line.split()[-1:] == ["device"] for line in out if line.strip()):
            return False                                   # no phone connected right now
        ascii_title = title.encode("ascii", "ignore").decode() or "Jarvis"
        ascii_msg = message.encode("ascii", "ignore").decode() or "Task finished."
        r = subprocess.run([adb, "shell", "cmd", "notification", "post", "-S", "bigtext",
                            "-t", _sh_quote(ascii_title), "jarvis", _sh_quote(ascii_msg)],
                           capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        traceback.print_exc()
        return False
 
 
# ---------------------------------------------------------------- public
def send(title, message):
    """Shows the pop-up(s) in a background thread. Safe to call from anywhere, never raises."""
    title, message = _clean(title, 60) or "Jarvis", _clean(message)
    if not message or _is_duplicate(title, message):
        return
 
    def run():
        try:
            if DESKTOP_ON and not _toast(title, message):
                _balloon(title, message)
            if PHONE_ON:
                _phone_push(title, message)
        except Exception:
            traceback.print_exc()
 
    threading.Thread(target=run, daemon=True).start()
 
 
if __name__ == "__main__":
    send("Echo", "Notifications are working. You should see this pop-up in the corner of your screen.")
    time.sleep(9)             # the pop-up is sent from a background thread: keep the script alive for it