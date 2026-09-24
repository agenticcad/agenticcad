"""Original soundtrack for the promo video, synthesised from scratch (no samples, no licensing).
Ambient electronica: detuned pads, plucked arpeggio with ping-pong delay, sub bass, soft four-on-the-floor,
side-chain pumping, a hall reverb, and an arrangement that builds and resolves. Usage: music.py out.wav [seconds]"""
from __future__ import annotations

import sys
import numpy as np
from scipy import signal
from scipy.io import wavfile

SR = 44100
BPM = 100
BEAT = 60 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(7)

def midi(n): return 440.0 * 2 ** ((n - 69) / 12)

# A minor progression, 1 bar each: Am – F – C – G  (with 9ths/7ths for colour)
CHORDS = [[57, 60, 64, 71], [53, 57, 60, 67], [48, 55, 60, 64], [55, 59, 62, 67]]
ROOTS = [45, 41, 36, 43]

def env_adsr(n, a, d, s, r, hold):
    t = np.arange(n) / SR
    e = np.ones(n) * s
    e[t < a] = t[t < a] / a
    dm = (t >= a) & (t < a + d)
    e[dm] = 1 - (1 - s) * (t[dm] - a) / d
    rel = t >= hold
    e[rel] = s * np.exp(-(t[rel] - hold) / max(r, 1e-3))
    return e

def saw(freq, n, detune=0.0):
    t = np.arange(n) / SR
    f = freq * (1 + detune)
    out = np.zeros(n)
    for k in range(1, 14):            # band-limited saw
        out += ((-1) ** (k + 1)) * np.sin(2 * np.pi * f * k * t) / k
    return out * 2 / np.pi

def lowpass(x, cutoff, order=2):
    b, a = signal.butter(order, min(cutoff, SR / 2 - 100) / (SR / 2))
    return signal.lfilter(b, a, x)

def highpass(x, cutoff, order=2):
    b, a = signal.butter(order, cutoff / (SR / 2), btype="high")
    return signal.lfilter(b, a, x)

def pad_note(freq, dur, cutoff):
    n = int(dur * SR)
    x = saw(freq, n, -0.006) + saw(freq, n, 0.006) + 0.6 * saw(freq / 2, n, 0.002)
    x = lowpass(x, cutoff)
    return x * env_adsr(n, 0.9, 0.4, 0.8, 1.2, dur - 1.2)

def pluck(freq, dur=0.5):
    n = int(dur * SR); t = np.arange(n) / SR
    x = np.sin(2 * np.pi * freq * t) + 0.35 * signal.sawtooth(2 * np.pi * freq * t, 0.5) + 0.15 * np.sin(2 * np.pi * freq * 2 * t)
    return x * np.exp(-t / 0.16) * (1 - np.exp(-t / 0.002))

def kick(dur=0.35):
    n = int(dur * SR); t = np.arange(n) / SR
    f = 42 + 110 * np.exp(-t / 0.045)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / 0.14)
    click = highpass(rng.normal(0, 1, n), 2500) * np.exp(-t / 0.004) * 0.5
    return np.tanh(1.6 * (body + click))

def hat(dur=0.09, vel=1.0):
    n = int(dur * SR); t = np.arange(n) / SR
    return highpass(rng.normal(0, 1, n), 7000, 4) * np.exp(-t / 0.022) * 0.35 * vel

def clap(dur=0.25):
    n = int(dur * SR); t = np.arange(n) / SR
    x = np.zeros(n)
    for k, off in enumerate((0, 0.012, 0.024, 0.04)):
        i = int(off * SR)
        b = rng.normal(0, 1, n - i) * np.exp(-t[: n - i] / (0.02 if k < 3 else 0.09))
        x[i:] += b
    b_, a_ = signal.butter(2, [900 / (SR / 2), 4000 / (SR / 2)], btype="band")
    return signal.lfilter(b_, a_, x) * 0.55

def add(buf, x, t0, gain=1.0, pan=0.0):
    i = int(t0 * SR)
    if i >= buf.shape[1] or i < 0:
        return
    n = min(len(x), buf.shape[1] - i)
    l = np.sqrt(0.5 * (1 - pan)); r = np.sqrt(0.5 * (1 + pan))
    buf[0, i:i + n] += x[:n] * gain * l
    buf[1, i:i + n] += x[:n] * gain * r

def reverb(x, seconds=2.2, mix=0.28):
    n = int(seconds * SR); t = np.arange(n) / SR
    ir = rng.normal(0, 1, (2, n)) * np.exp(-t / (seconds / 3.5))
    ir = lowpass(ir, 5000)
    wet = np.stack([signal.fftconvolve(x[c], ir[c])[: x.shape[1]] for c in range(2)])
    return x + mix * wet / (np.abs(wet).max() + 1e-9) * np.abs(x).max()

