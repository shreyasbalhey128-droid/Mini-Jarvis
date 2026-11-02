"""
photo_helper.py - edit suggestions for your photos, for Mini-Jarvis (made for wildlife shots edited in Lightroom).

Drop a photo on the Jarvis window (or just shot one and exported it), then say:
    "how should I edit this photo"      "edit suggestions for this picture"      "critique this photo"
    "is this photo sharp"               "how can I improve this photo"           "what's wrong with this photo"
    "how should I edit my latest photo" "how should I edit the photo called kite"
Echo gives the 3 or 4 most important Lightroom moves out loud; the full list with slider values (Exposure,
Highlights, Shadows, Whites, Blacks, Contrast, Vibrance, Texture, Sharpening, Noise reduction, Crop) is on screen.

How it works: it MEASURES the picture (brightness spread, blown highlights, crushed shadows, colour strength,
how sharp the sharpest part is, how noisy the flat parts are) and reads the camera's EXIF data (ISO, shutter speed,
aperture, focal length) when the file has it. The suggestions are rules of thumb turned into slider numbers,
a starting point to nudge in Lightroom, not an artistic verdict. It can't see WHAT is in the photo, so
composition (where the bird sits in the frame) is only advised in general terms.

Works on JPG / PNG / WebP / BMP photos. It can't open camera RAW files (.CR2): export a JPG from Lightroom first.
Needs: numpy and Pillow (you already have both).
"""
import math
import re

import numpy as np

EXPORT_HINT = "I can only open JPG, PNG and WebP photos. Export a JPG from Lightroom, then ask again."
MAX_SIDE = 2400                 # the picture is shrunk to this before measuring (keeps it quick)


def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


# ---------------- reading the file ----------------
def _exif(img):
    """ISO, shutter, aperture, focal length, camera as far as the file knows them."""
    out = {}
    try:
        ex = img.getexif()
        sub = ex.get_ifd(0x8769) if hasattr(ex, "get_ifd") else {}
        iso = sub.get(0x8827) or ex.get(0x8827)
        if isinstance(iso, (tuple, list)):
            iso = iso[0] if iso else None
        if iso:
            out["iso"] = int(iso)
        t = sub.get(0x829A)
        if t:
            out["shutter"] = float(t)
        f = sub.get(0x829D)
        if f:
            out["aperture"] = float(f)
        fl = sub.get(0x920A)
        if fl:
            out["focal"] = float(fl)
        model = ex.get(0x0110)
        if model:
            out["camera"] = str(model).strip()
    except Exception:
        pass
    return out


def _percentiles(lum):
    return {k: float(np.percentile(lum, k)) for k in (1, 5, 50, 95, 99)}


