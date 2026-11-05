"""
download_agent.py - lets Jarvis (Vortex) download songs and PDFs by voice or typing.

Setup (use the SAME Python interpreter VS Code uses for Jarvis):
    python -m pip install yt-dlp requests ddgs
    FFmpeg must be installed and on PATH (needed for mp3 conversion)

voice.py already calls this through handle_download(); you do not need to edit anything here.

Voice examples:
    "download the song Blinding Lights by The Weeknd"
    "download bad angel by lisa"        (no kind said: treated as a song, he asks before saving)
    "download the pdf of attention is all you need"
Typed example (links only survive when typed):
    "download this pdf https://example.com/file.pdf"
"""

import os
import re
import shutil
import threading
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
MUSIC_DIR = Path.home() / "Music" / "Jarvis"
PDF_DIR = Path.home() / "Downloads" / "Jarvis"

MAX_SONG_SECONDS = 15 * 60         # refuse long videos (mixes, podcasts, etc.)
MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB cap
REQUEST_TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://\S+")
DOWNLOAD_START_RE = re.compile(
    r"^(?:(?:please|just|go ahead and|can you|could you|will you|would you|i want to|i want you to|i need to|"
    r"i would like to|i'd like to|i wanna)\s+)*(?:download|grab)\b", re.IGNORECASE)
PDF_WORDS = {"pdf", "book", "ebook", "paper", "document"}
SONG_WORDS = {"song", "music", "mp3", "track"}
# things this agent cannot download: it says so instead of letting the chat model pretend
OTHER_WORDS = {"app", "apps", "software", "installer", "setup", "exe", "zip", "driver", "drivers", "game", "games",
               "windows", "movie", "movies", "film", "films", "video", "videos", "series", "episode", "episodes",
               "season", "torrent", "apk", "iso", "file", "files", "image", "images", "photo", "photos",
               "wallpaper", "wallpapers", "chrome", "firefox", "python", "vscode"}
CANT_LINE = ("I can only download songs and PDFs. Say download the song, or download the PDF of, "
             "then the name. For a PDF link, type it so it ends in dot pdf.")
