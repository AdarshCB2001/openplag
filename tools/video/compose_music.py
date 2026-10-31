"""Original background track for the OpenPlag video (synthesised from scratch, royalty-free)."""
import os
import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt

SR = 44100
DUR = 56.0
N = int(SR * DUR)
BPM = 112
BEAT = 60 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(7)
SCENES = [5, 10, 16.5, 24.5, 31.5, 39, 45.5, 50.5]

def midi(n): return 440.0 * 2 ** ((n - 69) / 12)
def lp(x, f, order=2): return sosfilt(butter(order, f, "low", fs=SR, output="sos"), x)
def hp(x, f, order=2): return sosfilt(butter(order, f, "high", fs=SR, output="sos"), x)
def bp(x, lo, hi): return sosfilt(butter(2, [lo, hi], "band", fs=SR, output="sos"), x)

def env_adsr(n, a, d, s, r):
    a, d, r = int(a * SR), int(d * SR), int(r * SR)
    e = np.ones(n) * s
    e[:a] = np.linspace(0, 1, a, endpoint=False) if a else e[:a]
    e[a:a + d] = np.linspace(1, s, min(d, max(0, n - a)), endpoint=False)[: len(e[a:a + d])]
    if r and n > r:
        e[-r:] *= np.linspace(1, 0, r)
    return e

def add(buf, start, sig):
    i = int(start * SR)
    if i >= len(buf): return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i]

def saw(f, t, detune=0.0):
    out = np.zeros_like(t)
    for d in (-detune, 0, detune):
        ph = (f * (1 + d) * t) % 1.0
        out += 2 * ph - 1
    return out / 3

# Am – F – C – G   (vi–IV–I–V), with add9 colour
PROG = [(57, [57, 60, 64, 71]), (53, [53, 57, 60, 67]), (48, [55, 60, 64, 67]), (55, [55, 59, 62, 69])]

pad = np.zeros(N); bass = np.zeros(N); arp = np.zeros(N); drums = np.zeros(N); fx = np.zeros(N)

bars = int(np.ceil(DUR / BAR))
for b in range(bars):
    t0 = b * BAR
    root, chord = PROG[b % 4]
    # ---- pad: detuned saws, slow attack, low-passed
    n = int((BAR + 0.6) * SR); t = np.arange(n) / SR
    sig = sum(saw(midi(m), t, 0.004) for m in chord) / len(chord)
    sig = lp(sig, 1400) * env_adsr(n, 0.5, 0.3, 0.85, 0.6) * 0.32
    add(pad, t0, sig)
    # ---- bass: soft triangle on 8ths (from bar 3)
    if t0 >= 4.2 and t0 < 50.5:
        for k in range(8):
            n = int(BEAT / 2 * SR * 0.95); t = np.arange(n) / SR
            f = midi(root - 12 + (7 if k in (3, 7) else 0))
            tri = 2 * np.abs(2 * ((f * t) % 1) - 1) - 1
            add(bass, t0 + k * BEAT / 2, lp(tri, 600) * env_adsr(n, 0.005, 0.08, 0.6, 0.05) * (0.55 if k % 2 == 0 else 0.4))
    # ---- arpeggio: plucky 16ths (from step 1 scene, quieter in intro)
    if t0 < 52:
        level = 0.10 if t0 < 10 else 0.16
        pattern = [0, 2, 1, 3, 2, 1, 3, 2]
        for k in range(16):
            if t0 + k * BEAT / 4 >= 52: break
            m = chord[pattern[k % 8]] + 12
            n = int(0.35 * SR); t = np.arange(n) / SR
            s = np.sin(2 * np.pi * midi(m) * t) + 0.3 * np.sin(2 * np.pi * 2 * midi(m) * t)
            add(arp, t0 + k * BEAT / 4, s * np.exp(-t * 14) * level * (1.0 if k % 4 == 0 else 0.7))

# ---- drums from 5 s to 50.5 s
def kick():
    n = int(0.35 * SR); t = np.arange(n) / SR
    f = 50 + 90 * np.exp(-t * 30)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9) * 0.9
def clap():
    n = int(0.22 * SR); t = np.arange(n) / SR
    return bp(rng.standard_normal(n), 900, 3500) * np.exp(-t * 22) * 0.35
def hat(open_=False):
    n = int((0.12 if open_ else 0.05) * SR); t = np.arange(n) / SR
    return hp(rng.standard_normal(n), 7000) * np.exp(-t * (25 if open_ else 70)) * 0.12

beat_t = 5.0 - (5.0 % BEAT) + BEAT
i = 0
while beat_t < 50.4:
    pos = i % 4
    if pos in (0, 2): add(drums, beat_t, kick())
    if pos in (1, 3) and beat_t > 10: add(drums, beat_t, clap())
    add(drums, beat_t + BEAT / 2, hat(open_=(pos == 3)))
    if beat_t > 16.5: add(drums, beat_t, hat() * 0.6)
    beat_t += BEAT; i += 1

# ---- transitions: soft noise whoosh + low "boom" on each scene change
for s in SCENES:
    n = int(0.9 * SR); t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    w = np.clip(t / 0.9, 0, 1)
    sweep = lp(noise, 900) * (1 - w) + bp(noise, 2500, 7000) * w   # smooth low-to-high sweep, no clicks
    sweep *= np.sin(np.pi * np.clip(t / 0.9, 0, 1)) ** 2 * 0.22
    add(fx, s - 0.7, sweep)
    nb = int(0.6 * SR); tb = np.arange(nb) / SR
    add(fx, s, np.sin(2 * np.pi * 55 * tb) * np.exp(-tb * 6) * 0.25)

# ---- final chord (A minor add9) ringing out over the outro
n = int(5.5 * SR); t = np.arange(n) / SR
final = sum(np.sin(2 * np.pi * midi(m) * t) * np.exp(-t * 0.6) for m in [57, 64, 69, 71, 76]) / 5
add(fx, 50.5, lp(final, 3000) * 0.5)

# ---- mix
mix = pad * 0.9 + bass * 0.55 + arp + drums * 0.8 + fx
# side-chain style pump on pads/arp by kick (subtle)
mix = np.tanh(mix * 1.3) / 1.3
# fade in/out
fi = int(0.8 * SR); fo = int(3.5 * SR)
mix[:fi] *= np.linspace(0, 1, fi)
mix[-fo:] *= np.linspace(1, 0, fo) ** 1.5
# stereo widening: tiny delay on right channel for pad/arp
d = int(0.012 * SR)
L = mix
R = np.concatenate([mix[:d], mix[:-d]]) * 0.5 + mix * 0.5
st = np.stack([L, R], axis=1)
st /= np.max(np.abs(st)) / 0.89  # peak ≈ -1 dBFS
wavfile.write(os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.wav"), SR, (st * 32767).astype(np.int16))
print("written", st.shape, "rms dB", 20 * np.log10(np.sqrt(np.mean(st ** 2))))
