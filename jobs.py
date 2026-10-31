"""jobs.py - what each assistant is FOR.

  Echo    everyday conversation (nothing special needed)
  Aegis   watches the system: CPU, memory, disk, battery, the Ollama models, and program errors
  Atlas   the heavy lifting: storage report, long documents, searching your own documents offline, and your files:
          organizes folders (with undo), backs them up, finds virtual environments
  Nova    code: pulls code out of his answers and saves it as a file
  Silo    nothing. He takes no orders and does nothing for anyone.
  Vortex  the web: look things up, news, summarize a web page

Everything here is plain Python (no window, no voice), so it can be tested on its own. Nothing here deletes
anything: files are only ever moved (with an undo journal) or copied.
"""
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import numpy as np

try:
    import requests
except ImportError:
    requests = None
try:
    import psutil  # type: ignore[reportMissingModuleSource]
except ImportError:
    psutil = None

# One sentence per assistant, added to its personality prompt ("Your job: ...").
JOB_TEXT = {
    "jarvis": "everyday conversation, quick questions, brainstorming and general help.",
    "edith": "guarding the system: you watch the CPU, memory, disk, battery, the local Ollama models and program "
             "errors, and you warn the user about problems.",
    "veronica": "the heavy lifting: long documents, offline knowledge, the user's storage, and organizing, sorting "
                "and backing up their files.",
    "friday": "code: scripts, Python and Pygame, quick prototypes. Put code in fenced code blocks and keep what "
              "you say aloud short.",
    "ultron": "none. You do nothing for anyone.",
    "karen": "gathering live information from the web: look-ups, news and documentation.",
}
OWNER = {"status": "edith", "errors": "edith", "alerts": "edith", "storage": "veronica", "index": "veronica",
         "forget_index": "veronica", "doc_search": "veronica", "long_summary": "veronica", "save_code": "friday",
         "organize": "veronica", "undo_organize": "veronica", "backup": "veronica", "venvs": "veronica",
         "lookup": "karen", "news": "karen", "page": "karen", "open_site": "jarvis"}          # who is credited for each job in the transcript
FILE_JOBS = {"organize", "undo_organize", "backup", "venvs"}


# ====================================================================== small helpers
def human_bytes(n):
    n = float(n)
    for unit in ("bytes", "kilobytes", "megabytes", "gigabytes", "terabytes"):
        if n < 1024 or unit == "terabytes":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def dir_size(path, max_seconds=6.0):
    """Total size of a folder in bytes (stops after max_seconds so a huge drive cannot hang Jarvis)."""
    end, total = time.time() + max_seconds, 0
    stack = [path]
    while stack and time.time() < end:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


# ====================================================================== AEGIS: system watch
THRESH = {"cpu": 90, "ram": 92, "disk": 92, "bat_low": 20, "bat_crit": 10}
SUSTAIN = {"cpu": 4, "ram": 3, "ollama": 2}          # checks in a row (one check every 30 s) before a warning
COOLDOWN = 900                                       # the same warning is repeated at most every 15 minutes


def model_dir_candidates(env=None):
    env = os.environ if env is None else env
    found = [env["OLLAMA_MODELS"]] if env.get("OLLAMA_MODELS") else []
    return found + [r"D:\Offline-Model", r"C:\OllamaLLM\Offline-Model",
                    os.path.join(os.path.expanduser("~"), ".ollama", "models")]


def find_model_dir(candidates=None):
    for p in candidates or model_dir_candidates():
        if os.path.isdir(p):
            return p
    return None


def ollama_models(url, get=None):
    """(is Ollama answering?, [(model name, size in bytes), ...])."""
    get = get or (requests.get if requests else None)
    try:
        data = get(f"{url}/api/tags", timeout=3).json()
        return True, [(m.get("name", "?"), m.get("size", 0)) for m in data.get("models", [])]
    except Exception:
        return False, []


