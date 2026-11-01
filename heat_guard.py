"""
heat_guard.py - Aegis's heat guard for Mini-Jarvis.

Watches how hot the laptop is and speaks up (through Aegis) before it overheats.

What it does
  * Reads the CPU/GPU temperature when it can, and warns at WARN_C and again (louder) at HOT_C.
  * If no temperature sensor can be read (common on Windows), it falls back to watching the CPU load:
    a processor working flat out for LOAD_MINUTES minutes is treated as "probably getting hot".
  * Every warning names the busiest app, so you can say "Aegis, cool down" to close it
    (voice.py asks you to confirm first, and closes it politely like clicking the X).
  * Voice commands: "how hot is my laptop", "cool down", "heat guard off / on".

Temperature sources, tried in this order (the first one that works is used):
  1. psutil's built-in sensors (works on Linux/Mac, almost never on Windows)
  2. LibreHardwareMonitor or OpenHardwareMonitor running in the background (free, gives real temperatures)
  3. The Windows ACPI thermal zone (needs Jarvis to run as administrator, and many laptops report a
     fixed or inaccurate number there)

Needs:  pip install psutil
Best:   LibreHardwareMonitor running in the background (https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
"""
import re
import subprocess
import sys
import threading
import time
from collections import deque

try:
    import psutil  # pyright: ignore[reportMissingModuleSource]
except ImportError:
    psutil = None

# ---------------- settings (change these to taste) ----------------
WARN_C = 85              # degrees Celsius: "running warm" warning
HOT_C = 93               # degrees Celsius: "overheating" warning
RESET_MARGIN = 8         # it must cool this many degrees below WARN_C before a new episode can start
LOAD_WARN = 92           # % CPU (average) that counts as "flat out" when there is no temperature sensor
LOAD_MINUTES = 4         # how long the load has to stay that high
READ_SECONDS = 60        # how often the temperature / load is sampled
COOLDOWN_WARN = 600      # seconds between repeated "warm" warnings
COOLDOWN_HOT = 180       # seconds between repeated "overheating" warnings
MIN_CULPRIT_CPU = 8      # an app must use at least this % of the whole CPU to be called out
REDETECT_SECONDS = 1200  # if no sensor was found, look again every 20 minutes

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# never blamed, never offered for closing (voice.py also passes its own PROTECTED list in)
SYSTEM_NAMES = {"system", "system idle process", "idle", "registry", "memcompression", "memory compression",
                "msmpeng", "svchost", "csrss", "dwm", "wininit", "services", "lsass", "smss",
                "searchindexer", "audiodg", "fontdrvhost", "runtimebroker", "sihost", "ctfmon",
                "taskhostw", "winlogon", "ollama", "python", "pythonw"}

PRETTY = {"chrome": "Chrome", "msedge": "Edge", "opera": "Opera", "firefox": "Firefox", "code": "VS Code",
          "pycharm64": "PyCharm", "spotify": "Spotify", "vlc": "VLC", "excel": "Excel", "winword": "Word",
          "powerpnt": "PowerPoint", "virtualbox": "VirtualBox", "virtualboxvm": "VirtualBox"}


# ---------------- temperature sources ----------------
def _powershell(script, timeout=10):
    if sys.platform != "win32":
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _plausible(v):
    return v is not None and 15 < v < 130


def _temp_psutil():
    if psutil is None or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        data = psutil.sensors_temperatures()
    except Exception:
        return None
    vals = [t.current for entries in data.values() for t in entries if _plausible(t.current)]
    return max(vals) if vals else None


_LHM_SCRIPT = ("foreach($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor'){"
               "try{Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop | "
               "Where-Object {$_.SensorType -eq 'Temperature'} | "
               "ForEach-Object { $_.Name + '|' + $_.Value }}catch{}}")


def _temp_hwmonitor():
    vals = []
    for line in _powershell(_LHM_SCRIPT).splitlines():
        name, _, value = line.partition("|")
        try:
            v = float(value.replace(",", "."))
        except ValueError:
            continue
        if _plausible(v):
            vals.append(v)
    return max(vals) if vals else None


_ACPI_SCRIPT = ("try{(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                "-ErrorAction Stop | ForEach-Object { [math]::Round($_.CurrentTemperature/10-273.15,1) }) "
                "-join ' '}catch{}")


