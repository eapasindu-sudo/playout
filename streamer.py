"""Live streaming of the program output to up to 3 RTMP/RTMPS destinations (Facebook, YouTube...).

The program is encoded once (H.264 + AAC, in-process with PyAV) into an MPEG-TS
byte stream. Each destination is its own FFmpeg process that copies that stream
to its RTMP(S) URL, so destinations start, stop and reconnect independently.
"""
import queue
import shutil
import subprocess
import sys
import threading
import time
from fractions import Fraction

import av
import numpy as np

from ndi_io import SAMPLE_RATE


def find_ffmpeg(configured=""):
    if configured:
        return configured
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


class _Fanout:
    """File-like sink for the MPEG-TS muxer; copies every write to each running destination."""

    def __init__(self, streamer):
        self.streamer = streamer

    def write(self, data):
        self.streamer._fanout(bytes(data))
        return len(data)

    def flush(self):
        pass


class Destination:
    def __init__(self, slot, ffmpeg):
        self.slot = slot
        self.ffmpeg = ffmpeg
        self.name = f"Destination {slot + 1}"
        self.url = ""
        self.key = ""
        self.enabled = False      # operator wants it live
        self.proc = None
        self.started = None
        self.status = "off"
        self.error = ""
        self.bytes = 0
        self._log = []
        self._restart_at = 0.0

    def target(self):
        url = self.url.strip()
        if self.key.strip():
            url = url.rstrip("/") + "/" + self.key.strip()
        return url

    def spawn(self):
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-fflags", "+genpts", "-f", "mpegts", "-i", "pipe:0",
               "-c", "copy", "-bsf:a", "aac_adtstoasc", "-f", "flv", self.target()]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=flags)
        self.started = time.time()
        self.bytes = 0
        self.status = "connecting"
        threading.Thread(target=self._read_errors, args=(self.proc,), daemon=True).start()

    def _read_errors(self, proc):
        for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.error = text[-300:]

    def write(self, data):
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.write(data)
            self.bytes += len(data)
            if self.status == "connecting" and time.time() - self.started > 3:
                self.status = "live"
                self.error = ""
        except (BrokenPipeError, OSError, ValueError):
            self.kill()
            self.status = "reconnecting"
            self._restart_at = time.time() + 5

    def kill(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def info(self):
        up = time.time() - self.started if (self.proc and self.started) else 0
        return {"slot": self.slot, "name": self.name, "url": self.url, "key_set": bool(self.key.strip()),
                "enabled": self.enabled, "status": self.status if self.enabled else "off",
                "error": self.error, "uptime": round(up), "kbps": round(self.bytes * 8 / up / 1000) if up > 1 else 0}


class Streamer:
    def __init__(self, engine, cfg):
        s = cfg.get("stream", {})
        self.engine = engine
        self.width = int(s.get("width", engine.width))
        self.height = int(s.get("height", engine.height))
        self.video_kbps = int(s.get("video_kbps", 4500))
        self.audio_kbps = int(s.get("audio_kbps", 128))
        self.encoder = s.get("encoder", "libx264")
        self.preset = s.get("preset", "veryfast")
        self.ffmpeg = find_ffmpeg(s.get("ffmpeg_path", ""))
        self.dests = [Destination(i, self.ffmpeg) for i in range(3)]
        self._q = queue.Queue(maxsize=8)
        self._lock = threading.Lock()
        self._thread = None
        self._encoding = False
        self.dropped = 0

    # -- config ---------------------------------------------------------
    def load(self, saved):
        for d, s in zip(self.dests, saved or []):
            d.name = s.get("name") or d.name
            d.url = s.get("url", "")
            d.key = s.get("key", "")

    def dump(self):
        return [{"name": d.name, "url": d.url, "key": d.key} for d in self.dests]

    def configure(self, slot, name=None, url=None, key=None):
        d = self.dests[slot]
        if name is not None:
            d.name = name
        if url is not None:
            d.url = url
        if key is not None and key != "":
            d.key = key
        if d.enabled:   # apply new settings
            self.set_enabled(slot, False)
            self.set_enabled(slot, True)

    # -- start / stop ----------------------------------------------------
    def set_enabled(self, slot, on):
        d = self.dests[slot]
        with self._lock:
            if on and not d.target().startswith(("rtmp://", "rtmps://", "srt://")):
                d.error = "Enter an rtmp:// or rtmps:// URL first"
                d.status = "error"
                return
            d.enabled = on
            if on:
                d.status = "starting"
                d.error = ""
            if not on:
                d.kill()
                d.status = "off"
            need = any(x.enabled for x in self.dests)
        if need and not self._encoding:
            self._start_encoder()
        elif not need and self._encoding:
            self._encoding = False

    def stop_all(self):
        for d in self.dests:
            self.set_enabled(d.slot, False)

    def push(self, video, audio):
        """Called by the engine's output loop for every program frame."""
        if not self._encoding:
            return
        try:
            self._q.put_nowait((video, audio))
        except queue.Full:
            self.dropped += 1

    # -- encoder thread --------------------------------------------------
    def _start_encoder(self):
        self._encoding = True
        self._q = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._encode, daemon=True, name="stream encoder")
        self._thread.start()

    def _fanout(self, data):
        with self._lock:
            now = time.time()
            for d in self.dests:
                if not d.enabled:
                    continue
                if d.proc is None and now >= d._restart_at:
                    try:
                        d.spawn()
                    except Exception as e:
                        d.error = str(e)
                        d.status = "error"
                        d._restart_at = now + 5
                        continue
                if d.proc is not None and d.proc.poll() is not None:
                    d.kill()
                    d.status = "reconnecting"
                    d._restart_at = now + 5
                    continue
                d.write(data)

    def _encode(self):
        fps = self.engine.fps
        out = None
        try:
            out = av.open(_Fanout(self), "w", format="mpegts")
            vs = out.add_stream(self.encoder, rate=fps)
            vs.width, vs.height = self.width, self.height
            vs.pix_fmt = "yuv420p"
            vs.bit_rate = self.video_kbps * 1000
            vtb = Fraction(1, 1) / fps   # keep: vs.time_base changes to 1/90000 once muxing starts
            vs.time_base = vtb
            gop = int(round(float(fps) * 2))   # keyframe every 2 s (Facebook requirement)
            opts = {"g": str(gop), "bf": "0"}
            if self.encoder == "libx264":
                opts.update(preset=self.preset, tune="zerolatency",
                            **{"x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:nal-hrd=cbr",
                               "maxrate": f"{self.video_kbps}k", "bufsize": f"{self.video_kbps * 2}k"})
            vs.codec_context.options = opts
            aus = out.add_stream("aac", rate=SAMPLE_RATE)
            aus.layout = "stereo"
            aus.bit_rate = self.audio_kbps * 1000
            vpts = apts = 0
            while self._encoding:
                try:
                    video, audio = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                img = np.asarray(video).reshape(self.engine.height, self.engine.width, 4)
                vf = av.VideoFrame.from_ndarray(img, format="bgra")
                vf = vf.reformat(width=self.width, height=self.height, format="yuv420p")
                vf.pts = vpts
                vf.time_base = vtb
                vpts += 1
                for pkt in vs.encode(vf):
                    out.mux(pkt)
                af = av.AudioFrame.from_ndarray(np.ascontiguousarray(audio, dtype=np.float32),
                                                format="fltp", layout="stereo")
                af.sample_rate = SAMPLE_RATE
                af.pts = apts
                af.time_base = Fraction(1, SAMPLE_RATE)
                apts += af.samples
                for pkt in aus.encode(af):
                    out.mux(pkt)
        except Exception as e:
            for d in self.dests:
                if d.enabled:
                    d.error = f"Encoder: {e}"
                    d.status = "error"
        finally:
            self._encoding = False
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            with self._lock:
                for d in self.dests:
                    d.kill()
                    d.enabled = False
                    if d.status != "error":
                        d.status = "off"

    RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}

    def settings(self):
        res = next((k for k, v in self.RESOLUTIONS.items() if v == (self.width, self.height)),
                   f"{self.width}x{self.height}")
        return {"resolution": res, "video_kbps": self.video_kbps, "audio_kbps": self.audio_kbps,
                "encoder": self.encoder}

    def apply_settings(self, resolution=None, video_kbps=None, audio_kbps=None, encoder=None):
        """Change stream size / bitrate. Running destinations restart with the new settings."""
        if resolution in self.RESOLUTIONS:
            self.width, self.height = self.RESOLUTIONS[resolution]
        if video_kbps:
            self.video_kbps = max(300, min(50000, int(video_kbps)))
        if audio_kbps:
            self.audio_kbps = max(64, min(320, int(audio_kbps)))
        if encoder in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf"):
            self.encoder = encoder
        live = [d.slot for d in self.dests if d.enabled]
        if live:
            self.stop_all()
            if self._thread:
                self._thread.join(timeout=5)
            for slot in live:
                self.set_enabled(slot, True)
        return self.settings()

    def info(self):
        return {"encoding": self._encoding, "dropped": self.dropped,
                "format": f"{self.height}p {self.video_kbps} kbps",
                "settings": self.settings(),
                "destinations": [d.info() for d in self.dests]}
