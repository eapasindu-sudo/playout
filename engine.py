"""Playout engine: runs a playlist of commercial clips and LIVE cards on one output clock.

Playlist item (dict):
    id        unique id
    type      "clip" | "live"
    name      display name
    file      clip file name in the media folder (clip only)
    duration  seconds; for clips filled from the file (after trimming), for live cards optional
              (None = stay live until the operator presses Next)
    fixed     "HH:MM:SS" or "" - item jumps on air at that time of day
    client    advertiser name (clips), for the ad report
    in, out   clip trim points in seconds (0 / empty = start / end of the file)
    gain_db   extra volume for this clip
    category  clip type: Commercial (default), Program, Starting Soon, Theme, Promo, ...
    cgs       CGs (logo, bug, lower third) that go on air with this item
    squeeze   Squeeze Feed preset (.cgp) that goes IN with this item; squeeze_at = seconds after the
              item starts, squeeze_for = seconds on air (0 = until the item ends)
"""
import csv
import datetime as dt
import os
import threading
import logging
import time
import traceback
import uuid
from fractions import Fraction

import numpy as np

import gpuengine

from audiofx import MasterAudio
from media import ClipPlayer, probe
from capture import CaptureInput, UrlInput, make_bars
from ndi_extra import ExtraOutput, still_frame
from ndi_io import CHANNELS, SAMPLE_RATE, LiveInput, NDIOutput, SourceFinder

FADE = 480  # 10 ms audio fade at every cut, avoids clicks


def parse_fps(value):
    s = str(value).strip()
    table = {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001), "59.94": Fraction(60000, 1001)}
    return table.get(s) or Fraction(s)


class MainInput:
    """The main live input (top right picker, and every live card set to "Main input"): a vMix / NDI
    source, or a USB capture device such as a Blackmagic ATEM Mini in USB mode ("dev:Video|Sound")."""

    def __init__(self, finder, width, height, fps):
        self.ndi = LiveInput(finder, width, height)
        self.width, self.height, self.fps = width, height, fps
        self.dev = None
        self.source_name = None

    def set_source(self, name):
        name = name or None
        self.source_name = name
        if name and name.startswith("dev:"):
            self.ndi.set_source(None)
            if self.dev is None or self.dev.key != name:
                if self.dev is not None:
                    self.dev.close()
                self.dev = CaptureInput(name, self.width, self.height, self.fps)
        else:
            if self.dev is not None:
                self.dev.close()
                self.dev = None
            self.ndi.set_source(name)

    @property
    def connected(self):
        return self.dev.connected if self.dev is not None else self.ndi.connected

    @property
    def is_device(self):
        return self.dev is not None

    def __getattr__(self, name):          # mode, in_fps, error of the USB device
        dev = self.__dict__.get("dev")
        if dev is not None and name in ("mode", "in_fps", "error"):
            return getattr(dev, name)
        raise AttributeError(name)

    def grab(self, n, want_video=True):
        return self.dev.grab(n, want_video) if self.dev is not None else self.ndi.grab(n, want_video)

    def close(self):
        if self.dev is not None:
            self.dev.close()
        self.ndi.close()


def new_id():
    return uuid.uuid4().hex[:8]


def _num(v):
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


class AsRunLog:
    FIELDS = ["date", "start", "end", "type", "item", "planned_sec", "played_sec", "status", "client", "file",
              "category"]

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, day):
        return os.path.join(self.folder, f"asrun-{day:%Y-%m-%d}.csv")

    def _upgrade(self, path):
        """Logs from older versions have fewer columns: rewrite them with the new header."""
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if not rows or rows[0] == self.FIELDS:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self.FIELDS)
            for r in rows[1:]:
                w.writerow((r + [""] * len(self.FIELDS))[:len(self.FIELDS)])

    def write(self, kind, name, started, ended, planned, status, client="", file="", category=""):
        path = self.path_for(started.date())
        with self._lock:
            new = not os.path.exists(path)
            if not new:
                try:
                    self._upgrade(path)
                except Exception:
                    pass
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(self.FIELDS)
                w.writerow([f"{started:%Y-%m-%d}", f"{started:%H:%M:%S}", f"{ended:%H:%M:%S}", kind, name,
                            f"{planned:.1f}" if planned else "", f"{(ended - started).total_seconds():.1f}", status,
                            client or "", file or "", category or ""])

    def read(self, day):
        path = self.path_for(day)
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