def _temp_acpi():
    vals = []
    for token in _powershell(_ACPI_SCRIPT).replace(",", ".").split():
        try:
            v = float(token)
        except ValueError:
            continue
        if _plausible(v):
            vals.append(v)
    return max(vals) if vals else None


SOURCES = [("the built-in sensors", _temp_psutil),
           ("LibreHardwareMonitor", _temp_hwmonitor),
           ("the ACPI thermal zone", _temp_acpi)]


# ---------------- spoken commands ----------------
def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


_DEVICE = r"(laptop|pc|computer|cpu|processor|machine|system)"
_HEAT_WORD = r"(hot|heat|heating|temperature|temp|warm|overheat\w*)"
_NOT_WEATHER = r"\b(weather|outside|today|tomorrow|forecast|in [a-z]+)\b"
_STATUS_PHRASES = {"heat status", "check the heat", "check heat", "check the temperature", "check temperature",
                   "temperature check", "how hot are you", "how hot are you running", "are you overheating",
                   "how hot is it running"}


def match_command(c):
    """'status' | 'cool' | 'on' | 'off' for the heat commands, else None."""
    t = _norm(c)
    if not t:
        return None
    m = (re.fullmatch(r"(?:turn |switch )?(on|off) (?:the )?(?:heat guard|heat alerts?|heat warnings?|overheat\w* alerts?)", t)
         or re.fullmatch(r"(?:heat guard|heat alerts?) (on|off)", t))
    if m:
        return m.group(1)
    if t in _STATUS_PHRASES:
        return "status"
    if re.search(_NOT_WEATHER, t):
        return None
    if re.search(r"\b" + _DEVICE + r"\b", t) and re.search(r"\b" + _HEAT_WORD + r"\b", t) \
            and re.search(r"\b(how|is|are|check|what|whats|tell)\b", t):
        return "status"
    if re.match(r"(cool|chill) (it |this |that )?down\b", t) \
            or re.fullmatch(r"cool (down )?(the |my )?" + _DEVICE + r"( down)?", t) \
            or re.fullmatch(r"(reduce|lower|cut|fix|stop) (the )?(heat|overheating|temperature)", t) \
            or re.search(r"\bclose\b.*\b(heavy|hot|heating|hungry)\b", t):
        return "cool"
    return None


