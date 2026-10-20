"""Live streaming of the program output to up to 3 RTMP/RTMPS destinations (Facebook, YouTube...).

The program is encoded once (H.264 + AAC, in-process with PyAV) into an MPEG-TS
byte stream. Each destination is its own FFmpeg process that copies that stream
to its RTMP(S) URL, so destinations start, stop and reconnect independently.

A destination can also be vertical (9:16, for Facebook Reels / YouTube Shorts / TikTok live):
its FFmpeg turns the program into a 1080x1920 picture (centre crop, or the whole picture with
blurred top and bottom) and encodes that, so the other destinations are not touched.
"""
import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from fractions import Fraction

import av
import numpy as np

import gpu
from framequeue import FrameQueue

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


BACKLOG_SECONDS = 30    # how far a destination may fall behind (connecting, a slow moment) before it reconnects


LAYOUTS = ("", "crop", "blur")      # 16:9 as is / 9:16 centre crop / 9:16 whole picture on a blurred background


def vertical_filter(layout, pos=50, out_w=1080, out_h=1920):
    """FFmpeg filter that makes a 9:16 picture from the 16:9 program."""
    p = max(0, min(100, int(pos))) / 100
    if layout == "crop":
        return f"crop=trunc(ih*9/16/2)*2:ih:trunc((iw-ih*9/16)*{p:.2f}/2)*2:0,scale={out_w}:{out_h},setsar=1"
    return (f"split[a][b];[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
            f"scale=iw/8:ih/8,boxblur=12:2,scale={out_w}:{out_h},eq=brightness=-0.12[bg];"
            f"[b]scale={out_w}:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1")


