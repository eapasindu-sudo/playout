"""Stream health: one place that says whether the live is OK, for the phone (/health) and alerts.

Every 2 s it looks at each live destination (is it live, is the bitrate going out what it should be,
how often it had to reconnect), the stream encoder (frames it could not keep up with), the
program output (frames per second really sent), the live input, the recording, CPU / GPU and the
free disk space, and the viewer counts from YouTube / Facebook (the links in CG > Comments).
Each line gets a level: ok / warn / bad. A problem that lasts 30 s sends a Telegram alert.
"""
import collections
import os
import shutil
import threading
import time

LEVELS = {"ok": 0, "warn": 1, "bad": 2}


class Health:
    def __init__(self, engine, streamer, recorder, sysmon, alerts=None, viewers=None):
        self.engine, self.streamer, self.recorder, self.sysmon = engine, streamer, recorder, sysmon
        self.alerts = alerts
        self.atem = None                # AtemLink (ATEM & Cameras)
        self.viewers = viewers          # fn() -> {"YouTube": 123, "Facebook": 45}
        self.out_fps = None
        self._k = None
        self._dropped = collections.deque(maxlen=16)    # (time, encoder frames dropped so far)
        self._bad_since = None
        self._alerted = False
        self.report = {"level": "ok", "items": [], "time": time.time()}
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="health").start()

    def _run(self):
        last = time.monotonic()
        while True:
            time.sleep(2)
            now = time.monotonic()
            k = getattr(self.engine, "_k", None)
            if k is not None and self._k is not None:
                self.out_fps = round((k - self._k) / max(0.5, now - last), 1)
            self._k, last = k, now
            self._dropped.append((time.time(), self.streamer.dropped))
            try:
                rep = self.build()
                with self._lock:
                    self.report = rep
                self._alert(rep)
            except Exception as e:
                with self._lock:
                    self.report = {"level": "warn", "items": [{"name": "Health check", "level": "warn",
                                                               "text": str(e)[:200]}], "time": time.time()}

    def get(self):
        with self._lock:
            return dict(self.report)

    # ---------------------------------------------------------------
    def build(self):
        items = []
        add = lambda name, level, text, **kw: items.append({"name": name, "level": level, "text": text, **kw})
        st = self.streamer.info()
        target = self.streamer.video_kbps + self.streamer.audio_kbps
        live_dests = [d for d in st["destinations"] if d["enabled"]]
        for d in live_dests:
            s, now = d["status"], d.get("now_kbps", 0)
            extra = {"kbps": now, "target": target, "uptime": d.get("uptime"), "reconnects": d.get("reconnects", 0),
                     "layout": d.get("layout", "")}
            name = d["name"] + (" (9:16)" if d.get("layout") else "")
            if s == "live":
                if now and now < target * 0.6:
                    add(name, "bad", f"Upload too slow: {now} of {target} kbps", **extra)
                elif now and now < target * 0.85:
                    add(name, "warn", f"Upload a bit slow: {now} of {target} kbps", **extra)
                elif d.get("reconnects"):
                    add(name, "warn", f"Live, {now} kbps, reconnected {d['reconnects']}x", **extra)
                else:
                    add(name, "ok", f"Live, {now} kbps", **extra)
            elif s in ("connecting", "starting"):
                add(name, "warn", "Connecting...", **extra)
            else:
                add(name, "bad", (d.get("error") or s)[:160], **extra)
        if not live_dests:
            add("Live stream", "ok", "Not streaming")
        else:
            old = [x for x in self._dropped if x[0] >= time.time() - 30]
            new_drops = (old[-1][1] - old[0][1]) if len(old) > 1 else 0
            if new_drops > 25:
                enc = st.get("active_encoder") or "?"
                add("Stream encoder", "bad", f"Can't keep up: {new_drops} frames dropped in 30 s, encoder {enc}" +
                    (" (pick the NVIDIA / GPU encoder in Stream settings, or a lower bitrate)" if "CPU" in enc else " (the CPU is too busy: see CPU use)"))
            elif new_drops > 0:
                add("Stream encoder", "warn", f"{new_drops} frames dropped in 30 s")
            else:
                add("Stream encoder", "ok", f"{st['format']}, {st.get('active_encoder') or ''} encoder, no dropped frames")
        fps = float(self.engine.fps)
        if self.out_fps is not None:
            lvl = "ok" if self.out_fps >= fps * 0.95 else "warn" if self.out_fps >= fps * 0.8 else "bad"
            load = getattr(self.engine, "load", 0.0)
            text = f"{self.out_fps:g} of {fps:g} fps"
            if load >= 0.9:
                lvl = "bad" if load >= 1.0 else "warn" if lvl == "ok" else lvl
                text += (f", PC busy {load * 100:.0f}% of each frame: choose a lower output format "
                         f"(Settings > Output format, e.g. 1080p 25) so sound and picture stay smooth")
            add("Program output", lvl, text)
        e = self.engine
        if getattr(e, "failover_active", False):
            add("Live input", "bad", "LOST - filler on air")
        elif e.live.connected and getattr(e.live, "is_device", False):
            got, text, lvl = e.live.in_fps, "USB: " + e.live.source_name[4:].split("|")[0] + " " + (e.live.mode or ""), "ok"
            if got and not any(abs(got - fps * k) < 1.5 for k in (1, 2)):
                lvl = "warn"
                text += f", {got:g} fps from the device on a {fps:g} fps output: set the device / ATEM to {fps:g} fps"
            add("Live input", lvl, text)
        elif e.live.connected:
            add("Live input", "ok", f"{e.live.source_name or 'connected'}")
        else:
            try:
                on_live = e.state != "PLAYING" or (e._item(e.on_air) or {}).get("type") == "live"
            except Exception:
                on_live = True
            add("Live input", "warn" if on_live else "ok",
                "not connected" + (f" ({e.live.source_name})" if e.live.source_name else ""))
        for key, inp in list(getattr(e, "inputs", {}).items()):
            if not key.startswith("dev:"):
                continue
            name = "USB: " + key[4:].split("|")[0]
            if not inp.connected:
                add(name, "warn", "no picture" + (f" ({inp.error[:120]})" if inp.error else ""))
                continue
            text, lvl = inp.mode or "connected", "ok"
            got = inp.in_fps
            if got and not any(abs(got - fps * k) < 1.5 for k in (1, 2)):
                lvl = "warn"
                text += f", {got:g} fps from the device on a {fps:g} fps output: set the device / ATEM to {fps:g} or {fps * 2:g} fps"
            if "|" in key and "no sound" in text:
                lvl = "warn"
            if inp.error:
                text += f" - {inp.error[:120]}"
            add(name, lvl, text)
        for i in e.ndi_inputs() if hasattr(e, "ndi_inputs") else []:
            name = f"NDI in: {i['source']}"
            if not i["connected"]:
                add(name, "warn", f"{i['use']}: not connected (is the source on and on this network?)")
                continue
            f, got = i["format"], i["in_fps"]
            text, lvl = f"{i['use']}: " + (f"{f['width']}x{f['height']}" if f.get("width") else "connected"), "ok"
            if got is not None:
                text += f", {got:g} fps received"
                if got < 1:
                    lvl, text = "bad", text + " - no pictures coming in"
                elif f.get("fps") and abs(got - f["fps"]) > max(1.5, f["fps"] * 0.1):
                    lvl, text = "warn", text + f" of {f['fps']:g} (network too slow?)"
            if f.get("fps") and not any(abs(f["fps"] - fps * k) < 0.1 for k in (0.5, 1, 2)):
                lvl = "warn" if lvl == "ok" else lvl
                text += f"; the source runs at {f['fps']:g} fps on a {fps:g} fps output: set it to {fps:g} fps"
            if i.get("dropped"):
                text += f", {i['dropped']} dropped"
            add(name, lvl, text)
        if getattr(e, "output", True) is None:
            add("NDI output", "warn", "Program NDI output is OFF (NDI panel)")
        at = getattr(self, "atem", None)
        if at is not None and (at.auto.get("enabled") or at.linked):
            a = at.atem
            if not at.running:
                add("ATEM", "bad" if at.auto.get("enabled") else "warn", "ATEM Switcher not running: Playout cannot switch the ATEM")
            elif not at.linked:
                add("ATEM", "warn", "connecting to the ATEM Switcher...")
            elif not a.get("connected"):
                add("ATEM", "bad" if at.auto.get("enabled") else "warn", f"ATEM not connected{' (' + a['ip'] + ')' if a.get('ip') else ''}")
            else:
                add("ATEM", "warn" if at.error else "ok", f"{a.get('model') or 'ATEM'} at {a.get('ip')}, on air: {at.label(a.get('program'))}"
                    + (f" - {at.error}" if at.error else ""))
        r = self.recorder.info()
        if r["recording"]:
            lvl = "warn" if r.get("dropped") else "ok"
            add("Recording", lvl, f"REC {r['elapsed'] // 3600}:{r['elapsed'] // 60 % 60:02d}:{r['elapsed'] % 60:02d}, "
                                 f"{r['size_mb'] / 1024:.1f} GB" + (f", {r['dropped']} frames dropped" if r.get("dropped") else ""))
        elif r.get("error"):
            add("Recording", "bad", r["error"][:160])
        s = self.sysmon.stats
        if s.get("cpu") is not None:
            cpu, gpu = s["cpu"], s.get("gpu")
            lvl = "bad" if cpu > 92 or (gpu or 0) > 95 else "warn" if cpu > 80 or (gpu or 0) > 85 else "ok"
            import media as _media
            dec = _media.DECODE
            if dec.get("gpu") is not None:
                add("Video decoding", "ok" if dec["gpu"] else "warn",
                    f"Clips play on the GPU ({dec.get('device', 'graphics card')}, {dec.get('codec')})" if dec["gpu"]
                    else f"Clips play on the CPU ({dec.get('codec')}: this graphics card cannot decode it, or GPU decoding is off)")
            import gpudriver as _d
            import gpu as _gpu
            if any(c.get("vendor") == "NVIDIA" and c.get("ok") for c in _d.STATE.get("cards") or []) and "h264_nvenc" in _gpu._tested \
                    and not _gpu._tested["h264_nvenc"]:
                add("NVIDIA encoder", "bad", "Not working, so streaming / recording use the CPU: " +
                    (_gpu.ERRORS.get("h264_nvenc") or "did not open") + ". Update the NVIDIA driver (Settings > Output > Maker's driver page)")
            if _d.STATE.get("missing"):
                add("Graphics driver", "warn", "No graphics card driver: " + ", ".join(c["name"] for c in _d.STATE["cards"]) +
                    ". Settings > Output > Install graphics driver")
            import gpucg as _g
            if _g.STATE.get("gpu") is not None:
                add("CG blending", "ok" if _g.STATE["gpu"] else "warn",
                    f"CGs are drawn on the GPU ({_g.STATE['renderer']})" if _g.STATE["gpu"]
                    else f"CGs are drawn on the CPU ({_g.STATE['error'] or 'turned off'})")
            if _g.CONVERT.get("used") is not None:
                add("Picture scaling", "ok" if _g.CONVERT["used"] else "warn",
                    "Clip and USB pictures are scaled and colour-converted on the GPU" if _g.CONVERT["used"]
                    else f"Clip and USB pictures are scaled on the CPU ({_g.CONVERT['error']})")
            import conform as _c
            busy = {n: v for n, v in dict(_c.STATUS).items() if v != "ready"}
            if busy:
                add("Converter", "warn" if any(v.startswith("failed") for v in busy.values()) else "ok",
                    "Converting to the program's frame rate and size: " +
                    ", ".join(f"{n} {v}" for n, v in list(busy.items())[:4]))
            if s.get("app_cpu") is not None and cpu > 80:
                inside = ", ".join(f"{k} {v}%" for k, v in s.get("app_parts") or [])
                parts = [f"Playout {s['app_cpu'] + (s.get('app_children_cpu') or 0)}%" + (f" ({inside})" if inside else "")] + [f"{n} {c}%" for n, c in s.get("top_other") or [] if c >= 3]
                add("CPU use", "warn", "Where the CPU goes: " + ", ".join(parts) +
                    (" - close the other programs or move them to another PC" if any(c >= 10 for _, c in s.get("top_other") or []) else ""))
            add("Computer", lvl, f"CPU {cpu:.0f}%" + (f", GPU {gpu:.0f}%" if gpu is not None else "") +
                (f", RAM {s['ram']:.0f}%" if s.get("ram") is not None else "") +
                (f", upload {s['net_up_mbps']:.1f} Mbps" if s.get("net_up_mbps") is not None else ""))
        try:
            folder = self.recorder.folder if os.path.isdir(self.recorder.folder) else os.getcwd()
            free = shutil.disk_usage(folder).free / 1e9
            add("Disk", "bad" if free < 5 else "warn" if free < 20 else "ok", f"{free:.0f} GB free")
        except OSError:
            pass
        if self.viewers:
            try:
                v = {k: n for k, n in (self.viewers() or {}).items() if n is not None}
            except Exception:
                v = {}
            if v:
                add("Viewers", "ok", ", ".join(f"{k} {n:,}" for k, n in v.items()), viewers=v)
        level = max((i["level"] for i in items), key=LEVELS.get, default="ok")
        return {"level": level, "items": items, "time": time.time()}

    # these have their own alerts already
    OWN_ALERTS = ("Live input", "Recording", "Disk")

    def _alert(self, rep):
        bad = [f"{i['name']}: {i['text']}" for i in rep["items"] if i["level"] == "bad" and i["name"] not in self.OWN_ALERTS]
        if not bad:
            if self._alerted and self.alerts:
                self.alerts.send("health", "Stream health is OK again", every=60)
            self._bad_since, self._alerted = None, False
            return
        if self._bad_since is None:
            self._bad_since = time.time()
        if not self._alerted and time.time() - self._bad_since > 30 and self.alerts:
            self.alerts.send("health", "STREAM HEALTH PROBLEM\n" + "\n".join(bad[:5]), every=300)
            self._alerted = True