def _tiles(a, n=8):
    h, w = a.shape[0] // n * n, a.shape[1] // n * n
    return a[:h, :w].reshape(n, h // n, n, w // n)


def analyze(path):
    """Measurements of a photo. Raises ValueError if it can't be opened."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(path)
        info = _exif(img)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise ValueError(str(e))
    width, height = img.size
    if max(img.size) > MAX_SIDE:
        s = MAX_SIDE / max(img.size)
        img = img.resize((max(1, int(width * s)), max(1, int(height * s))))
    rgb = np.asarray(img, dtype=np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b

    a = {"width": width, "height": height, "exif": info}
    a["pct"] = _percentiles(lum)
    a["blown"] = float((lum >= 250).mean() * 100)             # % of pixels at pure white
    a["crushed"] = float((lum <= 5).mean() * 100)             # % at pure black
    a["range"] = a["pct"][95] - a["pct"][5]

    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    a["saturation"] = float(np.mean((mx - mn) / np.maximum(mx, 1.0)))
    a["warmth"] = float(r.mean() / max(b.mean(), 1.0))

    h, w = lum.shape                                           # a bright background with a darker subject
    cy0, cy1, cx0, cx1 = int(h * .3), int(h * .7), int(w * .3), int(w * .7)
    centre = lum[cy0:cy1, cx0:cx1]
    mask = np.ones_like(lum, dtype=bool)
    mask[cy0:cy1, cx0:cx1] = False
    a["centre_mean"], a["edge_mean"] = float(centre.mean()), float(lum[mask].mean())

    def laplacian(x):
        return 4 * x[1:-1, 1:-1] - x[:-2, 1:-1] - x[2:, 1:-1] - x[1:-1, :-2] - x[1:-1, 2:]

    def tile_vars(x):
        return _tiles(laplacian(x)).var(axis=(1, 3)).ravel()

    raw = tile_vars(lum)                                       # grain: how rough the flattest areas are
    flat = np.sort(raw)[: max(4, len(raw) // 4)]
    a["noise"] = float(math.sqrt(float(np.median(flat))) / math.sqrt(20.0))   # about the grain's size in brightness levels

    pad = np.pad(lum, 1, mode="edge")                          # sharpness on a lightly smoothed copy, minus the grain's share
    smooth = sum(pad[i:i + lum.shape[0], j:j + lum.shape[1]] for i in range(3) for j in range(3)) / 9.0
    sv = tile_vars(smooth)
    floor = float(np.median(np.sort(sv)[: max(4, len(sv) // 4)]))
    a["sharp_peak"] = max(0.0, float(np.percentile(sv, 95)) - floor)   # the sharpest part (your subject)
    return a


# ---------------- measurements -> advice ----------------
def _signed(v, step=1):
    v = int(round(v / step) * step)
    return f"+{v}" if v > 0 else str(v)


def _ev(a):
    """Exposure change in stops that would bring the middle grey to about 118, damped and capped."""
    med = max(a["pct"][50], 1.0)
    ev = 2.2 * math.log2(118.0 / med) * 0.6
    return max(-1.5, min(1.5, round(ev * 10) / 10))


def suggest(a):
    """[(priority, 'Slider +N', 'why')] sorted most important first, plus notes about the shot itself."""
    s, notes = [], []
    p = a["pct"]
    ex = a["exif"]
    bright_bg = (a["edge_mean"] - a["centre_mean"] > 35 and a["centre_mean"] < 110) \
        or (p[50] > 150 and p[1] < 90)                          # a darker subject against a bright sky / background
    ev = _ev(a)

    if bright_bg:
        s.append((90, "Shadows +50", "your subject is darker than the bright background around it; "
                                     "that's normal for a bird against the sky, so lift the subject, not the sky"))
    elif abs(ev) >= 0.3 and (p[50] < 90 or p[50] > 150):
        s.append((85, f"Exposure {ev:+.1f}", "the picture is dark overall" if ev > 0 else "the picture is bright overall"))
    if a["blown"] > 1.5:
        s.append((95, f"Highlights {_signed(-min(90, 40 + a['blown'] * 6), 5)}",
                  f"{a['blown']:.1f}% of the picture is pure white (blown highlights)"))
        s.append((50, f"Whites {_signed(-min(50, 15 + a['blown'] * 3), 5)}", "to bring the brightest parts back"))
    elif p[99] < 235 and p[95] < 205 and not bright_bg:
        s.append((45, "Whites +25", "nothing in the picture reaches full brightness, so it looks flat"))
    if a["crushed"] > 6:
        s.append((70, f"Shadows {_signed(min(60, 25 + a['crushed'] * 3), 5)}",
                  f"{a['crushed']:.1f}% of the picture is pure black (lost detail)"))
        s.append((40, "Blacks +10", "to lift the very darkest parts"))
    elif p[5] > 45 and not bright_bg:
        s.append((45, "Blacks -20", "there is no real black, so the picture looks washed out"))
    if a["range"] < 115 and not bright_bg:
        s.append((55, "Contrast +20", "the tones are bunched together (low contrast)"))
    elif a["range"] > 215:
        s.append((35, "Contrast -10", "very strong contrast; this softens it"))
    if a["saturation"] < 0.20:
        s.append((40, "Vibrance +20", "the colours are muted"))
    elif a["saturation"] > 0.55:
        s.append((40, "Saturation -10", "the colours are very strong"))
    if a["warmth"] > 1.4:
        notes.append("It looks quite warm. Check White Balance (Temp) if that wasn't the golden-hour look you wanted.")
    elif a["warmth"] < 0.8:
        notes.append("It looks quite cool or blue. Check White Balance (Temp) if the light wasn't really that cold.")

    iso = ex.get("iso")
    noisy = a["noise"] > 2.5 or (iso or 0) >= 1600
    if noisy:
        lum_nr = 40 if (iso or 0) >= 3200 or a["noise"] > 4 else 30
        s.append((75, f"Noise Reduction (Luminance) {lum_nr}, Colour 25",
                  "grain is visible" + (f" at ISO {iso}" if iso else "")))
    elif (iso or 0) >= 800 or a["noise"] > 1.5:
        s.append((40, "Noise Reduction (Luminance) 15", "a little grain" + (f" at ISO {iso}" if iso else "")))

    soft = a["sharp_peak"] < 6                                   # rough rule: below this even the sharpest part is soft
    if soft:
        notes.append("Even its sharpest part looks soft. Sharpening won't rescue real focus or motion blur, "
                     "so if the eye isn't crisp at 100% zoom, consider picking another frame.")
        s.append((65, "Sharpening Amount 60, Radius 1.0, Masking 40", "a small boost; don't push it, it only adds noise"))
    else:
        s.append((35, f"Sharpening Amount {70 if a['sharp_peak'] < 20 else 50}, Radius 0.9, Masking {50 if noisy else 30}",
                  "bring out feather detail without sharpening the grain"))
        if not noisy:
            s.append((25, "Texture +10", "a touch of feather and fur detail"))

    shutter, focal = ex.get("shutter"), ex.get("focal")
    if shutter and focal:
        if shutter > 1.0 / (focal * 1.6):
            notes.append(f"The shutter was 1/{int(round(1 / shutter))} at {int(focal)}mm, slower than the usual 1 over focal "
                         "length rule on a crop-sensor camera, so camera shake could be the softness.")
        elif shutter > 1 / 1000 and focal >= 200:
            notes.append(f"1/{int(round(1 / shutter))} is on the slow side for a bird in flight. 1/1600 or faster freezes the wings.")
    if ex.get("aperture") and ex["aperture"] >= 11:
        notes.append(f"f/{ex['aperture']:.0f} is small; it can cost sharpness. Wide open or one stop down is usually best.")

    if min(a["width"], a["height"]) < 1080:
        notes.append(f"It's only {a['width']}x{a['height']} pixels, below Instagram's 1080 width. Export from the original.")
    s.sort(key=lambda x: -x[0])
    return s, notes


def _say(slider):
    """A slider line the way you'd say it: 'Sharpening Amount 50, Radius 0.9...' -> 'sharpening around 50'."""
    m = re.match(r"Sharpening Amount (\d+)", slider)
    if m:
        return f"sharpening around {m.group(1)}"
    m = re.match(r"Noise Reduction \(Luminance\) (\d+)", slider)
    if m:
        return f"noise reduction around {m.group(1)}"
    return slider.replace(" -", " minus ").replace(" +", " plus ")


def report(a):
    """(spoken, full): what Echo says, and the complete list for the screen."""
    s, notes = suggest(a)
    ex = a["exif"]
    top = [_say(x[1]) for x in s[:4]]
    spoken = ("In Lightroom I'd try: " + ", ".join(top) + ". "
              if top else "The exposure and colour look balanced, so it needs very little. ")
    if notes:
        spoken += notes[0].split(". ")[0].rstrip(".") + ". "
    spoken += "The full list is on screen."

    shot = []
    if ex.get("camera"):
        shot.append(ex["camera"])
    if ex.get("focal"):
        shot.append(f"{int(ex['focal'])}mm")
    if ex.get("aperture"):
        shot.append(f"f/{ex['aperture']:.1f}")
    if ex.get("shutter"):
        shot.append(f"1/{int(round(1 / ex['shutter']))}s" if ex["shutter"] < 1 else f"{ex['shutter']:.1f}s")
    if ex.get("iso"):
        shot.append(f"ISO {ex['iso']}")
    lines = ["PHOTO CHECK" + (" - " + ", ".join(shot) if shot else ""),
             f"{a['width']}x{a['height']}   sharpness: {a['sharp_peak']:.0f} (under 6 is soft)   grain: {a['noise']:.1f}   "
             f"blown: {a['blown']:.1f}%   crushed: {a['crushed']:.1f}%", "", "Lightroom, in this order:"]
    lines += [f"  {i}. {t}  ({why})" for i, (_, t, why) in enumerate(s, 1)] or ["  (nothing needed)"]
    if notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in notes]
    lines += ["", "Crop: Instagram's feed looks best at 4:5 (1080x1350). For a bird in flight, crop so it fills about a third "
                  "of the frame, with a little more room in front of it than behind.",
              "These are starting points from measurements, not a verdict. Nudge by eye."]
    return spoken, "\n".join(lines)


