import atexit
import os
import queue
import re
import time
import threading
from concurrent.futures import Future
from contextlib import contextmanager

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

WA_URL = "https://web.whatsapp.com/"
IG_INBOX_URL = "https://www.instagram.com/direct/inbox/"
IG_LOGIN_URL = "https://www.instagram.com/accounts/login/"

# Same flags everywhere so the browser fingerprint stays consistent between
# the login session and the background session.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]

_STOP = object()

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_IMAGE_BYTES = 16 * 1024 * 1024   # WhatsApp's limit for photos

# Lines inside a WhatsApp chat row that are NOT the message preview (times / dates)
TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}(\s?[ap]\.?m\.?)?|yesterday|today|mon|tue|wed|thu|fri|sat|sun|"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})$",
    re.IGNORECASE,
)
# Lines inside an Instagram thread row that are NOT the message preview
IG_NOISE_RE = re.compile(
    r"^(active (now|\d+\s?\w+ ago)|\d+\s?(s|m|h|d|w|min|hr)s?|·|\d*\s*unread( messages?)?)$",
    re.IGNORECASE,
)


class _WorkerStopped(Exception):
    pass


class SocialAgent:
    """
    One background thread owns ONE headless browser that stays logged into
    WhatsApp Web and Instagram. Everything (checking, reading, sending and the
    live new-message watcher) runs on that thread, so nothing fights over the
    browser profile.
    """

    def __init__(self, headless=True, poll_seconds=5):
        # Shared session folder so you stay logged into both sites
        self.user_data_dir = os.path.expanduser("~/.jarvis_browser_profile")
        self.headless = headless
        self.poll_seconds = poll_seconds

        self._jobs = queue.Queue()
        self._thread = None
        self._start_lock = threading.Lock()
        self._ctx = None
        self._pages = {}

        self._alerts_on = False
        self._on_message = None
        self._watch = {"wa": True, "ig": True}

        self._wa_prev = {}
        self._wa_seeded = False
        self._ig_prev = {}
        self._ig_seeded = False
        self._retry_at = {"wa": 0.0, "ig": 0.0}
        self._login_warned = set()

        atexit.register(self.stop)

    # ================================================================== #
    # Worker thread plumbing
    # ================================================================== #
    def _launch(self, p, headless):
        args = list(LAUNCH_ARGS)
        if headless:
            # "New" headless behaves like a normal Chrome window. Playwright's
            # default headless gets blocked by WhatsApp Web.
            args.append("--headless=new")
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=False,
            args=args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 800},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return ctx

    def _ensure_started(self):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker_main, daemon=True, name="social-agent"
            )
            self._thread.start()

    def _fail_pending(self, exc):
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is _STOP:
                continue
            fut = job[0]
            if not fut.done():
                fut.set_exception(exc)

    def _worker_main(self):
        try:
            with sync_playwright() as p:
                self._ctx = self._launch(p, self.headless)
                self._pages = {}
                last_poll = 0.0
                last_active = time.time()
                try:
                    while True:
                        try:
                            job = self._jobs.get(timeout=0.5)
                        except queue.Empty:
                            job = None

                        if job is _STOP:
                            break

                        if job is not None:
                            fut, fn, args, kw = job
                            if fut.set_running_or_notify_cancel():
                                try:
                                    fut.set_result(fn(*args, **kw))
                                except BaseException as e:
                                    fut.set_exception(e)
                            last_active = time.time()

                        now = time.time()
                        if self._alerts_on and now - last_poll >= self.poll_seconds:
                            last_poll = last_active = now
                            self._poll_once()
                        elif not self._alerts_on and job is None and now - last_active > 60:
                            break  # nothing to do for a minute: close the browser
                finally:
                    try:
                        self._ctx.close()
                    except Exception:
                        pass
                    self._ctx = None
                    self._pages = {}
        except Exception as e:
            self._fail_pending(e)
        finally:
            self._fail_pending(_WorkerStopped())

    def _submit(self, fn, *args, timeout=90, **kw):
        """Runs fn on the browser thread and waits for its result."""
        for _ in range(3):
            self._ensure_started()
            fut = Future()
            self._jobs.put((fut, fn, args, kw))
            try:
                return fut.result(timeout=timeout)
            except _WorkerStopped:
                time.sleep(0.4)
        raise RuntimeError("the messaging browser keeps stopping")

    def _run(self, label, fn, *args, **kw):
        try:
            return self._submit(fn, *args, **kw)
        except Exception as e:
            return f"{label}: Failed. Error: {e}"

    def stop(self):
        """Closes the background browser (it restarts by itself on the next request)."""
        t = self._thread
        if t and t.is_alive():
            self._jobs.put(_STOP)
            t.join(timeout=20)

    def _page(self, kind):
        """One persistent tab per site (worker thread only)."""
        pg = self._pages.get(kind)
        if pg is None or pg.is_closed():
            used = list(self._pages.values())
            free = [p for p in self._ctx.pages if p not in used and not p.is_closed()]
            pg = free[0] if free else self._ctx.new_page()
            self._pages[kind] = pg
        return pg

    # ================================================================== #
    # Live alerts (public)
    # ================================================================== #
    def start_alerts(self, callback, whatsapp=True, instagram=True):
        """
        callback(text) is called, from the browser thread, whenever a new message
        shows up. Keep it quick (hand the speaking off to another thread).
        """
        self._on_message = callback
        self._watch = {"wa": whatsapp, "ig": instagram}
        self._alerts_on = True
        self._ensure_started()

    def stop_alerts(self):
        """Stops watching and closes the browser (e.g. for sleep mode)."""
        self._alerts_on = False
        self.stop()

    # ================================================================== #
    # Login helpers (run once, manually, with a visible window)
    # ================================================================== #
    @contextmanager
    def _visible_session(self):
        was_watching = self._alerts_on
        self.stop()  # the profile can only be open in one browser at a time
        try:
            with sync_playwright() as p:
                ctx = self._launch(p, headless=False)
                try:
                    yield ctx.pages[0] if ctx.pages else ctx.new_page()
                finally:
                    ctx.close()
        finally:
            if was_watching:
                self._ensure_started()

    def setup_whatsapp_login(self):
        """Opens WhatsApp Web so you can scan the QR code once."""
        print("\n[+] Opening WhatsApp Web...")
        with self._visible_session() as page:
            page.goto(WA_URL, wait_until="domcontentloaded")
            print("--> Scan the QR code with your phone, wait for your chats to load,")
            input("--> then press ENTER here: ")
        print("[+] WhatsApp session saved!\n")

    def setup_instagram_login(self):
        """Opens Instagram so you can log in / solve any CAPTCHA by hand."""
        print("\n[+] Opening browser for Instagram login...")
        with self._visible_session() as page:
            page.goto(IG_LOGIN_URL)
            print("\n========================================================")
            print("--> Log into Instagram now (solve any CAPTCHA).")
            print("--> Once you see your feed or DMs, come back and press ENTER.")
            print("========================================================\n")
            input("Press ENTER here after logging in: ")
        print("[+] Instagram session saved successfully!\n")

    # ================================================================== #
    # WhatsApp internals (worker thread)
    # ================================================================== #
    def _wa_ready(self, timeout=25000):
        """Makes sure the WhatsApp tab shows the chat list. Returns the page or None."""
        page = self._page("wa")
        if "web.whatsapp.com" not in page.url:
            page.goto(WA_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("#pane-side, div[aria-label='Chat list']", timeout=timeout)
            return page
        except PWTimeout:
            return None

    def _wa_page_for_poll(self):
        """Like _wa_ready but never makes a poll wait long or hammer the site."""
        page = self._page("wa")
        if "web.whatsapp.com" in page.url and page.locator("#pane-side").count() > 0:
            return page
        if time.time() < self._retry_at["wa"]:
            return None
        self._retry_at["wa"] = time.time() + 60
        page = self._wa_ready(timeout=15000)
        if page is None:
            self._warn_login_once("wa", "I can't see your WhatsApp chats. You may need to log in again.")
        return page

    @staticmethod
    def _wa_preview(raw, name, count):
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        if lines and re.fullmatch(r"\d+\+?", lines[-1]):
            lines.pop()  # the unread-count badge
        keep = [
            l for l in lines
            if l != name and not TIME_RE.match(l) and "unread message" not in l.lower()
        ]
        return " ".join(keep).strip()

    def _whatsapp_unread(self, page):
        """Returns {chat_name: {"count": int, "preview": str}} for chats with unread messages."""
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
                raw = row.inner_text(timeout=2000)
                unread[name] = {"count": n, "preview": self._wa_preview(raw, name, n)}
            except Exception:
                continue
        return unread

    def _wa_changes(self, page):
        current = self._whatsapp_unread(page)
        alerts = []
        for name, info in current.items():
            prev = self._wa_prev.get(name)
            preview = info["preview"]
            if re.search(r"typing|recording", preview, re.IGNORECASE):
                # the preview briefly shows "typing..." instead of the message
                info["preview"] = preview = prev["preview"] if prev else ""
            if self._wa_seeded:
                is_new = (
                    prev is None
                    or info["count"] > prev["count"]
                    or (preview and preview != prev["preview"])
                )
                if is_new:
                    alerts.append(("WhatsApp", name, self._fmt("WhatsApp", name, info["count"], preview)))
        self._wa_prev = current
        self._wa_seeded = True
        return alerts

    def _reset_wa(self, page):
        """Closes the open chat/search so new messages show as unread again."""
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            page.keyboard.press("Escape")
        except Exception:
            pass

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

    # ================================================================== #
    # Instagram internals (worker thread)
    # ================================================================== #
    def _dismiss_ig_popups(self, page):
        for _ in range(2):
            try:
                btn = page.get_by_role("button", name="Not Now")
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(500)
                else:
                    break
            except Exception:
                break

    def _ig_open_inbox(self, page, reload=False):
        """Goes to the inbox if needed. Returns False if you're not logged in."""
        if reload or "instagram.com/direct" not in page.url:
            page.goto(IG_INBOX_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        if "login" in page.url:
            return False
        self._dismiss_ig_popups(page)
        page.wait_for_selector("div[role='grid'], div[role='main']", timeout=15000)
        return True

    def _ig_page_for_poll(self):
        page = self._page("ig")
        if "instagram.com/direct" in page.url and "login" not in page.url:
            self._dismiss_ig_popups(page)
            return page
        if time.time() < self._retry_at["ig"]:
            return None
        self._retry_at["ig"] = time.time() + 60
        if self._ig_open_inbox(page):
            return page
        self._warn_login_once("ig", "Instagram isn't logged in. Run the Instagram login setup.")
        return None

    def _ig_unread_count(self, page):
        # NOTE: Instagram's class names (like _ac5f) are auto-generated and change
        # without warning. If this stops finding unread DMs, this is the line to fix.
        unread = page.locator(
            "div[role='listitem'] div._ac5f, div._ac5f, span[data-testid='unread-badge']"
        )
        return unread.count()

    def _ig_threads(self, page):
        """Returns {contact_name: latest_preview_text} from the inbox list."""
        out = {}
        anchors = page.locator("a[href^='/direct/t/']")
        for i in range(min(anchors.count(), 15)):
            try:
                text = anchors.nth(i).inner_text(timeout=1500)
            except Exception:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            lines = [l for l in lines if not IG_NOISE_RE.match(l)]
            if len(lines) < 2:
                continue
            name = lines[0]
            preview = re.sub(r"\s*·\s*\w+$", "", lines[1]).strip()
            out[name] = preview
        return out

    def _ig_changes(self, page):
        current = self._ig_threads(page)
        alerts = []
        if self._ig_seeded:
            for name, preview in current.items():
                if self._ig_prev.get(name) == preview:
                    continue
                if re.match(r"^you\b", preview, re.IGNORECASE):  # your own message
                    continue
                alerts.append(("Instagram", name, self._fmt("Instagram", name, 1, preview)))
        self._ig_prev.update(current)
        self._ig_seeded = True
        return alerts

    # ================================================================== #
    # Alert formatting + polling (worker thread)
    # ================================================================== #
    @staticmethod
    def _fmt(platform, name, count, preview):
        preview = (preview or "").strip()
        if len(preview) > 200:
            preview = preview[:200].rsplit(" ", 1)[0] + "..."
        if count > 1:
            head = f"You have {count} new {platform} messages from {name}"
            return f"{head}. The latest says: {preview}" if preview else head + "."
        head = f"You have a new {platform} message from {name}"
        return f"{head}, saying: {preview}" if preview else head + "."

    def _warn_login_once(self, key, text):
        if key not in self._login_warned:
            self._login_warned.add(key)
            self._deliver([("System", key, text)])

    def _deliver(self, alerts):
        cb = self._on_message
        if not alerts or not cb:
            return
        if len(alerts) > 3:
            names = ", ".join(a[1] for a in alerts[:5])
            alerts = [("Summary", "", f"You have new messages from {len(alerts)} chats: {names}.")]
        for _, _, text in alerts:
            try:
                cb(text)
            except Exception:
                pass

    def _poll_once(self):
        alerts = []
        if self._watch.get("wa"):
            try:
                page = self._wa_page_for_poll()
                if page:
                    alerts += self._wa_changes(page)
            except Exception:
                pass
        if self._watch.get("ig"):
            try:
                page = self._ig_page_for_poll()
                if page:
                    alerts += self._ig_changes(page)
            except Exception:
                pass
        self._deliver(alerts)

    # ================================================================== #
    # Jobs (run on the worker thread)
    # ================================================================== #
    def _job_check_whatsapp(self):
        page = self._wa_ready()
        if not page:
            return "WhatsApp: Could not load or requires QR scan."
        unread = self._whatsapp_unread(page)
        if not unread:
            return "WhatsApp: No unread messages."
        parts = [f"{name} ({info['count']})" for name, info in unread.items()]
        return f"WhatsApp: {len(unread)} unread chat(s) from " + ", ".join(parts)

    def _job_check_instagram(self):
        page = self._page("ig")
        if not self._ig_open_inbox(page):
            return "Instagram: Session expired or not logged in. Please run setup login."
        count = self._ig_unread_count(page)
        if count == 0:
            return "Instagram: No unread DMs found."
        return f"Instagram: You have approximately {count} unread DM conversation(s)."

    def _job_check_all(self):
        out = [self._job_check_whatsapp()]
        try:
            out.append(self._job_check_instagram())
        except Exception:
            out.append("Instagram: Could not check DMs.")
        return " | ".join(out)

    def _job_read_whatsapp(self, contact_name, limit, allow_partial):
        page = self._wa_ready()
        if not page:
            return "WhatsApp: Could not load or requires QR scan."
        try:
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
        finally:
            self._reset_wa(page)

    def _job_send_whatsapp(self, contact_name, message_text, allow_partial):
        page = self._wa_ready()
        if not page:
            return "Failed to send WhatsApp message: not loaded or requires QR scan."
        try:
            opened = self._open_whatsapp_chat(page, contact_name, allow_partial)
            if not opened:
                return f"Didn't send: no exact contact named {contact_name}. Nothing was sent."
            msg_box = page.locator(
                "footer div[contenteditable='true'], div[aria-label='Type a message']"
            ).first
            msg_box.click()
            msg_box.fill(message_text)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)  # give it a moment to actually go out
            return f"Successfully sent WhatsApp message to {opened}."
        finally:
            self._reset_wa(page)

    def _attach_image(self, page, path):
        """Opens the paperclip/plus menu and hands the file to WhatsApp."""
        attach = page.locator(
            "button[aria-label='Attach'], div[aria-label='Attach'], "
            "span[data-icon='plus-rounded'], span[data-icon='attach-menu-plus'], span[data-icon='clip']"
        ).first
        attach.click(timeout=8000)
        page.wait_for_timeout(600)

        file_input = page.locator("input[type='file'][accept*='image']").first
        if file_input.count() > 0:
            file_input.set_input_files(path)
        else:  # fall back to clicking the menu entry and answering the file dialog
            with page.expect_file_chooser(timeout=5000) as fc:
                page.get_by_text(re.compile(r"photos?\s*&\s*videos?", re.IGNORECASE)).first.click()
            fc.value.set_files(path)

    def _job_send_whatsapp_image(self, contact_name, path, caption, allow_partial):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return f"Didn't send: I can't find the file {os.path.basename(path)}."
        if not path.lower().endswith(IMG_EXT):
            return "Didn't send: that isn't an image type I can send (jpg, png, webp or gif)."
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            return "Didn't send: that image is over WhatsApp's 16 MB limit."

        page = self._wa_ready()
        if not page:
            return "Failed to send the image: WhatsApp not loaded or requires QR scan."
        try:
            opened = self._open_whatsapp_chat(page, contact_name, allow_partial)
            if not opened:
                return f"Didn't send: no exact contact named {contact_name}. Nothing was sent."

            self._attach_image(page, path)

            send_btn = page.locator(
                "div[aria-label='Send'], button[aria-label='Send'], span[data-icon='send']"
            ).last
            send_btn.wait_for(timeout=12000)          # the preview screen is up

            if caption:
                cap_box = page.locator(
                    "div[contenteditable='true'][aria-label*='caption' i], "
                    "div[contenteditable='true'][aria-placeholder*='caption' i]"
                ).first
                if cap_box.count() > 0:
                    cap_box.click()
                    cap_box.fill(caption)

            send_btn.click()
            page.wait_for_timeout(4000)               # let the upload finish before closing the chat
            return f"Successfully sent the image to {opened}."
        except PWTimeout:
            return "Failed to send the image: WhatsApp's attach screen didn't respond (its layout may have changed)."
        finally:
            self._reset_wa(page)

    # ================================================================== #
    # Public API (same names as before)
    # ================================================================== #
    def check_whatsapp_only(self):
        return self._run("WhatsApp", self._job_check_whatsapp)

    def check_instagram_only(self):
        return self._run("Instagram", self._job_check_instagram)

    def check_all_messages(self):
        return self._run("Messaging check", self._job_check_all)

    def read_whatsapp_chat(self, contact_name, limit=5, allow_partial=False):
        """Reads the last `limit` messages (this marks the chat as read on your phone too)."""
        return self._run("WhatsApp", self._job_read_whatsapp, contact_name, limit, allow_partial)

    def send_whatsapp(self, contact_name, message_text, allow_partial=False):
        """Sends a WhatsApp message (exact contact-name match by default)."""
        return self._run("WhatsApp", self._job_send_whatsapp, contact_name, message_text, allow_partial)

    def send_whatsapp_image(self, contact_name, image_path, caption="", allow_partial=False):
        """Sends an image file (jpg/png/webp/gif, up to 16 MB) with an optional caption."""
        return self._run("WhatsApp", self._job_send_whatsapp_image,
                         contact_name, image_path, caption, allow_partial)


if __name__ == "__main__":
    agent = SocialAgent(headless=False)  # visible while testing
    # First time only:
    # agent.setup_whatsapp_login()
    # agent.setup_instagram_login()
    agent.start_alerts(lambda t: print("ALERT:", t))
    print("Watching... send yourself a message from another phone. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop_alerts()