class Engine:
    def __init__(self, cfg, media_dir, log_dir):
        self.width, self.height = int(cfg["width"]), int(cfg["height"])
        self.fps = parse_fps(cfg["fps"])
        self.media_dir = media_dir
        self.asrun = AsRunLog(log_dir)
        self.finder = SourceFinder()
        self.live = MainInput(self.finder, self.width, self.height, self.fps)
        self.master = MasterAudio()          # program sound: auto level + limiter
        self.output_name = cfg.get("output_name", "ENDORA PLAYOUT FINAL")
        self.output = NDIOutput(self.output_name, self.width, self.height, self.fps, paced=True)   # None = NDI output off
        self.extra = {}              # "clean" / "preview": ExtraOutput (NDI Sources panel)
        self.ndi_key = {"source": "", "on": False}   # an NDI source with alpha keyed over the program
        self.key_input = None
        self.audio_pairs = {}        # NDI source name -> channel pair to take (0 = 1-2, 1 = 3-4, ...)
        self._preview_next = None    # id of the item the Preview output shows
        self._stills = {}            # (file, in) -> first picture of the clip, for the Preview output
        self.listeners = []          # fn(item, state, idle) when what is on air changes (ATEM link)
        self._air_key = None
        self.black = np.zeros(self.width * self.height * 4, np.uint8)
        self.black[3::4] = 255

        self._lock = threading.RLock()
        self.playlist = []
        self.version = 0             # bumps on every playlist change
        self.state = "STOPPED"       # STOPPED (= live on air) | PLAYING | PAUSED
        self.auto = True             # advance to the next item by itself
        self.loop = False            # wrap from last item to first
        self.on_air = None           # id of the item on air
        self.cued = None             # id of the item Play will start
        self.player = None           # ClipPlayer for an on-air clip
        self.preload = None          # (item id, ClipPlayer) decoding ahead
        self.item_started = None     # wall clock when the item went to air
        self.item_ticks = 0          # frames the on-air item has run
        self.last_frame = self.black
        self.clean_frame = self.black
        self.last_clip_frame = None
        self.last_source = None
        self.meters = [-90.0, -90.0]
        self.played = set()          # ids already aired in this run (greyed on screen)
        self.message = ""
        self.on_change = None        # callback to persist the playlist
        self.streamer = None         # Streamer fed with every program frame
        self.audio_out = None        # program sound on this PC's speakers
        self.recorder = None         # program recording to a file
        self.sdi = None              # DeckLink SDI output
        self.cg = None               # CGManager keyed over the program
        self.inputs = {}             # extra live sources used by live cards: key -> input
        self._main_wanted = True
        self.idle = "live"           # what goes on air when stopped: "live" (TAKE LIVE) or "bars" (Stop)
        self.bars = make_bars(self.width, self.height)
        self.stinger = {"file": "", "cut": 0.0, "auto": False}   # cut 0 = half of the stinger's length
        self._pending = None         # [frames left, action, item id]: switch at the stinger's cut point
        self._stinger_item = None    # on-air item whose end already got its stinger
        self.loudness = None         # Loudness: audio normalize gains
        self.ffmpeg = None           # FFmpeg program for network feeds (url: sources)
        # failover: when the live input is lost, put a filler on air instead of a frozen / black picture
        self.failover = {"enabled": True, "after": 3.0, "mode": "bars", "file": ""}
        self.failover_active = False
        self._lost_since = None
        self._fail_started = None
        self._filler = None          # ClipPlayer for the filler clip (looped)
        self._fill_audio = None
        self._fill_last = None
        self._fo_checked = False
        self.heartbeat = time.monotonic()   # output loop alive (watchdog)
        self.loop_errors = 0
        self.late_frames = 0                # frames that took longer than one frame time to make
        self.gpu_frames = self.gpu_misses = 0   # frames mixed by the GPU engine / mixed here because it did not answer
        self.load = 0.0                     # average work per frame / frame time (1.0 = no time to spare)
        self._running = False
        self._thread = None

    def set_failover(self, enabled=None, after=None, mode=None, file=None):
        with self._lock:
            f = self.failover
            if enabled is not None:
                f["enabled"] = bool(enabled)
            if after not in (None, ""):
                f["after"] = max(0.5, min(60.0, float(after)))
            if mode in ("bars", "black", "clip"):
                f["mode"] = mode
            if file is not None:
                f["file"] = file
                if self._filler:
                    self._filler.close()
                    self._filler = None
            return dict(f)

    # -- NDI outputs, key, sound channels (NDI Sources panel) --------------------
    def set_ndi_outputs(self, enabled=None, name=None, clean=None, preview=None, led=None):
        """Program NDI output on / off and its name; the extra Clean, Preview and LED outputs on / off."""
        with self._lock:
            name = (str(name).strip()[:80] if name is not None else "") or self.output_name
            renamed = name != self.output_name
            self.output_name = name
            want = (self.output is not None) if enabled is None else bool(enabled)
            old = []
            if self.output is not None and (renamed or not want):
                old.append(self.output)
                self.output = None
            if want and self.output is None:
                self.output = NDIOutput(self.output_name, self.width, self.height, self.fps, paced=True)
            extra = dict(self.extra)
            for kind, on in (("clean", clean), ("preview", preview), ("led", led)):
                cur = extra.get(kind)
                on = (cur is not None) if on is None else bool(on)
                if cur is not None and (renamed or not on):
                    old.append(extra.pop(kind))
                if on and kind not in extra:
                    extra[kind] = ExtraOutput(f"{self.output_name} - {kind.upper() if kind == 'led' else kind.title()}",
                                              self.width, self.height, self.fps, sound=(kind != "preview"),
                                              compose=self._led_frame if kind == "led" else None)
            self.extra = extra           # a new dict: the output loop never sees it half changed
            if old:
                # the output loop may be sending on an old one this very moment: close it a little later
                threading.Timer(0.5, lambda: [o.close() for o in old]).start()
            return self.ndi_outputs()

    def ndi_outputs(self):
        prog = None
        if self.output is not None:
            on_pgm, on_pvw = self.output.tally()
            prog = {"name": self.output.name, "connections": self.output.connections(), "tally": [on_pgm, on_pvw]}
        return {"name": self.output_name, "enabled": self.output is not None, "program": prog,
                "clean": self.extra["clean"].info() if "clean" in self.extra else None,
                "preview": self.extra["preview"].info() if "preview" in self.extra else None,
                "led": self.extra["led"].info() if "led" in self.extra else None}

    def _led_frame(self, job):
        """LED output thread: the program with the CGs it was handed (no score overlay)."""
        src, layers = job
        if not layers or self.cg is None:
            return np.asarray(src).reshape(-1)
        return np.asarray(self.cg.composite(np.asarray(src).reshape(self.height, self.width, 4), layers)).reshape(-1)

    def set_ndi_key(self, source=None, on=None):
        """An NDI source with alpha (graphics from vMix, CasparCG, a titler PC) keyed over the program."""
        with self._lock:
            if source is not None and source != self.ndi_key["source"]:
                self.ndi_key["source"] = source
                if self.key_input is not None:
                    old, self.key_input = self.key_input, None
                    old.set_source(None)          # (also stops the output loop opening it again)
                    old.close()
            if on is not None:
                self.ndi_key["on"] = bool(on)
            if self.ndi_key["source"] and self.key_input is None:
                self.key_input = LiveInput(self.finder, self.width, self.height)
                self.key_input.keep_alpha = True
                self.key_input.set_source(self.ndi_key["source"])
            return self.key_info()

    def key_info(self):
        k = self.key_input
        return dict(self.ndi_key, **({"input": k.info()} if k is not None else {}))

    def set_audio_pair(self, source, pair):
        with self._lock:
            pair = max(0, min(7, int(pair or 0)))
            if pair:
                self.audio_pairs[source] = pair
            else:
                self.audio_pairs.pop(source, None)
            return dict(self.audio_pairs)

    def ndi_inputs(self):
        """Every NDI source Playout is receiving now, with what it sends."""
        out = []
        if not self.live.is_device and self.live.source_name:
            out.append(dict(self.live.ndi.info(), use="Main input"))
        for key, inp in list(self.inputs.items()):
            if key.startswith("ndi:"):
                out.append(dict(inp.info(), use="Live card"))
        if self.key_input is not None:
            out.append(dict(self.key_input.info(), use="Key over program"))
        return out

    def _ndi_receivers(self):
        """(source key used by live cards, LiveInput) of every NDI input."""
        rx = []
        if not self.live.is_device and self.live.source_name:
            rx.append(("", self.live.ndi))
        rx += [(k, i) for k, i in list(self.inputs.items()) if k.startswith("ndi:")]
        return rx

    def _tally(self, source, next_item):
        """NDI tally: red on the source on air, green on the next live card's source. Sound channel pairs."""
        prog = "" if source == "LIVE" else source[5:] if source.startswith("LIVE:") else None
        nxt = None
        if next_item is not None and next_item.get("type") == "live":
            nxt = next_item.get("source") or ""
            if nxt == self.live.source_name:
                nxt = ""
        for key, inp in self._ndi_receivers():
            if key == self.live.source_name:
                key = ""
            inp.set_tally(prog == key, nxt == key)
            inp.audio_pair = self.audio_pairs.get(inp.source_name, 0)
        if self.key_input is not None:
            self.key_input.set_tally(bool(self.ndi_key.get("on")), False)

    def _preview_frame(self, n, live_v):
        """What the Preview output shows: the next item."""
        item = self._item(self._preview_next)
        if item is None:
            return self.black
        if item["type"] == "live":
            key = item.get("source") or ""
            on = self._item(self.on_air) or {}
            if on.get("type") == "live" and (on.get("source") or "") == key:
                return self.clean_frame                      # the same source is on air already
            if not key or key == self.live.source_name:
                return live_v if live_v is not None else self.black
            if key == "bars":
                return self.bars
            inp = self.inputs.get(key)
            v = inp.grab(n)[0] if inp is not None else None
            return v if v is not None else self.black
        sk = (item.get("file"), _num(item.get("in")))
        if sk not in self._stills:
            if len(self._stills) > 8:
                self._stills.clear()
            self._stills[sk] = None
            path = os.path.join(self.media_dir, item.get("file") or "")

            def load():
                self._stills[sk] = still_frame(path, sk[1], self.width, self.height)
            threading.Thread(target=load, daemon=True, name="preview still").start()
        v = self._stills.get(sk)
        return v if v is not None else self.black

    def _notify_air(self):
        key = (self.on_air if self.state == "PLAYING" else None, self.state, self.idle)
        if key == self._air_key:
            return
        self._air_key = key
        item = self._item(self.on_air) if self.state == "PLAYING" else None
        for fn in list(self.listeners):
            try:
                fn(item, self.state, self.idle)
            except Exception:
                logging.error("on-air listener error:\n%s", traceback.format_exc())

    # -- playlist editing ----------------------------------------------
    def set_playlist(self, items):
        with self._lock:
            clean = []
            for it in items:
                it = dict(it)
                it["id"] = it.get("id") or new_id()
                it["fixed"] = it.get("fixed") or ""
                if it.get("type") == "clip" and it.get("file"):
                    path = os.path.join(self.media_dir, it["file"])
                    full = probe(path)
                    it["file_duration"] = full
                    t_in = _num(it.get("in"))
                    t_out = _num(it.get("out"))
                    if full and t_out and t_out >= full:
                        t_out = 0.0
                    if full and t_in >= full:
                        t_in = 0.0
                    if t_out and t_out <= t_in:
                        t_out = 0.0
                    it["in"], it["out"] = t_in, t_out
                    it["duration"] = ((t_out or full) - t_in) if full else None
                    it["gain_db"] = _num(it.get("gain_db"))
                    it["client"] = (it.get("client") or "").strip()
                    it["category"] = (it.get("category") or "Commercial").strip()[:30] or "Commercial"
                    it["name"] = it.get("name") or os.path.splitext(it["file"])[0]
                    if self.loudness is not None and full:
                        self.loudness.request(path)
                elif it.get("type") == "live":
                    d = it.get("duration")
                    it["duration"] = float(d) if d not in (None, "", 0, "0") else None
                    it["name"] = it.get("name") or "LIVE"
                    try:
                        it["atem_input"] = max(0, int(it.get("atem_input") or 0))   # ATEM camera on air with it
                    except (TypeError, ValueError):
                        it["atem_input"] = 0
                else:
                    continue
                it["squeeze"] = os.path.basename(str(it.get("squeeze") or ""))
                it["squeeze_at"] = max(0.0, _num(it.get("squeeze_at")))
                it["squeeze_for"] = max(0.0, _num(it.get("squeeze_for")))
                clean.append(it)
            self.playlist = clean
            if self._index(self.cued) is None:
                self.cued = None
            self._changed()

    def _changed(self):
        self.version += 1
        if self.on_change:
            try:
                self.on_change(self.playlist)
            except Exception:
                pass

    def _index(self, item_id):
        if item_id is None:
            return None
        for i, it in enumerate(self.playlist):
            if it["id"] == item_id:
                return i
        return None

    def _item(self, item_id):
        i = self._index(item_id)
        return self.playlist[i] if i is not None else None

    def _next_id(self):
        """Item that follows the on-air one (or the cued one when stopped)."""
        if not self.playlist:
            return None
        if self.on_air is None:
            return self.cued or self.playlist[0]["id"]
        i = self._index(self.on_air)
        if i is None:
            return self.cued
        if i + 1 < len(self.playlist):
            return self.playlist[i + 1]["id"]
        return self.playlist[0]["id"] if self.loop else None

    # -- transport (web / scheduler threads) ----------------------------
    def play(self, item_id=None):
        with self._lock:
            if self.state == "PAUSED" and item_id is None:
                self.state = "PLAYING"
                self.message = "Resumed"
                return
            target = item_id or self.cued or (self.playlist[0]["id"] if self.playlist else None)
            if target is None:
                self.message = "Playlist is empty"
                return
            self._finish("taken off")
            self._start(target)

    def pause(self):
        with self._lock:
            if self.state == "PLAYING" and self.player is not None:
                self.state = "PAUSED"
                self.message = "Paused"
            elif self.state == "PAUSED":
                self.state = "PLAYING"
                self.message = "Resumed"

    def next(self):
        with self._lock:
            if self.on_air is None:
                self.play()
                return
            nxt = self._next_id()
            self._finish("skipped")
            if nxt:
                self._start(nxt)
            else:
                self._stop_to_live()
                self._changed()

    def stop(self):
        """Stop the playlist and put colour bars on air."""
        with self._lock:
            nxt = self._next_id()
            self._finish("stopped")
            self._stop_to_live()
            self.idle = "bars"
            self._pending = None
            self.cued = nxt
            self.message = "Stopped - colour bars on air"
            self._changed()

    def take_live(self):
        """Stop the playlist and put the live feed on air at once."""
        with self._lock:
            nxt = self._next_id() if self.on_air else self.cued
            self._finish("stopped")
            self._stop_to_live()
            self.idle = "live"
            self._pending = None
            self.cued = nxt
            self.message = "Live on air"
            self._changed()

    # -- stinger transitions -------------------------------------------
    def set_stinger(self, file=None, cut=None, auto=None):
        with self._lock:
            if file is not None:
                self.stinger["file"] = file
            if cut is not None:
                self.stinger["cut"] = max(0.0, float(cut or 0))
            if auto is not None:
                self.stinger["auto"] = bool(auto)
            return dict(self.stinger)

    def _cut_frames(self):
        cut = self.stinger.get("cut") or 0
        if not cut:
            from cg import cg_info
            path = os.path.join(self.cg.folder, self.stinger["file"]) if self.cg else ""
            d = cg_info(path).get("duration") if os.path.isfile(path) else None
            cut = (d or 1.0) / 2
        return max(1, int(round(cut * float(self.fps))))

    def _fire_stinger(self):
        return bool(self.cg is not None and self.stinger.get("file") and self.cg.start_stinger(self.stinger["file"]))

    def stinger_take(self, item_id=None):
        """Play the stinger now and switch to the next item (or item_id) at its cut point."""
        with self._lock:
            if self._pending:
                return
            if not self._fire_stinger():
                self.message = "Pick a stinger file (a video CG) first"
                return
            self._pending = [self._cut_frames(), "play" if item_id else "next", item_id]

    def take_next(self):
        """Next button: through the stinger when 'auto stinger' is on."""
        if self.stinger.get("auto") and self.stinger.get("file") and self.on_air:
            self.stinger_take()
        else:
            self.next()

    def take_item(self, item_id):
        if self.stinger.get("auto") and self.stinger.get("file") and item_id:
            self.stinger_take(item_id)
        else:
            self.play(item_id)

    def cue(self, item_id):
        with self._lock:
            if self._index(item_id) is not None:
                self.cued = item_id
                self._changed()

    def set_modes(self, auto=None, loop=None):
        with self._lock:
            if auto is not None:
                self.auto = bool(auto)
            if loop is not None:
                self.loop = bool(loop)
            self._changed()

    def set_live_source(self, name):
        self.live.set_source(name)

    def reopen_device(self, video):
        """A USB device's picture format changed: open it again wherever it is in use."""
        dev = lambda k: (k or "").startswith("dev:") and k[4:].partition("|")[0] == video
        with self._lock:
            m = self.live
            if m.dev is not None and dev(m.dev.key):
                m.dev.close()
                m.dev = CaptureInput(m.dev.key, self.width, self.height, self.fps)
            for key in [k for k in self.inputs if dev(k)]:
                self.inputs.pop(key).close()
                self.inputs[key] = CaptureInput(key, self.width, self.height, self.fps)

    def _sync_inputs(self):
        """Open the extra inputs live cards use (other NDI sources, USB capture, bars); close the rest."""
        wanted = {it.get("source") for it in self.playlist if it.get("type") == "live" and it.get("source")}
        wanted.discard(self.live.source_name)     # the main input's own device: it is open already
        for key in list(self.inputs):
            if key not in wanted:
                self.inputs.pop(key).close()
        for key in wanted - set(self.inputs):
            try:
                if key.startswith("ndi:"):
                    inp = LiveInput(self.finder, self.width, self.height)
                    inp.set_source(key[4:])
                elif key.startswith("url:"):
                    inp = UrlInput(key, self.width, self.height, self.ffmpeg)
                else:
                    inp = CaptureInput(key, self.width, self.height, self.fps)
                self.inputs[key] = inp
            except Exception as e:
                self.message = f"Cannot open {key}: {e}"

    # -- internals (lock held) ------------------------------------------
    def _open(self, item, offset=0.0):
        path = os.path.join(self.media_dir, item.get("file") or "")
        if not os.path.isfile(path):
            return None
        db = _num(item.get("gain_db"))
        if self.loudness is not None:
            db += self.loudness.gain_db(path)
        return ClipPlayer(path, self.width, self.height, self.fps,
                          start=_num(item.get("in")) + max(0.0, offset), end=_num(item.get("out")) or None,
                          gain=10 ** (db / 20))

    def resume(self, item_id, offset, message="Resumed after restart"):
        """After a restart: put item_id back on air, offset seconds in (clips continue where they were)."""
        with self._lock:
            item = self._item(item_id)
            if item is None:
                return False
            self._finish("taken off")
            if item["type"] == "clip" and item.get("duration") and offset >= item["duration"] - 1:
                self.on_air = item_id
                self._item_done()
                return True
            if self.preload:
                self.preload[1].close()
                self.preload = None
            if item["type"] == "clip":
                p = self._open(item, offset)
                if p is None:
                    return False
                self.preload = (item_id, p)
            self._start(item_id)
            if item["type"] == "live":
                self.item_ticks = int(offset * float(self.fps))
            elif self.player is not None:
                self.player.tick = 0
                self.item_ticks = int(offset * float(self.fps))
            self.message = f"{message}: {item['name']}"
            return True

    def _start(self, item_id, depth=0):
        item = self._item(item_id)
        if item is None:
            return
        player = None
        if self.preload and self.preload[0] == item_id:
            player = self.preload[1]
            self.preload = None
        elif item["type"] == "clip":
            player = self._open(item)
            if player is None:
                now = dt.datetime.now()
                self.asrun.write("clip", item["name"], now, now, item.get("duration"), "missing file",
                                 item.get("client", ""), item.get("file", ""), item.get("category", ""))
                self.message = f"Missing file: {item.get('file')}"
                self.on_air = item_id
                if depth < len(self.playlist):
                    self._item_done(depth + 1)
                return
        if self._index(item_id) == 0:
            self.played.clear()
        self.played.discard(item_id)
        self.player = player
        self.on_air = item_id
        if self.cg is not None:
            if item.get("cgs"):
                self.cg.start_card(item_id, item.get("cgs"))
            else:
                self.cg.stop_card()
            if item.get("squeeze"):
                err = self.cg.squeeze_item(item_id, item["squeeze"], item.get("squeeze_at"), item.get("squeeze_for"))
                if err:
                    self.message = f"Squeeze: {err}"
        self.cued = None
        self.state = "PLAYING"
        self.idle = "live"
        self.item_started = None
        self.item_ticks = 0
        self.message = f"On air: {item['name']}"
        self._changed()
        self._refresh_preload()

    def _refresh_preload(self):
        nxt = self._next_id() if self.on_air else None
        item = self._item(nxt)
        want = nxt if item and item["type"] == "clip" and nxt != self.on_air else None
        if self.preload and self.preload[0] != want:
            self.preload[1].close()
            self.preload = None
        if want and not self.preload:
            p = self._open(item)
            if p:
                self.preload = (want, p)

    def _finish(self, status):
        """Take the on-air item off and write it to the as-run log."""
        if self.on_air is None:
            return
        item = self._item(self.on_air) or {"name": "?", "type": "?"}
        if self.item_started is not None:
            st = status
            if self.player and self.player.error:
                st = "error: " + self.player.error
            self.asrun.write(item["type"], item["name"], self.item_started, dt.datetime.now(),
                             item.get("duration"), st, item.get("client", ""), item.get("file", ""),
                             item.get("category", ""))
        self.played.add(self.on_air)
        if self.cg is not None and item.get("cgs"):
            self.cg.stop_card()
        if self.cg is not None and item.get("squeeze"):
            self.cg.squeeze_item_end(self.on_air)
        if self.player:
            self.player.close()
        self.player = None
        self.on_air = None
        self.item_started = None

    def _item_done(self, depth=0):
        nxt = self._next_id()
        self._finish("played")
        if nxt and self.auto:
            self._start(nxt, depth)
        else:
            self._stop_to_live()
            self.cued = nxt
            self.message = "Manual: next item cued, live on air" if nxt else "End of playlist - live on air"
            self._changed()

    def _stop_to_live(self):
        if self.player:
            self.player.close()
        self.player = None
        if self.preload:
            self.preload[1].close()
            self.preload = None
        self.on_air = None
        self.state = "STOPPED"
        self.item_started = None
        self.item_ticks = 0

    # -- output loop -----------------------------------------------------
    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="playout output")
        self._thread.start()

    def shutdown(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._finish("stopped")
            self._stop_to_live()
        self.live.close()
        for inp in self.inputs.values():
            inp.close()
        if self.key_input is not None:
            self.key_input.close()
        for ex in self.extra.values():
            ex.close()
        if self.output is not None:
            self.output.close()
        self.finder.close()

    def _samples_for(self, k):
        f = self.fps
        return int(round((k + 1) * SAMPLE_RATE / f)) - int(round(k * SAMPLE_RATE / f))

    def _run(self):
        self._k = 0
        self._last_version = -1
        # the loop keeps its own clock: frame k goes out at start + k / fps. A frame that took too long is
        # made up by the next ones (no waiting), so over any second exactly fps frames and 48000 sound
        # samples go to NDI, the stream and the recording. (Waiting a full frame after a late one, as the
        # NDI sender's own clock does, lets the program fall behind real time: the stream then runs short
        # of sound and the sound breaks up.)
        period = 1.0 / float(self.fps)
        due = time.perf_counter()
        while self._running:
            self.heartbeat = time.monotonic()
            due += period
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                # never let one bad frame stop the output: log it, send black and carry on
                self.loop_errors += 1
                if self.loop_errors <= 20 or self.loop_errors % 500 == 0:
                    logging.error("output loop error:\n%s", traceback.format_exc())
                    print("output loop error:", traceback.format_exc())
                self.message = "Output error (see log) - still running"
                try:
                    if self.output is not None:
                        self.output.send(self.black, np.zeros((CHANNELS, self._samples_for(0)), np.float32))
                except Exception:
                    pass
            now = time.perf_counter()
            work = now - t0
            self.load = self.load * 0.98 + (work / period) * 0.02      # share of the frame time the work takes
            wait = due - now
            if wait > 0:
                time.sleep(wait)
            else:
                self.late_frames += 1
                if wait < -1.0:          # stalled for over a second: start the clock again, don't rush
                    due = time.perf_counter()

    def _tick(self):
        n = self._samples_for(self._k)
        self._k += 1
        self._fo_checked = False
        live_v, live_a = self.live.grab(n, self._main_wanted)   # always pulled, keeps the input in sync
        video = audio = None
        source = "LIVE"
        pend = self._pending
        if pend:
            pend[0] -= 1
            if pend[0] <= 0:
                self._pending = None
                self.next() if pend[1] == "next" else self.play(pend[2])
        with self._lock:
            if self.version != self._last_version:
                self._last_version = self.version
                self._sync_inputs()
                if self.on_air:
                    self._refresh_preload()
            item = self._item(self.on_air)
            self._preview_next = self._next_id()
            if self.on_air and item is None and self.player is None:
                self._stop_to_live()   # the on-air live card was deleted
                self._changed()
            if self.state == "PLAYING" and self.on_air:
                self._auto_stinger(item)
                if self.player is not None and self.player.finished():
                    self._item_done()   # starts the next item in this same frame
                    item = self._item(self.on_air)
                if self.state != "PLAYING" or not self.on_air:
                    pass
                elif self.player is not None:
                    p = self.player
                    if p.ready():
                        if self.item_started is None:
                            self.item_started = dt.datetime.now()
                        video, audio = p.next_frame(n)
                        self.item_ticks += 1
                        source = "CLIP:" + self.on_air
                        if video is None:
                            video = self.last_frame
                        self.last_clip_frame = video
                    elif self.last_source and self.last_source.startswith("CLIP"):
                        # next clip still opening: hold the last picture, never flash live
                        video, source = self.last_frame, self.last_source
                        audio = np.zeros((CHANNELS, n), np.float32)
                else:  # live card
                    if self.item_started is None:
                        self.item_started = dt.datetime.now()
                    self.item_ticks += 1
                    key = (item or {}).get("source")
                    inp = self.inputs.get(key) if key else None
                    if key and key == self.live.source_name:
                        inp = self.live                  # the same USB device as the main input
                    if inp is not None:
                        v, a = (live_v, live_a) if inp is self.live else inp.grab(n)
                        ok = v is not None and getattr(inp, "connected", True)
                        video = self._failover(ok, n) if not ok else v
                        if video is not None and not ok:
                            audio, source = self._fill_audio, "FILLER"
                        else:
                            video = v if v is not None else self.black
                            audio = a
                            source = "LIVE:" + key
                    dur = item.get("duration") if item else None
                    if dur and self.item_ticks / float(self.fps) >= dur:
                        self._item_done()
            elif self.state == "PAUSED" and self.player is not None:
                video = self.last_clip_frame if self.last_clip_frame is not None else self.black
                audio = np.zeros((CHANNELS, n), np.float32)
                source = "PAUSE"
        if video is None and self.state == "STOPPED" and self.idle == "bars":
            video, audio, source = self.bars, np.zeros((CHANNELS, n), np.float32), "BARS"
        if video is None:
            ok = live_v is not None and self.live.connected
            fv = self._failover(ok, n)
            if fv is not None:
                video, audio, source = fv, self._fill_audio, "FILLER"
            else:
                video = live_v if live_v is not None else self.black
                audio = live_a if live_a is not None else np.zeros((CHANNELS, n), np.float32)
        elif not self._fo_checked:
            self._failover(True, n)       # live not on air: nothing to cover
        self._main_wanted = source in ("LIVE", "FILLER")
        self.clean_frame = video          # program before CGs (squeeze preview)
        # the GPU engine (gpuengine.py) mixes the CGs itself: here only the list of what goes over the program
        nat = gpuengine.active()
        layers = None
        led_src = video
        if self.cg is not None:
            try:
                if nat is not None or "led" in self.extra:
                    video, layers = self.cg.render(video, source.startswith("LIVE"), n, collect=True)
                    led_src = video
                    if nat is None:
                        video = self.cg.composite(video, layers)
                else:
                    video = self.cg.render(video, source.startswith("LIVE"), n)
                sa = self.cg.stinger_audio
                if sa is not None and sa.shape == audio.shape:
                    audio = np.clip(audio + sa, -1.0, 1.0)
            except Exception as e:
                self.message = f"CG error: {e}"
        if self.key_input is not None and self.ndi_key.get("on"):
            kv, _ = self.key_input.grab(n)
            if kv is not None:
                if nat is not None:
                    layers = (layers or []) + [(kv.reshape(self.height, self.width, 4), 0, 0, 255)]
                else:
                    from cg import composite
                    out = np.array(video, copy=True).reshape(self.height, self.width, 4)
                    composite(out, kv.reshape(self.height, self.width, 4), 0, 0)
                    video = out.reshape(-1)
        if self._k % 5 == 0:
            try:
                self._tally(source, self._item(self._preview_next))
            except Exception:
                pass
        if source != self.last_source:
            audio = audio.copy()
            m = min(FADE, audio.shape[1])
            audio[:, :m] *= np.linspace(0.0, 1.0, m, dtype=np.float32)
            self.last_source = source
        try:
            audio = self.master.process(audio)
        except Exception as e:
            self.message = f"Audio limiter error: {e}"
        peak = np.abs(audio).max(axis=1) if audio.size else np.zeros(CHANNELS)
        self.meters = [round(float(20 * np.log10(max(float(x), 1e-5))), 1) for x in peak]
        if nat is not None:
            # mixed (and encoded for the stream / recording) on the GPU; the mixed picture comes back for NDI
            mixed = None
            try:
                mixed = nat.frame(video, audio, layers or [])
            except Exception as e:
                self.message = f"GPU engine error: {e}"
            if mixed is not None:
                video = mixed
                self.gpu_frames += 1
            else:
                self.gpu_misses += 1
                if layers and self.cg is not None:
                    video = self.cg.composite(np.asarray(video).reshape(self.height, self.width, 4), layers)
            video = np.asarray(video).reshape(-1)
        self.last_frame = video
        try:
            if self.output is not None:
                self.output.send(video, audio)   # NDIOutput's pacer sends it on an exact frame clock
        except Exception:
            pass
        ex = self.extra
        if "clean" in ex:
            ex["clean"].push(self.clean_frame, audio)
        if "led" in ex:
            hide = getattr(self.cg, "led_hide", ()) if self.cg is not None else ()
            ex["led"].push((led_src, [L for L in (layers or []) if isinstance(L, dict) or id(L[0]) not in hide]), audio)
        if "preview" in ex:
            try:
                ex["preview"].push(self._preview_frame(n, live_v), audio)
            except Exception:
                pass
        self._notify_air()
        if self.streamer is not None:
            self.streamer.push(video, audio)
        if self.audio_out is not None:
            self.audio_out.push(audio)
        if self.recorder is not None:
            self.recorder.push(video, audio)
        if self.sdi is not None:
            self.sdi.push(video, audio)

    def _failover(self, ok, n):
        """Called every frame live is wanted on air. Returns the filler picture while the input is lost."""
        self._fo_checked = True
        f = self.failover
        now = time.monotonic()
        if ok or not f.get("enabled"):
            self._lost_since = None
            if self.failover_active:
                self.failover_active = False
                self.message = "Live input is back - failover ended"
                if self._fail_started:
                    self.asrun.write("failover", "Live input lost", self._fail_started, dt.datetime.now(), None,
                                     "filler " + f.get("mode", "bars"))
                    self._fail_started = None
            if self._filler is not None and ok:
                self._filler.close()
                self._filler = None
            return None
        if self._lost_since is None:
            self._lost_since = now
        if now - self._lost_since < float(f.get("after") or 3):
            return None
        if not self.failover_active:
            self.failover_active = True
            self._fail_started = dt.datetime.now()
            self.message = "LIVE INPUT LOST - filler on air (failover)"
        self._fill_audio = np.zeros((CHANNELS, n), np.float32)
        mode = f.get("mode", "bars")
        if mode == "clip" and f.get("file"):
            p = self._filler
            if p is None or p.finished():
                if p is not None:
                    p.close()
                path = os.path.join(self.media_dir, f["file"])
                p = self._filler = ClipPlayer(path, self.width, self.height, self.fps) if os.path.isfile(path) else None
            if p is not None and p.ready():
                v, a = p.next_frame(n)
                if v is not None:
                    self._fill_audio = a
                    self._fill_last = v
                    return v
            return self._fill_last if (p is not None and self._fill_last is not None) else self.bars
        if mode == "black":
            return self.black
        return self.bars

    def _auto_stinger(self, item):
        """Automatic mode with auto stinger: start the stinger so its cut lands on the item's last frame."""
        if (not self.auto or not self.stinger.get("auto") or not self.stinger.get("file") or self._pending
                or self._stinger_item == self.on_air or item is None):
            return
        fps = float(self.fps)
        if self.player is not None:
            if not self.player.ready() or not self.player.duration:
                return
            left = self.player.duration - self.player.position
        elif item.get("duration"):
            left = item["duration"] - self.item_ticks / fps
        else:
            return
        if left * fps <= self._cut_frames() and self._next_id():
            self._stinger_item = self.on_air
            self._fire_stinger()

    # -- status ------------------------------------------------------------
    def snapshot(self):
        """Everything the control screen needs, with planned start/end per item."""
        with self._lock:
            now = dt.datetime.now()
            fps = float(self.fps)
            pos = self.item_ticks / fps
            on_idx = self._index(self.on_air)
            nxt = self._next_id() if self.on_air else None
            if on_idx is not None:
                start_idx = on_idx
            else:
                start_idx = self._index(self.cued)
                if start_idx is None:
                    start_idx = 0
            rows = []
            t = None
            for i, it in enumerate(self.playlist):
                row = dict(it)
                row["status"] = ""
                row["played"] = it["id"] in self.played
                row["start"] = row["end"] = None
                if i == on_idx:
                    row["status"] = "ON AIR"
                    row["start"] = (self.item_started or now).timestamp()
                    t = row["start"] + it["duration"] if it["duration"] else None
                    row["end"] = t
                elif i >= start_idx:
                    if it.get("fixed"):
                        try:
                            t = dt.datetime.combine(now.date(), dt.time.fromisoformat(it["fixed"])).timestamp()
                        except ValueError:
                            pass
                    elif i == start_idx:
                        t = now.timestamp()   # would start now if Play is pressed
                    row["start"] = t
                    t = t + it["duration"] if (t is not None and it["duration"]) else None
                    row["end"] = t
                if it["id"] == self.cued:
                    row["status"] = "CUED"
                elif it["id"] == nxt:
                    row["status"] = "NEXT"
                rows.append(row)
            item = self._item(self.on_air)
            dur = (item or {}).get("duration") or 0
            remaining_list = None
            if on_idx is not None:
                if dur:   # up to the first open-ended live card
                    remaining_list = max(0.0, dur - pos)
                    for r in self.playlist[on_idx + 1:]:
                        if not r["duration"]:
                            break
                        remaining_list += r["duration"]
            return {
                "state": self.state,
                "on_air": item,
                "position": pos if item else 0,
                "duration": dur,
                "remaining": max(0.0, dur - pos) if dur else None,
                "playlist_remaining": remaining_list,
                "auto": self.auto,
                "loop": self.loop,
                "cued": self.cued,
                "playlist": rows,
                "version": self.version,
                "meters": self.meters,
                "master": self.master.info(),
                "live_source": self.live.source_name,
                "idle": self.idle,
                "stinger": dict(self.stinger, active=bool(self.cg and self.cg.stinger_active)),
                "live_connected": self.live.connected,
                "failover": dict(self.failover, active=self.failover_active),
                "inputs": {k: bool(getattr(v, "connected", False)) for k, v in self.inputs.items()},
                "output": self.output_name,
                "ndi_out": self.output is not None,
                "ndi_pacing": self.output.stats() if self.output is not None else None,
                "ndi_extra": sorted(self.extra),
                "ndi_key": dict(self.ndi_key),
                "fps": fps,
                "width": self.width,
                "height": self.height,
                "format": f"{self.width}x{self.height} @ {fps:g}",
                "message": self.message,
                "now": now.timestamp(),
            }
