"""Master sound processing on the program output (NDI, stream, recording, SDI, PC speakers):

  Auto level (AGC)   brings quiet and loud sources to one level (the target, about LUFS), slowly,
                     like a broadcast leveller; pauses and silence are not pumped up
  Limiter            a look-ahead peak limiter: nothing goes over the ceiling (e.g. -1 dBFS), and
                     loud parts are turned down smoothly instead of being cut off (no distortion)

Both are numpy only and work block by block (one block = one video frame of sound).
"""
import numpy as np

SAMPLE_RATE = 48000
LOOKAHEAD = 240                    # 5 ms: the limiter sees peaks this early (adds 5 ms of delay)


def db(x):
    return 20.0 * np.log10(max(float(x), 1e-9))


class MasterAudio:
    def __init__(self):
        self.agc = False
        self.target = -18.0        # auto level target, dBFS RMS (about LUFS for speech / music)
        self.max_boost = 12.0      # dB
        self.limiter = True
        self.ceiling = -1.0        # dBFS
        self.release = 20.0        # dB per second the limiter lets go
        self._agc_db = 0.0
        self._loud = None          # smoothed level of the input, dB
        self._carry = None         # limiter delay line (channels, LOOKAHEAD)
        self._hist = np.ones(LOOKAHEAD - 1, np.float32)
        self._g = 1.0
        self.reduction = 0.0       # dB the limiter took off in the last block (for the meter)

    # ---- settings ---------------------------------------------------
    def configure(self, agc=None, target=None, limiter=None, ceiling=None):
        if agc is not None:
            self.agc = bool(agc)
        if target is not None:
            self.target = max(-30.0, min(-9.0, float(target)))
        if limiter is not None:
            self.limiter = bool(limiter)
        if ceiling is not None:
            self.ceiling = max(-12.0, min(0.0, float(ceiling)))
        return self.settings()

    def settings(self):
        return {"agc": self.agc, "target": self.target, "limiter": self.limiter, "ceiling": self.ceiling}

    def info(self):
        return {**self.settings(), "agc_gain": round(self._agc_db, 1), "reduction": round(self.reduction, 1)}

    # ---- processing -------------------------------------------------
    def process(self, audio):
        if not (self.agc or self.limiter) or audio is None or audio.size == 0:
            self.reduction = 0.0
            return audio
        a = np.asarray(audio, np.float32)
        n = a.shape[1]
        if self.agc:
            a = self._level(a, n)
        if self.limiter:
            a = self._limit(a, n)
        return a

    def _level(self, a, n):
        rms = float(np.sqrt(np.mean(a * a))) if n else 0.0
        level = db(rms)
        dt = n / SAMPLE_RATE
        old = self._agc_db
        if level > -50.0:                                     # sound, not a pause
            if self._loud is None:
                self._loud = level
            k = min(1.0, dt / (2.0 if level < self._loud else 0.5))   # follows a louder source in ~0.5 s, a quieter one in ~2 s
            self._loud += (level - self._loud) * k
            want = max(-12.0, min(self.max_boost, self.target - self._loud))
            step = (4.0 if want > old else 6.0) * dt          # dB per second: gently up, a bit faster down
            self._agc_db = old + max(-step, min(step, want - old))
        g0, g1 = 10 ** (old / 20), 10 ** (self._agc_db / 20)
        if g0 == g1:
            return a * g0
        return a * np.linspace(g0, g1, n, dtype=np.float32)[None, :]

    def _limit(self, a, n):
        ch = a.shape[0]
        if self._carry is None or self._carry.shape[0] != ch:
            self._carry = np.zeros((ch, LOOKAHEAD), np.float32)
        x = np.concatenate([self._carry, a], axis=1)          # LOOKAHEAD + n samples
        self._carry = x[:, -LOOKAHEAD:].copy()
        peak = np.abs(x).max(axis=0)
        ceil = 10 ** (self.ceiling / 20)
        # the loudest sample in the next 5 ms decides the gain now (output sample i = x[i])
        ahead = np.lib.stride_tricks.sliding_window_view(peak, LOOKAHEAD + 1).max(axis=1)[:n]
        t = np.minimum(1.0, ceil / np.maximum(ahead, 1e-9)).astype(np.float64)
        # release: the gain may rise only slowly (exact, vectorised: running minimum in the log domain)
        lr = np.log(10 ** (self.release / 20)) / SAMPLE_RATE
        idx = np.arange(1, n + 1)
        logt = np.log(t)
        seed = np.log(max(self._g, 1e-9))
        run = np.minimum.accumulate(np.minimum(logt - idx * lr, seed))
        g = np.minimum(np.exp(run + idx * lr), t)
        self._g = float(g[-1])
        # smooth over the look-ahead window (still never above what the peak needs)
        full = np.concatenate([self._hist, g])
        cs = np.cumsum(np.concatenate([[0.0], full]))
        sm = (cs[LOOKAHEAD:] - cs[:-LOOKAHEAD]) / LOOKAHEAD
        self._hist = full[-(LOOKAHEAD - 1):].astype(np.float32)
        out = x[:, :n] * sm[None, :n].astype(np.float32)
        self.reduction = max(0.0, -db(float(sm.min()))) if sm.size else 0.0
        return np.clip(out, -ceil, ceil)
