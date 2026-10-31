"""music.py - a different short piece of music for every transition. Synthesized with numpy: nothing to download.

  Echo     calm, clean: three rising notes with a soft echo
  Nova     bright, quick: a sparkling climb ending on a bell
  Aegis    firm, armour-plated: two hard hits a fourth apart and a bright ping
  Vortex   swirling: a pitch that spirals upward through a rush of air
  Atlas    slow, deep, powerful: a low open chord rising like a weight being lifted
  sleep    a soft descending lullaby          wake    a quick bright rise
  (Hades keeps the dark theme that voice.py already makes for him.)

The keys are the assistants' internal ids (jarvis = Echo, friday = Nova, edith = Aegis, karen = Vortex, veronica = Atlas).
"""
import numpy as np

SR = 22050
_rng = np.random.default_rng(7)


def _n(dur):
    return int(SR * dur)


def env(n, attack=0.01, decay=0.3, sustain=0.0, release=0.08):
    """Quick attack, exponential decay towards `sustain`, and a fade-out so a note never clicks at its end."""
    t = np.arange(n) / SR
    e = np.minimum(1.0, t / max(attack, 1e-4)) * (sustain + (1 - sustain) * np.exp(-t / max(decay, 1e-3)))
    return (e * np.clip((n / SR - t) / max(release, 1e-3), 0, 1)).astype(np.float32)


def note(freq, dur, amp=1.0, harmonics=(1.0,), attack=0.01, decay=0.3, sustain=0.0, release=0.08, glide=None):
    """One note: a stack of harmonics under an envelope. `glide` slides the pitch towards that frequency."""
    n = _n(dur)
    t = np.arange(n) / SR
    f = np.full(n, freq, np.float64) if glide is None else freq + (glide - freq) * (t / dur)
    ph = 2 * np.pi * np.cumsum(f) / SR
    wave = sum(h * np.sin(k * ph) for k, h in enumerate(harmonics, 1))
    return (wave * env(n, attack, decay, sustain, release) * amp / max(1.0, sum(harmonics) * 0.6)).astype(np.float32)


def hit(dur, amp=1.0, decay=0.04):
    """A short burst of noise (a metallic clank)."""
    n = _n(dur)
    return (_rng.uniform(-1, 1, n) * np.exp(-np.arange(n) / SR / decay) * amp).astype(np.float32)


def place(track, start, arr):
    i = _n(start)
    j = min(len(track), i + len(arr))
    if i < len(track):
        track[i:j] += arr[: j - i]


def add_echo(x, delay, gain, taps):
    y = x.copy()
    for k in range(1, taps + 1):
        d = _n(delay * k)
        if d < len(x):
            y[d:] += x[: len(x) - d] * gain ** k
    return y


def finish(x, peak=0.85):
    """Short fades at both ends, then a fixed loudness so no piece is louder than another."""
    x = x.astype(np.float32)
    f = _n(0.01)
    x[:f] *= np.linspace(0, 1, f)
    x[-f:] *= np.linspace(1, 0, f)
    return (x / (np.abs(x).max() + 1e-9) * peak).astype(np.float32)


# ---------------------------------------------------------------------- the pieces
def echo_theme():
    out = np.zeros(_n(2.2), np.float32)
    for i, f in enumerate((523.25, 659.25, 783.99)):                         # C E G, gently
        place(out, i * 0.24, note(f, 0.9, 0.6, (1.0, 0.25), attack=0.015, decay=0.35))
    return finish(add_echo(out, 0.28, 0.4, 3))


def nova_theme():
    out = np.zeros(_n(1.5), np.float32)
    for i, f in enumerate((1046.5, 1174.7, 1318.5, 1568.0, 1760.0, 2093.0, 2349.3, 2637.0)):   # a quick pentatonic climb
        place(out, i * 0.07, note(f, 0.35, 0.5, (1.0, 0.4, 0.15), attack=0.004, decay=0.12))
    place(out, 0.62, note(3135.9, 0.85, 0.45, (1.0, 0.3), attack=0.004, decay=0.35))              # the closing bell
    return finish(out)


def aegis_theme():
    out = np.zeros(_n(1.6), np.float32)
    for start, f in ((0.0, 196.0), (0.28, 261.6)):                          # G then C: a firm fourth
        place(out, start, note(f, 0.35, 0.8, (1.0, 0.7, 0.5, 0.35, 0.2), attack=0.003, decay=0.09))
        place(out, start, hit(0.12, 0.45))
    place(out, 0.62, note(1318.5, 0.95, 0.35, (1.0, 0.5), attack=0.002, decay=0.4))               # a bright ping
    return finish(out)


def vortex_theme():
    n = _n(2.0)
    t = np.arange(n) / SR
    f = 330 + 260 * np.sin(2 * np.pi * 2.2 * t) + 520 * t                    # the pitch spirals while climbing
    ph = 2 * np.pi * np.cumsum(f) / SR
    swell = np.sin(np.pi * np.minimum(1.0, t / 2.0)) ** 1.2
    wave = (np.sin(ph) + 0.35 * np.sin(2 * ph)) * (0.6 + 0.4 * np.sin(2 * np.pi * 7 * t)) * swell * 0.6
    air = _rng.uniform(-1, 1, n) * 0.12 * swell * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
    return finish(wave + air)


def atlas_theme():
    out = np.zeros(_n(2.5), np.float32)
    for f, a in ((65.41, 1.0), (98.0, 0.8), (130.81, 0.7), (164.81, 0.5)):    # C G C E, very low
        place(out, 0.0, note(f, 2.4, 0.5 * a, (1.0, 0.6, 0.4, 0.25, 0.15, 0.1), attack=0.35, decay=1.2, sustain=0.2, release=0.6))
    place(out, 0.0, note(90, 0.5, 0.9, (1.0,), attack=0.003, decay=0.15, glide=40))                # the weight lands
    return finish(out)


def sleep_theme():
    out = np.zeros(_n(2.2), np.float32)
    for i, f in enumerate((783.99, 659.25, 523.25)):                         # G E C, falling asleep
        place(out, i * 0.4, note(f, 1.0, 0.5, (1.0, 0.2), attack=0.04, decay=0.5, release=0.3))
    return finish(add_echo(out, 0.45, 0.35, 2), peak=0.6)


def wake_theme():
    out = np.zeros(_n(1.4), np.float32)
    for i, f in enumerate((523.25, 783.99, 1046.5)):                         # C G C, rising
        place(out, i * 0.13, note(f, 0.6, 0.6, (1.0, 0.5, 0.2), attack=0.005, decay=0.25))
    return finish(out)


THEMES = {"jarvis": echo_theme, "friday": nova_theme, "edith": aegis_theme, "karen": vortex_theme,
          "veronica": atlas_theme, "sleep": sleep_theme, "wake": wake_theme}
_cache = {}


def transition(name):
    """(int16 samples, sample rate) of the music for a transition, or None if there is none (made once, then kept)."""
    if name not in THEMES:
        return None
    if name not in _cache:
        _cache[name] = (np.int16(THEMES[name]() * 32767), SR)
    return _cache[name]