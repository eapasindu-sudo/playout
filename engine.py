"""Playout engine: runs a playlist of commercial clips and LIVE cards on one output clock.

Playlist item (dict):
    id        unique id
    type      "clip" | "live"
    name      display name
    file      clip file name in the media folder (clip only)
    duration  seconds; for clips filled from the file, for live cards optional
              (None = stay live until the operator presses Next)
    fixed     "HH:MM:SS" or "" - item jumps on air at that time of day
"""
import csv
import datetime as dt
import os
import threading
import time
import uuid
from fractions import Fraction

import numpy as np

from media import ClipPlayer, probe
from capture import CaptureInput, make_bars
from ndi_io import CHANNELS, SAMPLE_RATE, LiveInput, NDIOutput, SourceFinder

FADE = 480  # 10 ms audio fade at every cut, avoids clicks


def parse_fps(value):
    s = str(value).strip()
    table = {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001), "59.94": Fraction(60000, 1001)}
    return table.get(s) or Fraction(s)


def new_id():
    return uuid.uuid4().hex[:8]


class AsRunLog:
    FIELDS = ["date", "start", "end", "type", "item", "planned_sec", "played_sec", "status"]

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, day):
        return os.path.join(self.folder, f"asrun-{day:%Y-%m-%d}.csv")

    def write(self, kind, name, started, ended, planned, status):
        path = self.path_for(started.date())
        with self._lock:
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(self.FIELDS)
                w.writerow([f"{started:%Y-%m-%d}", f"{started:%H:%M:%S}", f"{ended:%H:%M:%S}", kind, name,
                            f"{planned:.1f}" if planned else "", f"{(ended - started).total_seconds():.1f}", status])

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
        self.live = LiveInput(self.finder, self.width, self.height)
        self.output = NDIOutput(cfg.get("output_name", "PLAYOUT PGM"), self.width, self.height, self.fps)
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
        self.last_clip_frame = None
        self.last_source = None
        self.meters = [-90.0, -90.0]
        self.played = set()          # ids already aired in this run (greyed on screen)
        self.message = ""
        self.on_change = None        # callback to persist the playlist
        self.streamer = None         # Streamer fed with every program frame
        self.audio_out = None        # program sound on this PC's speakers
        self.recorder = None         # program recording to a file
        self.cg = None               # CGManager keyed over the program
        self.inputs = {}             # extra live sources used by live cards: key -> input
        self._main_wanted = True
        self.idle = "live"           # what goes on air when stopped: "live" (TAKE LIVE) or "bars" (Stop)
        self.bars = make_bars(self.width, self.height)
        self.stinger = {"file": "", "cut": 0.0, "auto": False}   # cut 0 = half of the stinger's length
        self._pending = None         # [frames left, action, item id]: switch at the stinger's cut point
        self._stinger_item = None    # on-air item whose end already got its stinger
        self._running = False
        self._thread = None

    # -- playlist editing ----------------------------------------------
    def set_playlist(self, items):
        with self._lock:
            clean = []
            for it in items:
                it = dict(it)
                it["id"] = it.get("id") or new_id()
                it["fixed"] = it.get("fixed") or ""
                if it.get("type") == "clip" and it.get("file"):
                    it["duration"] = probe(os.path.join(self.media_dir, it["file"]))
                    it["name"] = it.get("name") or os.path.splitext(it["file"])[0]
                elif it.get("type") == "live":
                    d = it.get("duration")
                    it["duration"] = float(d) if d not in (None, "", 0, "0") else None
                    it["name"] = it.get("name") or "LIVE"
                else:
                    continue
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

    def _sync_inputs(self):
        """Open the extra inputs live cards use (other NDI sources, USB capture, bars); close the rest."""
        wanted = {it.get("source") for it in self.playlist if it.get("type") == "live" and it.get("source")}
        for key in list(self.inputs):
            if key not in wanted:
                self.inputs.pop(key).close()
        for key in wanted - set(self.inputs):
            try:
                if key.startswith("ndi:"):
                    inp = LiveInput(self.finder, self.width, self.height)
                    inp.set_source(key[4:])
                else:
                    inp = CaptureInput(key, self.width, self.height)
                self.inputs[key] = inp
            except Exception as e:
                self.message = f"Cannot open {key}: {e}"

    # -- internals (lock held) ------------------------------------------
    def _open(self, item):
        path = os.path.join(self.media_dir, item.get("file") or "")
        if not os.path.isfile(path):
            return None
        return ClipPlayer(path, self.width, self.height, self.fps)

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
                self.asrun.write("clip", item["name"], now, now, item.get("duration"), "missing file")
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
            if item["type"] == "live":
                self.cg.start_card(item_id, item.get("cgs"))
            else:
                self.cg.stop_card()
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
                             item.get("duration"), st)
        self.played.add(self.on_air)
        if self.cg is not None and item.get("type") == "live":
            self.cg.stop_card()
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
        self.output.close()
        self.finder.close()

    def _samples_for(self, k):
        f = self.fps
        return int(round((k + 1) * SAMPLE_RATE / f)) - int(round(k * SAMPLE_RATE / f))

    def _run(self):
        k = 0
        last_version = -1
        while self._running:
            n = self._samples_for(k)
            k += 1
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
                if self.version != last_version:
                    last_version = self.version
                    self._sync_inputs()
                    if self.on_air:
                        self._refresh_preload()
                item = self._item(self.on_air)
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
                        if inp is not None:
                            v, a = inp.grab(n)
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
                video = live_v if live_v is not None else self.black
                audio = live_a if live_a is not None else np.zeros((CHANNELS, n), np.float32)
            self._main_wanted = source == "LIVE"
            if self.cg is not None:
                try:
                    video = self.cg.render(video, source.startswith("LIVE"), n)
                    sa = self.cg.stinger_audio
                    if sa is not None and sa.shape == audio.shape:
                        audio = np.clip(audio + sa, -1.0, 1.0)
                except Exception as e:
                    self.message = f"CG error: {e}"
            if source != self.last_source:
                audio = audio.copy()
                m = min(FADE, audio.shape[1])
                audio[:, :m] *= np.linspace(0.0, 1.0, m, dtype=np.float32)
                self.last_source = source
            peak = np.abs(audio).max(axis=1) if audio.size else np.zeros(CHANNELS)
            self.meters = [round(float(20 * np.log10(max(float(x), 1e-5))), 1) for x in peak]
            self.last_frame = video
            try:
                self.output.send(video, audio)   # blocks to frame rate (NDI clock)
            except Exception:
                time.sleep(float(1 / self.fps))
            if self.streamer is not None:
                self.streamer.push(video, audio)
            if self.audio_out is not None:
                self.audio_out.push(audio)
            if self.recorder is not None:
                self.recorder.push(video, audio)

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
                "live_source": self.live.source_name,
                "idle": self.idle,
                "stinger": dict(self.stinger, active=bool(self.cg and self.cg.stinger_active)),
                "live_connected": self.live.connected,
                "inputs": {k: bool(getattr(v, "connected", False)) for k, v in self.inputs.items()},
                "output": self.output.sender.ndi_name,
                "fps": fps,
                "width": self.width,
                "height": self.height,
                "format": f"{self.width}x{self.height} @ {fps:g}",
                "message": self.message,
                "now": now.timestamp(),
            }