# ---------------- what you said ----------------
_PHOTO = r"(?:photo|picture|pic|image|shot|photograph|frame)"


def match(c):
    """{'which': 'dropped'|'latest'|'named', 'name'?} for photo-advice requests, else None."""
    t = _norm(c)
    if not t:
        return None
    asks = (r"how (?:should|can|do|could) i (?:edit|improve|fix|enhance|grade|process|retouch)|"
            r"(?:edit|editing) (?:suggestions?|tips|advice|ideas) (?:for|on)|what (?:edits?|adjustments?|changes?) (?:does|should|would)|"
            r"what s wrong with|whats wrong with|what is wrong with|critique|give me (?:some )?(?:feedback|tips|advice) on|"
            r"is (?:this|that|it) (?:photo|picture|pic|image|shot) (?:sharp|noisy|too dark|too bright|overexposed|underexposed|blurry|in focus)|"
            r"is (?:this|that|the) .{0,20}?(?:sharp|noisy|blurry|in focus|overexposed|underexposed)|"
            r"how (?:can|do) i make (?:this|that|it) (?:look )?better|improve|lightroom (?:settings|tips|advice)")
    if not re.search(asks, t) or not re.search(r"\b" + _PHOTO + r"s?\b", t):
        return None
    if re.search(r"\b(?:what is in|whats in|describe|what do you see|read the text)\b", t):
        return None                                                  # that is the vision feature, not edit advice
    m = re.search(r"\b" + _PHOTO + r" (?:called|named) (?P<n>.+)", t)
    if m:
        return {"which": "named", "name": m.group("n")}
    if re.search(r"\b(?:latest|last|newest|most recent|recent|new)\b", t):
        return {"which": "latest"}
    return {"which": "dropped"}