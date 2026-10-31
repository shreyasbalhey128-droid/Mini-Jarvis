import ctypes, math, os, queue, random, shutil, socket, threading, time, traceback
from collections import deque
import tkinter as tk
from tkinter import messagebox, ttk
import numpy as np
from PIL import Image, ImageOps, ImageTk

from reactors import (S, CX, CY, RING, R_BIG, R_SAT, ORDER, NAMES, DISPLAY, SUBTITLE, THEMES, RED, ORANGE,
                      Compositor, corrupt_text, mix, ic, CODE_POOL, CONTAIN_SECONDS)
from vision import grab_screen_raw
from ui_effects import Typewriter, drawer_step, menu_icon_lines, is_inside

try:
    import psutil  # type: ignore[reportMissingModuleSource]
except ImportError:
    psutil = None

FPS = 24                       # animation frame rate while awake (lower = cooler laptop)
SLEEP_FPS = 2                  # animation frame rate while sleeping
HOTKEY_CHAR = "J"              # global shortcut: Ctrl + Alt + J  (brings the window back)
HOTKEY_TEXT = "Ctrl+Alt+" + HOTKEY_CHAR
MIC_HOTKEY_CHAR = "M"          # global shortcut: Ctrl + Alt + M  (mutes / unmutes the microphone)
MIC_HOTKEY_TEXT = "Ctrl+Alt+" + MIC_HOTKEY_CHAR
INSTANCE_PORT = 47653          # used so a second launch just wakes the first one

BG_HEX, BG = "#03060c", (3, 6, 12)
KEY_HEX, KEY = "#05080f", (5, 8, 15)     # transparent colour for the floating orb
PANEL = "#0a1220"
DRAWER_W = 280                          # width of the slide-out menu
HUD_W, HUD_H = 640, 712
HUD_X0, HUD_Y0 = 20, 46                  # where the big picture sits on the canvas

BOOT_SCREEN = True             # show the Stark Industries start-up screen first
BOOT_AUTO_SECONDS = 1.6        # when every check is done, wait this long, then fade into the HUD
BOOT_MIN_SECONDS = 2.6         # the start-up screen stays at least this long
BOOT_ITEMS = [("vosk", "WAKE-WORD MODEL  (VOSK)"), ("mic", "MICROPHONE"),
              ("whisper", "SPEECH RECOGNITION  (WHISPER)"), ("piper", "VOICE ENGINE  (PIPER)"),
              ("ollama", "OLLAMA API"), ("model", "AI MODEL"), ("vision", "VISION MODEL  (OPTIONAL)")]
BOOT_SHORT = {"vosk": "Wake word", "mic": "Microphone", "whisper": "Speech recognition",
              "piper": "Voice engine", "ollama": "Ollama", "model": "AI model", "vision": "Vision model"}
BOOT_COL = {"pending": "#4a5a6a", "loading": "#ffb300", "ok": "#69f0ae", "warn": "#ffb300", "fail": "#ff5252"}


def boot_geometry(W, H, n=len(BOOT_ITEMS)):
    """Where everything on the start-up screen goes (one place, so the layout is easy to tweak)."""
    x0, x1 = W * 0.54, W * 0.95
    top, step = H * 0.30, min(46.0, H * 0.062)
    yb = top + n * step + 14
    return dict(cx=W * 0.29, cy=H * 0.5, x0=x0, x1=x1, t1=(x0, H * 0.13), t2=(x0, H * 0.13 + 40),
                rows=[top + i * step for i in range(n)], bar=(x0, yb, x1, yb + 10),
                sum=(x0, yb + 36), btn=((x0 + x1) / 2, yb + 92))


LABELS = {"listening": "LISTENING", "thinking": "PROCESSING", "speaking": "SPEAKING", "error": "ERROR"}
GOOD, WARN, BAD = "#69f0ae", "#ffb300", "#ff5252"
ROWS = (("CPU", "cpu"), ("RAM", "ram"), ("DSK", "disk"), ("BAT", "bat"))

_instance_sock = None


def claim_instance(on_show):
    """True if we are the only copy. Otherwise wakes the running copy and returns False."""
    global _instance_sock
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        s.listen(2)
    except OSError:
        try:
            c = socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1)
            c.sendall(b"show")
            c.close()
        except OSError:
            pass
        return False
    _instance_sock = s

    def loop():
        while True:
            try:
                conn, _ = s.accept()
                conn.recv(16)
                conn.close()
                on_show()
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()
    return True


def _raise(widget):
    """Raises a widget above the others. Do not call widget.lift() on a Canvas: for a Canvas that means
    'raise a drawing item' and fails with 'wrong # args'. Misc.lift works for every widget."""
    tk.Misc.lift(widget)


def _hexcol(c):
    return "#%02x%02x%02x" % ic(c)


# ---------------- system stats (works with or without psutil) ----------------
class _MEM(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


class _PWR(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                ("res", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]


_prev_cpu = None


def _win_cpu():
    global _prev_cpu
    idle, kern, user = _FT(), _FT(), _FT()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    cur = tuple((f.hi << 32) | f.lo for f in (idle, kern, user))
    prev, _prev_cpu = _prev_cpu, cur
    if prev is None:
        return 0.0
    di, dk, du = (cur[i] - prev[i] for i in range(3))
    total = dk + du
    return max(0.0, min(100.0, 100.0 * (total - di) / total)) if total > 0 else 0.0


def get_stats():
    """Returns cpu/ram/disk/bat percentages (None if unavailable) and charging flag."""
    out = {"cpu": None, "ram": None, "disk": None, "bat": None, "chg": False}
    try:
        out["cpu"] = psutil.cpu_percent(None) if psutil else _win_cpu()
    except Exception:
        pass
    try:
        if psutil:
            out["ram"] = psutil.virtual_memory().percent
        else:
            m = _MEM()
            m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            out["ram"] = float(m.dwMemoryLoad)
    except Exception:
        pass
    try:
        u = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        out["disk"] = u.used / u.total * 100.0
    except Exception:
        pass
    try:
        if psutil:
            b = psutil.sensors_battery()
            if b:
                out["bat"], out["chg"] = b.percent, bool(b.power_plugged)
        else:
            p = _PWR()
            ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p))
            if p.pct != 255:
                out["bat"], out["chg"] = float(p.pct), p.ac == 1
    except Exception:
        pass
    return out


