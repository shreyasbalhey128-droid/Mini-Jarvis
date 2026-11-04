"""
music_agent.py - lets Jarvis control whatever music is playing, by voice or typing.

It presses the Windows media keys, the same ones on a keyboard, so it works with Spotify,
YouTube Music, YouTube in the browser (Opera GX, Chrome, Edge), VLC and most other players.
Nothing to install: it only uses Python's built-in ctypes (Windows only).

Voice examples:
    "pause the music"      "resume"      "play"
    "next song"            "skip this"   "previous song"

Note: Windows has ONE play/pause key that toggles. If the music is already paused, "pause" will
start it again. Say "resume" or "play" to start it, "pause" to stop it.
"""

import ctypes
import re
import sys
import time

# Windows virtual-key codes for the media keys
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

ACTIONS = {
    "pause": (VK_MEDIA_PLAY_PAUSE, "Paused."),
    "resume": (VK_MEDIA_PLAY_PAUSE, "Playing."),
    "next": (VK_MEDIA_NEXT_TRACK, "Skipping."),
    "previous": (VK_MEDIA_PREV_TRACK, "Going back."),
}

_PAUSE = {
    "pause", "pause music", "pause the music", "pause song", "pause the song", "pause it",
    "pause playback", "pause the playback", "stop music", "stop the music", "stop song",
    "stop the song", "stop playing music", "stop the music please",
}
_RESUME = {
    "resume", "resume music", "resume the music", "resume song", "resume the song",
    "resume playback", "continue music", "continue the music", "keep playing", "unpause",
    "unpause music", "unpause the music", "play", "play music", "play the music",
    "play song", "play the song", "play it",
}
_NEXT_RE = re.compile(
    r"^(?:(?:play|go to|put on) )?(?:the )?next(?: (?:song|track|one))?$"
    r"|^skip(?: (?:this|the))?(?: (?:song|track|one))?$"
    r"|^skip it$")
_PREV_RE = re.compile(
    r"^(?:(?:play|go to|put on) )?(?:the )?(?:previous|last)(?: (?:song|track|one))?$"
    r"|^go back (?:a|one) (?:song|track)$"
    r"|^back one (?:song|track)$")


def parse(c):
    """Returns 'pause', 'resume', 'next', 'previous', or None if this is not a music-control command."""
    c = (c or "").strip().lower()
    if c in _PAUSE:
        return "pause"
    if c in _RESUME:
        return "resume"
    if _NEXT_RE.match(c):
        return "next"
    if _PREV_RE.match(c):
        return "previous"
    return None


def supported():
    return sys.platform.startswith("win")


def _press(vk):
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


def handle(c, speak=print):
    """Runs a music-control command. Returns True if it was one (handled), False if not."""
    action = parse(c)
    if action is None:
        return False
    if not supported():
        speak("Music control only works on Windows.")
        return True
    vk, line = ACTIONS[action]
    try:
        _press(vk)
    except Exception:
        speak("I could not press the media key.")
        return True
    speak(line)
    return True


if __name__ == "__main__":
    # Quick test from the terminal: python music_agent.py
    while True:
        command = input("Command (or 'quit'): ").strip().lower()
        if command in ("quit", "exit"):
            break
        if not handle(command):
            print("(not a music command)")