import os
import re
import threading
from contextlib import contextmanager

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

WA_URL = "https://web.whatsapp.com/"
IG_INBOX_URL = "https://www.instagram.com/direct/inbox/"
IG_LOGIN_URL = "https://www.instagram.com/accounts/login/"

# Same flags everywhere so the browser fingerprint stays consistent between
# the login session and the later background checks.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]


class SocialAgent:
    def __init__(self):
        # Shared session folder so you stay logged into both sites
        self.user_data_dir = os.path.expanduser("~/.jarvis_browser_profile")

        # Chromium can't open the same profile twice at once. This lock makes the
        # background poller and your voice commands take turns instead of crashing.
        self._lock = threading.Lock()

        # chat name -> last seen unread count (used to detect NEW messages)
        self._last_unread = {}

    # ------------------------------------------------------------------ #
    # Browser session helper
    # ------------------------------------------------------------------ #
    @contextmanager
    def _session(self, headless=False):
        """Opens the shared profile, yields a page, always closes afterwards."""
        with self._lock:
            with sync_playwright() as p:
                args = list(LAUNCH_ARGS)
                if headless:
                    # "New" headless mode behaves like a normal Chrome window.
                    # Playwright's default headless gets blocked by WhatsApp Web.
                    args.append("--headless=new")

                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=False,
                    args=args,
                    ignore_default_args=["--enable-automation"],
                    viewport={"width": 1280, "height": 800},
                )
                try:
                    context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    yield page
                finally:
                    context.close()

    # ------------------------------------------------------------------ #
    # Login helpers (run once, manually)
    # ------------------------------------------------------------------ #
    def setup_whatsapp_login(self):
        """Opens WhatsApp Web so you can scan the QR code once."""
        print("\n[+] Opening WhatsApp Web...")
        with self._session(headless=False) as page:
            page.goto(WA_URL, wait_until="domcontentloaded")
            print("--> Scan the QR code with your phone, wait for your chats to load,")
            input("--> then press ENTER here: ")
        print("[+] WhatsApp session saved!\n")

    def setup_instagram_login(self):
        """Opens Instagram so you can log in / solve any CAPTCHA by hand."""
        print("\n[+] Opening browser for Instagram login...")
        with self._session(headless=False) as page:
            page.goto(IG_LOGIN_URL)
            print("\n========================================================")
            print("--> Log into Instagram now (solve any CAPTCHA).")
            print("--> Once you see your feed or DMs, come back and press ENTER.")
            print("========================================================\n")
            input("Press ENTER here after logging in: ")
        print("[+] Instagram session saved successfully!\n")

    # ------------------------------------------------------------------ #
    # WhatsApp internals
    # ------------------------------------------------------------------ #
    def _open_whatsapp(self, page, timeout=25000):
        """Loads WhatsApp Web. Returns True if the chat list appeared."""
        page.goto(WA_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(
                "#pane-side, div[aria-label='Chat list']", timeout=timeout
            )
            return True
        except PWTimeout:
            return False

    def _whatsapp_unread(self, page):
        """Returns {chat_name: unread_count} for every chat with unread messages."""
        unread = {}
        badges = page.locator(
            "span[aria-label*='unread message'], span[data-testid='icon-unread-count']"
        )
        for i in range(badges.count()):
            badge = badges.nth(i)
            try:
                row = badge.locator("xpath=ancestor::*[@role='row'][1]")
                name = row.locator("span[title]").first.get_attribute("title", timeout=2000)
                if not name:
                    continue
                try:
                    n = int(re.sub(r"\D", "", badge.inner_text(timeout=1000)) or 1)
                except Exception:
                    n = 1
                unread[name] = n
            except Exception:
                continue
        return unread

    def _open_whatsapp_chat(self, page, contact_name, allow_partial=False):
        """
        Searches for a contact and opens the chat.
        Only opens an EXACT (case-insensitive) match unless allow_partial=True,
        so a misheard name can't send to the wrong person.
        Returns the opened chat's name, or None.
        """
        search = page.locator(
            "div[aria-label='Search input textbox'], div[contenteditable='true'][data-tab='3']"
        ).first
        search.click()
        search.fill(contact_name)
        page.wait_for_timeout(1200)

        results = page.locator("#pane-side span[title]")
        exact = results.filter(
            has_text=re.compile(rf"^\s*{re.escape(contact_name)}\s*$", re.IGNORECASE)
        ).first
        if exact.count() > 0:
            name = exact.get_attribute("title")
            exact.click()
            return name

        if allow_partial and results.count() > 0:
            first = results.first
            name = first.get_attribute("title")
            first.click()
            return name

        return None

    # ------------------------------------------------------------------ #
    # WhatsApp public API
    # ------------------------------------------------------------------ #
    def check_whatsapp_only(self, headless=False):
        """Reports unread WhatsApp chats."""
        try:
            with self._session(headless=headless) as page:
                if not self._open_whatsapp(page):
                    return "WhatsApp: Could not load or requires QR scan."
                unread = self._whatsapp_unread(page)
        except Exception as e:
            return f"WhatsApp: Could not check. Error: {e}"

        if not unread:
            return "WhatsApp: No unread messages."
        parts = [f"{name} ({n})" for name, n in unread.items()]
        return f"WhatsApp: {len(unread)} unread chat(s) from " + ", ".join(parts)

    def read_whatsapp_chat(self, contact_name, limit=5, allow_partial=False):
        """
        Reads the last `limit` messages from a chat.
        Note: opening a chat marks its messages as read on your phone too.
        """
        try:
            with self._session(headless=False) as page:
                if not self._open_whatsapp(page):
                    return "WhatsApp: Could not load or requires QR scan."

                opened = self._open_whatsapp_chat(page, contact_name, allow_partial)
                if not opened:
                    return f"WhatsApp: I couldn't find a chat named {contact_name}."

                try:
                    page.wait_for_selector("div[data-pre-plain-text]", timeout=8000)
                except PWTimeout:
                    return f"WhatsApp: No readable text messages in {opened}."

                msgs = page.locator("div[data-pre-plain-text]")
                total = msgs.count()
                lines = []
                for i in range(max(0, total - limit), total):
                    m = msgs.nth(i)
                    meta = m.get_attribute("data-pre-plain-text") or ""
                    # meta looks like "[10:42 am, 30/10/2026] Name: "
                    sender = meta.split("] ", 1)[-1].strip().rstrip(":").strip() or "Unknown"
                    text_el = m.locator("span.selectable-text").first
                    text = text_el.inner_text(timeout=2000) if text_el.count() > 0 else "[media]"
                    lines.append(f"{sender}: {text}")

                if not lines:
                    return f"WhatsApp: No messages found in {opened}."
                return f"Last {len(lines)} message(s) in {opened}:\n" + "\n".join(lines)
        except Exception as e:
            return f"WhatsApp: Failed to read chat. Error: {e}"

    def send_whatsapp(self, contact_name, message_text, allow_partial=False):
        """Sends a WhatsApp message to a specific contact (exact name match by default)."""
        try:
            with self._session(headless=False) as page:
                if not self._open_whatsapp(page):
                    return "Failed to send WhatsApp message: not loaded or requires QR scan."

                opened = self._open_whatsapp_chat(page, contact_name, allow_partial)
                if not opened:
                    return (
                        f"Didn't send: no exact contact named {contact_name}. "
                        "Nothing was sent."
                    )

                msg_box = page.locator(
                    "footer div[contenteditable='true'], div[aria-label='Type a message']"
                ).first
                msg_box.click()
                msg_box.fill(message_text)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1500)  # give it a moment to actually go out
                return f"Successfully sent WhatsApp message to {opened}."
        except Exception as e:
            return f"Failed to send WhatsApp message: {e}"

    def get_new_unread_chats(self):
        """
        Background check. Returns alerts only for chats whose unread count went UP
        since the last check (so a second message from the same person still alerts).
        """
        alerts = []
        try:
            with self._session(headless=True) as page:
                if not self._open_whatsapp(page, timeout=15000):
                    return alerts
                current = self._whatsapp_unread(page)
        except Exception:
            return alerts

        for name, n in current.items():
            if n > self._last_unread.get(name, 0):
                alerts.append(f"New WhatsApp message from {name}")

        # Replace with the current state so read chats are forgotten
        self._last_unread = current
        return alerts

    # ------------------------------------------------------------------ #
    # Instagram
    # ------------------------------------------------------------------ #
    def _instagram_unread_count(self, page):
        """Returns an int, or None if not logged in."""
        page.goto(IG_INBOX_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if "login" in page.url:
            return None

        # Dismiss "Turn on notifications" style popups
        for _ in range(2):
            try:
                page.get_by_role("button", name="Not Now").click(timeout=2000)
                page.wait_for_timeout(500)
            except Exception:
                break

        page.wait_for_selector("div[role='grid'], div[role='main']", timeout=15000)

        # NOTE: Instagram's class names (like _ac5f) are auto-generated and change
        # without warning. If this stops finding unread DMs, this is the line to fix.
        unread = page.locator(
            "div[role='listitem'] div._ac5f, div._ac5f, span[data-testid='unread-badge']"
        )
        return unread.count()

    def check_instagram_only(self, headless=False):
        """Checks unread Instagram Direct messages."""
        try:
            with self._session(headless=headless) as page:
                count = self._instagram_unread_count(page)
        except Exception as e:
            return f"Instagram: Could not check DMs. Error: {e}"

        if count is None:
            return "Instagram: Session expired or not logged in. Please run setup login."
        if count == 0:
            return "Instagram: No unread DMs found."
        return f"Instagram: You have approximately {count} unread DM conversation(s)."

    # ------------------------------------------------------------------ #
    # Both at once
    # ------------------------------------------------------------------ #
    def check_all_messages(self):
        """Checks both platforms in a single browser session."""
        results = []
        try:
            with self._session(headless=False) as page:
                # WhatsApp
                if self._open_whatsapp(page):
                    unread = self._whatsapp_unread(page)
                    if unread:
                        parts = [f"{name} ({n})" for name, n in unread.items()]
                        results.append(
                            f"WhatsApp: {len(unread)} unread chat(s) from " + ", ".join(parts)
                        )
                    else:
                        results.append("WhatsApp: No unread messages.")
                else:
                    results.append("WhatsApp: Could not load or requires QR scan.")

                # Instagram
                try:
                    count = self._instagram_unread_count(page)
                    if count is None:
                        results.append("Instagram: Not logged in. Please run setup login.")
                    elif count == 0:
                        results.append("Instagram: No unread DMs found.")
                    else:
                        results.append(
                            f"Instagram: You have approximately {count} unread DM conversation(s)."
                        )
                except Exception:
                    results.append("Instagram: Could not check DMs.")
        except Exception as e:
            return f"Could not open browser: {e}"

        return " | ".join(results)


if __name__ == "__main__":
    agent = SocialAgent()
    # First time only:
    # agent.setup_whatsapp_login()
    # agent.setup_instagram_login()
    print(agent.check_all_messages())