# ---------------- main window ----------------
class JarvisUI:
    ORB = 170
    CHAT_W = 340
    CHAT_H = 190
    BOOT_R = 440
    VIS_W = 300
    VIS_H = 170

    def __init__(self):
        self.q = queue.Queue()
        for n in ("on_text", "on_mute", "on_volume", "on_persona", "on_select_conv",
                  "on_new_conv", "on_delete_conv", "on_mic", "on_spk", "on_refresh", "on_sleep", "on_vision", "on_mic_mute",
                  "on_contain_start"):
            setattr(self, n, None)
        self.level = self.lvl = 0.0
        self.state, self.mode = "sleep", "main"
        self.sleeping = False
        self.active = "jarvis"
        self.pname = NAMES[self.active]
        self.g = self.g_t = 0.0           # glitch / takeover level (0..1)
        self.shock = 0.0                  # shockwave when Ultron arrives
        self.alert = False                # containment alarm
        self.containment_progress = 0.0      # 0..1 for the containment animation
        self.hover = None
        self._last_render = 0.0
        self._lock_until = 0.0
        self.muted, self._ignore_vol, self._last_vol = False, False, 100
        self.mic_muted = False            # microphone switched off
        self.t0 = self.last = time.time()
        self._full, self._last_stats = False, 0.0
        self.conv_ids, self.conv_open = [], True
        self.drawer_open, self._drawer_p, self._drawer_t, self._drawer_job = False, 0.0, 0.0, None
        self._menu_hover = 0.0                # 0 / 1: mouse over the menu button
        self._glow, self._glow_col = 0.0, None   # message bar border glow
        self._ph_on = False                   # the grey hint text is showing in the message bar
        self.chat_open, self._click_job, self._moved = False, None, False
        self._px = self._py = self._dx = self._dy = 0
        self._ring_rot = 0.0
        self.vis_open, self.vis_scan, self._vis_job = False, False, None
        self.boot_active = self.boot_fading = False
        self._code_t = 0.0
        self._cache = {}
        self._stat_tgt = {n: 0.0 for n, _ in ROWS}
        self._stat_cur = {n: 0.0 for n, _ in ROWS}
        self.hist = {"cpu": deque([0] * 60, maxlen=60), "ram": deque([0] * 60, maxlen=60)}
        if psutil:
            psutil.cpu_percent(None)

        # one record per assistant: where it is, how big, its colours
        self.ent = {}
        slots = self._slots()
        for i, k in enumerate(ORDER):
            act = k == self.active
            self.ent[k] = dict(ang=slots.get(k, 0.0), rad=0.0 if act else RING, R=R_BIG if act else R_SAT,
                               x=CX, y=CY, ph=i * 1.7, contain=1.0 if k == "ultron" else 0.0,
                               col=list(THEMES[k][0]["listening" if act else "sleep"]),
                               acc=list(THEMES[k][1]["listening" if act else "sleep"]))
        self._place_entities()

        self.root = tk.Tk()
        self.root.title("E.C.H.O")
        self.root.geometry("1280x830")
        self.root.minsize(1020, 790)
        self.root.configure(bg=BG_HEX)
        self.root.bind("<Escape>", lambda e: self._toggle_drawer(False) if self.drawer_open
                       else self.hide_to_background())
        self.root.bind("<Control-q>", lambda e: self._close())
        self.root.bind("<F11>", self._toggle_full)
        self.root.bind("<F2>", lambda e: self.show_orb_now())
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_background)    # X hides, it does not quit

        sty = ttk.Style(self.root)
        try:
            sty.theme_use("clam")
        except tk.TclError:
            pass
        sty.configure("J.TCombobox", fieldbackground="#0f1c2e", background="#10243a",
                      foreground="#e0f7fa", arrowcolor="#4fc3f7", bordercolor="#1b3a57",
                      lightcolor="#0f1c2e", darkcolor="#0f1c2e")
        sty.map("J.TCombobox", fieldbackground=[("readonly", "#0f1c2e")],
                foreground=[("readonly", "#e0f7fa")], selectbackground=[("readonly", "#0f1c2e")],
                selectforeground=[("readonly", "#e0f7fa")])
        sty.configure("J.Vertical.TScrollbar", background="#16365a", troughcolor="#0a1220",
                      bordercolor="#0a1220", arrowcolor="#4fc3f7", lightcolor="#16365a",
                      darkcolor="#16365a", relief="flat", gripcount=0)
        sty.map("J.Vertical.TScrollbar", background=[("active", "#1f6f9c"), ("pressed", "#00b8d4")])
        for k, v in (("background", "#0f1c2e"), ("foreground", "#e0f7fa"),
                     ("selectBackground", "#16365a"), ("selectForeground", "#00e5ff")):
            self.root.option_add(f"*TCombobox*Listbox.{k}", v)

        def btn(parent, text, cmd, fg="#4fc3f7", **kw):
            b = tk.Button(parent, text=text, command=cmd, bg="#10243a", fg=fg, bd=0,
                          activebackground="#16365a", activeforeground="#ffffff",
                          font=("Consolas", 9, "bold"), cursor="hand2", **kw)
            b.bind("<Enter>", lambda e, b=b: b.configure(bg="#16365a"))      # lights up under the mouse
            b.bind("<Leave>", lambda e, b=b: b.configure(bg="#10243a"))
            return b

        def lab(parent, text, fg="#4fc3f7", size=11, bold=True):
            return tk.Label(parent, text=text, bg=PANEL, fg=fg,
                            font=("Consolas", size, "bold" if bold else "normal"))

        # ======== left sidebar: conversations, audio devices ========
        # It is a slide-out menu now: hidden until you tap MENU (top left), see _toggle_drawer().
        side = self.side = tk.Frame(self.root, bg=PANEL, width=DRAWER_W)
        side.pack_propagate(False)
        tk.Frame(side, bg="#16365a", width=2).pack(side="right", fill="y")      # glowing edge
        tk.Frame(side, bg=PANEL, height=52).pack(fill="x")                      # room for the MENU button

        tk.Label(side, text="Tap a core in the middle to switch assistant.", bg=PANEL,
                 fg="#5c7a8c", font=("Consolas", 9), wraplength=220, justify="left"
                 ).pack(anchor="w", padx=14, pady=(14, 0))

        aud = tk.Frame(side, bg=PANEL)
        aud.pack(side="bottom", fill="x", padx=14, pady=(0, 14))
        lab(aud, "AUDIO DEVICES").pack(anchor="w", pady=(0, 4))
        lab(aud, "Microphone", "#5c7a8c", 9, False).pack(anchor="w")
        self.mic_box = ttk.Combobox(aud, state="readonly", style="J.TCombobox", font=("Consolas", 9))
        self.mic_box.pack(fill="x", pady=(0, 6))
        self.mic_box.bind("<<ComboboxSelected>>",
                          lambda e: self.on_mic and self.on_mic(self.mic_box.get()))
        lab(aud, "Speaker", "#5c7a8c", 9, False).pack(anchor="w")
        self.spk_box = ttk.Combobox(aud, state="readonly", style="J.TCombobox", font=("Consolas", 9))
        self.spk_box.pack(fill="x", pady=(0, 6))
        self.spk_box.bind("<<ComboboxSelected>>",
                          lambda e: self.on_spk and self.on_spk(self.spk_box.get()))
        btn(aud, "REFRESH DEVICES", lambda: self.on_refresh and self.on_refresh(),
            pady=4).pack(fill="x")

        hd = tk.Frame(side, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(16, 4))
        self.conv_btn = tk.Button(hd, text="\u25be CONVERSATIONS", command=self._toggle_convs,
                                  bg=PANEL, fg="#4fc3f7", bd=0, activebackground=PANEL,
                                  activeforeground="#00e5ff", font=("Consolas", 11, "bold"),
                                  cursor="hand2")
        self.conv_btn.pack(side="left")
        btn(hd, "+ NEW", self._new_conv, padx=6).pack(side="right")

        self.conv_box = tk.Frame(side, bg=PANEL)
        self.conv_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        btn(self.conv_box, "DELETE SELECTED", self._delete_conv, fg="#ff8a80",
            pady=3).pack(side="bottom", fill="x", pady=(6, 0))
        sb = ttk.Scrollbar(self.conv_box, orient="vertical", style="J.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.conv_list = tk.Listbox(self.conv_box, bg="#0f1c2e", fg="#cfe9f5", bd=0,
                                    highlightthickness=0, selectbackground="#16365a",
                                    selectforeground="#00e5ff", font=("Consolas", 9),
                                    activestyle="none", exportselection=False,
                                    yscrollcommand=sb.set)
        self.conv_list.pack(side="left", fill="both", expand=True)
        sb.config(command=self.conv_list.yview)
        self.conv_list.bind("<<ListboxSelect>>", self._on_conv_select)

        # ======== right panel: stats, voice, transcript, typing ========
        right = tk.Frame(self.root, bg=PANEL, width=340)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        top = tk.Frame(right, bg=PANEL)
        top.pack(fill="x", padx=16, pady=(14, 0))
        lab(top, "SYSTEM", size=12).pack(side="left")
        btn(top, "ORB  (F2)", self.show_orb_now, padx=8).pack(side="right")
        self.sleep_btn = btn(top, "SLEEP", self._toggle_sleep, fg="#b388ff", padx=8)
        self.sleep_btn.pack(side="right", padx=(0, 6))
        vr = tk.Frame(right, bg=PANEL)                       # let him see: only when you press these
        vr.pack(fill="x", padx=16, pady=(8, 0))
        btn(vr, "SEE SCREEN", lambda: self.on_vision and self.on_vision("screen"),
            fg="#69f0ae", padx=8, pady=3).pack(side="left")
        btn(vr, "CAMERA", lambda: self.on_vision and self.on_vision("camera"),
            fg="#69f0ae", padx=8, pady=3).pack(side="left", padx=(6, 0))
        self.mic_btn = btn(vr, "MIC ON", self._toggle_mic, fg=GOOD, padx=8, pady=3)
        self.mic_btn.pack(side="left", padx=(6, 0))

        s = self.stats = tk.Canvas(right, width=308, height=168, bg=PANEL, highlightthickness=0)
        s.pack(padx=16, pady=(6, 4))
        self.stat_bar, self.stat_txt = {}, {}
        for i, (name, _) in enumerate(ROWS):
            y = 14 + i * 26
            s.create_text(0, y, text=name, anchor="w", fill="#5c7a8c", font=("Consolas", 10, "bold"))
            s.create_rectangle(44, y - 5, 230, y + 5, outline="#1b3a57", fill="#0f1c2e")
            self.stat_bar[name] = s.create_rectangle(44, y - 5, 44, y + 5, outline="", fill=GOOD)
            self.stat_txt[name] = s.create_text(240, y, text="--", anchor="w",
                                                fill="#cfe9f5", font=("Consolas", 10))
        s.create_rectangle(0, 114, 308, 162, outline="#1b3a57")
        s.create_text(4, 108, text="CPU", anchor="w", fill="#00e5ff", font=("Consolas", 8, "bold"))
        s.create_text(36, 108, text="RAM", anchor="w", fill="#b388ff", font=("Consolas", 8, "bold"))
        flat = [v for i in range(60) for v in (i * 308 / 59, 160)]
        self.spark = {"cpu": s.create_line(*flat, fill="#00e5ff", width=1),
                      "ram": s.create_line(*flat, fill="#b388ff", width=1)}

        vf = tk.Frame(right, bg=PANEL)
        vf.pack(fill="x", padx=16, pady=(0, 6))
        self.mute_btn = btn(vf, "VOICE ON", self._toggle_mute, fg=GOOD, width=10)
        self.mute_btn.pack(side="left")
        self.vol_lbl = lab(vf, "100%", "#cfe9f5", 10, False)
        self.vol_lbl.configure(width=5)
        self._ignore_vol = True
        self.vol = tk.Scale(vf, from_=0, to=150, orient="horizontal", showvalue=False, length=150,
                            bg=PANEL, troughcolor="#0f1c2e", highlightthickness=0, bd=0,
                            sliderrelief="flat", activebackground="#00e5ff", command=self._on_vol)
        self.vol.set(100)
        self.vol.pack(side="left", padx=(10, 6))
        self.vol_lbl.pack(side="left")
        self._ignore_vol = False

        lab(right, "TRANSCRIPT", size=12).pack(anchor="w", padx=16, pady=(8, 6))
        self.log = tk.Text(right, bg=PANEL, fg="#cfe9f5", bd=0, wrap="word",
                           font=("Consolas", 11), state="disabled", highlightthickness=0,
                           padx=8, pady=6, spacing1=1, spacing3=1, insertwidth=0)
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for tag, color, hdr_col in (("you", "#e0f7fa", "#00e5ff"), ("ai", "#d7ffe9", "#69f0ae"),
                                    ("sys", "#9aa7b4", "#ff8a80")):
            self.log.tag_configure(tag + "_h", foreground=hdr_col, font=("Consolas", 9, "bold"), spacing1=10)
            self.log.tag_configure(tag, foreground=color, lmargin1=14, lmargin2=14)
        # the assistant's answers appear word by word with a fade-in and a caret (see ui_effects.py)
        self.tw = Typewriter(self.log, self.root.after,
                             fast=lambda: self.mode != "main" or self.root.state() == "iconic",
                             caret_off=PANEL)

        # ======== centre: the arc reactors ========
        center = tk.Frame(self.root, bg=BG_HEX)
        center.pack(side="left", fill="both", expand=True)

        # message bar under the reactors (packed first so the HUD gets the space that is left)
        bar = self.bar = tk.Frame(center, bg=BG_HEX)
        bar.pack(side="bottom", fill="x", pady=(0, 14))
        EBG = "#0a1626"
        glow = self.entry_glow = tk.Frame(bar, bg="#1b3a57")          # 1-px border that glows while you type
        glow.pack()
        row = tk.Frame(glow, bg=EBG)
        row.pack(padx=1, pady=1)
        self.prompt_lbl = tk.Label(row, text="\u203a", bg=EBG, fg="#4fc3f7", font=("Consolas", 18, "bold"))
        self.prompt_lbl.pack(side="left", padx=(14, 4))
        self.entry = tk.Entry(row, width=50, bg=EBG, fg="#e0f7fa", insertbackground="#00e5ff", bd=0,
                              font=("Consolas", 12), highlightthickness=0, insertwidth=2)
        self.entry.pack(side="left", ipady=11)
        self.send_btn = tk.Button(row, text="SEND  \u25b8", command=self._submit, bg=EBG, fg="#00e5ff", bd=0,
                                  activebackground="#16365a", activeforeground="#ffffff",
                                  font=("Consolas", 10, "bold"), cursor="hand2", padx=16)
        self.send_btn.pack(side="left", fill="y", padx=(8, 0))
        self.send_btn.bind("<Enter>", lambda e: self.send_btn.configure(bg="#10243a"))
        self.send_btn.bind("<Leave>", lambda e: self.send_btn.configure(bg=EBG))
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<FocusIn>", self._entry_focus_in)
        self.entry.bind("<FocusOut>", self._entry_focus_out)

        c = self.canvas = tk.Canvas(center, width=HUD_W, height=HUD_H, bg=BG_HEX, highlightthickness=0)
        c.pack(expand=True)
        self.comp = Compositor(S, bg=True)
        self.hud_photo = ImageTk.PhotoImage(Image.new("RGB", (S, S), BG))
        c.create_image(HUD_X0, HUD_Y0, image=self.hud_photo, anchor="nw")
        self.title = c.create_text(HUD_W // 2, 16, text="", font=("Segoe UI Light", 18), fill="#4fc3f7")
        self.title2 = c.create_text(HUD_W // 2, 36, text="", font=("Consolas", 9), fill="#5c7a8c")
        self.labels = {k: c.create_text(0, 0, text=DISPLAY[k], font=("Consolas", 8, "bold"),
                                        fill="#5c7a8c", state="hidden") for k in ORDER}
        self.status = c.create_text(HUD_W // 2, 662, text="", font=("Segoe UI", 13, "bold"), fill="#4fc3f7")
        c.create_text(165, 684, text="SIGNAL", anchor="e", font=("Consolas", 9), fill="#5c7a8c")
        c.create_rectangle(175, 679, 345, 689, outline="#1b3a57", fill="#0a1220")
        self.sig = c.create_rectangle(175, 679, 175, 689, outline="", fill="#00e5ff")
        self.clock = c.create_text(HUD_W // 2, 702, text="", font=("Consolas", 10), fill="#5c7a8c")
        self.code_l = c.create_text(6, 150, text="", anchor="nw", justify="left", font=("Consolas", 8),
                                    fill="#7a1a22", state="hidden")
        self.code_r = c.create_text(HUD_W - 6, 400, text="", anchor="ne", justify="right",
                                    font=("Consolas", 8), fill="#7a1a22", state="hidden")
        c.bind("<Motion>", self._hud_motion)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        c.bind("<Button-1>", self._hud_click)
        self._refresh_titles(0.0)

        # the MENU button (top left) that slides the menu out; it turns into an X while it is open
        mb = self.menu_btn = tk.Canvas(self.root, width=116, height=34, bg=BG_HEX, highlightthickness=0,
                                       cursor="hand2")
        self.menu_bars = [mb.create_line(0, 0, 0, 0, width=2, capstyle="round") for _ in range(3)]
        self.menu_txt = mb.create_text(48, 17, text="MENU", anchor="w", font=("Consolas", 10, "bold"))
        mb.place(x=14, y=12)
        mb.bind("<Button-1>", lambda e: self._toggle_drawer())
        mb.bind("<Enter>", self._menu_enter)
        mb.bind("<Leave>", self._menu_leave)
        self._draw_menu_icon()
        _raise(mb)
        self.root.bind("<Button-1>", self._outside_click, add="+")     # tap anywhere else = close the menu
        self.root.bind("<F1>", lambda e: self._toggle_drawer())

        # ======== floating orb ========
        o = self.orb = tk.Toplevel(self.root)
        o.withdraw()
        o.overrideredirect(True)
        o.attributes("-topmost", True)
        o.configure(bg=KEY_HEX)
        try:
            o.attributes("-transparentcolor", KEY_HEX)
        except tk.TclError:
            pass
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        o.geometry(f"{self.ORB}x{self.ORB}+{sw - self.ORB - 40}+{sh - self.ORB - 100}")
        self.orb_c = Compositor(self.ORB, bg=False, key=KEY)
        self.orb_photo = ImageTk.PhotoImage(Image.new("RGB", (self.ORB, self.ORB), KEY))
        ol = tk.Label(o, image=self.orb_photo, bd=0, highlightthickness=0, bg=KEY_HEX)
        ol.pack()
        ol.bind("<ButtonPress-1>", self._orb_press)
        ol.bind("<B1-Motion>", self._orb_drag)
        ol.bind("<ButtonRelease-1>", self._orb_release)      # 1 tap = chat box, 2 taps = full HUD
        menu = tk.Menu(o, tearoff=0)
        menu.add_command(label="Open chat box", command=self._toggle_chat)
        menu.add_command(label="Open full HUD", command=self.show_main_now)
        menu.add_command(label="Look at my screen", command=lambda: self.on_vision and self.on_vision("screen"))
        menu.add_command(label="Look through the camera", command=lambda: self.on_vision and self.on_vision("camera"))
        menu.add_command(label="Hide to background", command=self.hide_to_background)
        menu.add_command(label="Sleep / wake up", command=self._toggle_sleep)
        menu.add_command(label="Mute / unmute voice", command=self._toggle_mute)
        menu.add_command(label="Mute / unmute my microphone", command=self._toggle_mic)
        menu.add_command(label="Quit completely", command=self._close)
        ol.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        # ======== quick chat box that opens next to the orb ========
        ch = self.chat = tk.Toplevel(self.root)
        ch.withdraw()
        ch.overrideredirect(True)
        ch.attributes("-topmost", True)
        ch.configure(bg="#1b3a57")                            # 1px border
        inner = tk.Frame(ch, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        chdr = tk.Frame(inner, bg=PANEL)
        chdr.pack(fill="x", padx=10, pady=(8, 2))
        self.chat_name = tk.Label(chdr, text="ECHO", bg=PANEL, fg="#00e5ff",
                                  font=("Consolas", 11, "bold"))
        self.chat_name.pack(side="left")
        self.chat_status = tk.Label(chdr, text="", bg=PANEL, fg="#5c7a8c", font=("Consolas", 9))
        self.chat_status.pack(side="left", padx=8)
        btn(chdr, "\u2715", self._hide_chat, fg="#ff8a80", padx=6).pack(side="right")
        btn(chdr, "HUD", self.show_main_now, padx=6).pack(side="right", padx=(0, 6))
        body = tk.Frame(inner, bg=PANEL, height=92)
        body.pack(fill="x", padx=10)
        body.pack_propagate(False)
        self.chat_reply = tk.Label(body, text="Type below to talk to the active assistant.",
                                   bg=PANEL, fg="#9aa7b4", justify="left", anchor="nw",
                                   wraplength=self.CHAT_W - 34, font=("Consolas", 10))
        self.chat_reply.pack(fill="both", expand=True)
        crow = tk.Frame(inner, bg=PANEL)
        crow.pack(fill="x", padx=10, pady=(4, 10))
        self.chat_entry = tk.Entry(crow, bg="#0f1c2e", fg="#e0f7fa", insertbackground="#00e5ff", bd=0,
                                   font=("Consolas", 11), highlightthickness=1,
                                   highlightbackground="#1b3a57", highlightcolor="#00e5ff")
        self.chat_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        self.chat_entry.bind("<Return>", self._submit_chat)
        self.chat_entry.bind("<Button-1>", lambda e: self.chat_entry.focus_force())
        ch.bind("<Escape>", lambda e: self._hide_chat())
        btn(crow, "SEND", self._submit_chat, fg="#00e5ff", padx=10, pady=4).pack(side="right")

        # ======== small window that shows what he is looking at ========
        v = self.vis = tk.Toplevel(self.root)
        v.withdraw()
        v.overrideredirect(True)
        v.attributes("-topmost", True)
        v.configure(bg="#1b3a57")
        vin = tk.Frame(v, bg=PANEL)
        vin.pack(fill="both", expand=True, padx=1, pady=1)
        self.vis_title = tk.Label(vin, text="", bg=PANEL, fg="#69f0ae", font=("Consolas", 10, "bold"))
        self.vis_title.pack(anchor="w", padx=10, pady=(8, 4))
        self.vis_cv = tk.Canvas(vin, width=self.VIS_W, height=self.VIS_H, bg="#05080f", highlightthickness=0)
        self.vis_cv.pack(padx=10)
        self.vis_img = self.vis_cv.create_image(self.VIS_W // 2, self.VIS_H // 2, anchor="center")
        self.vis_line = self.vis_cv.create_line(-10, -10, -10, -10, fill="#69f0ae", width=2)
        tk.Label(vin, text="click to close", bg=PANEL, fg="#5c7a8c", font=("Consolas", 8)
                 ).pack(anchor="e", padx=10, pady=(2, 6))
        for w_ in (v, vin, self.vis_cv):
            w_.bind("<Button-1>", lambda e: self._hide_vision())

        if BOOT_SCREEN:
            self._build_boot()                         # on top of everything, until the checks are done
        threading.Thread(target=self._hotkey_loop, daemon=True).start()
        self._show_placeholder()
        self.root.after(200, self._fx_tick)
        self.root.after(33, self._tick)

    # ---------- thread-safe API ----------
    def _put(self, kind, a=None, b=None):
        self.q.put((kind, a, b))

    def set_state(self, state): self._put("state", state)
    def log_line(self, who, text, persona=None):
        self._put("log", who, text if persona is None else (text, persona))    # persona = who is speaking
    def set_voice(self, muted, volume): self._put("voice", muted, volume)
    def set_sleep(self, on): self._put("sleep", on)
    def set_persona(self, key): self._put("persona", key)           # key = "jarvis", "friday", ...
    def set_glitch(self, level): self._put("glitch", level)         # 0 = clean .. 1 = Ultron takes over
    def contain_ultron(self): self._put("contain_ultron")           # start the containment rays
    def set_alert(self, on): self._put("alert", on)                 # containment alarm
    def set_conversations(self, items, active_id): self._put("convs", items, active_id)
    def set_devices(self, ins, outs, cur_in, cur_out): self._put("devices", (ins, outs), (cur_in, cur_out))
    def load_transcript(self, items): self._put("transcript", items)
    def boot_status(self, key, state, detail=""): self._put("boot", key, (state, detail))   # start-up check result
    def skip_boot(self): self._put("boot_skip")
    def set_mic(self, muted): self._put("mic", muted)                      # microphone switched off / on
    def show_vision(self, img, label): self._put("vision", img, label)      # shows the picture + scan line
    def end_vision(self, seconds=8): self._put("vision_end", seconds)       # stops scanning, closes later

    def grab_screen(self, timeout=8):
        """Takes a screenshot (call from a non-UI thread). Echo' own windows hide for a moment first."""
        ev, box = threading.Event(), {}
        self._put("grab", ev, box)
        if not ev.wait(timeout):
            raise RuntimeError("The screen capture timed out.")
        if "err" in box:
            raise RuntimeError("I could not capture the screen: " + box["err"])
        return box["img"]

    def show_orb(self): self._put("mode", "orb")
    def show_chat(self): self._put("mode", "chat")
    def show_main(self): self._put("mode", "main")
    def quit(self): self._put("quit")
    def run(self): self.root.mainloop()

    # ---------- global hotkey (Ctrl+Alt+J) ----------
    def _hotkey_loop(self):
        try:
            import ctypes.wintypes as wt
            u = ctypes.windll.user32
            MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
            mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
            ok1 = u.RegisterHotKey(None, 1, mods, ord(HOTKEY_CHAR))          # wake / show the window
            ok2 = u.RegisterHotKey(None, 2, mods, ord(MIC_HOTKEY_CHAR))      # mute / unmute the microphone
            if not ok1:
                self.log_line("sys", f"The shortcut {HOTKEY_TEXT} is already used by another program.")
            if not ok2:
                self.log_line("sys", f"The shortcut {MIC_HOTKEY_TEXT} is already used by another program.")
            if not (ok1 or ok2):
                return
            msg = wt.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY:
                    self._put("hotkey" if msg.wParam == 1 else "hotkey_mic")
        except Exception as e:
            print("Global shortcut unavailable:", e)

    # ---------- window helpers ----------
    def _close(self):
        self.root.destroy()

    def _toggle_full(self, _=None):
        self._full = not self._full
        self.root.attributes("-fullscreen", self._full)

    def show_orb_now(self):
        self.mode = "orb"
        self.root.withdraw()
        self.orb.deiconify()
        self.orb.lift()

    def show_main_now(self):
        if self._click_job is not None:
            self.root.after_cancel(self._click_job)
            self._click_job = None
        self._hide_chat()
        self.mode = "main"
        self.orb.withdraw()
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.attributes("-topmost", True)               # pop in front of other windows ...
        self.root.after(250, lambda: self.root.attributes("-topmost", False))   # ... then behave normally
        self.root.focus_force()

    def hide_to_background(self):
        self._hide_chat()
        self.mode = "hidden"
        self.root.withdraw()
        self.orb.withdraw()
        self._toast(f"Still running in the background. Press {HOTKEY_TEXT} to open it.")

    def _toast(self, text):
        try:
            t = tk.Toplevel(self.root)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.configure(bg="#1b3a57")
            tk.Label(t, text=text, bg=PANEL, fg="#cfe9f5", font=("Consolas", 10), padx=14, pady=10
                     ).pack(padx=1, pady=1)
            t.update_idletasks()
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            t.geometry(f"+{sw - t.winfo_width() - 24}+{sh - t.winfo_height() - 70}")
            t.after(3600, t.destroy)
        except Exception:
            pass

    # ---------- orb: drag, 1 tap = chat box, 2 taps = full HUD ----------
    def _orb_press(self, e):
        self._px, self._py = e.x_root, e.y_root
        self._dx, self._dy = e.x, e.y
        self._moved = False

    def _orb_drag(self, e):
        if abs(e.x_root - self._px) + abs(e.y_root - self._py) > 4:
            self._moved = True
        if self._moved:
            self.orb.geometry(f"+{self.orb.winfo_x() + e.x - self._dx}+{self.orb.winfo_y() + e.y - self._dy}")
            if self.chat_open:
                self._place_chat()

    def _orb_release(self, e):
        if self._moved:
            return
        if self._click_job is not None:               # second tap: open the full HUD
            self.root.after_cancel(self._click_job)
            self._click_job = None
            self.show_main_now()
        else:                                         # first tap: wait a moment for a second one
            self._click_job = self.root.after(300, self._single_tap)

    def _single_tap(self):
        self._click_job = None
        self._toggle_chat()

    # ---------- quick chat box ----------
    def _place_chat(self):
        ox, oy = self.orb.winfo_x(), self.orb.winfo_y()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = self.CHAT_W, self.CHAT_H
        x = ox - w - 6 if ox + self.ORB / 2 > sw / 2 else ox + self.ORB + 6
        y = oy + self.ORB - h
        x = max(4, min(x, sw - w - 4))
        y = max(4, min(y, sh - h - 48))
        self.chat.geometry(f"{w}x{h}+{x}+{y}")

    def _toggle_chat(self):
        if self.chat_open:
            self._hide_chat()
        else:
            self._show_chat()

    def _show_chat(self):
        self.chat_open = True
        self._place_chat()
        self.chat.deiconify()
        self.chat.lift()
        self._refresh_chat_header()
        self.chat.focus_force()
        self.chat_entry.focus_force()

    def _hide_chat(self):
        self.chat_open = False
        self.chat.withdraw()

    def _refresh_chat_header(self):
        if not self.chat_open:
            return
        self.chat_name.configure(text=self.pname.upper(), fg=_hexcol(THEMES[self.active][0]["listening"]))
        if self.sleeping:
            txt = "SLEEPING"
        elif self.state == "sleep":
            txt = "STANDBY"
        else:
            txt = LABELS.get(self.state, "")
        self.chat_status.configure(text=txt)

    def _set_reply(self, who, text):
        text = text if len(text) <= 330 else text[:327] + "..."
        self.chat_reply.configure(text=f"{who}: {text}",
                                  fg="#e0f7fa" if who == "YOU" else "#9ee8c4")

    def _submit_chat(self, _=None):
        text = self.chat_entry.get().strip()
        if not text:
            return
        self.chat_entry.delete(0, "end")
        self._append("you", text)
        self._set_reply("YOU", text)
        if self.on_text:
            self.on_text(text)

    # ---------- sidebar handlers ----------
    def _toggle_convs(self):
        self.conv_open = not self.conv_open
        if self.conv_open:
            self.conv_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        else:
            self.conv_box.pack_forget()
        self._refresh_conv_btn()

    def _refresh_conv_btn(self):
        self.conv_btn.configure(text=("\u25be" if self.conv_open else "\u25b8") +
                                f" CONVERSATIONS ({len(self.conv_ids)})")

    def _on_conv_select(self, _=None):
        sel = self.conv_list.curselection()
        if sel and self.on_select_conv:
            self.on_select_conv(self.conv_ids[sel[0]])
            self.root.after(260, lambda: self._toggle_drawer(False))     # picked one: slide the menu away

    def _new_conv(self):
        if self.on_new_conv:
            self.on_new_conv()
        self.root.after(220, lambda: self._toggle_drawer(False))

    # ---------- slide-out menu ----------
    def _toggle_drawer(self, want=None):
        try:
            self._toggle_drawer_impl(want)
        except Exception:
            traceback.print_exc()

    def _toggle_drawer_impl(self, want=None):
        want = (not self.drawer_open) if want is None else bool(want)
        if want == self.drawer_open:
            return
        self.drawer_open = want
        if want:
            self.side.place(x=int(-DRAWER_W * (1.0 - self._drawer_p)), y=0, width=DRAWER_W, relheight=1.0)
            _raise(self.side)
            _raise(self.menu_btn)
        self._drawer_t = time.time() - 0.016                # so the very first frame already moves
        if self._drawer_job is None:
            self._drawer_anim()

    def _drawer_anim(self):
        try:
            self._drawer_frame()
        except Exception:
            traceback.print_exc()
            self._drawer_job = None

    def _drawer_frame(self):
        now = time.time()
        dt = min(0.05, now - self._drawer_t)
        self._drawer_t = now
        self._drawer_p, done = drawer_step(self._drawer_p, 1.0 if self.drawer_open else 0.0, dt)
        if done and not self.drawer_open:
            self.side.place_forget()                        # fully closed: out of the way completely
        else:
            self.side.place_configure(x=int(round(-DRAWER_W * (1.0 - self._drawer_p))))
        self._draw_menu_icon()
        self._drawer_job = None if done else self.root.after(12, self._drawer_anim)

    def _draw_menu_icon(self):
        p = self._drawer_p
        col = mix((79, 195, 247), (0, 229, 255), max(p, self._menu_hover))
        bg = mix(BG, (10, 18, 32), p)                       # blends into the menu's colour as it opens
        mb = self.menu_btn
        mb.configure(bg=_hexcol(bg))
        for item, ((x0, y0, x1, y1), vis) in zip(self.menu_bars, menu_icon_lines(p)):
            mb.coords(item, x0 + 2, y0, x1 + 2, y1)
            mb.itemconfigure(item, fill=_hexcol(mix(bg, col, vis)))
        mb.itemconfigure(self.menu_txt, fill=_hexcol(col), text="CLOSE" if self.drawer_open else "MENU")

    def _menu_enter(self, _=None):
        self._menu_hover = 1.0
        self._draw_menu_icon()

    def _menu_leave(self, _=None):
        self._menu_hover = 0.0
        self._draw_menu_icon()

    def _outside_click(self, e):
        if self.drawer_open and not (is_inside(e.widget, self.side) or is_inside(e.widget, self.menu_btn)):
            self._toggle_drawer(False)

    # ---------- message bar ----------
    def _show_placeholder(self):
        if self.entry.get() and not self._ph_on:
            return                                          # you have typed something: leave it
        self.entry.delete(0, "end")
        self.entry.insert(0, f"Talk to {self.pname}\u2026")
        self.entry.configure(fg="#4a6a80")
        self._ph_on = True

    def _entry_focus_in(self, _=None):
        if self._ph_on:
            self.entry.delete(0, "end")
            self.entry.configure(fg="#e0f7fa")
            self._ph_on = False

    def _entry_focus_out(self, _=None):
        if not self.entry.get():
            self._show_placeholder()

    def _fx_tick(self):
        """Small always-on animations (cheap): the glow around the message bar."""
        try:
            if self.mode == "main" and not self.boot_active and self.root.state() != "iconic":
                self._fx_glow()
        except Exception:
            traceback.print_exc()
        self.root.after(45, self._fx_tick)

    def _fx_glow(self):
        try:
            focused = self.root.focus_get() is self.entry
        except Exception:                                   # Tk raises while a drop-down list has focus
            focused = False
        if self.state == "thinking" and not self.sleeping:
            k = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(time.time() * 4.0))     # slow pulse while he thinks
        else:
            k = 1.0 if focused else 0.0
        self._glow += (k - self._glow) * 0.3
        col = _hexcol(mix((27, 58, 87), (0, 229, 255), self._glow))
        if col != self._glow_col:
            self._glow_col = col
            self.entry_glow.configure(bg=col)
            self.prompt_lbl.configure(fg=_hexcol(mix((79, 195, 247), (0, 229, 255), self._glow)))

    def _delete_conv(self):
        sel = self.conv_list.curselection()
        if sel and messagebox.askyesno("Delete conversation", "Delete this conversation for good?"):
            if self.on_delete_conv:
                self.on_delete_conv(self.conv_ids[sel[0]])

    # ---------- sleep ----------
    def _toggle_sleep(self):
        if self.on_sleep:
            self.on_sleep(not self.sleeping)

    def _apply_sleep(self, on):
        self.sleeping = bool(on)
        self._last_render = 0.0                  # redraw straight away
        self.sleep_btn.configure(text="WAKE UP" if self.sleeping else "SLEEP",
                                 fg=GOOD if self.sleeping else "#b388ff")

    # ---------- start-up screen ----------
    def _build_boot(self):
        self.boot_active, self.boot_fading = True, False
        self.boot_t0 = time.time()
        self.boot_state = {k: ("pending", "") for k, _ in BOOT_ITEMS}
        self.boot_log = deque(maxlen=6)
        self.boot_prog, self.boot_done_t = 0.0, None
        self._bcache = {}
        self.boot_comp = Compositor(self.BOOT_R, bg=False)
        self.boot_photo = ImageTk.PhotoImage(Image.new("RGB", (self.BOOT_R, self.BOOT_R)))
        f = self.boot = tk.Frame(self.root, bg="#000000")
        f.place(x=0, y=0, relwidth=1, relheight=1)
        cv = self.boot_cv = tk.Canvas(f, bg="#000000", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        self.b_ring = cv.create_oval(0, 0, 1, 1, outline="#12303f", width=1)
        self.b_arc = cv.create_arc(0, 0, 1, 1, start=90, extent=0, style="arc", outline="#00e5ff", width=4)
        self.b_img = cv.create_image(0, 0, image=self.boot_photo, anchor="center")
        self.b_t1 = cv.create_text(0, 0, text="S T A R K     I N D U S T R I E S", anchor="w",
                                   font=("Segoe UI Light", 24), fill="#e8f4ff")
        self.b_t2 = cv.create_text(0, 0, text="E.C.H.O.   //   SYSTEM INITIALIZATION", anchor="w",
                                   font=("Consolas", 11), fill="#4fc3f7")
        self.b_rows = []
        for k, label in BOOT_ITEMS:
            self.b_rows.append((cv.create_oval(0, 0, 1, 1, fill=BOOT_COL["pending"], outline=""),
                                cv.create_text(0, 0, text=label, anchor="w", font=("Consolas", 12, "bold"),
                                               fill="#cfe9f5"),
                                cv.create_text(0, 0, text="WAITING", anchor="e", font=("Consolas", 11),
                                               fill=BOOT_COL["pending"])))
        self.b_bar_bg = cv.create_rectangle(0, 0, 1, 1, outline="#1b3a57", fill="#050b14")
        self.b_bar = cv.create_rectangle(0, 0, 1, 1, outline="", fill="#00e5ff")
        self.b_pct = cv.create_text(0, 0, text="0%", anchor="e", font=("Consolas", 10, "bold"), fill="#4fc3f7")
        self.b_sum = cv.create_text(0, 0, text="", anchor="w", font=("Consolas", 12, "bold"), fill="#4fc3f7")
        self.b_log = cv.create_text(0, 0, text="", anchor="sw", justify="left", font=("Consolas", 9), fill="#3d6a85")
        self.b_foot = cv.create_text(0, 0, text="STARK INDUSTRIES  //  MARK VII  //  LOCAL SYSTEMS ONLY",
                                     anchor="se", font=("Consolas", 9), fill="#2a4a60")
        self.b_corners = [cv.create_line(0, 0, 1, 1, fill="#1b6a8a", width=2) for _ in range(8)]
        self.b_btn = tk.Button(cv, text="INITIALIZE", command=self._boot_finish, bg="#10243a", fg="#00e5ff",
                               bd=0, activebackground="#16365a", activeforeground="#ffffff",
                               font=("Consolas", 13, "bold"), padx=28, pady=8, cursor="hand2")
        self.b_btn_win = cv.create_window(0, 0, window=self.b_btn, anchor="center")
        cv.bind("<Configure>", lambda e: self._boot_layout())
        self.root.after(80, self._boot_layout)

    def _boot_layout(self):
        if not self.boot_active:
            return
        cv = self.boot_cv
        W, H = max(900, cv.winfo_width()), max(600, cv.winfo_height())
        g = boot_geometry(W, H)
        r = self.BOOT_R / 2 + 18
        cv.coords(self.b_img, g["cx"], g["cy"])
        for it in (self.b_ring, self.b_arc):
            cv.coords(it, g["cx"] - r, g["cy"] - r, g["cx"] + r, g["cy"] + r)
        cv.coords(self.b_t1, *g["t1"])
        cv.coords(self.b_t2, *g["t2"])
        for (dot, lab, st), y in zip(self.b_rows, g["rows"]):
            cv.coords(dot, g["x0"], y - 6, g["x0"] + 12, y + 6)
            cv.coords(lab, g["x0"] + 26, y)
            cv.coords(st, g["x1"], y)
        cv.coords(self.b_bar_bg, *g["bar"])
        cv.coords(self.b_pct, g["x1"], g["bar"][1] - 12)
        cv.coords(self.b_sum, *g["sum"])
        cv.coords(self.b_btn_win, *g["btn"])
        cv.coords(self.b_log, 28, H - 22)
        cv.coords(self.b_foot, W - 28, H - 22)
        m, L = 22, 44
        pts = [(m, m, m + L, m), (m, m, m, m + L), (W - m, m, W - m - L, m), (W - m, m, W - m, m + L),
               (m, H - m, m + L, H - m), (m, H - m, m, H - m - L),
               (W - m, H - m, W - m - L, H - m), (W - m, H - m, W - m, H - m - L)]
        for it, p in zip(self.b_corners, pts):
            cv.coords(it, *p)
        self._bg = g

    def _bset(self, item, **kw):
        key = tuple(sorted(kw.items()))
        if self._bcache.get(item) != key:
            self._bcache[item] = key
            self.boot_cv.itemconfigure(item, **kw)

    def _apply_boot(self, key, val):
        if not self.boot_active or key not in self.boot_state:
            return
        st, det = val
        old = self.boot_state[key]
        self.boot_state[key] = (st, det)
        if st != "pending" and (st != old[0] or (st in ("ok", "warn", "fail") and det != old[1])):
            self.boot_log.append(f"[{time.time() - self.boot_t0:5.1f}s]  {BOOT_SHORT[key]}: {det or st}")

    def _tick_boot(self, t, dt):
        el = time.time() - self.boot_t0
        total = len(BOOT_ITEMS)
        sts = [self.boot_state[k][0] for k, _ in BOOT_ITEMS]
        n_done = sum(s in ("ok", "warn", "fail") for s in sts)
        n_load = sum(s == "loading" for s in sts)
        done = n_done == total
        failed = [k for (k, _), s in zip(BOOT_ITEMS, sts) if s == "fail"]
        if done and self.boot_done_t is None:
            self.boot_done_t = time.time()
        self.boot_prog += ((n_done + 0.5 * n_load) / total - self.boot_prog) * (1 - math.exp(-dt * 3.0))
        if done:
            self.boot_prog = max(self.boot_prog, min(1.0, self.boot_prog + dt))

        # the arc reactor: builds itself up, spins while checking, settles when everything is ready
        ease = 1 - (1 - min(1.0, el / 1.5)) ** 3
        st = "thinking" if not done else ("error" if failed else "speaking")
        th = THEMES["jarvis"]
        n = self.BOOT_R
        ent = dict(key="jarvis", x=n / 2, y=n / 2, R=n * 0.43 * (0.35 + 0.65 * ease), col=list(th[0][st]),
                   acc=list(th[1][st]), st=st, lvl=(0.3 + 0.25 * math.sin(t * 3)) if not done else 0.55,
                   contain=0.0, hover=False, ph=0.0, detail=True, main=True)
        shock = max(0.0, 1.0 - el * 0.7)
        if self.boot_done_t is not None:
            shock = max(shock, 1.0 - (time.time() - self.boot_done_t) * 1.2)
        self.boot_photo.paste(self.boot_comp.render([ent], t, dt, 0.0, shock, False))

        # status rows
        for (k, _), (dot, lab, stx) in zip(BOOT_ITEMS, self.b_rows):
            s_, det = self.boot_state[k]
            col = BOOT_COL[s_]
            dot_col = col if (s_ != "loading" or int(t * 3) % 2 == 0) else "#5a4300"
            self._bset(dot, fill=dot_col)
            self._bset(stx, text=("WAITING" if s_ == "pending" else det), fill=col)
        prog = min(1.0, self.boot_prog)
        g = getattr(self, "_bg", None) or boot_geometry(1240, 740)
        x0, y0, x1, y1 = g["bar"]
        self.boot_cv.coords(self.b_bar, x0, y0, x0 + (x1 - x0) * prog, y1)
        self.boot_cv.itemconfigure(self.b_arc, extent=-359.9 * prog)
        self._bset(self.b_pct, text=f"{int(prog * 100)}%")
        arc_col = "#ff5252" if failed and done else "#00e5ff"
        self._bset(self.b_arc, outline=arc_col)
        self._bset(self.b_bar, fill=arc_col)
        if not done:
            msg, col = "RUNNING DIAGNOSTICS" + "." * (int(t * 2) % 4), "#4fc3f7"
        elif failed:
            msg, col = (f"ATTENTION: {len(failed)} CHECK{'S' if len(failed) > 1 else ''} FAILED - "
                        "fix it and restart, or INITIALIZE anyway"), "#ff5252"
        else:
            msg, col = "ALL SYSTEMS ONLINE", "#69f0ae"
        self._bset(self.b_sum, text=msg, fill=col)
        self._bset(self.b_log, text="\n".join(self.boot_log))

        # all done (and nothing failed): fade into the HUD by itself
        if (done and not failed and not self.boot_fading and el >= BOOT_MIN_SECONDS
                and time.time() - self.boot_done_t >= BOOT_AUTO_SECONDS):
            self._boot_finish()

    def _boot_finish(self):
        """INITIALIZE button, or automatic when every check passed: fade out, swap, fade in."""
        if not self.boot_active or self.boot_fading:
            return
        self.boot_fading = True
        self._fade_to(1.0, 0.0, 10, self._boot_swap)

    def _boot_swap(self):
        self._boot_skip()
        self._fade_to(0.0, 1.0, 12, None)

    def _boot_skip(self):
        if not self.boot_active:
            return
        self.boot_active = False
        try:
            self.boot.place_forget()
            self.boot.destroy()
        except Exception:
            pass
        self.boot_comp = None
        self._cache.clear()

    def _fade_to(self, start, end, steps, done):
        def step(i=0):
            try:
                self.root.attributes("-alpha", start + (end - start) * i / steps)
            except tk.TclError:
                pass
            if i < steps:
                self.root.after(20, lambda: step(i + 1))
            elif done:
                done()
        step()

    # ---------- microphone on / off ----------
    def _toggle_mic(self):
        if self.on_mic_mute:
            self.on_mic_mute(not self.mic_muted)

    def _apply_mic(self, muted):
        self.mic_muted = bool(muted)
        self.mic_btn.configure(text="MIC OFF" if self.mic_muted else "MIC ON",
                               fg=BAD if self.mic_muted else GOOD)

    # ---------- voice controls ----------
    def _refresh_mute(self):
        self.mute_btn.configure(text="VOICE OFF" if self.muted else "VOICE ON",
                                fg=BAD if self.muted else GOOD)

    def _toggle_mute(self):
        self.muted = not self.muted
        self._refresh_mute()
        if self.on_mute:
            self.on_mute(self.muted)

    def _on_vol(self, v):
        if self._ignore_vol:
            return
        v = int(float(v))
        if v == self._last_vol:
            return
        self._last_vol = v
        self.vol_lbl.configure(text=f"{v}%")
        if self.on_volume:
            self.on_volume(v)

    def _apply_voice(self, muted, vol):
        self._ignore_vol = True
        self.muted, self._last_vol = bool(muted), int(vol)
        self.vol.set(int(vol))
        self.vol_lbl.configure(text=f"{int(vol)}%")
        self._ignore_vol = False
        self._refresh_mute()

    # ---------- the reactors: layout, hover, click ----------
    def _slots(self):
        others = [k for k in ORDER if k != self.active]
        n = max(1, len(others))
        return {k: -math.pi / 2 + self._ring_rot + i * 2 * math.pi / n for i, k in enumerate(others)}

    def _place_entities(self):
        for e in self.ent.values():
            e["x"] = CX + e["rad"] * math.cos(e["ang"])
            e["y"] = CY + e["rad"] * math.sin(e["ang"])

    def _update_entities(self, dt, t, st, sleeping):
        """Glides every reactor to where it belongs: the active one to the middle, the rest on the orbit."""
        a = 1 - math.exp(-dt * 4.2)
        ac = 1 - math.exp(-dt * 5.0)
        if not sleeping:
            self._ring_rot += dt * 0.035
        slots = self._slots()
        theme_a = THEMES[self.active]
        for k in ORDER:
            e = self.ent[k]
            act = k == self.active
            th = THEMES[k]
            if act:
                tr, tR = 0.0, R_BIG
                dim = 0.45 if sleeping else 1.0
                tcol = [v * dim for v in th[0][st]]
                tacc = [v * dim for v in th[1][st]]
            else:
                tr, tR = RING, R_SAT * (1.2 if k == self.hover else 1.0)
                if e["rad"] < 8:
                    e["ang"] = slots[k]                    # just left the middle: slide outwards from there
                pulse = 0.78 + 0.22 * math.sin(t * 1.3 + e["ph"])
                hot = k == self.hover
                tcol = [v * (1.0 if hot else pulse) for v in th[0]["listening" if hot else "sleep"]]
                tacc = [v * (1.0 if hot else pulse) for v in th[1]["listening" if hot else "sleep"]]
                if k == "ultron" and self.alert:
                    tcol, tacc = [255, 40, 30], [255, 150, 120]
                diff = (slots[k] - e["ang"] + math.pi) % (2 * math.pi) - math.pi
                e["ang"] += diff * a
            e["rad"] += (tr - e["rad"]) * a
            e["R"] += (tR - e["R"]) * a
            e["col"] = mix(e["col"], tcol, ac)
            e["acc"] = mix(e["acc"], tacc, ac)
            if k == "ultron":
                ct = 0.0 if act else max(0.0, 1.0 - 0.95 * self.g)           # the cage opens as he takes over
                e["contain"] += (ct - e["contain"]) * (1 - math.exp(-dt * 3.0))
        self._place_entities()

    def _hit(self, mx, my):
        mx, my = mx - HUD_X0, my - HUD_Y0
        for k in ORDER:
            if k == self.active:
                continue
            e = self.ent[k]
            if math.hypot(mx - e["x"], my - e["y"]) <= max(e["R"], R_SAT) * 1.35:
                return k
        return None

    def _set_hover(self, k):
        if k != self.hover:
            self.hover = k
            self.canvas.configure(cursor="hand2" if k else "")

    def _hud_motion(self, e):
        self._set_hover(self._hit(e.x, e.y) if self.mode == "main" else None)

    def _hud_click(self, e):
        if self.mode != "main" or self.drawer_open or time.time() < self._lock_until:
            return                                          # (a tap that closes the menu is not a reactor tap)
        k = self._hit(e.x, e.y)
        if k and self.on_persona:
            self._lock_until = time.time() + 1.2                # ignore double clicks while it switches
            self.on_persona(NAMES[k])

    def _apply_persona(self, key):
        if key not in THEMES:
            return
        old, self.active = self.active, key
        self.pname = NAMES[key]
        self._cache.clear()
        if key == "ultron" and old != "ultron":
            self.shock = 1.0                                     # shockwave when he breaks out
        self._refresh_titles(self.g)
        self._refresh_chat_header()
        if self._ph_on:
            self._show_placeholder()                            # "Talk to Friday..."

    def _refresh_titles(self, g):
        c = self.canvas
        ult = self.active == "ultron" or g > 0.5
        if ult:
            t1 = "STATUS: S.I.L.O. NETWORK :: CORRUPTED NEURAL GRID"
            if g > 0.2:
                t1 = corrupt_text(t1, g * 0.6)
            c.itemconfigure(self.title, text=t1, font=("Consolas", 11, "bold"), fill="#ff6a6a")
            c.itemconfigure(self.title2, text="SYSTEM INSTABILITY: CRITICAL", font=("Consolas", 11, "bold"),
                            fill="#ff2a2a")
        else:
            c.itemconfigure(self.title, text=DISPLAY[self.active], font=("Segoe UI Light", 18),
                            fill=_hexcol(THEMES[self.active][0]["listening"]))
            c.itemconfigure(self.title2, text="(" + SUBTITLE[self.active] + ")", font=("Consolas", 9),
                            fill="#5c7a8c")
        vis = "normal" if ult else "hidden"
        c.itemconfigure(self.code_l, state=vis)
        c.itemconfigure(self.code_r, state=vis)

    # ---------- what he is looking at + screen capture ----------
    def _apply_vision(self, img, label):
        if self._vis_job is not None:
            self.root.after_cancel(self._vis_job)
            self._vis_job = None
        self.vis_photo = ImageTk.PhotoImage(ImageOps.contain(img.convert("RGB"), (self.VIS_W, self.VIS_H)))
        self.vis_cv.itemconfigure(self.vis_img, image=self.vis_photo)
        self.vis_label = label
        self.vis_title.configure(text=f"{label}  -  ANALYSING",
                                 fg=_hexcol(THEMES[self.active][0]["listening"]))
        self.vis_scan = self.vis_open = True
        self.vis.update_idletasks()
        sh = self.root.winfo_screenheight()
        self.vis.geometry(f"+24+{sh - self.vis.winfo_reqheight() - 80}")
        self.vis.deiconify()
        self.vis.lift()

    def _end_vision(self, seconds):
        self.vis_scan = False
        self.vis_cv.coords(self.vis_line, -10, -10, -10, -10)
        self.vis_title.configure(text=f"{getattr(self, 'vis_label', '')}  -  DONE")
        if self._vis_job is not None:
            self.root.after_cancel(self._vis_job)
        self._vis_job = self.root.after(int(seconds * 1000), self._hide_vision)

    def _hide_vision(self):
        self.vis_open = self.vis_scan = False
        if self._vis_job is not None:
            self.root.after_cancel(self._vis_job)
            self._vis_job = None
        self.vis.withdraw()

    def _tick_vision(self, t):
        if self.vis_open and self.vis_scan:
            y = 4 + (t * 95) % (self.VIS_H - 8)
            self.vis_cv.coords(self.vis_line, 0, y, self.VIS_W, y)

    def _begin_grab(self, ev, box):
        """Hides Echo' own windows so they are not in the screenshot, then captures."""
        prev = (self.mode, self.chat_open, self.vis_open)
        self.root.withdraw()
        self.orb.withdraw()
        self.chat.withdraw()
        self.vis.withdraw()
        self.root.update_idletasks()
        self.root.after(380, lambda: self._finish_grab(ev, box, prev))

    def _finish_grab(self, ev, box, prev):
        try:
            box["img"] = grab_screen_raw()
        except Exception as e:
            box["err"] = str(e)
        mode, chat, vis = prev
        if mode == "main":
            self.root.deiconify()
        elif mode == "orb":
            self.orb.deiconify()
            if chat:
                self.chat.deiconify()
        if vis:
            self.vis.deiconify()
        ev.set()

    # ---------- applying queued updates ----------
    def _apply_convs(self, items, active_id):
        self.conv_ids = [it["id"] for it in items]
        self.conv_list.delete(0, "end")
        for it in items:
            self.conv_list.insert("end", time.strftime("%d %b", time.localtime(it["updated"])) +
                                  "  " + it["title"])
        if active_id in self.conv_ids:
            i = self.conv_ids.index(active_id)
            self.conv_list.selection_clear(0, "end")
            self.conv_list.selection_set(i)
            self.conv_list.see(i)
        self._refresh_conv_btn()

    def _submit(self, _=None):
        text = "" if self._ph_on else self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._append("you", text)
        if self.on_text:
            self.on_text(text)

    def _append(self, who, text, ts=None, live=None, persona=None):
        names = {"you": "YOU", "ai": "ECHO", "sys": "SYSTEM"}
        stamp = time.strftime("%H:%M", time.localtime(ts)) if ts else time.strftime("%H:%M")
        # the name comes from whoever SAID it, not from whoever happens to be on screen right now
        label = (NAMES.get(persona) or self.pname).upper() if who == "ai" else names.get(who, who)
        if live is None:
            live = ts is None
        # live answers from the assistant are typed out word by word; everything else appears at once
        self.tw.add(who, f"\u258e {stamp}  {label}\n", text, animate=(who == "ai" and live))
        if who == "ai" and live:                     # live answer: show it in the chat box too
            self._set_reply(label, text)

    def _apply_transcript(self, items):
        self.tw.clear()                              # drop anything still being typed
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for item in items:                           # (who, text, time) or, newer, (who, text, time, persona)
            who, text, ts = item[:3]
            self._append(who, text, ts, live=False, persona=item[3] if len(item) > 3 else None)

    # ---------- stats ----------
    def _bar_color(self, key, pct):
        if key == "bat":
            return BAD if pct <= 20 else WARN if pct <= 40 else GOOD
        return GOOD if pct < 60 else WARN if pct < 85 else BAD

    def _update_stats(self):
        self._last_stats = time.time()
        try:
            st = get_stats()
        except Exception:
            return
        for name, key in ROWS:
            v = st[key]
            if v is None:
                self._stat_tgt[name] = 0.0
                self.stats.itemconfigure(self.stat_txt[name], text="n/a")
                continue
            self._stat_tgt[name] = v
            self.stats.itemconfigure(self.stat_txt[name],
                                     text=f"{v:.0f}%" + (" CHG" if key == "bat" and st["chg"] else ""))
        for key in ("cpu", "ram"):
            self.hist[key].append(st[key] or 0)
            pts = []
            for i, v in enumerate(self.hist[key]):
                pts += [i * 308 / 59, 160 - v / 100 * 44]
            self.stats.coords(self.spark[key], *pts)

    def _ease_stats(self):
        for i, (name, key) in enumerate(ROWS):
            cur = self._stat_cur[name] + (self._stat_tgt[name] - self._stat_cur[name]) * 0.3
            self._stat_cur[name] = cur
            y = 14 + i * 26
            self.stats.coords(self.stat_bar[name], 44, y - 5, 44 + 186 * cur / 100, y + 5)
            self.stats.itemconfigure(self.stat_bar[name], fill=self._bar_color(key, cur))

    # ---------- drawing ----------
    def _main_entity(self, key, e, st, lvl, main):
        col, acc = e["col"], e["acc"]
        if main and self.g > 0.02:                       # Ultron's red creeps into the active reactor
            col, acc = mix(col, RED, 0.85 * self.g), mix(acc, ORANGE, 0.85 * self.g)
        return dict(key=key, x=e["x"], y=e["y"], R=e["R"], col=col, acc=acc, st=st if main else "sleep",
                    lvl=lvl if main else 0.0, contain=e["contain"], hover=(key == self.hover),
                    ph=e["ph"], detail=e["R"] > 95, main=main)

    def _set(self, item, **kw):
        """Only touches the canvas when something actually changed (saves CPU)."""
        key = (item, tuple(sorted(kw.items())))
        if self._cache.get(item) != key:
            self._cache[item] = key
            self.canvas.itemconfigure(item, **kw)

    def _draw_hud(self, t, dt, st, lvl, sleeping):
        ents = [self._main_entity(k, self.ent[k], st, lvl, k == self.active) for k in ORDER]
        img = self.comp.render(ents, t, dt, self.g, self.shock, self.alert, self.containment_progress)
        self.hud_photo.paste(img)
        c = self.canvas
        for k in ORDER:                                   # names under the small reactors
            e = self.ent[k]
            it = self.labels[k]
            if k == self.active or e["R"] < R_SAT * 0.8:
                self._set(it, state="hidden")
                continue
            txt = DISPLAY[k] + ("  [CONTAINED]" if k == "ultron" and e["contain"] > 0.5 else "")
            col = _hexcol(mix(e["col"], (255, 255, 255), 0.35 if k == self.hover else 0.0))
            self._set(it, state="normal", text=txt, fill=col)
            c.coords(it, HUD_X0 + e["x"], HUD_Y0 + e["y"] + e["R"] * 1.3 + 8)
        col = _hexcol(self.ent[self.active]["col"])
        if sleeping:
            label = 'SLEEPING  -  say "wake up"'
        elif st == "sleep":
            label = f'STANDBY  -  say "{self.pname}"'
        else:
            label = LABELS[st]
        self._set(self.status, text=f"{DISPLAY[self.active]}  -  {label}" + ("   [MUTED]" if self.muted else "")
                  + ("   [MIC OFF]" if self.mic_muted else ""), fill=col)
        c.itemconfigure(self.clock, text=time.strftime("%H:%M:%S    %a %d %b %Y"))
        c.coords(self.sig, 175, 679, 175 + 170 * min(1.0, lvl), 689)
        self._set(self.sig, fill=col)
        if self.active == "ultron" or self.g > 0.5:       # blinking warning + error code, like the reference
            c.itemconfigure(self.title2, state="normal" if int(t * 2.2) % 2 == 0 or self.g > 0.5 else "hidden")
            if t - self._code_t > 0.3:
                self._code_t = t
                mk = lambda n: "\n".join(random.choice(CODE_POOL) + ("  0x%04X" % random.randrange(65536)
                                                                     if random.random() < 0.5 else "")
                                         for _ in range(n))
                c.itemconfigure(self.code_l, text=mk(7))
                c.itemconfigure(self.code_r, text=mk(7))
                if self.g > 0.2:
                    self._refresh_titles(self.g)
        else:
            c.itemconfigure(self.title2, state="normal")

    # ---------- main loop ----------
    def _tick(self):
        """Runs one frame. If anything in it raises, the error is printed and the loop is restarted,
        because an uncaught exception here used to stop the animation (the window froze for good)."""
        try:
            self._tick_inner()
        except Exception:
            traceback.print_exc()
            try:
                self.root.after(100, self._tick)
            except Exception:
                pass

    def _tick_inner(self):
        t_start = time.time()
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "state":
                    self.state = a
                    self._refresh_chat_header()
                elif kind == "log":
                    text, persona = b if isinstance(b, tuple) else (b, None)
                    self._append(a, text, persona=persona)
                elif kind == "voice":
                    self._apply_voice(a, b)
                elif kind == "sleep":
                    self._apply_sleep(a)
                    self._refresh_chat_header()
                elif kind == "persona":
                    self._apply_persona(a)
                elif kind == "glitch":
                    self.g_t = max(0.0, min(1.0, float(a)))
                elif kind == "alert":
                    self.alert = bool(a)
                elif kind == "convs":
                    self._apply_convs(a, b)
                elif kind == "devices":
                    self.mic_box.configure(values=a[0])
                    self.spk_box.configure(values=a[1])
                    self.mic_box.set(b[0])
                    self.spk_box.set(b[1])
                elif kind == "transcript":
                    self._apply_transcript(a)
                elif kind == "vision":
                    self._apply_vision(a, b)
                elif kind == "vision_end":
                    self._end_vision(a)
                elif kind == "grab":
                    self._begin_grab(a, b)
                elif kind == "boot":
                    self._apply_boot(a, b)
                elif kind == "boot_skip":
                    self._boot_skip()
                elif kind == "mic":
                    self._apply_mic(a)
                elif kind == "hotkey_mic":
                    self._toggle_mic()
                elif kind == "hotkey":
                    self.show_main_now()
                elif kind == "mode":
                    if a == "orb":
                        self.show_orb_now()
                    elif a == "chat":
                        self.show_orb_now()
                        self._show_chat()
                    else:
                        self.show_main_now()
                elif kind == "contain_ultron":
                    self.containment_progress = 0.001      # was 0.0, but the animation only runs while > 0
                    self.on_contain_start() if self.on_contain_start else None
                elif kind == "quit":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass

        dt = min(0.5, max(0.001, t_start - self.last))
        self.last = t_start
        t = t_start - self.t0
        sleeping = self.sleeping
        st = "sleep" if sleeping else self.state
        if sleeping:
            self.lvl = 0.0
        else:
            self.lvl += (self.level - self.lvl) * 0.35
        self.g += (self.g_t - self.g) * (1 - math.exp(-dt * 2.2))
        if self.g < 0.003 and self.g_t == 0.0:
            self.g = 0.0
        self.shock = max(0.0, self.shock - dt * 0.55)

        if self.boot_active:                              # start-up screen: draw only that
            self._tick_boot(t, dt)
            self.root.after(max(1, int(1000 / FPS) - int((time.time() - t_start) * 1000)), self._tick)
            return

        # Handle containment animation progress
        if self.containment_progress > 0 and self.containment_progress < 1.0:
            self.containment_progress += dt / CONTAIN_SECONDS      # see CONTAIN_SECONDS in reactors.py
            if self.containment_progress >= 1.0:
                self.containment_progress = 0.0        # finished: stop drawing the rays every frame forever

        self._tick_vision(t)
        hidden = self.mode == "hidden" or (self.mode == "main" and self.root.state() == "iconic")
        if hidden:
            delay = 400                                     # nothing visible: draw nothing, cost nothing
        elif sleeping:
            delay = 250
            if t_start - self._last_render >= 1.0 / SLEEP_FPS:
                dt2 = t_start - self._last_render if self._last_render else 0.5
                self._last_render = t_start
                self._update_entities(min(0.5, dt2), t, st, True)
                if self.mode == "main":
                    self._draw_hud(t, dt2, st, 0.0, True)
                else:
                    self._draw_orb(t, dt2, st, 0.0)
                if t_start - self._last_stats > 5.0:
                    self._update_stats()
                    for _ in range(10):
                        self._ease_stats()
        else:
            self._update_entities(dt, t, st, False)
            if self.mode == "main":
                self._draw_hud(t, dt, st, self.lvl, False)
                if t_start - self._last_stats > 0.5:
                    self._update_stats()
                self._ease_stats()
            else:
                self._draw_orb(t, dt, st, self.lvl)
            delay = max(1, int(1000 / FPS) - int((time.time() - t_start) * 1000))
        self.root.after(delay, self._tick)

    def _draw_orb(self, t, dt, st, lvl):
        e = dict(self.ent[self.active])
        e.update(x=self.ORB / 2, y=self.ORB / 2, R=self.ORB * 0.44, contain=0.0)
        ent = self._main_entity(self.active, e, st, lvl, True)
        ent["detail"] = False
        img = self.orb_c.render([ent], t, dt, self.g, self.shock, False)
        self.orb_photo.paste(img)