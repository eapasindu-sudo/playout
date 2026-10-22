"""Playout's native GPU engine (pgpu.exe, see cefcg/pgpu.cc), built the way vMix's engine works.

The output loop hands each program frame (the clip / live picture and its sound) and the list of CGs on air to
pgpu.exe. pgpu mixes them on the graphics card (Direct3D 11), draws web overlays (scoreboards) with Chromium on
the GPU itself, and encodes the stream and the recording with the graphics card's encoder (NVENC) straight from
the mixed picture. The mixed picture comes back for NDI, the preview and SDI.

config.json "native_engine": "auto" (default: used when pgpu.exe came with Playout and starts) or "off" (the
older way: CGs mixed and pictures encoded by Playout itself). If pgpu.exe stops, it is started again; after
three stops in two minutes Playout goes back to the older way by itself.

Shared memory (made here, opened by pgpu.exe; keep with pgpu.cc):
    0 magic 'PGPU', 4 width, 8 height
    256: program picture (w * h * 4 BGRA), sound (2 * 8192 float32: left samples then right samples),
         the mixed picture read back (w * h * 4), upload area for CG pictures (2 * w * h * 4)
"""
import itertools
import json
import logging
import mmap
import os
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

MODE = "auto"                   # config "native_engine"
EXE = "pgpu.exe"
HEADER = 256
MAX_SAMPLES = 8192
CLIP_SLOTS = 4                  # clip pictures decoded ahead in pgpu (keep with pgpu.cc)
CLIP_PIXELS = 4096              # where the clip pictures start in a clip's shared memory
STATE = {"running": False, "gpu": "", "error": "", "restarts": 0, "stats": ""}   # health / diagnostics

_engine = None
_disabled = False
_lock = threading.Lock()
_ids = itertools.count(1)


class WebTex:
    """A web page's picture that lives on the GPU in pgpu.exe (a CG job's image in place of a numpy array)."""
    __slots__ = ("page", "zoom")

    def __init__(self, page, zoom):
        self.page, self.zoom = page, zoom


def exe():
    if sys.platform != "win32":
        return None
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
    p = os.path.join(base, "cef", EXE)
    return p if os.path.isfile(p) else None


def start(width, height, fps):
    """Called once by app.py. Returns the engine, or None when Playout mixes and encodes the older way."""
    global _engine
    if str(MODE).lower() == "off" or not exe():
        STATE["error"] = "off in config.json" if str(MODE).lower() == "off" else "pgpu.exe not installed"
        return None
    with _lock:
        if _engine is None:
            e = NativeEngine(width, height, fps)
            if e.launch():
                _engine = e
            else:
                STATE["error"] = e.error
                logging.warning("native GPU engine did not start: %s", e.error)
        return _engine


def active():
    """The running engine, or None (not installed, off, or given up on after repeated stops)."""
    e = _engine
    return e if e is not None and not _disabled and e.ready else None


def available():
    """pgpu is in use for this run (it may be restarting right now)."""
    return _engine is not None and not _disabled


def stop():
    e = _engine
    if e is not None:
        e.shutdown()


