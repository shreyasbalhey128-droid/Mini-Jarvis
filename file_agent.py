import os
import re
import time
import zipfile
from html import unescape

import requests

# pypdf is the maintained successor of PyPDF2; use whichever is installed
try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        PdfReader = None

SEARCH_EXTS = (".pdf", ".txt", ".py", ".md", ".docx")
# folders that are huge and never hold the files you mean (the old version walked ALL of them)
SKIP_DIRS = {"appdata", "node_modules", "site-packages", "__pycache__", "venv", "env",
             "anaconda3", "miniconda3", "$recycle.bin", "windows", "program files",
             "program files (x86)", "programdata"}
MAX_DEPTH = 5               # how many folders deep to look below your home folder
MAX_SCAN_SECONDS = 8        # stop searching after this long so Jarvis never hangs
FILLER = {"file", "files", "the", "my", "a", "an", "document", "documents", "called", "named", "for"}


class FileAgent:
    def __init__(self, ollama_url, model="llama3.2:3b"):
        self.url = ollama_url
        self.model = model

    # ---------------------------------------------------------------- search
    def search_files(self, query):
        """Finds files in your home folder whose NAME contains every word of the query (newest first)."""
        words = [w for w in re.split(r"\W+", query.lower()) if w and w not in FILLER]
        if not words:
            return []
        home = os.path.expanduser("~")
        base_depth = home.rstrip(os.sep).count(os.sep)
        deadline = time.time() + MAX_SCAN_SECONDS
        matches = []
        for root, dirs, files in os.walk(home):
            if time.time() > deadline:
                break
            if root.count(os.sep) - base_depth >= MAX_DEPTH:
                dirs[:] = []                      # do not go any deeper
            else:
                dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
            for name in files:
                low = name.lower()
                if low.endswith(SEARCH_EXTS) and all(w in low for w in words):
                    matches.append(os.path.join(root, name))
        matches.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
        return matches[:5]

    # ---------------------------------------------------------------- reading
    @staticmethod
    def _extract(path, pages=5, chars=5000):
        """Returns the text of a file (the first `pages` pages / `chars` characters). Raises an exception with a
        spoken-friendly message if it cannot."""
        low = path.lower()
        if not os.path.isfile(path):
            raise RuntimeError("that file does not exist")
        if low.endswith(".pdf"):
            if PdfReader is None:
                raise RuntimeError("PDF reading needs pypdf. Run: pip install pypdf")
            reader = PdfReader(path)
            parts = []
            for page in reader.pages[:pages]:     # the first 5 pages by default so the small model is not overloaded
                parts.append(page.extract_text() or "")   # extract_text() can return None on scanned pages
            text = "\n".join(parts).strip()
            if not text:
                raise RuntimeError("that PDF has no readable text, it may be a scanned image")
            return text[:chars]
        if low.endswith(".docx"):                 # a .docx is a zip file; no extra library needed
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = unescape(re.sub(r"<[^>]+>", "", xml)).strip()
            return text[:chars]
        if low.endswith((".txt", ".py", ".md")):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(chars)
        raise RuntimeError(f"I can't read {os.path.splitext(path)[1] or 'that'} files")

    def read_full(self, path, max_chars=250_000, max_pages=80):
        """The whole document (long ones up to max_chars), for indexing and long summaries. None if unreadable."""
        try:
            return self._extract(path, pages=max_pages, chars=max_chars)
        except Exception:
            return None

    def read_file(self, path):
        """Text of a file, or a short 'Error reading file: ...' message."""
        try:
            return self._extract(path)
        except Exception as e:
            return f"Error reading file: {e}"

    def summarize_file(self, path):
        """Reads a file and has the local model summarize it in 2-3 sentences."""
        try:
            content = self._extract(path)         # real exceptions, not fragile text matching
        except Exception as e:
            return f"Error reading file: {e}"
        if not content.strip():
            return "That file is empty."
        try:
            r = requests.post(
                f"{self.url}/api/chat",
                json={"model": self.model, "stream": False,
                      "messages": [{"role": "user", "content":
                                    "Summarize the following document in 2 or 3 short spoken sentences. "
                                    "No lists, no markdown.\n\n" + content}],
                      # the default context window is small; this keeps the whole excerpt in view
                      "options": {"num_ctx": 4096, "num_predict": 200, "temperature": 0.3}},
                timeout=120)
            data = r.json()
            if "error" in data:
                return f"Summarization error: {data['error']}"
            return (data.get("message", {}).get("content") or "Could not summarize the file.").strip()
        except Exception as e:
            return f"Summarization error: {e}"