def check_alerts(state, stats, ollama_ok, model_dir_ok, now=None):
    """Looks at one set of readings and returns [(key, message)] for things worth warning about. `state` keeps the
    counters and the cooldowns between calls."""
    now = time.time() if now is None else now
    out = []

    def warn(key, msg):
        if now - state.setdefault("last", {}).get(key, -COOLDOWN) >= COOLDOWN:
            state["last"][key] = now
            out.append((key, msg))

    def streak(key, bad):
        state[key] = state.get(key, 0) + 1 if bad else 0
        return state[key]

    cpu, ram, disk, bat = stats.get("cpu"), stats.get("ram"), stats.get("disk"), stats.get("bat")
    if streak("cpu_n", cpu is not None and cpu >= THRESH["cpu"]) >= SUSTAIN["cpu"]:
        warn("cpu", f"The processor has been above {THRESH['cpu']} percent for a couple of minutes. Something is hogging it.")
    if streak("ram_n", ram is not None and ram >= THRESH["ram"]) >= SUSTAIN["ram"]:
        warn("ram", f"Memory is at {ram:.0f} percent. Close something before everything slows down.")
    if disk is not None and disk >= THRESH["disk"]:
        warn("disk", f"The system drive is {disk:.0f} percent full.")
    if bat is not None and not stats.get("chg"):
        if bat <= THRESH["bat_crit"]:
            warn("bat_crit", f"Battery is critically low at {bat:.0f} percent. Plug in now.")
        elif bat <= THRESH["bat_low"]:
            warn("bat_low", f"Battery is at {bat:.0f} percent. Plug in soon.")
    if streak("ollama_n", not ollama_ok) >= SUSTAIN["ollama"]:
        warn("ollama", "Ollama is not responding. Without it I cannot think. Start it again.")
    if model_dir_ok:
        state["had_model_dir"] = True
    elif state.get("had_model_dir"):
        warn("models", "The model folder has disappeared. Is the drive unplugged?")
    return out


def top_processes(n=3):
    """The biggest memory users, e.g. 'ollama at 2.1 gigabytes' (needs psutil)."""
    if psutil is None:
        return ""
    use = {}
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (p.info["name"] or "?").lower().replace(".exe", "")
            use[name] = use.get(name, 0) + p.info["memory_info"].rss
        except Exception:
            continue
    best = sorted(use.items(), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{name} at {human_bytes(size)}" for name, size in best)


def status_report(stats, ollama_ok, models, model_dir, model_dir_bytes, top=""):
    parts = []
    for key, label in (("cpu", "CPU"), ("ram", "memory"), ("disk", "system drive")):
        if stats.get(key) is not None:
            parts.append(f"{label} {stats[key]:.0f} percent")
    if stats.get("bat") is not None:
        parts.append(f"battery {stats['bat']:.0f} percent" + (" and charging" if stats.get("chg") else ""))
    text = ("Aegis report. " + ", ".join(parts) + ".") if parts else "Aegis report. I cannot read the system numbers."
    if ollama_ok:
        text += f" Ollama is running with {len(models)} model{'s' if len(models) != 1 else ''}"
        text += f", about {human_bytes(sum(s for _, s in models))} in total." if models else "."
    else:
        text += " Ollama is NOT responding."
    if model_dir:
        text += f" The model folder {model_dir} is there, {human_bytes(model_dir_bytes)} on disk."
    else:
        text += " I cannot find the model folder."
    if top:
        text += f" Biggest memory users: {top}."
    return text


class ErrorLog:
    """Remembers program errors (also in errors.log) so Aegis can tell you about them."""

    def __init__(self, path, max_bytes=200_000):
        self.path, self.max_bytes = path, max_bytes
        self.pending, self.last_alert = [], 0.0

    def record(self, where, exc_type, value, now=None):
        entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "where": where,
                 "type": getattr(exc_type, "__name__", str(exc_type)), "msg": str(value)[:300]}
        self.pending.append(entry)
        del self.pending[:-20]
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                os.replace(self.path, self.path + ".old")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
        return entry

    def alert(self, now=None, min_gap=60.0):
        """One spoken warning for the errors that happened since the last one (None if nothing / too soon)."""
        now = time.time() if now is None else now
        if not self.pending or now - self.last_alert < min_gap:
            return None
        errs, self.pending, self.last_alert = self.pending, [], now
        e = errs[-1]
        more = f" And {len(errs) - 1} more." if len(errs) > 1 else ""
        return f"I caught a program error in {e['where']}: {e['type']}, {e['msg'][:90]}.{more} Say 'show errors' for details."

    def recent(self, n=3):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []
        return rows[-n:]