# ---------------- the guard ----------------
class HeatGuard:
    def __init__(self, protected=()):
        self.protected = {p.lower() for p in protected} | SYSTEM_NAMES
        self.has_psutil = psutil is not None
        self.source = None            # name of the sensor source that works, or None
        self.temp = None              # last temperature read (Celsius) or None
        self._fails = 0
        self._next_detect = 0.0
        self._last_read = 0.0
        self._loads = deque(maxlen=LOAD_MINUTES * max(1, 60 // READ_SECONDS))
        self._alert_level = 0         # the highest level already spoken about in this episode
        self._last_alert = 0.0
        self._lock = threading.Lock()
        if psutil:
            try:
                psutil.cpu_percent(None)      # the first reading is meaningless; this primes it
            except Exception:
                pass

    # ----- temperature -----
    def read_temp(self, force=False):
        with self._lock:
            now = time.time()
            if force:
                self._next_detect = 0.0
            if self.source:
                fn = dict(SOURCES)[self.source]
                v = fn()
                if v is None:
                    self._fails += 1
                    if self._fails >= 3:                 # the sensor went away: look for another one
                        self.source, self._fails = None, 0
                else:
                    self._fails = 0
                return v
            if now < self._next_detect:
                return None
            for name, fn in SOURCES:
                v = fn()
                if v is not None:
                    self.source = name
                    return v
            self._next_detect = now + REDETECT_SECONDS
            return None

    # ----- who is making the heat -----
    def _blocked(self, name):
        return any(name == p or name.startswith(p) for p in self.protected)

    def top_heavy(self, n=3, sample=1.0):
        """The busiest apps right now as [{'name', 'label', 'cpu'}], cpu = % of the whole processor.
        An app with many processes (like Chrome) is added up. Takes about `sample` seconds."""
        if psutil is None:
            return []
        procs = []
        try:
            for p in psutil.process_iter(["name"]):
                try:
                    p.cpu_percent(None)
                    procs.append(p)
                except Exception:
                    pass
            time.sleep(sample)
            ncpu = psutil.cpu_count() or 1
            totals = {}
            for p in procs:
                try:
                    name = (p.info.get("name") or "").lower()
                    name = name[:-4] if name.endswith(".exe") else name
                    if not name or self._blocked(name):
                        continue
                    totals[name] = totals.get(name, 0.0) + p.cpu_percent(None) / ncpu
                except Exception:
                    pass
        except Exception:
            return []
        rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return [{"name": k, "label": PRETTY.get(k, k.replace("_", " ").replace("-", " ")), "cpu": int(round(v))}
                for k, v in rows]

    def cool_target(self):
        """The app to offer closing: the busiest one right now, if it is really busy."""
        top = self.top_heavy(1)
        return top[0] if top and top[0]["cpu"] >= MIN_CULPRIT_CPU else None

    # ----- the watch (called every ~30 s by Aegis; it paces itself) -----
    def check(self):
        """None, or {'level': 1|2, 'text': what Aegis should say}."""
        now = time.time()
        if now - self._last_read < READ_SECONDS:
            return None
        self._last_read = now
        temp = self.read_temp()
        self.temp = temp
        if psutil:
            try:
                self._loads.append(psutil.cpu_percent(None))
            except Exception:
                pass
        sustained = (len(self._loads) == self._loads.maxlen and sum(self._loads) / len(self._loads) >= LOAD_WARN)

        level = 0
        if temp is not None:
            level = 2 if temp >= HOT_C else 1 if temp >= WARN_C else 0
        elif sustained:
            level = 1

        if level == 0:
            cooled = temp < WARN_C - RESET_MARGIN if temp is not None else (
                not self._loads or sum(self._loads) / len(self._loads) < LOAD_WARN - 15)
            if cooled:
                self._alert_level = 0                    # the episode is over
            return None

        cooldown = COOLDOWN_HOT if level == 2 else COOLDOWN_WARN
        if level <= self._alert_level and now - self._last_alert < cooldown:
            return None
        self._alert_level, self._last_alert = max(level, self._alert_level), now
        return {"level": level, "text": self._alert_text(level, temp)}

    def _alert_text(self, level, temp):
        if temp is not None:
            t = int(round(temp))
            head = (f"Warning. Your laptop is overheating at {t} degrees Celsius." if level == 2
                    else f"Heads up. Your laptop is running warm, at {t} degrees Celsius.")
        else:
            head = (f"Heads up. The processor has been working flat out for {LOAD_MINUTES} minutes, "
                    "so the laptop is probably getting hot.")
        culprit = self.cool_target()
        if culprit:
            return (f"{head} {culprit['label']} is using about {culprit['cpu']} percent of the processor. "
                    "Say Aegis, cool down, and I'll close it. Or say Aegis, go to sleep, to cut the load.")
        return f"{head} Say Aegis, go to sleep, and I'll cut the load."

    # ----- on request -----
    def status_text(self):
        temp = self.read_temp(force=True)
        self.temp = temp
        parts = []
        if temp is not None:
            if temp >= HOT_C:
                mood = "That's too hot."
            elif temp >= WARN_C:
                mood = "That's running hot."
            elif temp >= WARN_C - 15:
                mood = "Warm, but fine."
            else:
                mood = "That's comfortable."
            parts.append(f"Your laptop is at about {int(round(temp))} degrees Celsius. {mood}")
        else:
            if psutil is None:
                return ("I can't read a temperature here, and I need the psutil package to watch the processor. "
                        "Run pip install psutil.")
            load = psutil.cpu_percent(interval=0.5)
            parts.append("I can't read a temperature sensor on this machine, so I'm going by processor load, "
                         f"which is {int(load)} percent right now. Run LibreHardwareMonitor in the background "
                         "and I'll read the real temperature.")
        top = self.top_heavy(1)
        if top and top[0]["cpu"] >= MIN_CULPRIT_CPU:
            parts.append(f"The busiest app is {top[0]['label']}, at about {top[0]['cpu']} percent.")
        return " ".join(parts)


if __name__ == "__main__":
    # quick test:  python heat_guard.py
    g = HeatGuard()
    print("Temperature:", g.read_temp(force=True), "from", g.source or "no source")
    print("Top apps:", g.top_heavy(3))
    print(g.status_text())