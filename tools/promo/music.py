"""Original soundtrack for the promo video, synthesised from scratch (no samples, no licensing).
Punchy, upbeat electro/house: four-on-the-floor kick with a hard click, clap on 2 and 4, 16th hats, driving
saw bass, supersaw chord stabs, plucked lead arpeggio with ping-pong delay, risers and crashes at the drops,
heavy side-chain pumping. Arrangement: 2-bar intro → drop → breakdown → second drop → short outro.
Usage: music.py out.wav [seconds]"""
from __future__ import annotations

import sys
import numpy as np
from scipy import signal
from scipy.io import wavfile

SR = 44100
BPM = 126
BEAT = 60 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(11)

def midi(n): return 440.0 * 2 ** ((n - 69) / 12)

# F#m – D – A – E : energetic, major-leaning, 1 bar each
CHORDS = [[54, 57, 61, 66], [50, 54, 57, 62], [57, 61, 64, 69], [52, 56, 59, 64]]
ROOTS = [42, 38, 45, 40]

def lowpass(x, cutoff, order=2):
    b, a = signal.butter(order, min(cutoff, SR / 2 - 100) / (SR / 2)); return signal.lfilter(b, a, x)

def highpass(x, cutoff, order=2):
    b, a = signal.butter(order, cutoff / (SR / 2), btype="high"); return signal.lfilter(b, a, x)

def saw(freq, n, detune=0.0, harmonics=18):
    t = np.arange(n) / SR; f = freq * (1 + detune); out = np.zeros(n)
    for k in range(1, harmonics + 1):
        if f * k > 16000: break
        out += ((-1) ** (k + 1)) * np.sin(2 * np.pi * f * k * t) / k
    return out * 2 / np.pi

def env(n, a, d, s, r, hold):
    t = np.arange(n) / SR; e = np.full(n, s)
    e[t < a] = t[t < a] / max(a, 1e-4)
    dm = (t >= a) & (t < a + d); e[dm] = 1 - (1 - s) * (t[dm] - a) / max(d, 1e-4)
    rel = t >= hold; e[rel] = s * np.exp(-(t[rel] - hold) / max(r, 1e-3))
    return e

def supersaw(freq, dur, cutoff, stab=True):
    n = int(dur * SR)
    x = sum(saw(freq, n, d) for d in (-0.011, -0.005, 0, 0.005, 0.011)) + 0.5 * saw(freq / 2, n)
    x = lowpass(x, cutoff, 2)
    e = env(n, 0.004, 0.18, 0.35, 0.12, dur * 0.55) if stab else env(n, 0.6, 0.5, 0.8, 0.8, dur - 0.8)
    return x * e

def bass_note(freq, dur, cutoff=900):
    n = int(dur * SR); t = np.arange(n) / SR
    x = saw(freq, n) + 0.8 * np.sin(2 * np.pi * freq * t) + 0.4 * saw(freq, n, 0.004)
    x = lowpass(x, cutoff) * env(n, 0.003, 0.12, 0.55, 0.04, dur - 0.05)
    return np.tanh(2.2 * x)

def pluck(freq, dur=0.35):
    n = int(dur * SR); t = np.arange(n) / SR
    x = np.sin(2 * np.pi * freq * t) + 0.5 * signal.sawtooth(2 * np.pi * freq * t, 0.5) + 0.25 * np.sin(2 * np.pi * freq * 2 * t)
    return lowpass(x, 6000) * np.exp(-t / 0.11) * (1 - np.exp(-t / 0.0015))

def kick(dur=0.32):
    n = int(dur * SR); t = np.arange(n) / SR
    f = 48 + 150 * np.exp(-t / 0.032); ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / 0.13)
    click = highpass(rng.normal(0, 1, n), 1800) * np.exp(-t / 0.0035) * 0.9
    return np.tanh(2.4 * (body + click)) * 0.95

