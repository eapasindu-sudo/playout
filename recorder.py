"""Records the program output (with CGs and sound) to an MP4 file.

Files go to Documents\\Playout\\recordings\\PGM_<date>_<time>.mp4. The MP4 is written in
fragments, so a recording is still playable if the PC loses power mid-way.
"""
import datetime as dt
import os
import queue
import threading
import time
from fractions import Fraction

import av
import numpy as np

from ndi_io import SAMPLE_RATE

BITRATES = (10, 20, 50)            # Mbps choices on screen
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}
ENCODERS = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf")


class Recorder:
    def __init__(self, engine, folder):
        self.engine = engine
        self.folder = folder
        self.resolution = "1080p"
        self.mbps = 20
        self.encoder = "libx264"
        self.file = None
        self.started = None
        self.error = ""
        self.dropped = 0
        self.frames = 0
        self._q = None
        self._thread = None
        self._on = False

    # ---- settings ------------------------------------------------------
    def configure(self, resolution=None, mbps=None, encoder=None):
        if self._on:
            return self.info()          # settings are locked while recording
        if resolution in RESOLUTIONS:
            self.resolution = resolution
        if mbps:
            self.mbps = max(2, min(100, int(mbps)))
        if encoder in ENCODERS:
            self.encoder = encoder
        return self.info()

    def settings(self):
        return {"resolution": self.resolution, "mbps": self.mbps, "encoder": self.encoder}

    def info(self):
        up = time.time() - self.started if (self._on and self.started) else 0
        size = 0
        if self.file and os.path.isfile(self.file):
            size = os.path.getsize(self.file)
        return {**self.settings(), "recording": self._on, "file": os.path.basename(self.file) if self.file else "",
                "folder": self.folder, "elapsed": round(up), "size_mb": round(size / 1048576, 1),
                "dropped": self.dropped, "error": self.error}

    @property
    def recording(self):
        return self._on

    # ---- control -------------------------------------------------------
    def start(self):
        if self._on:
            return self.info()
        os.makedirs(self.folder, exist_ok=True)
        self.file = os.path.join(self.folder, dt.datetime.now().strftime("PGM_%Y-%m-%d_%H-%M-%S.mp4"))
        self.error = ""
        self.dropped = self.frames = 0
        self._q = queue.Queue(maxsize=int(float(self.engine.fps) * 2))
        self._on = True
        self.started = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="recorder")
        self._thread.start()
        return self.info()

    def stop(self):
        self._on = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        return self.info()

    def push(self, video, audio):
        """Called by the engine's output loop for every program frame."""
        if not self._on:
            return
        try:
            self._q.put_nowait((video, audio))
        except queue.Full:
            self.dropped += 1

    # ---- encoder thread -----------------------------------------------
    def _open(self, encoder):
        fps = self.engine.fps
        w, h = RESOLUTIONS[self.resolution]
        out = av.open(self.file, "w", format="mp4",
                      options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
        vs = out.add_stream(encoder, rate=fps)
        vs.width, vs.height = w, h
        vs.pix_fmt = "yuv420p"
        kbps = self.mbps * 1000
        vs.bit_rate = kbps * 1000
        gop = int(round(float(fps)))       # keyframe every second
        opts = {"g": str(gop), "maxrate": f"{kbps}k", "bufsize": f"{kbps * 2}k"}
        if encoder == "libx264":
            # constant bitrate, so the file really is the chosen 10 / 20 / 50 Mbps
            opts.update(preset="veryfast", minrate=f"{kbps}k", **{"x264-params": "nal-hrd=cbr:force-cfr=1"})
        elif encoder == "h264_nvenc":
            opts.update(preset="p4", rc="cbr")
        vs.codec_context.options = opts
        vs.time_base = Fraction(1, 1) / fps
        aus = out.add_stream("aac", rate=SAMPLE_RATE)
        aus.layout = "stereo"
        aus.bit_rate = 192000
        # open the encoder now, so a missing GPU encoder fails here and we can fall back
        vs.codec_context.open()
        return out, vs, aus, (w, h)

    def _run(self):
        out = None
        try:
            try:
                out, vs, aus, (w, h) = self._open(self.encoder)
            except Exception as e:
                if self.encoder == "libx264":
                    raise
                if os.path.isfile(self.file):
                    os.remove(self.file)
                names = {"h264_nvenc": "NVIDIA", "h264_qsv": "Intel", "h264_amf": "AMD"}
                print(f"recorder: {self.encoder} failed: {e}")
                self.error = f"{names.get(self.encoder, self.encoder)} encoder not available on this PC; recording with CPU (x264)"
                out, vs, aus, (w, h) = self._open("libx264")
            vtb = Fraction(1, 1) / self.engine.fps
            vpts = apts = 0
            while self._on or not self._q.empty():
                try:
                    video, audio = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                img = np.asarray(video).reshape(self.engine.height, self.engine.width, 4)
                vf = av.VideoFrame.from_ndarray(img, format="bgra").reformat(width=w, height=h, format="yuv420p")
                vf.pts, vf.time_base = vpts, vtb
                vpts += 1
                for pkt in vs.encode(vf):
                    out.mux(pkt)
                af = av.AudioFrame.from_ndarray(np.ascontiguousarray(audio, dtype=np.float32),
                                                format="fltp", layout="stereo")
                af.sample_rate = SAMPLE_RATE
                af.pts, af.time_base = apts, Fraction(1, SAMPLE_RATE)
                apts += af.samples
                for pkt in aus.encode(af):
                    out.mux(pkt)
                self.frames += 1
            for s in (vs, aus):
                for pkt in s.encode(None):
                    out.mux(pkt)
        except Exception as e:
            self.error = f"Recording stopped: {e}"
        finally:
            self._on = False
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
