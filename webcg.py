"""Web overlay CG: a web page (cricket scoreboard, score bug, any browser-source overlay link) keyed
over the program like any other CG.

The page is drawn by the Microsoft Edge (or Google Chrome / Chromium) already on the PC, started
headless and driven over its DevTools protocol: a see-through page background, the page size set to
the output size (or the size given), and a picture taken several times a second. The page keeps its
own live data (score updates, animations); Playout only shows what the page shows.

A web CG config (manual CG list):
    kind "web", name, url
    page_w, page_h      page size in pixels (0 = output size)
    x, y, zoom          where it goes on the output, size in %
    fade, live_only     like other manual CGs
    rate                pictures per second taken from the page (default 10)

Two ways to draw the page:
  - Playout's built-in browser (cefcg.exe, Chromium Embedded, like vMix's Web Browser input): the page is drawn
    off screen straight into shared memory, so there is no picture to compress and decode. Used when it came
    with Playout ({app}\\cef); set "web_engine": "edge" in config.json to use Edge / Chrome instead.
  - Microsoft Edge / Google Chrome headless, pictures taken over its DevTools protocol (PNG): the fallback.

The browser is looked for in the usual install folders, or set "web_browser" in config.json.
"""
import atexit
import base64
import io
import itertools
import json
import logging
import os
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import weakref

import numpy as np
from PIL import Image

_procs = set()
LIVE = weakref.WeakSet()       # pages open now (diagnostics file)
BROWSER = None          # config "web_browser" (app.py sets it)
ENGINE = None           # config "web_engine": "edge" = always Edge / Chrome, not the built-in browser
CEF_EXE = "cefcg.exe"


def find_browsers():
    """Edge / Chrome / Chromium on this PC, best first (the configured one before the others). The
    WebView2 runtime that the control screen itself uses comes last: it is on every PC that runs Playout."""
    out = []
    if BROWSER and os.path.isfile(BROWSER):
        out.append(BROWSER)
    if sys.platform == "win32":
        import glob
        bases = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe"]
        for rel in rels:
            for b in bases:
                if b and os.path.isfile(os.path.join(b, rel)):
                    out.append(os.path.join(b, rel))
        for b in bases:
            if b:
                out += sorted(glob.glob(os.path.join(b, r"Microsoft\EdgeWebView\Application\*\msedgewebview2.exe")),
                              reverse=True)[:1]
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            p = shutil.which(name)
            if p:
                out.append(p)
    return list(dict.fromkeys(out))


def find_browser():
    """Path of Edge / Chrome / Chromium, or None."""
    b = find_browsers()
    return b[0] if b else None


# which browser and headless mode start on this PC: the first that works is kept for every page after it
_ways = {"good": None}


def _kill_all():
    for p in list(_procs):
        try:
            p.kill()
        except Exception:
            pass


atexit.register(_kill_all)
close_all = _kill_all          # Playout closing: no browser is left running


_cleaned = []


def _clean_leftovers():
    """Once per run: stop browsers and remove temp folders left by an earlier Playout run that
    crashed (they kept using memory). Runs before this run starts its first browser."""
    if _cleaned:
        return
    _cleaned.append(True)
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "playout-web-" in cmd:
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass
    tmp = tempfile.gettempdir()
    try:
        for n in os.listdir(tmp):
            if n.startswith("playout-web-"):
                shutil.rmtree(os.path.join(tmp, n), ignore_errors=True)
    except OSError:
        pass


