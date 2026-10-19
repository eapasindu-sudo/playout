"""Blackmagic ATEM + Sony camera control: the ATEM Switcher app, run and driven by Playout.

The ATEM Switcher app (github.com/eapasindu-sudo/atem-web-switcher) comes with Playout in the
"atem" folder. It talks to the ATEM over the network (UDP 9910) and to Sony cameras (Sony Camera
Remote SDK), and serves the phone / tablet switcher and CCU page on port 3000. Playout:

  - starts it (Functions > ATEM & Cameras) and shows its pages in their own window
  - stays connected to it the way a phone does (WebSocket), so Playout always knows the ATEM's
    program / preview input and can switch it:
      * a live card with an "ATEM camera" puts that camera on air when the card goes on air
      * commercials can switch the ATEM to the input Playout's own output is plugged into
      * TAKE LIVE / end of playlist can go back to a main camera
  - writes every ATEM program change to the air log
  - CUT / AUTO / program / preview from the control screen, the phone remote and /trigger URLs

A switch is shown as done only when the ATEM's own state says so; until then it is "pending", and
when the ATEM does not follow within a few seconds the control screen says so.
"""
import datetime as dt
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time

PORT = 3000
EXE_NAME = "ATEM-Switcher.exe"
CONFIRM_SECS = 3.0