class Destination:
    def __init__(self, slot, ffmpeg, streamer=None):
        self.slot = slot
        self.ffmpeg = ffmpeg
        self.streamer = streamer
        self.layout = ""          # "" 16:9, "crop" / "blur" = vertical 9:16
        self.crop_pos = 50        # vertical crop: 0 left ... 50 centre ... 100 right
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
        self.reconnects = 0
        self._rate = []           # (time, bytes) for the bitrate of the last seconds
        self._out = None          # bytes waiting for this destination's FFmpeg (its own thread writes them)
        self._waiting = 0
        self.last_error = ""      # kept after a reconnect (self.error is cleared once live again)
        self.late = 0             # times this destination could not keep up and was restarted

    def target(self):
        url = self.url.strip()
        if self.key.strip():
            url = url.rstrip("/") + "/" + self.key.strip()
        return url

    def command(self):
        st = self.streamer
        vertical = self.layout in ("crop", "blur") and st is not None
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-fflags", "+genpts"]
        if vertical:
            cmd += ["-hwaccel", "auto"]          # decode on the graphics card when it can
        cmd += ["-f", "mpegts", "-i", "pipe:0"]
        if vertical:
            out_w, out_h = (720, 1280) if st.height <= 720 else (1080, 1920)
            kbps = st.video_kbps
            gop = str(int(round(float(st.engine.fps) * 2)))
            enc = gpu.resolve(st.encoder)
            cmd += ["-filter_complex", "[0:v:0]" + vertical_filter(self.layout, self.crop_pos, out_w, out_h) + "[v]",
                    "-map", "[v]", "-map", "0:a:0?", "-c:v", enc, "-pix_fmt", "yuv420p",
                    "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                    "-g", gop, "-keyint_min", gop, "-bf", "0"]
            if enc == "libx264":
                cmd += ["-preset", "veryfast", "-tune", "zerolatency", "-sc_threshold", "0"]
            else:
                for k, v in gpu.hw_options(enc, kbps, int(gop)).items():
                    if k not in ("maxrate", "bufsize", "g", "bf"):
                        cmd += ["-" + k, v]
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]
        return cmd + ["-bsf:a", "aac_adtstoasc", "-f", "flv", self.target()]

    def spawn(self):
        cmd = self.command()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=flags)
        self.started = time.time()
        self.bytes = 0
        self._rate = []
        self.status = "connecting"
        self._out = queue.Queue()
        self._waiting = 0         # bytes in self._out
        threading.Thread(target=self._read_errors, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._writer, args=(self.proc, self._out), daemon=True,
                         name=f"stream out {self.slot + 1}").start()

    def _read_errors(self, proc):
        for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text and not ("AVHWDeviceContext" in text or "Device creation failed" in text):   # GPU decode not there: fine
                self.error = self.last_error = text[-300:]
                logging.info("stream %s: %s", self.name, text[-300:])
        code = proc.wait()
        up = time.time() - (self.started or time.time())
        if self.enabled and code != 0:
            logging.info("stream %s: FFmpeg stopped (exit code %s) after %.0fs; last error: %s",
                         self.name, code, up, self.last_error or "none")

    def write(self, data):
        """From the encoder thread: never waits. A slow network or a stuck FFmpeg only holds up
        this destination; the encoder and the other destinations go on."""
        q = self._out
        if self.proc is None or q is None:
            return
        limit = max(8_000_000, (self.streamer.video_kbps + 320) * 125 * BACKLOG_SECONDS) if self.streamer else 16_000_000
        if self._waiting + len(data) <= limit:
            self._waiting += len(data)
            q.put_nowait(data)
        else:
            self.late += 1
            self.kill(wait=False)
            self.status = "reconnecting"
            self.error = "The connection could not keep up; reconnecting"
            self.reconnects += 1
            self._restart_at = time.time() + 2

    def _writer(self, proc, q):
        while True:
            data = q.get()
            if data is None:
                return
            if q is self._out:
                self._waiting -= len(data)
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                if self.proc is proc:
                    self.kill(wait=False)
                    self.status = "reconnecting"
                    self.reconnects += 1
                    self._restart_at = time.time() + 5
                return
            self.bytes += len(data)
            now = time.time()
            if not self._rate or now - self._rate[-1][0] >= 1:
                self._rate.append((now, self.bytes))
                del self._rate[:-6]
            if self.status == "connecting" and now - self.started > 3:
                self.status = "live"
                self.error = ""

    def kill(self, wait=True):
        p, self.proc = self.proc, None
        q, self._out = self._out, None
        if q is not None:
            while True:         # whatever is still waiting is dropped; the writer thread then ends
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            q.put(None)
        if p and not wait:          # from the encoder or writer thread: never block it
            try:
                p.kill()
            except Exception:
                pass
        elif p:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def now_kbps(self):
        """Bitrate going out over the last few seconds (0 when stalled)."""
        r = self._rate
        if self.proc is None or len(r) < 2 or time.time() - r[-1][0] > 3:
            return 0
        (t0, b0), (t1, b1) = r[0], r[-1]
        return round((b1 - b0) * 8 / max(0.5, t1 - t0) / 1000)

    def info(self):
        up = time.time() - self.started if (self.proc and self.started) else 0
        return {"slot": self.slot, "name": self.name, "url": self.url, "key_set": bool(self.key.strip()),
                "enabled": self.enabled, "status": self.status if self.enabled else "off",
                "error": self.error, "uptime": round(up), "kbps": round(self.bytes * 8 / up / 1000) if up > 1 else 0,
                "now_kbps": self.now_kbps(), "reconnects": self.reconnects, "last_error": self.last_error,
                "layout": self.layout, "crop_pos": self.crop_pos}


