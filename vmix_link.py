"""vMix link: scoreboard (and other CGs) out while vMix plays a replay, back in after it.

Playout keeps a connection to vMix's TCP API (port 8099 on the vMix PC; on in vMix by default) and
follows what vMix has on air:

  - "SUBSCRIBE ACTS" makes vMix send a line every time its program input or an overlay channel
    changes ("ACTS OK Input 3 1" = input 3 is now on air, "ACTS OK Overlay1 5 0" = input 5 left
    overlay 1), so the change is seen at once
  - "XML" every few seconds gives the list of inputs (number, title, type) and what is on air now,
    so the replay inputs are known and nothing is missed if a line was

Replay inputs are vMix's own Replay inputs (type "Replay"), or the inputs typed in the settings
(numbers or titles). When one of them goes on air (program or an overlay channel), Playout takes the
manual CGs that are on air OUT (the scoreboard links, or every CG); when vMix is back on another
input they go IN again. A CG the operator takes IN or OUT during the replay stays the way they left it.

vMix scripts / Stream Deck can do the same without the TCP link: /trigger/replay/on and /trigger/replay/off.
"""
import logging
import socket
import threading
import time
import xml.etree.ElementTree as ET

PORT = 8099
XML_EVERY = 3.0


class VmixLink:
    def __init__(self, cgm):
        self.cgm = cgm
        self.settings_ = {"enabled": False, "host": "127.0.0.1", "port": PORT, "inputs": "", "hide": "score",
                          "back_delay": 0.5}
        self.linked = False
        self.error = ""
        self.version = ""
        self.inputs = []                 # [{"number", "title", "type"}] from vMix's XML
        self.program = None              # vMix's program input number
        self.overlays = {}               # overlay channel -> input number
        self.replay_on = False           # a replay input is on air in vMix
        self.manual_replay = False       # /trigger/replay/on
        self.last = ""
        self._off_since = None
        self._running = False
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._eval_lock = threading.Lock()

    # ---- settings ------------------------------------------------------------------------
    def configure(self, s):
        s = s or {}
        with self._lock:
            c = self.settings_
            if "enabled" in s:
                c["enabled"] = bool(s["enabled"])
            if "host" in s:
                c["host"] = str(s["host"] or "").strip() or "127.0.0.1"
            if "port" in s:
                try:
                    c["port"] = max(1, min(65535, int(s["port"] or PORT)))
                except (TypeError, ValueError):
                    pass
            if "inputs" in s:
                c["inputs"] = str(s["inputs"] or "").strip()
            if s.get("hide") in ("score", "all"):
                c["hide"] = s["hide"]
            if "back_delay" in s:
                try:
                    c["back_delay"] = max(0.0, min(10.0, float(s["back_delay"] or 0)))
                except (TypeError, ValueError):
                    pass
        self._wake.set()                 # reconnect with the new host / port
        if not self.settings_["enabled"] and not self.manual_replay:
            with self._eval_lock:
                self._set_replay(False, "vMix link turned off")
        return self.settings()

    def settings(self):
        return dict(self.settings_)

    # ---- which inputs are replays -----------------------------------------------------------
    def replay_numbers(self):
        typed = [t.strip().lower() for t in self.settings_["inputs"].replace(";", ",").split(",") if t.strip()]
        out = set()
        for i in self.inputs:
            if typed:
                if str(i["number"]) in typed or i["title"].lower() in typed:
                    out.add(i["number"])
            elif i["type"].lower() == "replay":
                out.add(i["number"])
        for t in typed:                  # numbers typed before vMix's list has come
            if t.isdigit():
                out.add(int(t))
        return out

    def title(self, n):
        for i in self.inputs:
            if i["number"] == n:
                return i["title"] or f"Input {n}"
        return f"Input {n}" if n else "-"

    # ---- replay on / off --------------------------------------------------------------------
    def trigger(self, on):
        """/trigger/replay/on|off (vMix script, Stream Deck)."""
        self.manual_replay = bool(on)
        self._evaluate()

    def _evaluate(self):
        with self._eval_lock:
            self._evaluate_locked()

    def _evaluate_locked(self):
        reps = self.replay_numbers() if self.settings_["enabled"] and self.linked else set()
        on_air = [n for n in [self.program, *self.overlays.values()] if n in reps]
        now = time.monotonic()
        if on_air or self.manual_replay:
            self._off_since = None
            self._set_replay(True, f"vMix replay on air ({self.title(on_air[0])})" if on_air else "replay trigger")
        elif self.replay_on:
            if self._off_since is None:
                self._off_since = now
            if now - self._off_since >= self.settings_["back_delay"]:
                self._off_since = None
                self._set_replay(False, f"vMix back on {self.title(self.program)}")

    def _set_replay(self, on, why):
        if on == self.replay_on:
            return
        self.replay_on = on
        self.last = f"{time.strftime('%H:%M:%S')} {why}"
        logging.info("vMix link: %s", why)
        try:
            n = self.cgm.hold(on, scoreboards_only=self.settings_["hide"] == "score")
            if on:
                self.last += f": {n} CG(s) out" if n else ": no CG on air"
            elif n:
                self.last += f": {n} CG(s) back in"
        except Exception as e:
            logging.info("vMix link: CGs: %s", e)

    # ---- status ------------------------------------------------------------------------
    def info(self):
        reps = sorted(self.replay_numbers())
        return {**self.settings(), "linked": self.linked, "error": self.error, "version": self.version,
                "program": self.program, "program_title": self.title(self.program) if self.program else "",
                "replay_inputs": [{"number": n, "title": self.title(n)} for n in reps],
                "replay_on": self.replay_on, "last": self.last,
                "inputs": [{"number": i["number"], "title": i["title"], "type": i["type"]} for i in self.inputs]}

    # ---- TCP API --------------------------------------------------------------------------
    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="vmix link").start()

    def stop(self):
        self._running = False
        self._wake.set()

    def _run(self):
        while self._running:
            if not self.settings_["enabled"]:
                self.linked, self.error = False, ""
                self._wake.wait(2)
                self._wake.clear()
                continue
            host, port = self.settings_["host"], self.settings_["port"]
            try:
                sock = socket.create_connection((host, port), timeout=3)
            except OSError as e:
                self.linked = False
                self.error = f"Cannot reach vMix at {host}:{port} ({e}). Is vMix open there? (Settings > Web Controller / TCP API port {PORT})"
                self._evaluate()
                self._wake.wait(3)
                self._wake.clear()
                continue
            self.linked, self.error = True, ""
            logging.info("vMix link: connected to %s:%s", host, port)
            try:
                self._session(sock)
            except Exception as e:
                logging.info("vMix link closed: %s", e)
                self.error = f"vMix connection closed: {e}"
            finally:
                self.linked = False
                try:
                    sock.close()
                except OSError:
                    pass
            self.program, self.overlays = None, {}
            self._evaluate()
            self._wake.wait(1)
            self._wake.clear()

    def _session(self, sock):
        sock.settimeout(0.2)
        sock.sendall(b"SUBSCRIBE ACTS\r\nXML\r\n")
        buf = b""
        next_xml = time.monotonic() + XML_EVERY
        host, port = self.settings_["host"], self.settings_["port"]
        while self._running and self.settings_["enabled"] and (host, port) == (self.settings_["host"], self.settings_["port"]):
            try:
                data = sock.recv(65536)
                if not data:
                    raise ConnectionError("vMix closed the connection")
                buf += data
            except socket.timeout:
                pass
            while True:
                nl = buf.find(b"\r\n")
                if nl < 0:
                    break
                line = buf[:nl].decode("utf-8", "replace")
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "XML" and parts[1].isdigit():
                    size = int(parts[1])
                    if len(buf) < nl + 2 + size:
                        break                     # the rest of the XML has not come yet
                    self._xml(buf[nl + 2:nl + 2 + size])
                    buf = buf[nl + 2 + size:]
                    continue
                buf = buf[nl + 2:]
                self._line(parts)
            if time.monotonic() >= next_xml:
                sock.sendall(b"XML\r\n")
                next_xml = time.monotonic() + XML_EVERY
            self._evaluate()

    def _line(self, parts):
        # ACTS OK Input 3 1 / ACTS OK Overlay1 5 0
        if len(parts) < 5 or parts[0] != "ACTS" or parts[1] != "OK":
            return
        name, num, val = parts[2], parts[3], parts[4]
        try:
            n, on = int(num), float(val) >= 0.5
        except ValueError:
            return
        if name == "Input":
            if on:
                self.program = n
            elif self.program == n:
                self.program = None
        elif name.startswith("Overlay") and name[7:].isdigit():
            ch = int(name[7:])
            if on:
                self.overlays[ch] = n
            elif self.overlays.get(ch) == n:
                del self.overlays[ch]

    def _xml(self, raw):
        try:
            root = ET.fromstring(raw.decode("utf-8", "replace").strip())
        except ET.ParseError as e:
            logging.info("vMix link: XML: %s", e)
            return
        self.version = root.findtext("version") or ""
        ins = []
        for i in root.iter("input"):
            try:
                ins.append({"number": int(i.get("number")), "title": i.get("title") or i.get("shortTitle") or "",
                            "type": i.get("type") or ""})
            except (TypeError, ValueError):
                continue
        if ins:
            self.inputs = ins
        act = root.findtext("active")
        if act and act.strip().isdigit():
            self.program = int(act)
        ov = root.find("overlays")
        if ov is not None:
            o = {}
            for el in ov.findall("overlay"):
                try:
                    if (el.text or "").strip():
                        o[int(el.get("number"))] = int(el.text)
                except (TypeError, ValueError):
                    continue
            self.overlays = o