def clap(dur=0.32):
    n = int(dur * SR); t = np.arange(n) / SR; x = np.zeros(n)
    for k, off in enumerate((0, 0.011, 0.022, 0.036)):
        i = int(off * SR); x[i:] += rng.normal(0, 1, n - i) * np.exp(-t[: n - i] / (0.018 if k < 3 else 0.11))
    b, a = signal.butter(2, [800 / (SR / 2), 6500 / (SR / 2)], btype="band")
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.05) * 0.5
    return signal.lfilter(b, a, x) * 0.7 + tone

def hat(vel=1.0, open_=False):
    dur = 0.32 if open_ else 0.07; n = int(dur * SR); t = np.arange(n) / SR
    return highpass(rng.normal(0, 1, n), 8000, 4) * np.exp(-t / (0.09 if open_ else 0.018)) * 0.3 * vel

def crash(dur=2.2):
    n = int(dur * SR); t = np.arange(n) / SR
    return highpass(rng.normal(0, 1, n), 3000, 2) * np.exp(-t / 0.7) * 0.5

def riser(dur):
    n = int(dur * SR); t = np.arange(n) / SR
    noise = rng.normal(0, 1, n)
    out = np.zeros(n); step = 2048
    for i in range(0, n, step):              # sweep the filter up over the riser
        frac = i / n; b, a = signal.butter(2, [max(200, 200 + 6000 * frac ** 2) / (SR / 2), min(19000, 900 + 12000 * frac) / (SR / 2)], btype="band")
        out[i:i + step] = signal.lfilter(b, a, noise[i:i + step])
    return out * (t / dur) ** 2 * 0.6

def add(buf, x, t0, gain=1.0, pan=0.0):
    i = int(t0 * SR)
    if i < 0 or i >= buf.shape[1]: return
    n = min(len(x), buf.shape[1] - i); l = np.sqrt(0.5 * (1 - pan)); r = np.sqrt(0.5 * (1 + pan))
    buf[0, i:i + n] += x[:n] * gain * l; buf[1, i:i + n] += x[:n] * gain * r

def reverb(x, seconds=1.6, mix=0.22):
    n = int(seconds * SR); t = np.arange(n) / SR
    ir = lowpass(rng.normal(0, 1, (2, n)) * np.exp(-t / (seconds / 3.2)), 6000)
    wet = np.stack([signal.fftconvolve(x[c], ir[c])[: x.shape[1]] for c in range(2)])
    return x + mix * wet / (np.abs(wet).max() + 1e-9) * np.abs(x).max()