def _quiet_crashes():
    """Windows: a browser that crashes must not leave an "Application Error" box on the playout screen.
    The error mode is inherited by the browser processes Playout starts; Playout then starts it again."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        k.SetErrorMode(k.GetErrorMode() | 0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


class _StoppedAtOnce(Exception):
    pass


def cef_exe():
    """The built-in browser that came with Playout ({app}\\cef\\cefcg.exe), or one CI put in build\\cef."""
    if sys.platform != "win32":
        return None
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
    p = os.path.join(base, "cef", CEF_EXE)
    return p if os.path.isfile(p) else None


def WebRenderer(url, width, height, zoom=100, rate=10, css=""):
    """A web page drawn for a CG: the built-in browser when Playout has it, else Edge / Chrome."""
    if str(ENGINE or "").lower() != "edge" and cef_exe():
        return CefRenderer(url, width, height, zoom, rate, css)
    return EdgeRenderer(url, width, height, zoom, rate, css)


def _css_js(css):
    """JavaScript that adds the operator's CSS to the page, after the page's own (on every load)."""
    return ("(() => { const css = %s; const add = () => { const s = document.createElement('style');"
            " s.textContent = css; document.documentElement.appendChild(s); };"
            " if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add(); })();"
            % json.dumps(css))


def _finish(out, dx, dy, zoom, premultiplied):
    """A BGRA picture cropped to what is drawn: sized by zoom, and with straight alpha for the CG blend."""
    if abs(zoom - 1.0) > 0.001:
        h, w = out.shape[:2]
        im = Image.fromarray(out, "RGBA").resize((max(1, round(w * zoom)), max(1, round(h * zoom))), Image.BILINEAR)
        out = np.asarray(im).copy()
        dx, dy = round(dx * zoom), round(dy * zoom)
    if premultiplied:
        # straight = premultiplied * 255 / alpha, looked up in a 256 x 256 table (fast on a full-screen page)
        a = out[..., 3]
        rows = np.flatnonzero(((a > 0) & (a < 255)).any(axis=1))   # rows with partly see-through pixels
        if len(rows):
            part = out[rows[0]:rows[-1] + 1]
            idx = part[..., 3].astype(np.uint16)[..., None] << 8
            idx = idx | part[..., :3]
            part[..., :3] = _UNPREMUL.take(idx)
    return out, (dx, dy)


def _unpremul_table():
    a = np.arange(256, dtype=np.uint32)[:, None]
    v = np.arange(256, dtype=np.uint32)[None, :]
    t = np.where(a == 0, v, np.minimum(255, v * 255 // np.maximum(a, 1)))
    t[255] = np.arange(256)
    return t.astype(np.uint8).ravel()


_UNPREMUL = _unpremul_table()


_shm_ids = itertools.count(1)


class CefRenderer:
    """One page in Playout's built-in browser (cefcg.exe); .frame is the latest (bgra image, (dx, dy)) cropped
    to what is drawn. The browser draws only when the page changes, straight into shared memory; a picture is
    copied out at most `rate` times a second, and only while the CG is on air. If the built-in browser cannot
    run on this PC, the page goes to Edge / Chrome instead."""
    engine = "built-in"

    def __init__(self, url, width, height, zoom=100, rate=10, css=""):
        self.url = url
        self.css = str(css or "")
        self.width, self.height = max(16, int(width)), max(16, int(height))
        self.zoom = max(1, float(zoom or 100)) / 100.0
        self.rate = max(1.0, min(30.0, float(rate or 10)))
        self.interval = 1.0 / self.rate
        self.frame = None
        self.error = ""
        self.loaded = False
        self.frames = 0
        self.active = True
        self._reload = False
        self._running = True
        self._proc = None
        self._dir = None
        self._fallback = None
        self._wait = 1.5
        LIVE.add(self)
        self._thread = threading.Thread(target=self._run, daemon=True, name="web cg")
        self._thread.start()

    def _start(self, name):
        _clean_leftovers()
        self._dir = tempfile.mkdtemp(prefix="playout-web-")
        args = [cef_exe(), f"--cg-url={self.url}", f"--cg-width={self.width}", f"--cg-height={self.height}",
                f"--cg-fps={max(1, round(self.rate))}", f"--cg-shm={name}",
                f"--cg-cache={os.path.join(self._dir, 'cache')}"]
        if self.css.strip():
            js = os.path.join(self._dir, "page.js")
            with open(js, "w", encoding="utf-8") as f:
                f.write(_css_js(self.css))
            args.append(f"--cg-js={js}")
        _quiet_crashes()
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        _procs.add(self._proc)
        threading.Thread(target=self._read, args=(self._proc,), daemon=True, name="web cg messages").start()

    def _read(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line == "loaded":
                self.loaded, self.error = True, ""
            elif line.startswith("error "):
                self.error = line[6:][:200]

    def _tell(self, cmd):
        try:
            self._proc.stdin.write((cmd + "\n").encode())
            self._proc.stdin.flush()
        except (OSError, AttributeError, ValueError):
            pass

    def _stop(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.stdin.write(b"quit\n")
                p.stdin.close()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=3)
                except Exception:
                    pass
            _procs.discard(p)
        d, self._dir = self._dir, None
        if d:
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _grab(arr, zoom):
        """Crop / zoom / straight alpha of a picture already copied out of the shared memory."""
        alpha = arr[..., 3]
        rows = np.flatnonzero(alpha.any(axis=1))
        if not len(rows):
            return None, (0, 0)
        r0, r1 = rows[0], rows[-1] + 1
        cols = np.flatnonzero(alpha[r0:r1].any(axis=0))
        c0, c1 = cols[0], cols[-1] + 1
        return _finish(arr[r0:r1, c0:c1].copy(), int(c0), int(r0), zoom, premultiplied=True)

    def _run(self):
        import mmap
        quick_fails = 0
        while self._running:
            name = f"Local\\playout-cefcg-{os.getpid()}-{next(_shm_ids)}"
            mm = arr = None
            started, frames_before = time.monotonic(), self.frames
            restart = False
            try:
                mm = mmap.mmap(-1, 64 + self.width * self.height * 4, tagname=name)
                arr = np.frombuffer(mm, dtype=np.uint8, count=self.width * self.height * 4, offset=64)
                arr = arr.reshape(self.height, self.width, 4)
                buf = np.empty_like(arr)
                self._start(name)
                shown, last, seen, poked = True, -1, time.monotonic(), 0.0
                while self._running:
                    if self._reload:
                        # Reload = start the page in a fresh browser (the picture on air stays until the new one)
                        self._reload = False
                        restart = True
                        break
                    if self.active != shown:
                        shown = self.active
                        self._tell("show" if shown else "hide")
                        seen, poked = time.monotonic(), 0.0
                    if self._proc.poll() is not None:
                        raise RuntimeError(f"The built-in browser stopped (exit code {self._proc.returncode})")
                    seq = struct.unpack_from("<q", mm, 16)[0]
                    now = time.monotonic()
                    if seq != last:
                        seen, poked = now, 0.0
                    if shown and seq != last and seq > 0 and seq % 2 == 0:
                        # copy the picture out first (a few ms), then check the browser did not draw over it
                        # meanwhile; the slower work (crop, alpha) is done on the copy. Checking only after
                        # that work threw away nearly every picture of a busy page (it froze on air)
                        np.copyto(buf, arr)
                        if struct.unpack_from("<q", mm, 16)[0] == seq:
                            last = seq
                            img, off = self._grab(buf, self.zoom)
                            self.frame = (img, off) if img is not None else None
                            self.frames += 1
                    elif shown and self.loaded and now - seen > 5:
                        # nothing drawn for a while: a still page is fine, so ask for one picture ("show" redraws);
                        # no picture after that = the browser is stuck: start it again
                        if not poked:
                            poked = now
                            self._tell("show")
                        elif now - poked > 5:
                            raise RuntimeError("The built-in browser stopped drawing the page (started again)")
                    time.sleep(self.interval)
            except Exception as e:
                if self._running:
                    self.error = self.error or (str(e)[:200] or type(e).__name__)
                    logging.info("web CG (built-in browser) %s: %s", self.url, self.error)
            finally:
                self._stop()
                self.loaded = False
                arr = None
                if mm is not None:
                    try:
                        mm.close()
                    except Exception:
                        pass
            if not self._running:
                break
            if restart:
                quick_fails = 0
                continue
            # stopped at once without drawing anything: twice in a row = this PC cannot run it, use Edge / Chrome
            quick = self.frames == frames_before and time.monotonic() - started < 15
            quick_fails = quick_fails + 1 if quick else 0
            if quick_fails >= 2:
                logging.info("web CG: built-in browser does not run on this PC, using Edge / Chrome (%s)", self.error)
                self._use_edge()
                return
            self._wait = 3 if not quick else min(60, self._wait * 2)
            end = time.monotonic() + self._wait
            while self._running and time.monotonic() < end:
                time.sleep(0.2)

    def _use_edge(self):
        fb = self._fallback = EdgeRenderer(self.url, self.width, self.height, self.zoom * 100, self.rate, self.css)
        LIVE.discard(self)
        while self._running:
            fb.active = self.active
            self.frame, self.error, self.loaded, self.frames = fb.frame, fb.error, fb.loaded, fb.frames
            time.sleep(min(0.05, self.interval))

    def reload(self):
        if self._fallback:
            self._fallback.reload()
        else:
            self._reload = True

    def close(self):
        self._running = False
        if self._fallback:
            self._fallback.close()
        self._stop()


class EdgeRenderer:
    """One headless browser page; .frame is the latest (bgra image, (dx, dy)) cropped to what is drawn."""
    engine = "edge"

    def __init__(self, url, width, height, zoom=100, rate=10, css=""):
        self.url = url
        self.css = str(css or "")      # extra CSS for the page (like vMix's Web Browser input)
        self.width, self.height = max(16, int(width)), max(16, int(height))
        self.zoom = max(1, float(zoom or 100)) / 100.0
        self.interval = 1.0 / max(1.0, min(30.0, float(rate or 10)))
        self.frame = None
        self.error = ""
        self.loaded = False
        self.frames = 0
        self.active = True               # take pictures (off while the CG is off air: the page stays loaded)
        self._reload = False
        self._pending = None             # latest picture from the browser, not decoded yet
        self._last_data = None
        self._unacked = None             # screencast picture not acknowledged yet (the browser waits for it)
        self._straight = False           # _pending is a screenshot (straight alpha), not a screencast picture
        self._wait, self._frames_seen = 1.5, 0
        self._running = True
        self._proc = None
        self._dir = None
        LIVE.add(self)
        self._thread = threading.Thread(target=self._run, daemon=True, name="web cg")
        self._thread.start()

    # ---- browser ------------------------------------------------------------------------------
    def _start_browser(self):
        exes = find_browsers()
        if not exes:
            raise RuntimeError("Microsoft Edge or Google Chrome is needed for web overlays (not found on this PC)")
        # some PCs stop one way at once (an Edge policy, an Edge update in progress...): then try the next
        ways = [(e, m) for e in exes for m in ("--headless=new", "--headless")]
        if _ways["good"] in ways:
            ways.remove(_ways["good"])
            ways.insert(0, _ways["good"])
        why = []
        for exe, mode in ways:
            try:
                url = self._launch(exe, mode)
                if _ways["good"] != (exe, mode):
                    _ways["good"] = (exe, mode)
                    logging.info("web CG browser: %s %s", exe, mode)
                return url
            except _StoppedAtOnce as e:
                why.append(f"{os.path.basename(exe)} {mode}: {e}")
                logging.info("web CG browser did not start: %s %s: %s", exe, mode, e)
                self._stop_browser()
        raise RuntimeError("The browser stopped at once (" + "; ".join(why)[:300] + ")")

    def _launch(self, exe, mode):
        _clean_leftovers()
        self._dir = tempfile.mkdtemp(prefix="playout-web-")
        args = [exe, mode, "--remote-debugging-port=0", f"--user-data-dir={self._dir}",
                "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-extensions", "--disable-sync",
                "--disable-breakpad", "--disable-crash-reporter", "--disable-component-update", "--no-pings",
                "--renderer-process-limit=1", "--disable-features=Translate,MediaRouter,OptimizationHints,BackForwardCache,CalculateNativeWinOcclusion",
                "--force-device-scale-factor=1", f"--window-size={self.width},{self.height}", "about:blank"]
        if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
            args[1:1] = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]   # Linux as root (test machines / containers)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        _quiet_crashes()
        err = open(os.path.join(self._dir, "browser-errors.txt"), "wb")
        try:
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=err, creationflags=flags)
        finally:
            err.close()
        _procs.add(self._proc)
        port_file = os.path.join(self._dir, "DevToolsActivePort")
        for _ in range(150):
            if self._proc.poll() is not None:
                raise _StoppedAtOnce(f"exit code {self._proc.returncode}" + self._errors())
            if os.path.isfile(port_file):
                with open(port_file, encoding="utf-8") as f:
                    port = f.readline().strip()
                if port:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("The browser did not start")
        # a page of our own (the first blank page can be replaced while the browser starts)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
                with opener.open(req, timeout=2) as r:
                    return json.load(r)["webSocketDebuggerUrl"]
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise RuntimeError("The browser page did not open")

    def _errors(self):
        """The last lines the browser wrote about why it stopped."""
        try:
            with open(os.path.join(self._dir, "browser-errors.txt"), encoding="utf-8", errors="replace") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
            return (", " + " | ".join(lines[-3:])[:240]) if lines else ""
        except OSError:
            return ""

    def _stop_browser(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
            _procs.discard(p)
        d, self._dir = self._dir, None
        if d:
            shutil.rmtree(d, ignore_errors=True)

    # ---- DevTools ---------------------------------------------------------------------------------
    def _call(self, ws, method, params=None, timeout=10):
        self._id += 1
        ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        end = time.monotonic() + timeout
        import websocket
        while time.monotonic() < end:
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("method") == "Page.screencastFrame":
                self._got_frame(ws, msg["params"])
                continue
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "DevTools error"))
                return msg.get("result") or {}
        raise TimeoutError(method)

    def _open(self, ws):
        self._call(ws, "Emulation.setDeviceMetricsOverride",
                   {"width": self.width, "height": self.height, "deviceScaleFactor": 1, "mobile": False})
        self._call(ws, "Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
        if self.css.strip():
            # the operator's CSS goes in every time the page loads (also after a reload), after the page's own
            js = _css_js(self.css)
            self._call(ws, "Page.enable")
            self._call(ws, "Page.addScriptToEvaluateOnNewDocument", {"source": js})
        r = self._call(ws, "Page.navigate", {"url": self.url}, timeout=30)
        if r.get("errorText"):
            raise RuntimeError(f"Could not open the page: {r['errorText']}")
        self.loaded = True

    def _got_frame(self, ws, p):
        """The browser sent a new picture (it only does when the page changed): keep the latest. The browser
        sends the next one only after this one is acknowledged, so it is acknowledged when it is used: while
        the CG is off air the browser stops making pictures (each one is a full-size PNG: a moving
        scoreboard page would otherwise keep the CPU busy all the time)."""
        self._pending, self._straight = p["data"], False
        self._unacked = p["sessionId"]

    def _ack(self, ws):
        sid, self._unacked = self._unacked, None
        if sid is not None:
            self._send(ws, "Page.screencastFrameAck", {"sessionId": sid})

    def _send(self, ws, method, params):
        self._id += 1
        ws.send(json.dumps({"id": self._id, "method": method, "params": params}))

    def _decode(self, data, premultiplied=True):
        im = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGBA")
        box = im.getchannel("A").getbbox()
        if box is None:
            return None, (0, 0)                     # nothing drawn (yet)
        im = im.crop(box)
        out = np.ascontiguousarray(np.asarray(im)[..., [2, 1, 0, 3]])
        # screencast pictures come premultiplied: back to straight alpha for the CG blend
        return _finish(out, box[0], box[1], self.zoom, premultiplied)

    def _run(self):
        import websocket
        while self._running:
            ws = None
            try:
                self._id = 0
                url = self._start_browser()
                ws = websocket.create_connection(url, timeout=15, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
                self._open(ws)
                self.error = ""
                self._pending = None
                # the browser sends a picture only when the page changes (a still score costs nothing)
                self._unacked = None
                # the page draws at up to 60 pictures a second; take only about as many as are used
                nth = max(1, int(60 * self.interval))
                self._call(ws, "Page.startScreencast", {"format": "png", "everyNthFrame": nth})
                ws.settimeout(self.interval)
                last = got = time.monotonic()
                while self._running:
                    if self._reload:
                        self._reload = False
                        self._call(ws, "Page.reload", {"ignoreCache": True}, timeout=30)
                    try:
                        msg = json.loads(ws.recv())
                        if msg.get("method") == "Page.screencastFrame":
                            self._got_frame(ws, msg["params"])
                    except websocket.WebSocketTimeoutException:
                        pass
                    now = time.monotonic()
                    if self._pending is not None:
                        got = now
                    elif self.active and now - got > 5:
                        # the screencast can go quiet (the page did not change, or the browser stopped sending):
                        # take a picture directly, so a CG put on air always shows the page as it is now
                        got = now
                        shot = self._call(ws, "Page.captureScreenshot", {"format": "png"}, timeout=10)
                        if shot.get("data") and self._pending is None:
                            self._pending, self._straight = shot["data"], True   # not premultiplied
                            last = 0.0
                    if self.active and self._pending is not None and now - last >= self.interval:
                        data, self._pending, last = self._pending, None, now
                        self._ack(ws)                      # ready for the next picture
                        if data != self._last_data:        # same picture again: nothing to decode
                            self._last_data = data
                            img, off = self._decode(data, premultiplied=not self._straight)
                            self.frame = (img, off) if img is not None else None
                        self.frames += 1
                        self.error = ""
                    if self._proc is None or self._proc.poll() is not None:
                        raise RuntimeError("The browser stopped")
            except Exception as e:
                if self._running:
                    self.error = str(e)[:200] or type(e).__name__
                    logging.info("web CG %s: %s", self.url, self.error)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._stop_browser()
                self.loaded = False
            if self._running:
                # try again (the page or the PC may be back), waiting longer each time it fails at once
                self._wait = 3 if self.frames > self._frames_seen else min(60, self._wait * 2)
                self._frames_seen = self.frames
                end = time.monotonic() + self._wait
                while self._running and time.monotonic() < end:
                    time.sleep(0.2)

    def reload(self):
        self._reload = True

    def close(self):
        self._running = False
        self._stop_browser()


def snapshot(url, width, height, wait=3.0, timeout=20.0, css=""):
    """One picture of the page (RGBA PIL image on a see-through background) and an error text."""
    r = WebRenderer(url, width, height, rate=4, css=css)
    try:
        end = time.monotonic() + timeout
        seen = None
        while time.monotonic() < end:
            if r.loaded and seen is None:
                seen = time.monotonic()
            if seen is not None and time.monotonic() - seen >= wait and r.frames:
                break
            if r.error and not r.loaded and time.monotonic() > end - timeout + 8:
                break
            time.sleep(0.1)
        canvas = Image.new("RGBA", (r.width, r.height), (0, 0, 0, 0))
        f = r.frame
        if f is not None:
            img, (dx, dy) = f
            canvas.paste(Image.fromarray(img[..., [2, 1, 0, 3]], "RGBA"), (dx, dy))
        return canvas, ("" if r.loaded else r.error or "The page did not load")
    finally:
        r.close()