class NativeEngine:
    ACK_TIMEOUT = 0.25
    IMAGE_CACHE = 48

    def __init__(self, width, height, fps):
        self.width, self.height = int(width) & ~1, int(height) & ~1
        from fractions import Fraction
        f = Fraction(fps).limit_denominator(1001)
        self.fps_num, self.fps_den = f.numerator, f.denominator
        self.pic = self.width * self.height * 4
        self.audio_off = HEADER + self.pic
        self.readback_off = self.audio_off + 2 * MAX_SAMPLES * 4
        self.upload_off = self.readback_off + self.pic
        self.upload_size = 2 * self.pic
        self.size = self.upload_off + self.upload_size
        self.ready = False
        self.error = ""
        self.gpu = ""
        self._proc = None
        self._mm = None
        self._dir = None
        self._wlock = threading.Lock()
        self._ack = -1
        self._ack_rb = False
        self._cv = threading.Condition()
        self._k = 0
        self._timeouts = 0
        self._images = {}               # id(numpy array) -> (array, native id), oldest first
        self._drop = []
        self.pages = {}                 # page id -> NativeWebRenderer
        self.clips = {}                 # clip id -> NativeClip
        self.events = {}                # "stream" / "rec" -> (threading.Event, [last line])
        self.stats = {}
        self._stops = []
        self._closing = False
        self._restarting = False

    # ---- process -------------------------------------------------------
    def launch(self):
        name = f"Local\\playout-pgpu-{os.getpid()}-{next(_ids)}"
        try:
            if self._mm is None:
                self._mm = mmap.mmap(-1, self.size, tagname=name)
                self._mm_name = name
                self._base = np.frombuffer(self._mm, np.uint8, self.pic, HEADER)
                self._audio = np.frombuffer(self._mm, np.float32, 2 * MAX_SAMPLES, self.audio_off).reshape(2, MAX_SAMPLES)
                self._rb = np.frombuffer(self._mm, np.uint8, self.pic, self.readback_off)
                self._up = np.frombuffer(self._mm, np.uint8, self.upload_size, self.upload_off)
            self._dir = tempfile.mkdtemp(prefix="playout-pgpu-")
            args = [exe(), f"--pg-shm={self._mm_name}", f"--pg-width={self.width}", f"--pg-height={self.height}",
                    f"--pg-fps-num={self.fps_num}", f"--pg-fps-den={self.fps_den}",
                    f"--pg-cache={os.path.join(self._dir, 'cache')}"]
            self._ready_ev = threading.Event()
            self._proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._read, args=(self._proc,), daemon=True, name="gpu engine messages").start()
            if not self._ready_ev.wait(30) or not self.ready:
                raise RuntimeError(self.error or "pgpu.exe did not answer")
            self._images.clear()
            self._timeouts = 0
            STATE.update(running=True, gpu=self.gpu, error="")
            logging.info("native GPU engine running on %s", self.gpu)
            return True
        except Exception as e:
            self.error = self.error or str(e)[:200] or type(e).__name__
            self._kill()
            return False

    def _kill(self):
        self.ready = False
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass

    def _read(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            word, _, rest = line.partition(" ")
            if word == "ack":
                k, _, flag = rest.partition(" ")
                with self._cv:
                    self._ack = int(k)
                    self._ack_rb = flag == "rb"
                    self._cv.notify_all()
            elif word == "ready":
                self.gpu = rest
                self.ready = True
                self._ready_ev.set()
            elif word == "error":
                self.error = rest[:200]
                self._ready_ev.set()
            elif word == "web":
                pid, _, msg = rest.partition(" ")
                page = self.pages.get(int(pid)) if pid.isdigit() else None
                if page is not None:
                    page._event(msg)
            elif word == "clip":
                cid, _, msg = rest.partition(" ")
                clip = self.clips.get(int(cid)) if cid.isdigit() else None
                if clip is not None:
                    clip._event(msg)
            elif word == "enc":
                name, _, msg = rest.partition(" ")
                ev = self.events.get(name)
                if ev is not None:
                    ev[1][0] = msg
                    ev[0].set()
                if msg.startswith("error"):
                    logging.warning("GPU engine encoder %s: %s", name, msg)
            elif word == "stats":
                STATE["stats"] = rest
                d = {}
                for part in rest.split():
                    k, _, v = part.partition("=")
                    d[k] = v
                self.stats = d
        # the process ended
        for c in list(self.clips.values()):
            c.error = c.error or "the GPU engine stopped"
            c.done.set()
        was = self.ready
        self.ready = False
        with self._cv:
            self._cv.notify_all()
        if was and not self._closing and proc is self._proc:
            STATE["running"] = False
            logging.warning("native GPU engine stopped (exit code %s)", proc.poll())
            threading.Thread(target=self._restart, daemon=True, name="gpu engine restart").start()

    def _restart(self):
        global _disabled
        if self._restarting:
            return
        self._restarting = True
        try:
            now = time.monotonic()
            self._stops = [t for t in self._stops if now - t < 120] + [now]
            STATE["restarts"] += 1
            if len(self._stops) >= 3:
                _disabled = True
                STATE["error"] = "stopped 3 times in 2 minutes: Playout mixes and encodes the older way now"
                logging.error("native GPU engine: %s", STATE["error"])
                for p in list(self.pages.values()):
                    p._engine_gone()
                return
            self._kill()
            time.sleep(1)
            if self.launch():
                for p in list(self.pages.values()):
                    p._open()
        finally:
            self._restarting = False

    def shutdown(self):
        self._closing = True
        self._send({"op": "quit"})
        p = self._proc
        if p is not None:
            try:
                p.stdin.close()
                p.wait(timeout=8)
            except Exception:
                self._kill()
        self.ready = False
        STATE["running"] = False

    def _send(self, obj):
        p = self._proc
        if p is None:
            return False
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            try:
                p.stdin.write(data)
                p.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    # ---- the program frame ---------------------------------------------
    def frame(self, base, audio, jobs, readback=True):
        """base: the program picture (flat or (h, w, 4) BGRA); audio: (2, n) float32; jobs: [(image or WebTex,
        x, y, opacity 0-255)] bottom first. Returns the mixed picture (h * w * 4, a new array) or None when
        the engine is not there right now (the caller mixes on the CPU)."""
        if not self.ready:
            return None
        n = int(audio.shape[1]) if audio is not None else 0
        n = min(n, MAX_SAMPLES)
        np.copyto(self._base, np.asarray(base, np.uint8).reshape(-1))
        if n:
            self._audio[:, :n] = audio[:, :n]
        up, layers, used = [], [], 0
        box, under = None, []
        if jobs and isinstance(jobs[0], dict):              # a squeeze: the program goes into box
            b = jobs[0]["box"]
            box = [int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])]
            for img, x, y, op in jobs[0]["under"]:
                nid, used = self._image(img, up, used)
                if nid is not None:
                    under.append({"img": nid, "x": int(x), "y": int(y), "o": int(op)})
            jobs = jobs[1:]
        for img, x, y, op in jobs:
            if isinstance(img, WebTex):
                layers.append({"web": img.page, "x": int(x), "y": int(y), "o": int(op), "z": round(float(img.zoom), 4)})
                continue
            nid, used = self._image(img, up, used)
            if nid is not None:
                layers.append({"img": nid, "x": int(x), "y": int(y), "o": int(op)})
        if self._drop:
            self._send({"op": "drop", "ids": self._drop})
            self._drop = []
        self._k += 1
        k = self._k
        cmd = {"op": "frame", "k": k, "n": n, "up": up, "layers": layers, "rb": 1 if readback else 0}
        if box is not None:
            cmd["box"], cmd["under"] = box, under
        if not self._send(cmd):
            return None
        with self._cv:
            self._cv.wait_for(lambda: self._ack >= k or not self.ready, self.ACK_TIMEOUT)
            got, rb = self._ack >= k, self._ack_rb
        if not got:
            self._timeouts += 1
            if self._timeouts >= 8 and self.ready:       # 2 s without an answer: stuck, start it again
                logging.warning("native GPU engine stopped answering: starting it again")
                self._kill()
            return None
        self._timeouts = 0
        if not readback or not rb:
            return None
        return self._rb.copy()

    def _image(self, img, up, used):
        key = id(img)
        hit = self._images.get(key)
        if hit is not None and hit[0] is img:
            self._images[key] = self._images.pop(key)      # most recently used last
            return hit[1], used
        a = np.ascontiguousarray(img, np.uint8)
        if a.ndim != 3 or a.shape[2] != 4:
            return None, used
        h, w = a.shape[:2]
        nbytes = w * h * 4
        if used + nbytes > self.upload_size:
            return None, used                              # too much changed in one frame: next frame
        self._up[used:used + nbytes] = a.reshape(-1)
        nid = next(_ids)
        up.append({"id": nid, "w": w, "h": h, "off": used})
        if hit is not None:
            self._drop.append(hit[1])
        self._images[key] = (img, nid)
        while len(self._images) > self.IMAGE_CACHE:
            old = next(iter(self._images))
            self._drop.append(self._images.pop(old)[1])
        return nid, used + ((nbytes + 63) & ~63)

    # ---- web pages -----------------------------------------------------
    def web_open(self, page):
        self.pages[page.id] = page
        return self._send({"op": "web_open", "id": page.id, "url": page.url, "w": page.width, "h": page.height,
                           "fps": page.fps, "js": page.js})

    def web(self, op, pid, **kw):
        return self._send({"op": op, "id": pid, **kw})

    def web_close(self, pid):
        self.pages.pop(pid, None)
        self._send({"op": "web_close", "id": pid})

    # ---- the control screen's preview, drawn by the GPU ------------------
    def preview(self, hwnd, x, y, w, h, on):
        return self._send({"op": "preview", "hwnd": int(hwnd), "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                           "on": 1 if on else 0})

    # ---- clips (decoded on the GPU) -------------------------------------
    def clip_open(self, path, w, h, start, end, frame_dur):
        """A NativeClip decoding path into its own shared memory, or None."""
        c = NativeClip(self, w, h)
        self.clips[c.id] = c
        if not self._send({"op": "clip_open", "id": c.id, "path": path, "shm": c.name, "w": int(w), "h": int(h),
                           "start": float(start or 0), "end": float(end or 0), "dur": float(frame_dur)}):
            c.close()
            return None
        return c

    # ---- stream / recording --------------------------------------------
    def _encoder(self, cmd, timeout=15):
        name = cmd["name"]
        ev = self.events[name] = (threading.Event(), [""])
        if not self._send({"op": "enc_start", **cmd}):
            return False, "the GPU engine is not running"
        if not ev[0].wait(timeout):
            return False, "the GPU engine did not answer"
        msg = ev[1][0]
        if msg.startswith("started "):
            return True, msg[8:]
        return False, msg.partition(" ")[2] or msg

    def stream_start(self, w, h, kbps, akbps, enc, gop, name="stream", layout="", pos=50):
        """An MPEG-TS encode of the mixed picture (layout "crop" / "blur": a 9:16 picture made on the GPU).
        Returns (pipe name to read it from, encoder) or (None, why not)."""
        pipe = rf"\\.\pipe\playout-pgpu-{os.getpid()}-{next(_ids)}"
        ok, msg = self._encoder({"name": name, "kind": "stream", "pipe": pipe, "w": int(w), "h": int(h), "kbps": int(kbps),
                                 "akbps": int(akbps), "enc": enc, "gop": int(gop), "layout": layout, "pos": int(pos)})
        return (pipe, msg) if ok else (None, msg)

    def stream_stop(self, name="stream"):
        self._send({"op": "enc_stop", "name": name})

    def rec_start(self, path, w, h, kbps, enc, gop):
        return self._encoder({"name": "rec", "kind": "rec", "path": path, "w": int(w), "h": int(h), "kbps": int(kbps),
                              "akbps": 192, "enc": enc, "gop": int(gop)})

    def rec_stop(self):
        self._send({"op": "enc_stop", "name": "rec"})


class NativeClip:
    """One clip being decoded in pgpu.exe: CLIP_SLOTS pictures in shared memory (see pgpu.cc Clip)."""

    def __init__(self, engine, w, h):
        self.engine = engine
        self.id = next(_ids)
        self.name = f"Local\\playout-clip-{os.getpid()}-{self.id}"
        self.mm = mmap.mmap(-1, CLIP_PIXELS + CLIP_SLOTS * w * h * 4, tagname=self.name)
        self.mode = ""
        self.error = ""
        self.done = threading.Event()

    def _event(self, msg):
        word, _, rest = msg.partition(" ")
        if word in ("gpu", "cpu"):
            self.mode = msg
        elif word == "error":
            self.error = rest[:200] or "error"
            self.done.set()
        elif word == "end":
            self.done.set()

    def close(self):
        self.engine.clips.pop(self.id, None)
        self.engine._send({"op": "clip_close", "id": self.id})
        try:
            self.mm.close()
        except Exception:
            pass


class NativeWebRenderer:
    """A web overlay page drawn in pgpu.exe on the GPU (same interface as webcg.CefRenderer). .frame is
    (WebTex, (0, 0)): the picture never comes to Python, pgpu draws it over the program itself. If pgpu is
    given up on, the page moves to the built-in browser (cefcg.exe) on its own."""
    engine = "native GPU"

    def __init__(self, url, width, height, zoom=100, rate=10, css=""):
        import webcg
        self.url = url
        self.css = str(css or "")
        self.js = webcg._css_js(self.css) if self.css.strip() else ""
        self.width, self.height = max(16, int(width)), max(16, int(height))
        self.zoom = max(1, float(zoom or 100)) / 100.0
        self.rate = max(1.0, min(60.0, float(rate or 10)))
        self.fps = max(1, round(self.rate))
        self.id = next(_ids)
        self.error = ""
        self.loaded = False
        self.frames = 0
        self.mode = ""
        self._active = True
        self._fallback = None
        self._tex = WebTex(self.id, self.zoom)
        webcg.LIVE.add(self)
        self._open()

    def _open(self):
        self.loaded = False
        e = _engine
        if e is not None:
            e.web_open(self)
            if not self._active:
                e.web("web_show", self.id, on=False)

    def _event(self, msg):
        word, _, rest = msg.partition(" ")
        if word == "loaded":
            self.loaded, self.error = True, ""
        elif word == "error":
            self.error = rest[:200]
        elif word == "drawn":
            self.frames = max(self.frames, 1)
        elif word == "mode":
            self.mode = rest
        elif word == "note":
            logging.info("web CG (GPU engine) %s: %s", self.url, rest)

    def _engine_gone(self):
        import webcg
        if self._fallback is None:
            self._fallback = webcg.CefRenderer(self.url, self.width, self.height, self.zoom * 100, self.rate, self.css) \
                if webcg.cef_exe() else webcg.EdgeRenderer(self.url, self.width, self.height, self.zoom * 100, self.rate, self.css)
            self._fallback.active = self._active
            webcg.LIVE.discard(self)

    @property
    def frame(self):
        fb = self._fallback
        if fb is not None:
            return fb.frame
        e = _engine
        if e is None:
            return None
        try:
            n = int((e.stats.get(f"web{self.id}") or "0").rstrip("gc"))
        except ValueError:
            n = 0
        self.frames = max(self.frames, n)
        return (self._tex, (0, 0)) if self.frames > 0 else None

    @property
    def active(self):
        return self._active

    @active.setter
    def active(self, on):
        on = bool(on)
        if self._fallback is not None:
            self._fallback.active = on
        if on != self._active:
            self._active = on
            e = _engine
            if e is not None:
                e.web("web_show", self.id, on=on)

    def reload(self):
        if self._fallback is not None:
            self._fallback.reload()
            return
        e = _engine
        if e is not None:
            self.loaded = False
            e.web("web_reload", self.id)

    def close(self):
        if self._fallback is not None:
            self._fallback.close()
        e = _engine
        if e is not None:
            e.web_close(self.id)
