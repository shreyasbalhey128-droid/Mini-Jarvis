r"""
One-time login for Jarvis's WhatsApp + Instagram.

This opens your normal Google Chrome (NO automation attached) on the same
profile folder Jarvis uses. Instagram's bot check passes in a normal browser,
and Jarvis then reuses the saved login.

HOW TO USE
  1. Close Jarvis completely (the profile can only be open in one place).
  2. Run this file in the TERMINAL, not the "Run Code" output panel:
         .\.venv\Scripts\python.exe social_login.py
  3. Pick 1, 2 or 3. Chrome opens. Log in and wait until you see your chats /
     feed / DMs (approve any "was this you?" prompt on your phone).
  4. Close that Chrome window yourself with the X button. That saves the login,
     and this script then moves on / finishes.
  5. Start Jarvis.

You only need to redo this if WhatsApp / Instagram log you out.
"""
import os
import shutil
import subprocess
import sys

PROFILE_DIR = os.path.expanduser("~/.jarvis_browser_profile")   # same folder messaging_agent.py uses
WA_URL = "https://web.whatsapp.com/"
IG_URL = "https://www.instagram.com/accounts/login/"


def find_chrome():
    candidates = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    candidates.append(shutil.which("chrome") or "")
    candidates.append(shutil.which("google-chrome") or "")
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def login(chrome, name, url):
    print(f"\n[+] Opening Chrome for {name}...")
    print(f"--> Log into {name} and wait until you can see your {'chats' if name == 'WhatsApp' else 'feed or DMs'}.")
    print("--> Then close that Chrome window with the X button to save the login.")
    proc = subprocess.Popen([
        chrome,
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ])
    proc.wait()   # returns when you close the window
    print(f"[+] {name} window closed. Login saved.\n")


def main():
    chrome = find_chrome()
    if not chrome:
        print("Couldn't find Google Chrome. Install it, or tell me where chrome.exe is.")
        sys.exit(1)

    print("Make sure Jarvis is fully closed first.")
    while True:
        print("\n=== Jarvis social login ===")
        print("1) WhatsApp")
        print("2) Instagram")
        print("3) Both (WhatsApp, then Instagram)")
        print("q) Quit")
        choice = input("Choose: ").strip().lower()

        if choice == "1":
            login(chrome, "WhatsApp", WA_URL)
        elif choice == "2":
            login(chrome, "Instagram", IG_URL)
        elif choice == "3":
            login(chrome, "WhatsApp", WA_URL)
            login(chrome, "Instagram", IG_URL)
        elif choice in ("q", "quit", "exit"):
            break
        else:
            print("Please type 1, 2, 3 or q.")
            continue

        print("Done. You can now start Jarvis.")
        break


if __name__ == "__main__":
    main()