def render(total=92.0):
    N = int(total * SR)
    chords = np.zeros((2, N)); arps = np.zeros((2, N)); bass = np.zeros((2, N)); drums = np.zeros((2, N)); fx = np.zeros((2, N))
    n_bars = int(total / BAR) + 1
    DROP1, BREAK, DROP2, OUTRO = 2, 22, 26, n_bars - 3
    kicks = []
    stab_pattern = [0.5, 1.5, 2.5, 3.5]                         # offbeat stabs (house)
    bass_pattern = [(0, 0), (0.5, 0), (1, 12), (1.5, 0), (2, 0), (2.5, 7), (3, 0), (3.5, 12)]   # (beat, semitone offset)
    arp_pattern = [0, 2, 1, 3, 2, 0, 3, 1, 0, 3, 1, 2, 3, 0, 2, 1]
    for bar in range(n_bars):
        t0 = bar * BAR; chord = CHORDS[bar % 4]; root = ROOTS[bar % 4]
        intro = bar < DROP1; breakdown = BREAK <= bar < DROP2; outro = bar >= OUTRO
        full = not intro and not breakdown and not outro
        cutoff = 900 + 5200 * min(1.0, (bar - DROP1 + 1) / 4) if not intro else 700 + 800 * bar
        if breakdown or outro:
            for k, note in enumerate(chord):                  # sustained pad instead of stabs
                add(chords, supersaw(midi(note), BAR + 0.6, 1800 if breakdown else 1200, stab=False), t0, gain=0.07, pan=-0.5 + k * 0.33)
        else:
            for beat in stab_pattern:
                for k, note in enumerate(chord):
                    add(chords, supersaw(midi(note), BEAT * 0.9, cutoff), t0 + beat * BEAT, gain=0.085, pan=-0.45 + k * 0.3)
        if not intro and not outro:
            for beat, off in bass_pattern:
                if breakdown and beat % 1: continue
                add(bass, bass_note(midi(root + off), BEAT / 2 * 0.95, 700 if breakdown else 1300), t0 + beat * BEAT, gain=0.5)
        if not intro:
            for s in range(16):
                note = chord[arp_pattern[s]] + (12 if s % 3 == 0 else 24 if s % 7 == 0 else 12)
                if outro and s % 2: continue
                add(arps, pluck(midi(note)), t0 + s * BEAT / 4, gain=0.15 * (1.0 if s % 4 == 0 else 0.7), pan=0.7 * np.sin(s * 1.3))
        if (full or intro) and not outro:
            for b in range(4):
                tb = t0 + b * BEAT
                if not intro or bar >= 1:
                    add(drums, kick(), tb, gain=1.0); kicks.append(tb)
                if b in (1, 3) and not intro: add(drums, clap(), tb, gain=0.75)
                add(drums, hat(vel=0.9, open_=True), tb + BEAT / 2, gain=0.8)
                for q in range(4):
                    if not (intro and q % 2): add(drums, hat(vel=0.55 if q % 2 else 0.85), tb + q * BEAT / 4, gain=0.7, pan=0.25 * (1 if q % 2 else -1))
        if breakdown:                                          # sparse kick + hats keep momentum
            for b in (0, 2): add(drums, kick(), t0 + b * BEAT, gain=0.7); kicks.append(t0 + b * BEAT)
            for q in range(8): add(drums, hat(vel=0.5), t0 + q * BEAT / 2, gain=0.6)
        if bar in (DROP1 - 1, DROP2 - 1):                      # riser into each drop
            add(fx, riser(BAR), t0, gain=0.9)
        if bar in (DROP1, DROP2):
            add(fx, crash(), t0, gain=0.8)
    # side-chain pump (chords, bass, arps duck to every kick)
    duck = np.ones(N)
    for tk in kicks:
        i = int(tk * SR); n = int(0.24 * SR); seg = 1 - 0.7 * np.exp(-np.arange(n) / (0.075 * SR))
        m = max(0, min(n, N - i)); duck[i:i + m] = np.minimum(duck[i:i + m], seg[:m])
    chords *= duck; bass *= duck; arps *= duck
    # ping-pong delay on the arp (dotted 8th)
    d = int(BEAT * 0.75 * SR); echo = np.zeros_like(arps); cur = arps.copy(); swap = True
    for _ in range(4):
        cur = np.roll(cur, d, axis=1); cur[:, :d] = 0; cur *= 0.45; cur = cur[::-1] if swap else cur; swap = not swap; echo += cur
    arps = arps + lowpass(echo, 4000)
    mix = reverb(chords, 1.4, 0.2) + reverb(arps, 1.2, 0.18) + bass + drums + reverb(fx, 2.0, 0.3)
    mix = np.tanh(mix * 1.35) / np.tanh(1.35)                   # glue / soft clip for punch
    t = np.arange(N) / SR
    mix *= np.minimum(1, t / 0.6) * np.minimum(1, np.maximum(0, (total - t) / 5.0))
    return mix / (np.abs(mix).max() + 1e-9) * 0.93

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "promo-music.wav"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 92.0
    m = render(secs)
    wavfile.write(out, SR, (m.T * 32767).astype(np.int16))
    rms = float(np.sqrt(np.mean(m ** 2)))
    print(f"wrote {out}: {secs:.0f}s @ {BPM} BPM, peak {np.abs(m).max():.2f}, rms {20*np.log10(rms+1e-9):.1f} dBFS")
