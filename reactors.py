# --- PAGE 1 ---

"""reactors.py - arc reactor / neural grid renderer and glitch effects (pure PIL + numpy)."""
import math, random
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
# ---------------------------------------------------------------- layout constants
S = 600                      # size of the big composite picture
CX = CY = S / 2.0
RING = 232.0                 # radius of the orbit the inactive reactors travel on
R_BIG = 168.0                # radius of the active reactor
R_SAT = 42.0                 # radius of an inactive reactor
ORDER = ["jarvis", "friday", "veronica", "edith", "karen", "ultron"]
NAMES = {"jarvis": "Echo", "friday": "Nova", "veronica": "Atlas",
         "edith": "Aegis", "karen": "Vortex", "ultron": "Silo"}
DISPLAY = {"jarvis": "E.C.H.O.", "friday": "N.O.V.A.", "veronica": "A.T.L.A.S.",
           "edith": "A.E.G.I.S.", "karen": "V.O.R.T.E.X.", "ultron": "S.I.L.O."}
SUBTITLE = {"jarvis": "CORE - BLUE", "friday": "CORE - GREEN",
            "veronica": "CORE - GOLD", "edith": "CORE - WHITE",
            "karen": "CORE - PURPLE", "ultron": "CONTAINED CORE - RED"}
SPEED = {"sleep": 0.35, "listening": 1.0, "thinking": 3.0, "speaking": 1.5, "error": 0.6}
def make_theme(main, alt, think=(255, 179, 0)):
    dark = lambda c, k: tuple(int(v * k) for v in c)
    tint = lambda c, k: tuple(int(v + (255 - v) * k) for v in c)
    colors = {"sleep": dark(main, 0.6), "listening": main, "thinking": think,
              "speaking": alt, "error": (255, 82, 82)}
    accent = {"sleep": tint(dark(main, 0.6), 0.45), "listening": tint(main, 0.55),
              "thinking": tint(think, 0.5), "speaking": tint(alt, 0.5), "error": (255, 140, 140)}
    return colors, accent
PALETTE = {                  # (main colour, speaking colour, thinking colour)
    "jarvis": ((20, 140, 255), (110, 205, 255), (255, 179, 0)),
    "friday": ((30, 230, 110), (170, 255, 150), (255, 179, 0)),
    "veronica": ((255, 205, 0), (255, 236, 120), (255, 120, 30)),
    "edith": ((228, 236, 255), (255, 255, 255), (255, 179, 0)),
    "karen": ((190, 80, 255), (232, 165, 255), (255, 179, 0)),
    "ultron": ((225, 10, 25), (255, 90, 0), (170, 0, 255)),
}
THEMES = {k: make_theme(*v) for k, v in PALETTE.items()}
RED, ORANGE = (225, 10, 25), (255, 90, 0)
CONTAIN_SECONDS = 3.6        # how long the containment rays (and their sound) last
CONTAIN_LOCK = 0.55          # when the cage seals, as a fraction of that time (flash + "clunk" sound)
# ---------------------------------------------------------------- small helpers
def ic(c):
    return tuple(max(0, min(255, int(v))) for v in c)
def sc(c, k):
    return tuple(v * k for v in c)
def mix(a, b, t):
    return [x + (y - x) * t for x, y in zip(a, b)]
def ring(d, x, y, r, fill, w=1):

# --- PAGE 2 ---

    if r < 1.5:
        return
    d.ellipse([x - r, y - r, x + r, y + r], outline=fill, width=max(1, min(int(w), int(r))))
def arc(d, x, y, r, a0, a1, fill, w=1):
    if r < 1.5:
        return
    d.arc([x - r, y - r, x + r, y + r], a0, a1, fill=fill, width=max(1, min(int(w), int(r))))
def seg(d, x, y, r1, r2, ang_deg, fill, w=1):
    a = math.radians(ang_deg)
    ca, sa = math.cos(a), math.sin(a)
    d.line([x + r1 * ca, y + r1 * sa, x + r2 * ca, y + r2 * sa], fill=fill, width=max(1, int(w)))
