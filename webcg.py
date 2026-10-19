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

The browser is looked for in the usual install folders, or set "web_browser" in config.json.
"""
import atexit
import base64
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import numpy as np
from PIL import Image

_procs = set()
BROWSER = None          # config "web_browser" (app.py sets it)


def find_browser():
    """Path of Edge / Chrome / Chromium, or None."""
    if BROWSER and os.path.isfile(BROWSER):
        return BROWSER
    if sys.platform == "win32":
        bases = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe"]
        for rel in rels:
            for b in bases:
                if b and os.path.isfile(os.path.join(b, rel)):
                    return os.path.join(b, rel)
        return None
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _kill_all():
    for p in list(_procs):
        try:
            p.kill()
        except Exception:
            pass


atexit.register(_kill_all)
close_all = _kill_all          # Playout closing: no browser is left running


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


class WebRenderer:
    """One headless browser page; .frame is the latest (bgra image, (dx, dy)) cropped to what is drawn."""

    def __init__(self, url, width, height, zoom=100, rate=10):
        self.url = url
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
        self._unacked = None             # screencast picture not acknowledged yet (the browser waits for it)
        self._straight = False           # _pending is a screenshot (straight alpha), not a screencast picture
        self._wait, self._frames_seen = 1.5, 0
        self._running = True
        self._proc = None
        self._dir = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="web cg")
        self._thread.start()

    # ---- browser ------------------------------------------------------------------------------
    def _start_browser(self):
        exe = find_browser()
        if not exe:
            raise RuntimeError("Microsoft Edge or Google Chrome is needed for web overlays (not found on this PC)")
        self._dir = tempfile.mkdtemp(prefix="playout-web-")
        args = [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self._dir}",
                "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-extensions", "--disable-sync",
                "--disable-breakpad", "--disable-crash-reporter", "--disable-component-update", "--no-pings",
                "--renderer-process-limit=1", "--disable-features=Translate,MediaRouter,OptimizationHints,BackForwardCache",
                "--force-device-scale-factor=1", f"--window-size={self.width},{self.height}", "about:blank"]
        if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
            args[1:1] = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]   # Linux as root (test machines / containers)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        _quiet_crashes()
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=flags)
        _procs.add(self._proc)
        port_file = os.path.join(self._dir, "DevToolsActivePort")
        for _ in range(150):
            if self._proc.poll() is not None:
                raise RuntimeError(f"The browser stopped at once (exit code {self._proc.returncode})")
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
        dx, dy = box[0], box[1]
        if abs(self.zoom - 1.0) > 0.001:
            im = im.resize((max(1, round(im.width * self.zoom)), max(1, round(im.height * self.zoom))), Image.BILINEAR)
            dx, dy = round(dx * self.zoom), round(dy * self.zoom)
        a = np.asarray(im)
        out = np.ascontiguousarray(a[..., [2, 1, 0, 3]])
        # screencast pictures come premultiplied: back to straight alpha for the CG blend
        al = out[..., 3:4].astype(np.uint16)
        part = (al > 0) & (al < 255)
        if premultiplied and part.any():
            rgb = out[..., :3].astype(np.uint16) * 255 // np.maximum(al, 1)
            out[..., :3] = np.where(part, np.minimum(rgb, 255), out[..., :3]).astype(np.uint8)
        return out, (dx, dy)

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


def snapshot(url, width, height, wait=3.0, timeout=20.0):
    """One picture of the page (RGBA PIL image on a see-through background) and an error text."""
    r = WebRenderer(url, width, height, rate=4)
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