def render(total=92.0):
    N = int(total * SR)
    pads = np.zeros((2, N)); arps = np.zeros((2, N)); bass = np.zeros((2, N)); drums = np.zeros((2, N))
    n_bars = int(total / BAR) + 1
    intro_bars, drums_in, arp_in, outro_bars = 2, 6, 3, n_bars - 3
    kick_times = []
    for bar in range(n_bars):
        t0 = bar * BAR
        chord = CHORDS[bar % 4]; root = ROOTS[bar % 4]
        section_cut = 700 + 2200 * min(1.0, bar / 10)          # filter opens as the track builds
        if bar >= outro_bars:
            section_cut = 900
        for k, note in enumerate(chord):
            add(pads, pad_note(midi(note), BAR + 0.8, section_cut), t0, gain=0.11, pan=(-0.5 + k * 0.33))
        if arp_in <= bar < outro_bars + 1:
            pattern = [0, 2, 1, 3, 2, 0, 3, 1, 0, 2, 3, 1, 2, 0, 1, 3]
            for s in range(16):
                if bar < drums_in and s % 2:      # sparse before the drums come in
                    continue
                note = chord[pattern[s]] + (12 if s % 5 == 0 else 0)
                add(arps, pluck(midi(note)), t0 + s * BEAT / 4, gain=0.16 * (0.75 + 0.25 * (s % 4 == 0)), pan=0.6 * np.sin(s))
        if drums_in <= bar < outro_bars:
            for b in range(4):
                tb = t0 + b * BEAT
                add(drums, kick(), tb, gain=0.9); kick_times.append(tb)
                add(drums, hat(vel=0.8), tb + BEAT / 2, gain=1.0, pan=0.3)
                if bar >= drums_in + 2:
                    add(drums, hat(vel=0.45), tb + BEAT / 4, gain=1.0, pan=-0.3); add(drums, hat(vel=0.45), tb + 3 * BEAT / 4, gain=1.0, pan=-0.3)
                if b in (1, 3) and bar >= drums_in + 4:
                    add(drums, clap(), tb, gain=0.5)
            # bass: gated 8ths on the root
            for e in range(8):
                nb = int(BEAT / 2 * SR); tt = np.arange(nb) / SR
                x = np.sin(2 * np.pi * midi(root) * tt) + 0.3 * np.sin(2 * np.pi * midi(root) * 2 * tt)
                x = np.tanh(1.8 * x) * env_adsr(nb, 0.005, 0.05, 0.7, 0.05, BEAT / 2 - 0.06)
                add(bass, x, t0 + e * BEAT / 2, gain=0.42 if e % 2 == 0 else 0.3)
    # side-chain pump on pads + arps
    duck = np.ones(N)
    for tk in kick_times:
        i = int(tk * SR); n = int(0.28 * SR)
        seg = 1 - 0.55 * np.exp(-np.arange(n) / (0.09 * SR))
        duck[i:i + n] = np.minimum(duck[i:i + n], seg[: max(0, min(n, N - i))])
    pads *= duck; arps *= duck
    # ping-pong delay on arps (dotted 8th)
    d = int(BEAT * 0.75 * SR); echo = np.zeros_like(arps)
    g, cur, swap = 0.42, arps.copy(), True
    for _ in range(4):
        cur = np.roll(cur, d, axis=1); cur[:, :d] = 0; cur *= g
        cur = cur[::-1] if swap else cur; swap = not swap
        echo += cur
    arps = arps + lowpass(echo, 3500)
    mix = reverb(pads, 3.0, 0.35) + reverb(arps, 1.6, 0.22) + bass + drums
    # master: gentle glue, fades, normalise
    mix = np.tanh(mix * 1.15) / np.tanh(1.15)
    t = np.arange(N) / SR
    fade = np.minimum(1, t / 3.0) * np.minimum(1, np.maximum(0, (total - t) / 6.0))
    mix *= fade
    mix = mix / (np.abs(mix).max() + 1e-9) * 0.89
    return mix

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "promo-music.wav"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 92.0
    m = render(secs)
    wavfile.write(out, SR, (m.T * 32767).astype(np.int16))
    rms = float(np.sqrt(np.mean(m ** 2)))
    print(f"wrote {out}: {secs:.0f}s, peak {np.abs(m).max():.2f}, rms {rms:.3f} ({20*np.log10(rms+1e-9):.1f} dBFS)")