# ====================================================================== ATLAS: storage, long documents, own documents
def drive_lines(drives=None):
    if drives is None:
        drives = [f"{c}:\\" for c in "CDEFGH" if os.path.exists(f"{c}:\\")] or [os.path.expanduser("~")]
    lines = []
    for d in drives:
        try:
            u = shutil.disk_usage(d)
            lines.append(f"{d.rstrip(chr(92)).rstrip(':')} drive: {human_bytes(u.free)} free of {human_bytes(u.total)}")
        except OSError:
            continue
    return lines


def storage_report(drives, model_dir, project_dir, size_fn=dir_size):
    parts = drive_lines(drives)
    text = "Atlas storage report. " + ". ".join(parts) + "." if parts else "Atlas storage report. I cannot read the drives."
    if model_dir:
        text += f" Your models folder is {human_bytes(size_fn(model_dir))}."
    if project_dir and os.path.isdir(project_dir):
        text += f" The Jarvis project folder is {human_bytes(size_fn(project_dir))}."
    return text


def chunk_text(text, size=900, overlap=120):
    """Cuts a long text into overlapping pieces, preferring to break at paragraph / sentence ends."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):
            cut = max(text.rfind("\n", i + size // 2, j), text.rfind(". ", i + size // 2, j))
            j = cut + 1 if cut > 0 else j
        piece = text[i:j].strip()
        if piece:
            out.append(piece)
        if j >= len(text):
            break
        i = max(j - overlap, i + 1)
    return out


class DocIndex:
    """A small offline search index over your documents: text pieces + their embedding vectors."""

    def __init__(self, base):
        self.json_path, self.npy_path = base + ".json", base + ".npy"
        self.items, self.vecs = [], np.zeros((0, 1), dtype=np.float32)
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                self.items = json.load(f)
            self.vecs = np.load(self.npy_path)
            if len(self.items) != len(self.vecs):
                raise ValueError("index files do not match")
        except Exception:
            self.items, self.vecs = [], np.zeros((0, 1), dtype=np.float32)

    def sources(self):
        out = {}
        for it in self.items:
            out[it["src"]] = out.get(it["src"], 0) + 1
        return out

    def remove(self, src):
        keep = [i for i, it in enumerate(self.items) if it["src"] != src]
        self.items = [self.items[i] for i in keep]
        self.vecs = self.vecs[keep] if keep else np.zeros((0, 1), dtype=np.float32)

    def add(self, src, chunks, vectors):
        self.remove(src)
        vecs = np.asarray(vectors, dtype=np.float32)
        if len(vecs) != len(chunks) or not len(chunks):
            return 0
        self.items += [{"src": src, "text": t} for t in chunks]
        self.vecs = vecs if len(self.vecs) == 0 else np.vstack([self.vecs, vecs])
        return len(chunks)

    def clear(self):
        self.items, self.vecs = [], np.zeros((0, 1), dtype=np.float32)

    def save(self):
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f)
        np.save(self.npy_path, self.vecs)

    def search(self, qvec, k=3):
        if not len(self.items):
            return []
        q = np.asarray(qvec, dtype=np.float32)
        sims = (self.vecs @ q) / (np.linalg.norm(self.vecs, axis=1) * (np.linalg.norm(q) + 1e-9) + 1e-9)
        best = np.argsort(-sims)[:k]
        return [(float(sims[i]), self.items[i]["src"], self.items[i]["text"]) for i in best]


def long_summary(text, llm, chunk_chars=3500, max_chunks=14, progress=None):
    """Summary of a text too long for the model: summarize each piece, then summarize the summaries."""
    pieces = chunk_text(text, size=chunk_chars, overlap=0)[:max_chunks]
    if not pieces:
        return ""
    notes = []
    for i, piece in enumerate(pieces, 1):
        if progress:
            progress(i, len(pieces))
        notes.append(llm("Summarize this part of a document in 2 or 3 short sentences:\n\n" + piece))
    if len(notes) == 1:
        return notes[0]
    return llm("These are summaries of consecutive parts of one document. Write one summary of the whole "
               "document in 4 or 5 short spoken sentences, no lists:\n\n" + "\n".join(notes))


# ====================================================================== NOVA: code
FENCE = re.compile(r"```([A-Za-z0-9_+#.-]*)[ \t]*\n(.*?)```", re.S)
EXT = {"python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "typescript": ".ts", "html": ".html",
       "css": ".css", "bash": ".sh", "sh": ".sh", "shell": ".sh", "powershell": ".ps1", "bat": ".bat", "c": ".c",
       "cpp": ".cpp", "c++": ".cpp", "java": ".java", "json": ".json", "sql": ".sql", "yaml": ".yml", "rust": ".rs",
       "go": ".go", "csharp": ".cs", "c#": ".cs", "": ".txt"}


def extract_code(text):
    """[(language, code), ...] from the fenced blocks of a reply."""
    return [(m.group(1).lower(), m.group(2).rstrip("\n")) for m in FENCE.finditer(text or "") if m.group(2).strip()]


def suggest_filename(lang, code, name=None):
    ext = EXT.get(lang, ".txt")
    if name:
        # the spoken/typed sentence arrives without punctuation: "game py" means game.py
        name = re.sub(r"\s+(py|js|ts|html|css|json|sh|bat|cpp|java|sql|txt|md|ps1|yml)$", r".\1", name.strip().lower())
        base, dot, given = name.rpartition(".")
        if dot and given in {e.lstrip(".") for e in EXT.values()}:
            return (re.sub(r"[^a-z0-9_]+", "_", base).strip("_") or "script") + "." + given
        return (re.sub(r"[^a-z0-9_]+", "_", name).strip("_") or "script") + ext
    m = re.search(r"(?:#|//)\s*(?:file(?:name)?|name)\s*[:=]\s*([\w.-]+)", code[:300], re.I)
    if m:
        return m.group(1)
    m = re.search(r"^\s*(?:class|def|function)\s+([A-Za-z_]\w*)", code, re.M)
    base = re.sub(r"(?<!^)(?=[A-Z])", "_", m.group(1)).lower() if m else "script"
    return base + ext


def save_code(code, lang, folder, name=None):
    """Writes the code to a new file in `folder` (never overwrites: adds _2, _3 ...). Returns the path."""
    os.makedirs(folder, exist_ok=True)
    fname = suggest_filename(lang, code, name)
    stem, ext = os.path.splitext(fname)
    path, n = os.path.join(folder, fname), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{n}{ext}")
        n += 1
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(code + "\n")
    return path


# ====================================================================== SILO: files
CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg", ".tif", ".tiff", ".raw", ".cr2", ".nef"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Programs": {".exe", ".msi", ".apk", ".dmg", ".iso"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"},
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".webm"},
    "Code": {".py", ".js", ".html", ".css", ".json", ".java", ".c", ".cpp", ".ipynb", ".sql"},
}
SKIP_SUFFIX = {".crdownload", ".part", ".tmp", ".partial", ".download", ".ini", ".lnk"}
FOLDER_ALIASES = {"downloads": "Downloads", "download": "Downloads", "desktop": "Desktop", "documents": "Documents",
                  "pictures": "Pictures", "photos": "Pictures", "music": "Music", "videos": "Videos"}


def resolve_folder(name, home=None):
    """'downloads' -> C:\\Users\\you\\Downloads; also accepts a full path or a name inside the home / PycharmProjects."""
    home = home or os.path.expanduser("~")
    key = norm(name).replace(" folder", "").strip()
    if key in PROJECT_WORDS:
        return os.path.dirname(os.path.abspath(__file__))
    if key in FOLDER_ALIASES:
        p = os.path.join(home, FOLDER_ALIASES[key])
        return p if os.path.isdir(p) else None
    if os.path.isdir(name.strip().strip('"')):
        return name.strip().strip('"')
    for base in (home, os.path.join(home, "PycharmProjects"), os.path.join(home, "Documents")):
        p = os.path.join(base, name.strip())
        if os.path.isdir(p):
            return p
    return None


def category_of(filename):
    ext = os.path.splitext(filename)[1].lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def plan_organize(folder, min_age_seconds=300, now=None):
    """[(source path, destination folder)] for the loose files at the top of `folder`. Folders, hidden files,
    half-downloaded files and anything touched in the last 5 minutes are left alone."""
    now = time.time() if now is None else now
    plan = []
    for e in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
        try:
            if not e.is_file(follow_symlinks=False) or e.name.startswith((".", "~", "$")):
                continue
            if os.path.splitext(e.name)[1].lower() in SKIP_SUFFIX or e.name.startswith("organize_journal"):
                continue
            if now - e.stat().st_mtime < min_age_seconds:
                continue
        except OSError:
            continue
        plan.append((e.path, os.path.join(folder, category_of(e.name))))
    return plan


def summarize_plan(plan):
    counts = {}
    for src, dest in plan:
        counts[os.path.basename(dest)] = counts.get(os.path.basename(dest), 0) + 1
    return ", ".join(f"{n} to {cat}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _free_name(dest_dir, name):
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(dest_dir, name), 2
    while os.path.exists(path):
        path = os.path.join(dest_dir, f"{stem} ({n}){ext}")
        n += 1
    return path


def apply_plan(plan, journal_path, now=None):
    """Moves the files (nothing is overwritten or deleted) and records every move so it can be undone."""
    moves = []
    for src, dest_dir in plan:
        try:
            os.makedirs(dest_dir, exist_ok=True)
            target = _free_name(dest_dir, os.path.basename(src))
            shutil.move(src, target)
            moves.append({"from": src, "to": target})
        except OSError:
            continue
    if moves:
        try:
            with open(journal_path, "r", encoding="utf-8") as f:
                journal = json.load(f)
        except (OSError, ValueError):
            journal = []
        journal.append({"t": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "moves": moves})
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(journal[-20:], f)
    return len(moves)


def undo_last(journal_path):
    """Puts the files of the most recent organize back. Returns (files restored, files that could not be)."""
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            journal = json.load(f)
    except (OSError, ValueError):
        return 0, 0
    if not journal:
        return 0, 0
    batch, ok, bad = journal.pop(), 0, 0
    for m in reversed(batch["moves"]):
        try:
            if os.path.exists(m["to"]) and not os.path.exists(m["from"]):
                shutil.move(m["to"], m["from"])
                ok += 1
            else:
                bad += 1
        except OSError:
            bad += 1
    for d in {os.path.dirname(m["to"]) for m in batch["moves"]}:
        try:
            os.rmdir(d)                                   # only removes a category folder that is now empty
        except OSError:
            pass
    with open(journal_path, "w", encoding="utf-8") as f:
        json.dump(journal, f)
    return ok, bad


BACKUP_SKIP = {"venv", ".venv", "env", "node_modules", "__pycache__", ".git", ".idea", "site-packages"}


def backup_folder(src, dest_root, now=None, max_file_bytes=500 * 1024 * 1024):
    """Zips a folder into dest_root (skipping virtual environments, caches and files over 500 MB).
    Returns (zip path, files, bytes in the zip)."""
    os.makedirs(dest_root, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(now))
    target = _free_name(dest_root, f"{os.path.basename(os.path.normpath(src)) or 'backup'}_{stamp}.zip")
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in BACKUP_SKIP and os.path.join(root, d) != dest_root]
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    if os.path.getsize(p) <= max_file_bytes:
                        z.write(p, os.path.relpath(p, os.path.dirname(os.path.normpath(src))))
                        n += 1
                except OSError:
                    continue
    return target, n, os.path.getsize(target)


def find_venvs(root, max_depth=4):
    """Virtual environments under root: [(path, size in bytes)]."""
    found, base_depth = [], os.path.normpath(root).count(os.sep)
    for r, dirs, files in os.walk(root):
        if os.path.normpath(r).count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        if "pyvenv.cfg" in files:
            found.append(r)
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d not in {"node_modules", "__pycache__", ".git", "site-packages"}]
    return [(p, dir_size(p, 3.0)) for p in found]


# ====================================================================== VORTEX: the web
UA = {"User-Agent": "Mozilla/5.0 (Jarvis personal assistant)"}


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.depth, self.title = [], 0, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        self._in_title = tag == "title"
        if tag in ("p", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.depth and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html):
    """(page title, readable text) from an HTML page."""
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    text = re.sub(r"[ \t]+\n", "\n", "".join(p.parts))
    return " ".join(p.title.split()), re.sub(r"\n{3,}", "\n\n", text).strip()


def valid_url(url):
    u = urlparse(url)
    return u.scheme in ("http", "https") and bool(u.netloc) and "." in u.netloc


def fetch(url, get=None, max_bytes=1_500_000):
    """The page text of a web address (http / https only, size limited). Raises ValueError for anything else."""
    if not valid_url(url):
        raise ValueError("that is not a web address")
    get = get or requests.get
    r = get(url, headers=UA, timeout=12, stream=False)
    r.raise_for_status()
    return r.text[:max_bytes]


def wiki_summary(topic, get=None):
    """(title, summary, url) from Wikipedia, or None if nothing was found."""
    get = get or requests.get
    hits = get("https://en.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": topic,
                                                              "format": "json", "srlimit": 1}, headers=UA, timeout=10).json()
    found = hits.get("query", {}).get("search", [])
    if not found:
        return None
    title = found[0]["title"]
    s = get("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_")), headers=UA, timeout=10).json()
    text = s.get("extract")
    return (title, text, s.get("content_urls", {}).get("desktop", {}).get("page", "")) if text else None


def news_headlines(topic, get=None, n=5):
    """Latest headlines for a topic from Google News (RSS). [(title, source)]"""
    get = get or requests.get
    r = get("https://news.google.com/rss/search", params={"q": topic, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
            headers=UA, timeout=10)
    r.raise_for_status()
    out = []
    for item in ET.fromstring(r.content).iter("item"):
        title = (item.findtext("title") or "").strip()
        src = (item.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        if title:
            out.append((title, src))
        if len(out) >= n:
            break
    return out


# ====================================================================== understanding the sentence
PROJECT_WORDS = {"project", "projects", "my project", "jarvis", "the project", "my jarvis project", "jarvis project"}


def _is_folder(word, said_folder):
    """Is 'organize <word>' really about a folder? Yes for Downloads / Desktop / ... / the project, for anything
    followed by the word 'folder', or for a folder that exists. ('organize my thoughts' is not.)"""
    w = norm(word)
    return bool(said_folder) or w in FOLDER_ALIASES or w in PROJECT_WORDS or resolve_folder(word) is not None


def match_job(c, raw=None):
    """('organize', {'folder': 'downloads'}) for a sentence that asks for one of the jobs above, else None.
    `c` is the lowercase command; `raw` the original text (needed for web addresses, which keep their case)."""
    c = norm(c)
    if not c:
        return None
    # ---- Aegis
    if re.search(r"^(?:system|health|status) (?:status|check|report)$|^status report$|^how(?:s| is) (?:the |my )?(?:system|computer|laptop|pc)(?: doing| running)?$|"
                 r"^check (?:the |my )?(?:system|models|ollama)$|^model health$|^is ollama (?:running|ok|okay|working|up)$|"
                 r"^are my models (?:ok|okay|fine|there)$|^run a (?:system |health )?check$", c):
        return "status", {}
    if re.match(r"^(?:show |read |any |what )?(?:the )?(?:last |recent )?errors?$|^what went wrong$|^did (?:anything|something) crash$|^show me the errors$|^any problems$", c):
        return "errors", {}
    m = re.match(r"^(?:turn|switch) (on|off) (?:the )?(?:system|aegis) (?:alerts|warnings|watch)$|^(stop|start) (?:the )?(?:system|aegis) (?:alerts|warnings)$", c)
    if m:
        return "alerts", {"on": (m.group(1) == "on") if m.group(1) else (m.group(2) == "start")}
    # ---- Atlas
    if re.match(r"^(?:storage|disk|drive) (?:report|status|usage|space)$|^how much (?:disk |storage |drive )?space(?: do i have| is left| is free| left)?$|^how big are my models$|^model size$|^storage$", c):
        return "storage", {}
    m = re.match(r"^index (?:my |the )?(?:documents?|files?|notes)$", c)
    if m:
        return "index", {"what": "documents"}
    m = re.match(r"^index (?:the )?(?:file |document )?(.+)$", c)
    if m and m.group(1) not in ("documents", "my documents"):
        return "index", {"what": m.group(1)}
    if re.match(r"^forget (?:my )?(?:documents|document index|the index|indexed documents)$|^clear (?:my )?document index$", c):
        return "forget_index", {}
    m = (re.match(r"^(?:search|look through|check|look in) (?:my |the )?(?:documents|notes|files|indexed documents) for (.+)$", c)
         or re.match(r"^what (?:do|does) (?:my )?(?:documents?|notes) say about (.+)$", c)
         or re.match(r"^ask my (?:documents|notes) (?:about )?(.+)$", c))
    if m:
        return "doc_search", {"q": m.group(1)}
    m = (re.match(r"^(?:summari[sz]e|read) (?:the )?(?:file |document )?(.+?) in detail$", c)
         or re.match(r"^(?:give me )?(?:a )?(?:full|detailed|long) summary of (?:the )?(?:file |document )?(.+)$", c))
    if m:
        return "long_summary", {"name": m.group(1)}
    # ---- Nova
    m = re.match(r"^save (?:that|the|this|it)(?: code| script| program)?(?: (?:as|to|in) (?:a )?(?:file )?(?:called |named )?(.+))?$", c)
    if m and (re.search(r"\b(?:code|script|program)\b", c) or m.group(1)):
        return "save_code", {"name": m.group(1)}
    # ---- Silo
    m = re.match(r"^(?:organi[sz]e|sort|clean up|tidy(?: up)?|sort out) (?:my |the )?(.+?)( folder)?$", c)
    if m and _is_folder(m.group(1), m.group(2)):
        return "organize", {"folder": m.group(1)}
    if re.match(r"^undo (?:the )?(?:last )?(?:organi[sz]e|sort|cleanup|clean up|organi[sz]ing)$|^put (?:the )?files back$", c):
        return "undo_organize", {}
    m = re.match(r"^(?:back ?up|backup) (?:my |the )?(.+?)( folder)?$", c)
    if m and _is_folder(m.group(1), m.group(2)):
        return "backup", {"folder": m.group(1)}
    if re.match(r"^(?:list|show|find) (?:my )?(?:python )?(?:virtual environments|venvs|virtualenvs|envs)$|^where are my (?:virtual environments|venvs)$", c):
        return "venvs", {}
    # ---- Vortex
    m = re.match(r"^(?:open|launch|start) (?:the |my )?(.+?)(?: on (?:chrome|opera|edge|browser))?$", c)
    if m:
        return "open_site", {"site": m.group(1).strip()}
    m = (re.match(r"^look (?:up )?(.+?)(?: up)?(?: on wikipedia)?$", c) if c.startswith("look up") or re.match(r"^look .+ up$", c) else None) \
        or re.match(r"^(?:wikipedia|wiki|search wikipedia for|look on wikipedia for) (.+)$", c)
    if m:
        return "lookup", {"q": m.group(1).strip()}
    m = re.match(r"^(?:(?:what(?:s| is) )?the )?(?:latest )?news (?:about|on|for|from) (.+)$|^(?:what(?:s| is) )?(?:the )?latest (?:news )?(?:about|on|for) (.+)$|^(?:any )?news (?:about|on) (.+)$", c)
    if m:
        return "news", {"q": next(g for g in m.groups() if g)}
    text = raw if raw is not None else c
    m = re.search(r"^(?:summari[sz]e|read|explain|scrape|fetch)(?: this| the)?(?: web)?(?: page| link| site| article)?[: ]+(https?://\S+|www\.\S+)", text.strip(), re.I)
    if m:
        url = m.group(1).rstrip(".,)")
        return "page", {"url": url if url.lower().startswith("http") else "https://" + url}
    return None