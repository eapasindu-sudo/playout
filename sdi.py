"""SDI output through a Blackmagic DeckLink card (to a transmitter, switcher or monitor).

Program frames + sound go through FFmpeg's DeckLink output. The FFmpeg that comes with Playout
cannot do this (DeckLink support may not be shipped in public FFmpeg builds), so point Playout at an
ffmpeg.exe built with DeckLink (--enable-decklink) and install Blackmagic Desktop Video.
"""
import queue
import re
import subprocess
import sys
import threading
import time
from fractions import Fraction

import av
import numpy as np

from ndi_io import SAMPLE_RATE

FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def list_devices(ffmpeg):
    """(devices, error). DeckLink outputs FFmpeg can see."""
    if not ffmpeg:
        return [], "Pick an FFmpeg with DeckLink support"
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-sinks", "decklink"], capture_output=True, text=True,
                           timeout=15, creationflags=FLAGS)
    except Exception as e:
        return [], f"FFmpeg did not start: {e}"
    text = r.stdout + r.stderr
    if "Unknown output format" in text or ("not found" in text.lower() and "decklink" in text.lower()):
        return [], "This FFmpeg has no DeckLink support (needs --enable-decklink)"
    devs = re.findall(r"\[(.+?)\]", r.stdout) or re.findall(r"'(.+?)'", r.stderr)
    return devs, "" if devs else "No DeckLink card found (install Blackmagic Desktop Video)"


class SdiOutput:
    def __init__(self, engine):
        self.engine = engine
        self.ffmpeg = ""
        self.device = ""
        self.enabled = False
        self.status = "off"
        self.error = ""
        self.dropped = 0
        self._q = None
        self._thread = None
        self._on = False

    def configure(self, enabled=None, device=None, ffmpeg=None):
        restart = False
        if ffmpeg is not None and ffmpeg != self.ffmpeg:
            self.ffmpeg, restart = ffmpeg.strip().strip('"'), True
        if device is not None and device != self.device:
            self.device, restart = device, True
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled, restart = bool(enabled), True
        if restart:
            self.stop()
            if self.enabled:
                self.start()
        return self.info()

    def settings(self):
        return {"enabled": self.enabled, "device": self.device, "ffmpeg": self.ffmpeg}

    def info(self):
        return {**self.settings(), "status": self.status if self.enabled else "off", "error": self.error,
                "dropped": self.dropped}

    def start(self):
        if self._on or not self.device or not self.ffmpeg:
            self.error = "" if self.device and self.ffmpeg else "Pick the FFmpeg and the DeckLink output"
            return
        self._on = True
        self._q = queue.Queue(maxsize=int(float(self.engine.fps)))
        self._thread = threading.Thread(target=self._run, daemon=True, name="sdi out")
        self._thread.start()

    def stop(self):
        self._on = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self.status = "off"

    def push(self, video, audio):
        if not self._on:
            return
        try:
            self._q.put_nowait((video, audio))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while self._on:
            proc = out = None
            try:
                cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "nut", "-i", "pipe:0",
                       "-pix_fmt", "uyvy422", "-f", "decklink", self.device]
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=FLAGS)
                threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()
                eng = self.engine
                out = av.open(proc.stdin, "w", format="nut")
                vs = out.add_stream("rawvideo", rate=eng.fps)
                vs.width, vs.height, vs.pix_fmt = eng.width, eng.height, "uyvy422"
                vs.time_base = Fraction(1, 1) / eng.fps
                aus = out.add_stream("pcm_s16le", rate=SAMPLE_RATE)
                aus.layout = "stereo"
                self.status, self.error = "on air", ""
                vpts = apts = 0
                while self._on:
                    try:
                        video, audio = self._q.get(timeout=1)
                    except queue.Empty:
                        continue
                    img = np.asarray(video).reshape(eng.height, eng.width, 4)
                    vf = av.VideoFrame.from_ndarray(img, format="bgra").reformat(format="uyvy422")
                    vf.pts, vf.time_base = vpts, vs.time_base
                    vpts += 1
                    for p in vs.encode(vf):
                        out.mux(p)
                    pcm = (np.clip(audio, -1, 1).T * 32767).astype(np.int16)
                    af = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm.reshape(1, -1)), format="s16", layout="stereo")
                    af.sample_rate = SAMPLE_RATE
                    af.pts, af.time_base = apts, Fraction(1, SAMPLE_RATE)
                    apts += af.samples
                    for p in aus.encode(af):
                        out.mux(p)
                    if proc.poll() is not None:
                        raise RuntimeError(self.error or "FFmpeg stopped")
            except Exception as e:
                self.status = "error"
                self.error = self.error or str(e)
            finally:
                for x in (out,):
                    try:
                        x and x.close()
                    except Exception:
                        pass
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self._on:
                time.sleep(3)      # card busy / unplugged: try again

    def _read_err(self, proc):
        for line in proc.stderr:
            t = line.decode("utf-8", "replace").strip()
            if t:
                self.error = t[-200:]