PDF_SPOKEN_RE = re.compile(r"\bp\.?\s?d\.?\s?f\b", re.IGNORECASE)   # "p d f" -> "pdf"
OFFLINE_HINTS = ("getaddrinfo", "network is unreachable", "temporary failure", "timed out",
                 "connection", "name resolution", "unable to download")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _friendly(error) -> str:
    """Turn an exception into something nice to say out loud."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return "I can't reach the internet right now."
    if isinstance(error, requests.HTTPError):
        return "The website refused the download."
    message = str(error).strip() or "Something went wrong."
    if any(hint in message.lower() for hint in OFFLINE_HINTS):
        return "I can't reach the internet right now."
    return message.splitlines()[0][:160]


def _safe_name(name: str, default: str = "download") -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "", name).strip().strip(".")
    return name[:120] or default


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _clean_query(text: str) -> str:
    """'can you download the song blinding lights by the weeknd' -> 'blinding lights by the weeknd'."""
    query = re.sub(r"^.*?\b(?:download|grab)\b\s*", "", text.strip(), count=1, flags=re.IGNORECASE)
    query = re.sub(
        r"^(?:(?:me|the|a|an|this|that|some)\s+)*"
        r"(?:(?:song|music|track|mp3|pdf|file|document|book|paper)\s+)?"
        r"(?:(?:called|named|titled|of|for)\s+)?",
        "", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+(?:as\s+)?(?:an?\s+)?(?:song|mp3|pdf|please)$", "", query, flags=re.IGNORECASE)
    return query.strip()


def _default_confirm(prompt: str) -> bool:
    answer = input(f"{prompt} (y/n): ").strip().lower()
    return answer in ("y", "yes", "yeah", "yep", "ok", "okay", "sure")


# ----------------------------------------------------------------------------
# PDFs
# ----------------------------------------------------------------------------
def download_pdf_from_url(url: str, folder: Path = PDF_DIR) -> Path:
    """Download a PDF from a direct link. Raises RuntimeError on failure."""
    folder.mkdir(parents=True, exist_ok=True)

    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    ) as response:
        response.raise_for_status()

        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_PDF_BYTES:
            raise RuntimeError("That file is too big.")

        # File name from the server header, else from the URL
        filename = ""
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
        if match:
            filename = match.group(1)
        if not filename:
            filename = os.path.basename(response.url.split("?")[0]) or "document.pdf"
        filename = _safe_name(filename)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        target = _unique_path(folder / filename)

        total = 0
        first_chunk = True
        with open(target, "wb") as f:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if first_chunk:
                    first_chunk = False
                    if not chunk.lstrip().startswith(b"%PDF"):
                        f.close()
                        target.unlink(missing_ok=True)
                        raise RuntimeError("That link is not a real PDF.")
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise RuntimeError("That file is too big.")
                f.write(chunk)

    return target


def _search_pdf_links(query: str, max_results: int = 8) -> list:
    """Return a list of (title, url) for PDFs matching the query."""
    try:
        DDGS = __import__("ddgs", fromlist=["DDGS"]).DDGS
    except ImportError:
        try:
            DDGS = __import__("duckduckgo_search", fromlist=["DDGS"]).DDGS  # older package name
        except ImportError:
            raise RuntimeError("Searching for PDFs needs the ddgs package. Run: pip install ddgs")

    results = []
    with DDGS() as ddgs:
        for item in ddgs.text(f"{query} filetype:pdf", max_results=max_results):
            url = item.get("href") or item.get("url") or ""
            title = item.get("title") or url
            if url.lower().split("?")[0].endswith(".pdf"):
                results.append((title, url))
    return results


def download_pdf_by_search(query: str, folder: Path = PDF_DIR):
    """Search for a PDF and download the first one that works. Returns (title, path)."""
    candidates = _search_pdf_links(query)
    if not candidates:
        raise RuntimeError(f"I could not find a PDF for {query}.")

    last_error = None
    for title, url in candidates:
        try:
            return title, download_pdf_from_url(url, folder)
        except Exception as error:  # try the next result
            last_error = error
    raise RuntimeError(f"None of the PDF links worked. {_friendly(last_error)}")


# ----------------------------------------------------------------------------
# Songs
# ----------------------------------------------------------------------------
def _find_song(query: str) -> dict:
    """Look up the top YouTube result for the query (no download yet)."""
    try:
        from importlib import import_module

        yt_dlp = import_module("yt_dlp")
    except ImportError:
        raise RuntimeError("Song downloads need yt-dlp. Run: pip install yt-dlp")

    options = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)

    entries = info.get("entries") or []
    if not entries:
        raise RuntimeError(f"I could not find a song called {query}.")
    return entries[0]


def _download_song(entry: dict, folder: Path = MUSIC_DIR) -> Path:
    try:
        from importlib import import_module
        yt_dlp = import_module("yt_dlp")
    except ImportError:
        raise RuntimeError("Song downloads need yt-dlp. Run: pip install yt-dlp")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed or not on PATH, so I cannot make an mp3.")

    folder.mkdir(parents=True, exist_ok=True)
    title = _safe_name(entry.get("title") or "song")
    target_base = _unique_path(folder / f"{title}.mp3").with_suffix("")

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(target_base) + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([entry.get("webpage_url") or entry.get("url")])

    return target_base.with_suffix(".mp3")


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------
def handle_download_command(text, speak=print, confirm=None, notify=None,
                            background=True, raw=None) -> bool:
    """
    Try to handle a spoken or typed download command.

    text    - the cleaned command ("download the song blinding lights")
    speak   - says something right now (used while the command is still running)
    confirm - confirm(prompt) -> True/False; should speak the prompt and listen for yes/no
    notify  - says something later, from the download thread (defaults to speak)
    raw     - the original typed text, so links keep their punctuation
    Returns True if this was a download command (handled, even if it failed),
    False if it is not one, so your other handlers can run.
    """
    if not text or not DOWNLOAD_START_RE.match(text.strip().lower()):
        return False

    text = PDF_SPOKEN_RE.sub("pdf", text)
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9']+", lowered))
    url_match = URL_RE.search(raw or "") or URL_RE.search(text)
    wants_pdf = bool(PDF_WORDS & words) or bool(
        url_match and url_match.group(0).lower().split("?")[0].endswith(".pdf"))
    wants_song = bool(SONG_WORDS & words)

    if not (wants_pdf or wants_song):
        # "download bad angel by lisa": no kind said. Something else we cannot get -> say so (never hand it to the
        # chat model, which would pretend). Otherwise it is a song title.
        if not _clean_query(text):
            speak("What should I download? I can get songs and PDFs.")
            return True
        if url_match or (OTHER_WORDS & words):
            speak(CANT_LINE)
            return True
        wants_song = True

    confirm = confirm or _default_confirm
    notify = notify or speak

    def run(job):
        if background:
            threading.Thread(target=job, daemon=True).start()
        else:
            job()

    # ---------------- PDF ----------------
    if wants_pdf:
        if url_match and url_match.group(0).lower().startswith("http"):
            url = url_match.group(0).rstrip(".,)")

            def job():
                try:
                    path = download_pdf_from_url(url)
                    notify(f"Done. Saved {path.name} in your Downloads, Jarvis folder.")
                except Exception as error:
                    notify(f"I could not download that PDF. {_friendly(error)}")

            speak("Downloading the PDF now.")
            run(job)
            return True

        query = _clean_query(text)
        if not query:
            speak("Which PDF should I download?")
            return True

        def job():
            try:
                _title, path = download_pdf_by_search(query)
                notify(f"Done. I saved {path.name} in your Downloads, Jarvis folder.")
            except Exception as error:
                notify(f"I could not get that PDF. {_friendly(error)}")

        speak(f"Looking for a PDF of {query}.")
        run(job)
        return True

    # ---------------- Song ----------------
    query = _clean_query(text)
    if not query:
        speak("Which song should I download?")
        return True

    try:
        speak(f"Searching for {query}.")
        entry = _find_song(query)
    except Exception as error:
        speak(_friendly(error))
        return True

    title = entry.get("title") or query
    duration = entry.get("duration") or 0

    if duration and duration > MAX_SONG_SECONDS:
        speak(f"The top result, {title}, is too long to be a song. Try being more specific.")
        return True

    if not confirm(f"I found {title}. Download it?"):
        speak("Okay, cancelled. To try again, say download, then the song and the artist.")
        return True

    def job():
        try:
            path = _download_song(entry)
            notify(f"Done. {path.stem} is saved in your Music, Jarvis folder.")
        except Exception as error:
            notify(f"I could not download that song. {_friendly(error)}")

    speak("Downloading now.")
    run(job)
    return True


if __name__ == "__main__":
    # Quick test from the terminal: python download_agent.py
    while True:
        command = input("Say something (or 'quit'): ").strip()
        if command.lower() in ("quit", "exit"):
            break
        if not handle_download_command(command, background=False):
            print("(not a download command)")