def default_exe():
    """The ATEM Switcher that came with Playout (installed app), or one put in build\\atem (source)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "atem", EXE_NAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "build", "atem", EXE_NAME)


def port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


class AtemLink:
    def __init__(self, asrun=None, exe=None, port=PORT):
        self.asrun = asrun
        self.exe = exe or default_exe()
        self.port = port
        self.proc = None                  # the ATEM Switcher, when Playout started it
        self.linked = False               # WebSocket to the ATEM Switcher is up
        self.atem = {}                    # its last state: connected, ip, model, program, preview, sources...
        self.cams = {}                    # Sony cameras: available, cams
        self.auto = {"enabled": False, "mode": "cut", "ads_input": 0, "live_input": 0}
        self.start_with_playout = False
        self.pending = None               # {"input", "since", "why"}: sent, not yet seen on the ATEM
        self.error = ""
        self.last_switch = ""
        self._q = queue.Queue()
        self._air = None                  # (input, since) of the ATEM's program, for the air log
        self._running = False
        self._lock = threading.Lock()

    # ---- settings ------------------------------------------------------------------
    def configure(self, auto=None, start_with_playout=None):
        with self._lock:
            if auto:
                a = self.auto
                if "enabled" in auto:
                    a["enabled"] = bool(auto["enabled"])
                if auto.get("mode") in ("cut", "auto"):
                    a["mode"] = auto["mode"]
                for k in ("ads_input", "live_input"):
                    if k in auto:
                        try:
                            a[k] = max(0, min(40, int(auto[k] or 0)))
                        except (TypeError, ValueError):
                            pass
            if start_with_playout is not None:
                self.start_with_playout = bool(start_with_playout)
        if self.auto["enabled"] or self.start_with_playout:
            threading.Thread(target=self.launch, daemon=True, name="atem launch").start()
        return self.settings()

    def settings(self):
        return {"auto": dict(self.auto), "start_with_playout": self.start_with_playout}

    # ---- the ATEM Switcher app ---------------------------------------------------------
    @property
    def installed(self):
        return os.path.isfile(self.exe)

    @property
    def running(self):
        return port_open(self.port)

    def launch(self):
        """Start the ATEM Switcher (no window of its own: Playout shows its pages). (ok, error)"""
        if self.running:
            return True, ""
        if not self.installed:
            return False, ("The ATEM Switcher is not installed with this Playout. Install the latest Playout, "
                           "or start ATEM Switcher yourself (it is found on port 3000).")
        try:
            env = dict(os.environ, PORT=str(self.port))
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen([self.exe, "--no-window"], env=env, cwd=os.path.dirname(self.exe),
                                         creationflags=flags, close_fds=True)
        except OSError as e:
            return False, f"Could not start the ATEM Switcher: {e}"
        for _ in range(50):
            if self.running:
                return True, ""
            if self.proc.poll() is not None:
                return False, f"The ATEM Switcher stopped at once (exit code {self.proc.returncode})"
            time.sleep(0.2)
        return False, "The ATEM Switcher did not answer on port %d" % self.port

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="atem link").start()

    def stop(self):
        self._running = False
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()       # only the copy Playout started; a separate one keeps running
            except Exception:
                pass

    # ---- commands --------------------------------------------------------------------
    def command(self, cmd, value=None, why="operator"):
        """program / preview (value = input number), cut, auto. (ok, error)"""
        if cmd not in ("program", "preview", "cut", "auto"):
            return False, "Unknown ATEM command"
        if not self.linked:
            return False, "ATEM Switcher is not running (Functions > ATEM & Cameras)"
        if not self.atem.get("connected"):
            return False, "The ATEM is not connected (set its IP address in ATEM & Cameras > Setup)"
        msg = {"cmd": cmd}
        if cmd in ("program", "preview"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return False, "Pick an input"
            if not any(s.get("id") == value for s in self.atem.get("sources") or []):
                return False, f"The ATEM has no input {value}"
            msg["input"] = value
        target = value if cmd == "program" else self.atem.get("preview") if cmd in ("cut", "auto") else None
        if target is not None and target != self.atem.get("program"):
            self.pending = {"input": target, "since": time.monotonic(), "why": why}
        self._q.put(msg)
        return True, ""

    def take(self, inp, mode=None, why="operator"):
        """Put an input on air: a cut, or through preview + AUTO (the ATEM's own transition)."""
        if (mode or self.auto["mode"]) == "auto":
            ok, err = self.command("preview", inp, why)
            if ok:
                self.pending = {"input": int(inp), "since": time.monotonic(), "why": why}
                self._q.put({"cmd": "auto"})
            return ok, err
        return self.command("program", inp, why)

    # ---- Playout on air -> ATEM ------------------------------------------------------------
    def on_air(self, item, state, idle):
        """Engine listener: what is on air changed."""
        a = self.auto
        if not a["enabled"]:
            return
        target, why = None, ""
        if item is not None and item.get("type") == "live" and item.get("atem_input"):
            target, why = item["atem_input"], f"live card {item.get('name', '')}"
        elif item is not None and item.get("type") == "clip" and a["ads_input"]:
            target, why = a["ads_input"], f"clip {item.get('name', '')}"
        elif item is None and state == "STOPPED" and idle == "live" and a["live_input"]:
            target, why = a["live_input"], "take live"
        if not target:
            return
        try:
            target = int(target)
        except (TypeError, ValueError):
            return
        if target == self.atem.get("program"):
            return
        ok, err = self.take(target, why=why)
        if not ok:
            self.error = f"ATEM did not switch for {why}: {err}"

    # ---- status ----------------------------------------------------------------------
    def label(self, inp):
        for s in self.atem.get("sources") or []:
            if s.get("id") == inp:
                return s.get("label") or f"CAM {inp}"
        return f"Input {inp}" if inp else "-"

    def info(self):
        p = self.pending
        return {
            "installed": self.installed, "running": self.running, "linked": self.linked, "port": self.port,
            "atem": dict(self.atem), "cams": dict(self.cams), "pending": p["input"] if p else None,
            "error": self.error, "last_switch": self.last_switch, **self.settings(),
        }

    # ---- WebSocket to the ATEM Switcher -------------------------------------------------------
    def _run(self):
        import websocket
        while self._running:
            if not self.running:
                self.linked = False
                time.sleep(2)
                continue
            try:
                ws = websocket.create_connection(f"ws://127.0.0.1:{self.port}/", timeout=5,
                                                 header=["User-Agent: Endora Playout"])
            except Exception as e:
                self.linked = False
                self.error = f"Cannot reach the ATEM Switcher: {e}"
                time.sleep(3)
                continue
            self.linked, self.error = True, ""
            try:
                ws.send(json.dumps({"cmd": "hello", "role": "controller"}))
                ws.settimeout(0.2)
                while self._running:
                    try:
                        raw = ws.recv()
                        if raw:
                            self._message(json.loads(raw))
                    except websocket.WebSocketTimeoutException:
                        pass
                    while not self._q.empty():
                        ws.send(json.dumps(self._q.get_nowait()))
                    self._check_pending()
            except Exception as e:
                logging.info("ATEM Switcher link closed: %s", e)
            finally:
                self.linked = False
                try:
                    ws.close()
                except Exception:
                    pass
            time.sleep(1)

    def _message(self, msg):
        kind = msg.get("type")
        if kind == "state":
            prev = self.atem.get("program")
            self.atem = {k: msg.get(k) for k in ("connected", "ip", "model", "program", "preview", "inTransition",
                                                 "sources", "version")}
            if msg.get("connected") and msg.get("program") != prev:
                self._program_changed(msg.get("program"))
        elif kind == "cams":
            self.cams = {"available": msg.get("available"), "cams": msg.get("cams") or []}

    def _program_changed(self, inp):
        now = dt.datetime.now()
        p = self.pending
        if p and p["input"] == inp:
            self.pending = None
            self.error = ""
            self.last_switch = f"{now:%H:%M:%S} {self.label(inp)} ({p['why']})"
        else:
            self.last_switch = f"{now:%H:%M:%S} {self.label(inp)}"
        if self._air and self.asrun is not None:
            old, since = self._air
            try:
                self.asrun.write("atem", f"ATEM {self.label(old)}", since, now, None, "on air", category="ATEM")
            except Exception:
                pass
        self._air = (inp, now) if inp else None

    def _check_pending(self):
        p = self.pending
        if p and time.monotonic() - p["since"] > CONFIRM_SECS:
            self.pending = None
            if self.atem.get("program") != p["input"]:
                self.error = f"The ATEM did not go to {self.label(p['input'])} ({p['why']})"