class Streamer:
    # pictures waiting for the encoder: after a busy moment the output loop catches up with a burst of
    # frames, which 8 (0.3 s) could not hold; 16 rides it out (the RAM is only used while it is behind)
    QUEUE = 16

    def __init__(self, engine, cfg):
        s = cfg.get("stream", {})
        self.engine = engine
        self.width = int(s.get("width", engine.width))
        self.height = int(s.get("height", engine.height))
        self.video_kbps = int(s.get("video_kbps", 4500))
        self.audio_kbps = int(s.get("audio_kbps", 128))
        enc = s.get("encoder", "auto")
        self.encoder = "auto" if enc == "libx264" else enc     # CPU only when chosen in Stream settings
        self.active_encoder = None
        self.preset = s.get("preset", "veryfast")
        self.ffmpeg = find_ffmpeg(s.get("ffmpeg_path", ""))
        self.dests = [Destination(i, self.ffmpeg, self) for i in range(3)]
        self._q = FrameQueue(self.QUEUE)
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
            d.layout = s.get("layout", "") if s.get("layout", "") in LAYOUTS else ""
            d.crop_pos = int(s.get("crop_pos", 50))

    def dump(self):
        return [{"name": d.name, "url": d.url, "key": d.key, "layout": d.layout, "crop_pos": d.crop_pos}
                for d in self.dests]

    def configure(self, slot, name=None, url=None, key=None, layout=None, crop_pos=None):
        d = self.dests[slot]
        if layout is not None and layout in LAYOUTS:
            d.layout = layout
        if crop_pos is not None:
            d.crop_pos = max(0, min(100, int(crop_pos)))
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
                d.reconnects = 0
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
        if not self._q.put(video, audio):
            self.dropped += 1

    # -- encoder thread --------------------------------------------------
    def _start_encoder(self):
        self._encoding = True
        self._q = FrameQueue(self.QUEUE)
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
                    d.reconnects += 1
                    d._restart_at = now + 5
                    continue
                d.write(data)

    def _encode(self):
        fps = self.engine.fps
        out = None
        try:
            out = av.open(_Fanout(self), "w", format="mpegts")
            enc = gpu.resolve(self.encoder)
            self.active_encoder = enc
            pix = gpu.input_format(enc) or "yuv420p"
            vs = out.add_stream(enc, rate=fps)
            vs.width, vs.height = self.width, self.height
            vs.pix_fmt = pix
            vs.bit_rate = self.video_kbps * 1000
            vtb = Fraction(1, 1) / fps   # keep: vs.time_base changes to 1/90000 once muxing starts
            vs.time_base = vtb
            gop = int(round(float(fps) * 2))   # keyframe every 2 s (Facebook requirement)
            if enc == "libx264":
                opts = {"g": str(gop), "bf": "0", "preset": self.preset, "tune": "zerolatency",
                        "x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:nal-hrd=cbr",
                        "maxrate": f"{self.video_kbps}k", "bufsize": f"{self.video_kbps * 2}k"}
            else:
                opts = gpu.hw_options(enc, self.video_kbps, gop)
            vs.codec_context.options = opts
            aus = out.add_stream("aac", rate=SAMPLE_RATE)
            aus.layout = "stereo"
            aus.bit_rate = self.audio_kbps * 1000
            vpts = apts = 0
            while self._encoding:
                try:
                    video, audio, frames = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                img = np.asarray(video).reshape(self.engine.height, self.engine.width, 4)
                vf = av.VideoFrame.from_ndarray(img, format="bgra")
                if pix != "bgra" or (self.width, self.height) != (self.engine.width, self.engine.height):
                    vf = vf.reformat(width=self.width, height=self.height, format=pix, interpolation="FAST_BILINEAR")
                vf.pts = vpts
                vf.time_base = vtb
                vpts += frames
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
                "encoder": self.encoder, "active_encoder": gpu.NAMES.get(gpu.resolve(self.encoder))}

    def apply_settings(self, resolution=None, video_kbps=None, audio_kbps=None, encoder=None):
        """Change stream size / bitrate. Running destinations restart with the new settings."""
        if resolution in self.RESOLUTIONS:
            self.width, self.height = self.RESOLUTIONS[resolution]
        if video_kbps:
            self.video_kbps = max(300, min(50000, int(video_kbps)))
        if audio_kbps:
            self.audio_kbps = max(64, min(320, int(audio_kbps)))
        if encoder in gpu.ENCODERS:
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
                "active_encoder": gpu.NAMES.get(self.active_encoder, self.active_encoder) if self._encoding else None,
                "format": f"{self.height}p {self.video_kbps} kbps",
                "settings": self.settings(),
                "destinations": [d.info() for d in self.dests]}