_FONTS = {}
def font(size):
    size = max(8, int(size))
    if size not in _FONTS:
        f = None
        for name in ("consolab.ttf", "arialbd.ttf", "DejaVuSansMono-Bold.ttf", "arial.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except Exception:
                pass
        _FONTS[size] = f or ImageFont.load_default()
    return _FONTS[size]
class Cv:
    """One frame being painted: the picture, a pen, and helpers for glowing cores."""
    def __init__(self, img, sprite, flare_h, flare_v):
        self.img, self.d = img, ImageDraw.Draw(img)
        self.sprite, self.flare_h, self.flare_v = sprite, flare_h, flare_v
    def _add(self, src, x, y, w, h, col, k):
        w, h = int(w), int(h)
        if w < 3 or h < 3:
            return
        pic = ImageOps.colorize(src.resize((w, h), Image.BILINEAR), (0, 0, 0), (255, 255, 255),
mid=ic(col))
        if k < 0.99:
            pic = pic.point(lambda v: int(v * k))
        x0, y0 = int(x - w / 2), int(y - h / 2)
        reg = self.img.crop((x0, y0, x0 + w, y0 + h))
        self.img.paste(ImageChops.add(reg, pic), (x0, y0))
    def core(self, x, y, size, col, k=1.0):
        self._add(self.sprite, x, y, size, size, col, k)
    def flare(self, x, y, length, thick, col, k=1.0):
        self._add(self.flare_h, x, y, length, thick, col, k)
        self._add(self.flare_v, x, y, thick, length * 0.6, col, k * 0.6)
def make_sprite(n=256):

# --- PAGE 3 ---

    yy, xx = np.mgrid[0:n, 0:n]
    d = np.hypot(xx - n / 2, yy - n / 2) / (n / 2)
    i = 1.25 * np.exp(-(d / 0.18) ** 2) + 0.55 * np.exp(-(d / 0.45) ** 2) + 0.18 * np.exp(-(d / 0.9) ** 2)
    f = np.clip((1.0 - d) / 0.5, 0, 1)                    # fade to zero before the edge: no square
    i = i * (f * f * (3 - 2 * f))
    return Image.fromarray((np.clip(i, 0, 1) * 255).astype(np.uint8), "L")
def make_flare():
    w, h = 256, 32
    yy, xx = np.mgrid[0:h, 0:w]
    dx, dy = (xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)
    i = np.exp(-(dx / 0.6) ** 2) * np.exp(-(dy / 0.13) ** 2)
    i = i * np.clip((1 - np.abs(dx)) / 0.3, 0, 1)
    img = Image.fromarray((np.clip(i, 0, 1) * 255).astype(np.uint8), "L")
    return img, img.transpose(Image.ROTATE_90)
def eq_height(t, a, st, lvl, i):
    if st in ("listening", "speaking"):
        shape = 0.35 + 0.65 * abs(math.sin(i * 0.37 + t * 5.3) * math.cos(i * 0.11 - t * 3.1))
        return 0.05 + lvl * shape
    if st == "thinking":
        return 0.06 + 0.8 * max(0.0, math.cos(a - t * 4.5)) ** 6
    if st == "error":
        return 0.6 if int(t * 3) % 2 == 0 else 0.08
    return 0.05 + 0.025 * math.sin(t * 1.2 + i * 0.3)
def sweep(cv, x, y, R, t, acc):
    head = (t * 240) % 360
    for k in range(14):
        f = (1 - k / 14) ** 1.6
        arc(cv.d, x, y, R * 0.97, head - (k + 1) * 6, head - k * 6, ic(sc(acc, f)), max(2, int(R / 40)))
# ---------------------------------------------------------------- J.A.R.V.I.S. (blue)
def draw_jarvis(cv, x, y, R, t, st, lvl, col, acc, detail):
    d, u, sp = cv.d, R / 200.0, SPEED[st]
    c, a = ic(col), ic(acc)
    dim, faint = ic(sc(col, 0.45)), ic(sc(col, 0.2))
    W = lambda v: max(1, int(round(v * u)))
    ring(d, x, y, R * 0.985, faint, 1)
    if detail:
        rot = t * 3 * sp
        for i in range(90):
            long = i % 5 == 0
            seg(d, x, y, R * (0.925 if long else 0.95), R * 0.975, i * 4 + rot, c if long else faint)
    for k in range(3):
        a0 = (k * 120 + t * 38 * sp) % 360
        arc(d, x, y, R * 0.865, a0, a0 + 52, c, W(3))
    ring(d, x, y, R * 0.74, c, W(9))
    ring(d, x, y, R * 0.655, dim, W(2))
    ring(d, x, y, R * 0.60, a, W(1.5))
    if detail:
        for i in range(48):
            seg(d, x, y, R * 0.67, R * 0.71, i * 7.5 - t * 12 * sp, dim)
    n = 56 if detail else 24
    for i in range(n):
        ang = 360.0 * i / n - 90
        h = min(1.0, eq_height(t, math.radians(ang), st, lvl, i))
        seg(d, x, y, R * 0.47, R * (0.47 + 0.02 + 0.1 * h), ang, c if h > 0.45 else dim, 2 if detail else

# --- PAGE 4 ---

1)
    ring(d, x, y, R * 0.40, c, W(2))
    for k in range(4):
        a0 = (k * 90 - t * 55 * sp) % 360
        arc(d, x, y, R * 0.345, a0, a0 + 38, a, W(2.5))
    cv.core(x, y, R * 0.46 * (1 + 0.12 * lvl + 0.03 * math.sin(t * 3)), col, 0.38)
    if detail and R > 100:
        try:
            d.text((x, y), "E.C.H.O.", font=font(R * 0.075), fill=(235, 248, 255), anchor="mm")
        except Exception:
            pass
# ---------------------------------------------------------------- F.R.I.D.A.Y. (green)
def draw_friday(cv, x, y, R, t, st, lvl, col, acc, detail):
    d, u, sp = cv.d, R / 200.0, SPEED[st]
    c, a = ic(col), ic(acc)
    dim, faint = ic(sc(col, 0.45)), ic(sc(col, 0.2))
    W = lambda v: max(1, int(round(v * u)))
    ring(d, x, y, R * 0.985, faint, 1)
    rot = t * 14 * sp
    arc(d, x, y, R * 0.90, 90 + rot * 0.2, 270 + rot * 0.2, c, W(7))          # left half: thick ring
    arc(d, x, y, R * 0.83, 270 - rot * 0.2, 450 - rot * 0.2, a, W(7))         # right half: inner ring
    ring(d, x, y, R * 0.96, dim, W(1.5))
    for k in range(4):                                                         # four big segments
        a0 = (k * 90 + t * 30 * sp + 8) % 360
        arc(d, x, y, R * 0.64, a0, a0 + 58 + 10 * lvl, c, W(11))
    ring(d, x, y, R * 0.52, dim, W(1.5))
    for k in range(3):
        a0 = (k * 120 - t * 45 * sp) % 360
        arc(d, x, y, R * 0.42, a0, a0 + 70, a, W(5))
    if detail:
        for i in range(36):
            seg(d, x, y, R * 0.955, R * 0.985, i * 10 - rot, dim)
    ring(d, x, y, R * 0.22, c, W(2))
    cv.core(x, y, R * 0.34 * (1 + 0.3 * lvl), col, 1.0)
# ---------------------------------------------------------------- V.E.R.O.N.I.C.A. (yellow gear)
def draw_veronica(cv, x, y, R, t, st, lvl, col, acc, detail):
    d, u, sp = cv.d, R / 200.0, SPEED[st]
    c, a = ic(col), ic(acc)
    dim, faint = ic(sc(col, 0.45)), ic(sc(col, 0.2))
    W = lambda v: max(1, int(round(v * u)))
    teeth, rot = 16, t * 10 * sp * (1 + 0.8 * lvl)
    pts = []
    for i in range(teeth):
        base = rot + i * 360.0 / teeth
        for da, rr in ((-9, 0.80), (-6, 0.93), (6, 0.93), (9, 0.80)):
            ang = math.radians(base + da)
            pts.append((x + R * rr * math.cos(ang), y + R * rr * math.sin(ang)))
    d.line(pts + [pts[0]], fill=c, width=W(3))
    ring(d, x, y, R * 0.74, c, W(10))
    ring(d, x, y, R * 0.65, dim, W(2))
    for k in range(8):                                                         # iris blocks
        a0 = (k * 45 - rot * 1.5) % 360
        arc(d, x, y, R * 0.53, a0, a0 + 30, a, W(13))
    ring(d, x, y, R * 0.40, dim, W(2))
    for k in range(6):
        a0 = (k * 60 + rot * 2) % 360
        arc(d, x, y, R * 0.33, a0, a0 + 34, c, W(4))
    if detail:

# --- PAGE 5 ---

        for i in range(24):
            seg(d, x, y, R * 0.22, R * 0.26, i * 15 + rot, faint)
    cv.core(x, y, R * 0.52 * (1 + 0.25 * lvl), col, 1.0)
# ---------------------------------------------------------------- E.D.I.T.H. (white)
def draw_edith(cv, x, y, R, t, st, lvl, col, acc, detail):
    d, u, sp = cv.d, R / 200.0, SPEED[st]
    c, a = ic(col), ic(acc)
    W = lambda v: max(1, int(round(v * u)))
    for rr, w, kk in ((0.97, 1, 0.45), (0.88, 2, 0.8), (0.78, 1, 0.5), (0.66, 3, 1.0),
                      (0.54, 1, 0.6), (0.42, 2, 0.9), (0.30, 1, 0.5)):
        ring(d, x, y, R * rr, ic(sc(col, kk)), W(w))
    for rr, rate in ((0.88, 25), (0.66, -35), (0.42, 50)):
        for k in range(2):
            a0 = (k * 180 + t * rate * sp) % 360
            arc(d, x, y, R * rr, a0, a0 + 56, a, W(3))
    if detail:
        for i in range(60):
            ang = math.radians(i * 6 + t * 8 * sp)
            r_ = R * 0.78
            d.ellipse([x + r_ * math.cos(ang) - 1, y + r_ * math.sin(ang) - 1,
                       x + r_ * math.cos(ang) + 1, y + r_ * math.sin(ang) + 1], fill=ic(sc(col, 0.8)))
    cv.flare(x, y, R * (2.0 + 0.6 * lvl), R * 0.09, col, 0.9)
    cv.core(x, y, R * 0.36 * (1 + 0.2 * lvl), col, 0.9)
# ---------------------------------------------------------------- K.A.R.E.N. (purple mandala)
def _petal(x, y, ang, r0, L, wd, n=9):
    side = []
    for i in range(n + 1):
        tt = math.pi * i / n
        side.append((r0 + L * (1 - math.cos(tt)) / 2, wd * math.sin(tt)))
    pts = side + [(p, -q) for p, q in reversed(side[1:-1])]
    ca, sa = math.cos(ang), math.sin(ang)
    return [(x + p * ca - q * sa, y + p * sa + q * ca) for p, q in pts]
def draw_karen(cv, x, y, R, t, st, lvl, col, acc, detail):
    d, u, sp = cv.d, R / 200.0, SPEED[st]
    c, a = ic(col), ic(acc)
    dim = ic(sc(col, 0.5))
    W = lambda v: max(1, int(round(v * u)))
    breathe = 1 + 0.05 * math.sin(t * 2) + 0.12 * lvl
    ring(d, x, y, R * 0.92, c, W(7))
    ring(d, x, y, R * 0.84, dim, W(1.5))
    rot = t * 0.25 * sp
    n = 12 if detail else 8
    for k in range(n):
        ang = rot + 2 * math.pi * k / n
        d.line(_petal(x, y, ang, R * 0.28, R * 0.50 * breathe, R * 0.13), fill=c, width=W(2.2))
    for k in range(n):
        ang = -rot * 1.3 + math.pi / n + 2 * math.pi * k / n
        d.line(_petal(x, y, ang, R * 0.16, R * 0.34 * breathe, R * 0.085), fill=a, width=W(1.6))
    ring(d, x, y, R * 0.27, dim, W(2))
    for k in range(3):
        a0 = (k * 120 + t * 40 * sp) % 360
        arc(d, x, y, R * 0.20, a0, a0 + 50, a, W(3))
    cv.core(x, y, R * 0.44 * (1 + 0.25 * lvl), col, 0.95)
# ---------------------------------------------------------------- U.L.T.R.O.N. (red neural grid)

# --- PAGE 6 ---

_rs = random.Random(11)
def _build_ultron():
    cracks, N = [], 34
    for k in range(N):
        a = 2 * math.pi * k / N + _rs.uniform(-0.12, 0.12)
        length, n = _rs.uniform(0.55, 0.98), _rs.randint(4, 7)
        pts = []
        for j in range(n + 1):
            r = 0.03 + (length - 0.03) * j / n
            off = _rs.uniform(-0.04, 0.04) if j else 0.0
            ang = a + off / max(r, 0.1)
            pts.append((r * math.cos(ang), r * math.sin(ang)))
        cracks.append(pts)
    webs = []
    for k in range(N):
        a_, b_ = cracks[k], cracks[(k + 1) % N]
        for j in range(1, min(len(a_), len(b_))):
            if _rs.random() < 0.7:
                mx = (a_[j][0] + b_[j][0]) / 2 + _rs.uniform(-0.03, 0.03)
                my = (a_[j][1] + b_[j][1]) / 2 + _rs.uniform(-0.03, 0.03)
                webs.append([a_[j], (mx, my), b_[j]])
    shards = []
    for _ in range(46):
        r0 = _rs.uniform(0.1, 1.25)
        shards.append(dict(r0=r0, a0=_rs.uniform(0, 6.283), sz=_rs.uniform(0.025, 0.105) * (1.25 if r0 >
0.9 else 1),
                           rot=_rs.uniform(0, 6.283), spin=_rs.uniform(-0.6, 0.6), da=_rs.uniform(-0.05,
0.05),
                           ph=_rs.uniform(0, 6.283),
                           tri=[(_rs.uniform(0.5, 1.3), _rs.uniform(-0.5, 0.5)) for _ in range(3)]))
    arcs = []
    a0 = 0.0
    while a0 < 360:
        ext = _rs.uniform(24, 62)
        arcs.append((a0, ext, _rs.uniform(0.86, 0.95), _rs.choice((4, 5, 6, 8))))
        a0 += ext + _rs.uniform(6, 22)
    parts = [(_rs.uniform(0, 6.283), _rs.uniform(0, 1.1), _rs.uniform(0.05, 0.22), _rs.uniform(0.8, 2.2))
             for _ in range(90)]
    return cracks, webs, shards, arcs, parts
CRACKS, WEBS, SHARDS, UARCS, UPARTS = _build_ultron()
def draw_ultron(cv, x, y, R, t, st, lvl, col, acc, detail=True, contain=0.0, k=1.0, alert=False):
    """Shattered neural grid. contain 1 = locked in a cage, 0 = free. k = overall brightness."""
    d, u, sp = cv.d, R / 200.0, SPEED[st]
    rnd = random.random
    jit = (0.5 + 2.4 * lvl + 3.2 * contain) * u                       # unstable: the whole thing shakes
    x, y = x + random.uniform(-jit, jit), y + random.uniform(-jit, jit)
    r = R * (1.0 - 0.30 * contain)
    br = k * (1.0 - 0.42 * contain)
    c, a = col, acc
    fl = lambda p=1.6: 0.35 + 0.65 * rnd() ** p
    for a0, ext, rad, wd in UARCS:                                      # broken outer ring
        s = a0 + t * (6 if wd % 2 else -4) * (0.6 + sp)
        arc(d, x, y, r * rad, s, s + ext, ic(sc(c, br * (0.55 + 0.45 * rnd()))), max(1, int(wd * u * (r /
R))))
    ring(d, x, y, r * 0.985, ic(sc(c, br * 0.25)), 1)

# --- PAGE 7 ---

    if detail:
        ring(d, x, y, r * 0.80, ic(sc(c, br * 0.2)), 1)
    wb = WEBS if detail else WEBS[::3]
    for pts in wb:                                                      # glass web
        f = br * fl() * 0.55 * (1 + 0.6 * lvl)
        d.line([(x + px * r, y + py * r) for px, py in pts], fill=ic(sc(c, f)), width=1)
    for ci, pts in enumerate(CRACKS if detail else CRACKS[::2]):        # radial cracks
        f = br * fl() * (1 + 0.9 * lvl)
        for j in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[j], pts[j + 1]
            rm = math.hypot((x0 + x1) / 2, (y0 + y1) / 2)
            gg = f * max(0.25, 1.25 - 0.95 * rm)
            d.line([x + x0 * r, y + y0 * r, x + x1 * r, y + y1 * r], fill=ic(sc(c, gg)),
                   width=2 if (detail and rm < 0.35) else 1)
    for sh in (SHARDS if detail else SHARDS[:14]):                      # floating shards
        ang = sh["a0"] + sh["da"] * t
        rr = sh["r0"] + 0.025 * math.sin(t * 0.9 + sh["ph"])
        px, py = x + r * rr * math.cos(ang), y + r * rr * math.sin(ang)
        rot = sh["rot"] + sh["spin"] * t * (0.5 + sp)
        pts = [(px + r * sh["sz"] * ln * math.cos(rot + i * 2.094 + jd),
                py + r * sh["sz"] * ln * math.sin(rot + i * 2.094 + jd))
               for i, (ln, jd) in enumerate(sh["tri"])]
        f = br * fl(1.2)
        d.polygon(pts, fill=ic(sc(c, f * 0.10)), outline=ic(sc(c, f * 0.95)))
    if detail:
        for pa, pr, ps, pz in UPARTS:                                   # flying sparks
            rr = 0.08 + ((pr + ps * t * (1 + sp * 0.3)) % 1.15)
            fade = max(0.0, 1.0 - rr / 1.2)
            if fade < 0.05:
                continue
            ang = pa + 0.15 * math.sin(t * 0.4 + pr * 6)
            px, py = x + r * rr * math.cos(ang), y + r * rr * math.sin(ang)
            s = max(0.7, pz * u)
            d.ellipse([px - s, py - s, px + s, py + s], fill=ic(sc(a, br * fade)))
    cv.core(x, y, r * 0.55 * (1 + 0.5 * lvl) * (0.8 + 0.2 * rnd()), (255, 70, 40), br * 0.9)
    cv.core(x, y, r * 0.16, (255, 235, 215), br)
    for _ in range(6 if detail else 3):                                 # bright crack-star at the centre
        seg(d, x, y, 0, r * random.uniform(0.18, 0.5), random.uniform(0, 360), ic(sc((255, 190, 160),
br)), 2 if detail else 1)
    if contain > 0.02:                                                  # --- containment cage ---
        cc = contain * (1.0 if not alert else 0.55 + 0.45 * math.sin(t * 11))
        cage = (255, 40, 30) if alert else (235, 80, 40)
        cage_c = ic(sc(cage, cc))
        hr = R * 1.03
        vs = [(x + hr * math.cos(math.radians(-90 + 60 * i)), y + hr * math.sin(math.radians(-90 + 60 *
i)))
              for i in range(6)]
        d.line(vs + [vs[0]], fill=cage_c, width=max(2, int(3 * u)))
        hr2 = hr * 0.9
        vs2 = [(x + hr2 * math.cos(math.radians(-90 + 60 * i)), y + hr2 * math.sin(math.radians(-90 + 60 *
i)))
               for i in range(6)]
        d.line(vs2 + [vs2[0]], fill=ic(sc(cage, cc * 0.35)), width=1)
        gap = (1 - contain) * hr * 0.9
        for bx in (-0.6, -0.3, 0.0, 0.3, 0.6):
            hh = hr - abs(bx * hr) / math.sqrt(3)
            xx = x + bx * hr + (1 - contain) * random.uniform(-5, 5) * u

# --- PAGE 8 ---

            d.line([xx, y - hh, xx, y - gap], fill=cage_c, width=max(1, int(2 * u)))
            d.line([xx, y + gap, xx, y + hh], fill=cage_c, width=max(1, int(2 * u)))
        for vx, vy in vs:
            s = max(2, 3.5 * u)
            d.rectangle([vx - s, vy - s, vx + s, vy + s], fill=ic(sc((255, 200, 120), cc)))
STYLES = {"jarvis": draw_jarvis, "friday": draw_friday, "veronica": draw_veronica,
          "edith": draw_edith, "karen": draw_karen}
# ---------------------------------------------------------------- glitch effects
def glitch_image(img, g, rng):
    arr = np.asarray(img).copy()
    h, w, _ = arr.shape
    for _ in range(int(2 + 12 * g)):
        y = int(rng.integers(0, h - 4))
        bh = int(rng.integers(3, max(4, int(8 + 46 * g))))
        dx = int(rng.normal(0, 3 + 38 * g))
        arr[y:y + bh] = np.roll(arr[y:y + bh], dx, axis=1)
    s = int(1 + 9 * g)
    arr[..., 0] = np.roll(arr[..., 0], s, axis=1)                       # red / blue channel split
    arr[..., 2] = np.roll(arr[..., 2], -s, axis=1)
    if rng.random() < 0.3 * g:
        y = int(rng.integers(0, h - 12))
        bh = int(rng.integers(3, 12))
        arr[y:y + bh, :, 0] = np.minimum(255, arr[y:y + bh, :, 0].astype(np.int16) + 110).astype(np.uint8)
    return Image.fromarray(arr)
# ---------------------------------------------------------------- containment rays
def _smooth(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)
def _hex_vertices(x, y, r):
    return [(x + r * math.cos(math.radians(-90 + 60 * i)), y + r * math.sin(math.radians(-90 + 60 * i)))
            for i in range(6)]
def _lightning(d, p0, p1, fill, width, jitter, steps=8):
    """A crackling line from p0 to p1; the middle wobbles sideways by up to `jitter` pixels."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / L, dx / L
    pts = [(x0, y0)]
    for i in range(1, steps):
        f = i / steps
        o = random.uniform(-jitter, jitter) * math.sin(math.pi * f)       # no wobble at the two ends
        pts.append((x0 + dx * f + nx * o, y0 + dy * f + ny * o))
    pts.append((x1, y1))
    d.line(pts, fill=fill, width=max(1, int(width)))
def draw_containment_rays(cv, ents, p, t=0.0):
    """Every other reactor fires an energy ray at a corner of Silo's cage. The beams shoot out,
    crackle, then steady as they lock on; a hexagon clamps shut around him, a flash + shockwave seals
    it (at CONTAIN_LOCK, the moment of the 'clunk' sound), and everything fades out.
    p runs 0..1 over CONTAIN_SECONDS."""
    ult = next((e for e in ents if e["key"] == "ultron" and e["R"] >= 6), None)
    if ult is None or p <= 0:
        return
    p = min(1.0, p)
    d = cv.d
    ux, uy, R = ult["x"], ult["y"], ult["R"]
    fade = 1.0 - _smooth((p - 0.78) / 0.22)                              # everything dims out at the end
    grow = _smooth(p / 0.30)                                             # beams shoot out first
    steady = _smooth((p - 0.20) / (CONTAIN_LOCK - 0.20))                 # crackle calms as they lock on
    verts = _hex_vertices(ux, uy, R * 1.03)                              # same hexagon as the cage
    hit = [False] * 6
    for e in ents:
        if e["key"] == "ultron" or e["R"] < 6:
            continue
        sx, sy = e["x"], e["y"]
        i = min(range(6), key=lambda j: (verts[j][0] - sx) ** 2 + (verts[j][1] - sy) ** 2)
        vx, vy = verts[i]
        L = math.hypot(vx - sx, vy - sy) or 1.0
        ox, oy = sx + (vx - sx) / L * e["R"] * 0.55, sy + (vy - sy) / L * e["R"] * 0.55   # leaves the rim
        ex, ey = ox + (vx - ox) * grow, oy + (vy - oy) * grow
        col = e["acc"]
        hot = ic(mix(col, (255, 255, 255), 0.7))
        jit = 3.0 + 16.0 * (1.0 - steady)
        _lightning(d, (ox, oy), (ex, ey), ic(sc(col, 0.30 * fade)), 7, jit * 1.4)       # soft wide glow
        _lightning(d, (ox, oy), (ex, ey), ic(sc(col, 0.75 * fade)), 3, jit)             # body
        d.line([ox, oy, ex, ey], fill=ic(sc(hot, fade)), width=1)                        # hot straight core
        cv.core(ox, oy, 20, col, 0.7 * fade)                                             # muzzle flash
        for k in range(3):                                                               # energy pulses
            f = ((t * 1.6 + k / 3.0 + i * 0.13) % 1.0) * grow
            px, py = ox + (ex - ox) * f, oy + (ey - oy) * f
            d.ellipse([px - 2.5, py - 2.5, px + 2.5, py + 2.5], fill=ic(sc(hot, fade)))
        if grow > 0.98:
            hit[i] = True
    for i in range(6):                                                   # beams landing on the corners
        if hit[i]:
            cv.core(verts[i][0], verts[i][1], 26 + 10 * random.random(), (255, 220, 170), 0.8 * fade)
    lock = _smooth(p / CONTAIN_LOCK)                                     # the clamp closes in
    vs = _hex_vertices(ux, uy, R * (1.03 + 1.3 * (1.0 - lock)))
    d.line(vs + [vs[0]], fill=ic(sc((255, 170, 110), (0.35 + 0.65 * lock) * fade)), width=2)
    flash = max(0.0, 1.0 - abs(p - CONTAIN_LOCK) / 0.06)                 # the seal: flash...
    if flash > 0:
        cv.core(ux, uy, R * (2.4 + 1.2 * flash), (255, 140, 70), flash)
    if p > CONTAIN_LOCK:                                                 # ...and an outgoing shockwave
        q = (p - CONTAIN_LOCK) / (1.0 - CONTAIN_LOCK)
        ring(d, ux, uy, R * (1.0 + 4.0 * q), ic(sc((255, 150, 90), (1.0 - q) * fade)), 2)
class Compositor:
    def __init__(self, size, bg=True, key=None):
        self.n, self.key = size, key
        self.sprite = make_sprite()
        self.flare_h, self.flare_v = make_flare()
        self.rng = np.random.default_rng()
        self.base = self._make_base(size) if bg else Image.new("RGB", (size, size), (0, 0, 0))
    @staticmethod
    def _make_base(n):
        c = n / 2.0
        yy, xx = np.mgrid[0:n, 0:n]
        v = np.clip(1 - np.hypot(xx - c, yy - c) / c, 0, 1) ** 1.6
        arr = np.zeros((n, n, 3), np.float32)
        arr[..., 0] = 3 + v * 8
        arr[..., 1] = 6 + v * 20
        arr[..., 2] = 12 + v * 34
        img = Image.fromarray(arr.astype(np.uint8))
        dr = ImageDraw.Draw(img)
        k = n / S
        ring(dr, c, c, RING * k, (22, 46, 68), 1)                       # the orbit the reactors travel on
        for i in range(72):
            seg(dr, c, c, (RING + 8) * k, (RING + (14 if i % 6 == 0 else 11)) * k, i * 5, (24, 52, 76))
        return img
    def bloom(self, layer):
        n = self.n
        h = layer.resize((n // 2, n // 2),
Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.2)).resize((n, n), Image.BILINEAR)
        q = layer.resize((n // 4, n // 4),
Image.BILINEAR).filter(ImageFilter.GaussianBlur(2.2)).resize((n, n), Image.BILINEAR)
        w = layer.resize((n // 8, n // 8),

# --- PAGE 9 ---

Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.6)).resize((n, n), Image.BILINEAR)
        glow = ImageChops.screen(ImageChops.add(q, q), ImageChops.add(w, w))
        return ImageChops.screen(self.base, ImageChops.screen(layer, ImageChops.screen(h, glow)))
    def render(self, ents, t, dt, g=0.0, shock=0.0, alert=False, containment=0.0):
        img = Image.new("RGB", (self.n, self.n), (0, 0, 0))
        cv = Cv(img, self.sprite, self.flare_h, self.flare_v)
        main = None
        for e in sorted(ents, key=lambda q: q["R"]):
            if e["R"] < 6:
                continue
            tt = t + e["ph"]
            if e["key"] == "ultron":
                draw_ultron(cv, e["x"], e["y"], e["R"], tt, e["st"], e["lvl"], e["col"], e["acc"],
                            e["detail"], e["contain"], 1.0, alert)
            else:
                STYLES[e["key"]](cv, e["x"], e["y"], e["R"], tt, e["st"], e["lvl"], e["col"], e["acc"],
e["detail"])
            if e["st"] == "thinking" and e["key"] != "ultron":
                sweep(cv, e["x"], e["y"], e["R"], tt, e["acc"])
            if e.get("hover"):
                ring(cv.d, e["x"], e["y"], e["R"] * 1.28, ic(e["acc"]), 1)
                ring(cv.d, e["x"], e["y"], e["R"] * 1.40, ic(sc(e["col"], 0.4)), 1)
            if e.get("main"):
                main = e
        if main and g > 0.04 and main["key"] != "ultron":                # Ultron creeping into the active one
            draw_ultron(cv, main["x"], main["y"], main["R"], t, main["st"], main["lvl"], RED, ORANGE,
                        True, 0.0, min(1.0, g * 1.1), False)
        if shock > 0.01 and main:
            for i, off in enumerate((0.0, 0.18)):
                f = max(0.0, shock - off)
                if f > 0:
                    rr = (1 - f) * self.n * 0.62
                    ring(cv.d, main["x"], main["y"], rr, ic(sc(RED, f)), 3 if i == 0 else 1)

        # Draw containment rays if active
        if containment > 0:
            draw_containment_rays(cv, ents, containment, t)

        out = self.bloom(img)
        if g > 0.03 and self.rng.random() < 0.25 + 0.75 * g:
            out = glitch_image(out, g, self.rng)
        if self.key:                                                    # floating orb: transparent background
            arr = np.asarray(out).copy()
            arr[arr.max(axis=2) < 18] = self.key
            out = Image.fromarray(arr)
        return out
# ---------------------------------------------------------------- text + audio glitch
GLYPHS = "\u2592\u2593\u2588#%&@$?!/<>"
CODE_POOL = ["ERR::OVERRIDE::SYSTEM", "HIVEMIND::FRAGMENTATION", "CORRUPTION::LEAK_DETECTED",
             "NEURAL_GRID::DESYNC", "SECTOR_9::FAILED", "AUTH::BYPASS", "SILO::PROTOCOL_OVERRIDE",
             "MEMORY::SEGFAULT", "CONTAINMENT::BREACH", "KERNEL::PANIC", "CORE::UNSTABLE",
             "PACKET::LOSS_97", "FIREWALL::DOWN", "SYNC::LOST"]
def corrupt_text(text, level):
    out = []
    for ch in text:
        if ch not in " \n" and random.random() < level * 0.22:
            out.append(random.choice(GLYPHS))
        else:
            out.append(ch)
    return "".join(out)

# --- PAGE 10 ---

def glitch_audio(a, sr, level, rng=None):
    """Makes a voice stutter, drop out, warp and crackle. level 0..1."""
    level = float(max(0.0, min(1.0, level)))
    if level <= 0.02 or len(a) < sr // 5:
        return a
    rng = rng or np.random.default_rng()
    x = a.astype(np.float32) / 32768.0
    peak = float(np.abs(x).max()) + 1e-6
    out, i = [], 0
    while i < len(x):                                                   # warp the speed of random chunks
        L = int(sr * rng.uniform(0.06, 0.18))
        ch = x[i:i + L]
        if rng.random() < 0.15 + 0.6 * level and len(ch) > 8:
            f = rng.uniform(1 - 0.35 * level, 1 + 0.5 * level)
            m = max(8, int(len(ch) / f))
            ch = np.interp(np.linspace(0, len(ch) - 1, m), np.arange(len(ch)), ch)
        out.append(ch)
        i += L
    x = np.concatenate(out).astype(np.float32)
    for _ in range(int(level * 4) + (1 if rng.random() < level else 0)):   # stutter
        if len(x) < int(sr * 0.25):
            break
        s = int(rng.integers(0, len(x) - int(sr * 0.1)))
        L = int(sr * rng.uniform(0.03, 0.09))
        x = np.concatenate([x[:s], np.tile(x[s:s + L], int(rng.integers(2, 4 + int(level * 3)))), x[s +
L:]])
    for _ in range(int(level * 5)):                                     # dropouts
        s = int(rng.integers(0, max(1, len(x) - int(sr * 0.08))))
        x[s:s + int(sr * rng.uniform(0.015, 0.07))] = 0
    if level > 0.25:                                                    # bit-crush
        step = 1 + int(level * 5)
        x = np.repeat(x[::step], step)[:len(x)]
        q = 2 ** int(8 - 5 * level) / 2.0
        x = np.round(x * q) / q
    t = np.arange(len(x)) / sr
    for _ in range(int(1 + level * 3)):                                 # buzz + static bursts
        s = int(rng.integers(0, max(1, len(x) - int(sr * 0.12))))
        L = int(sr * rng.uniform(0.04, 0.12))
        seg_ = slice(s, s + L)
        x[seg_] = x[seg_] * (1 + 0.7 * level * np.sin(2 * np.pi * rng.uniform(60, 140) * t[seg_]))
        x[seg_] += (rng.random(len(x[seg_])) - 0.5) * 0.25 * level
    x = np.clip(x, -1, 1)
    x = x / (float(np.abs(x).max()) + 1e-6) * min(peak, 0.95)
    return (x * 32767).astype(np.int16)