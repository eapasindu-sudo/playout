"""Playout server: playlist of commercials + LIVE cards (vMix NDI) -> NDI program out.

Run:  python app.py      then open http://localhost:8080
"""
import datetime as dt
import io
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory, session
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from audio_out import AudioOut
from recorder import Recorder
from capture import list_capture_devices
from cg import CG_EXT, CGManager, cg_preview, list_cg
from cgedit import PRESET_EXT, list_presets, make_blueprint, read_cgp
import media as media_mod
from engine import Engine
from system import SystemMonitor
from media import MEDIA_EXT, list_media, thumbnail
from scheduler import DayPlanner, Scheduler
from store import Store
from streamer import Streamer
from loudness import Loudness
from updater import Updater
from version import VERSION
import importer
import report

import paths

ROOT = paths.user_home()      # config, media, playlists, logs
BUNDLE = paths.bundle_dir()   # the control screen page

DEFAULT_CONFIG = {
    "output_name": "PLAYOUT PGM", "live_source": "", "width": 1920, "height": 1080, "fps": "25",
    "web_port": 8080, "media_dir": "media", "data_dir": "data", "log_dir": "logs", "cg_dir": "cg",
    "stream": {"width": 1920, "height": 1080, "video_kbps": 4500, "audio_kbps": 128,
               "encoder": "libx264", "preset": "veryfast", "ffmpeg_path": ""},
}


def load_config():
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("cg_dir", "cg")
    for key in ("media_dir", "data_dir", "log_dir", "cg_dir"):
        cfg[key] = os.path.join(ROOT, cfg.get(key, key.split("_")[0]))
        os.makedirs(cfg[key], exist_ok=True)
    return cfg


cfg = load_config()
media_mod.HWACCEL = cfg.get("hwaccel", "auto")   # GPU video decoding: auto / off / d3d11va / cuda / qsv
store = Store(cfg["data_dir"])
engine = Engine(cfg, cfg["media_dir"], cfg["log_dir"])
scheduler = Scheduler(engine)
streamer = Streamer(engine, cfg)
cgm = CGManager(cfg["cg_dir"], engine.width, engine.height, engine.fps)
engine.cg = cgm
sysmon = SystemMonitor()
audio_out = AudioOut()
engine.audio_out = audio_out
recorder = Recorder(engine, os.path.join(ROOT, cfg.get("record_dir", "recordings")))
engine.recorder = recorder
loudness = Loudness(cfg["data_dir"], streamer.ffmpeg)
engine.loudness = loudness
planner = DayPlanner(engine, store, recorder)
updater = Updater()
THUMBS = os.path.join(cfg["data_dir"], "thumbs")
RUN_FLAG = os.path.join(cfg["data_dir"], "running.flag")   # left behind = last run did not close normally
app = Flask(__name__, static_folder=None)


def _secret_key():
    path = os.path.join(cfg["data_dir"], "secret.key")
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


app.secret_key = _secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = dt.timedelta(days=30)
app.register_blueprint(make_blueprint(lambda: cfg["cg_dir"], lambda: (engine.width, engine.height),
                                      lambda: engine.last_frame))
fullscreen_on = False
fullscreen_hook = None   # set by the desktop app: fn(on, screen) opens / closes the full screen output window


# ---- security: remote password, lock ---------------------------------------
def is_local():
    return request.remote_addr in ("127.0.0.1", "::1", "localhost")


def sec():
    """Saved security settings."""
    s = store.data.get("security") or {}
    s.setdefault("remote_enabled", True)
    s.setdefault("remote_hash", "")
    s.setdefault("lock_hash", "")
    s.setdefault("epoch", 1)
    s.setdefault("lock_on_start", False)
    return s


OPEN_PATHS = ("/login", "/api/login")


@app.before_request
def remote_guard():
    """Other PCs / phones need the remote password (when one is set). This PC never does."""
    if is_local():
        return None
    s = sec()
    if not s["remote_enabled"]:
        return Response("Playout remote control is turned off on the playout PC (Settings > Remote).", 403)
    if not s["remote_hash"] or request.path in OPEN_PATHS:
        return None
    if session.get("ok") == s["epoch"]:
        return None
    key = request.args.get("key")
    if key and request.path.startswith("/trigger/") and check_password_hash(s["remote_hash"], key):
        return None
    if request.path.startswith("/api/") or request.path.startswith("/trigger/"):
        return jsonify(error="login"), 401
    return redirect("/login?next=" + request.path)


@app.get("/login")
def login_page():
    return send_from_directory(os.path.join(BUNDLE, "web"), "login.html")


@app.post("/api/login")
def login():
    s = sec()
    pw = (request.get_json(silent=True) or request.form or {}).get("password", "")
    if not s["remote_hash"] or check_password_hash(s["remote_hash"], pw):
        session.permanent = True
        session["ok"] = s["epoch"]
        return jsonify(ok=True)
    time.sleep(1)       # slow down guessing
    return jsonify(ok=False, error="Wrong password"), 403


@app.route("/api/lock", methods=["GET", "POST"])
def lock_api():
    """Lock the control screen; unlocking needs the lock password when one is set."""
    s = sec()
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        if b.get("lock"):
            store.set(locked=True)
        else:
            if s["lock_hash"] and not check_password_hash(s["lock_hash"], b.get("password", "")):
                time.sleep(0.5)
                return jsonify(ok=False, locked=True, error="Wrong password"), 403
            store.set(locked=False)
    return jsonify(ok=True, locked=bool(store.data.get("locked")), has_password=bool(s["lock_hash"]))


def check_admin(body):
    """Settings changes need the lock password (when one is set)."""
    h = sec()["lock_hash"]
    return not h or check_password_hash(h, (body or {}).get("admin_password", ""))


# ---- settings ----------------------------------------------------------------
@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        if not check_admin(b):
            time.sleep(0.5)
            return jsonify(error="Wrong password"), 403
        if "failover" in b:
            f = b["failover"] or {}
            store.set(failover=engine.set_failover(f.get("enabled"), f.get("after"), f.get("mode"), f.get("file")))
        if "audio" in b:
            a = b["audio"] or {}
            store.set(audio_norm=loudness.configure(a.get("normalize"), a.get("target")))
        if "station_name" in b:
            store.set(station_name=str(b["station_name"] or "")[:80])
        if "security" in b:
            s = sec()
            n = b["security"] or {}
            if "remote_enabled" in n:
                s["remote_enabled"] = bool(n["remote_enabled"])
            if "lock_on_start" in n:
                s["lock_on_start"] = bool(n["lock_on_start"])
            for k, field in (("remote_password", "remote_hash"), ("lock_password", "lock_hash")):
                if n.get(k) is not None and n.get(k) != "********":
                    s[field] = generate_password_hash(n[k]) if n[k] else ""
                    if k == "remote_password":
                        s["epoch"] = int(s["epoch"]) + 1      # log out every phone / tablet
            store.set(security=s)
        if "update" in b:
            u = b["update"] or {}
            updater.configure(u.get("repo"), u.get("token"), u.get("auto_check"))
            store.set(update=updater.settings())
    s = sec()
    return jsonify(
        failover=engine.failover, audio=loudness.settings(), station_name=store.data.get("station_name", ""),
        security={"remote_enabled": s["remote_enabled"], "remote_password_set": bool(s["remote_hash"]),
                  "lock_password_set": bool(s["lock_hash"]), "lock_on_start": s["lock_on_start"]},
        update={"repo": updater.repo, "token_set": bool(updater.token), "auto_check": updater.auto_check},
        version=VERSION)


@app.post("/api/admin_check")
def admin_check():
    if check_admin(request.get_json(silent=True)):
        return jsonify(ok=True)
    time.sleep(0.5)
    return jsonify(ok=False, error="Wrong password"), 403


# ---- remote control (phone / tablet) -------------------------------------------
def lan_addresses():
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


@app.get("/api/remote_info")
def remote_info():
    port = int(cfg.get("web_port", 8080))
    s = sec()
    return jsonify(urls=[f"http://{ip}:{port}/remote" for ip in lan_addresses()], enabled=s["remote_enabled"],
                   password_set=bool(s["remote_hash"]), host=socket.gethostname())


@app.get("/api/qr.png")
def qr_png():
    import qrcode
    img = qrcode.make(request.args.get("t", "")[:300], box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), mimetype="image/png")


@app.get("/remote")
def remote_page():
    return send_from_directory(os.path.join(BUNDLE, "web"), "remote.html")


@app.get("/")
def index():
    return send_from_directory(os.path.join(BUNDLE, "web"), "index.html")


@app.get("/cgedit")
def cgedit_page():
    return send_from_directory(os.path.join(BUNDLE, "web"), "cgedit.html")


@app.get("/output")
def output_page():
    return send_from_directory(os.path.join(BUNDLE, "web"), "output.html")


# ---- state & transport -------------------------------------------------
@app.get("/api/state")
def state():
    snap = engine.snapshot()
    snap["locked"] = bool(store.data.get("locked"))
    return jsonify(snap)


@app.post("/api/transport/<action>")
def transport(action):
    body = request.get_json(silent=True) or {}
    if action == "play":
        engine.take_item(body["id"]) if body.get("id") else engine.play()
    elif action == "pause":
        engine.pause()
    elif action == "stop":
        engine.stop()
    elif action == "next":
        engine.take_next()
    elif action == "live":
        engine.take_live()
    elif action == "stinger":
        engine.stinger_take(body.get("id"))
    elif action == "cue":
        engine.cue(body.get("id"))
    else:
        abort(404)
    return jsonify(ok=True)


@app.post("/api/modes")
def modes():
    body = request.get_json(silent=True) or {}
    engine.set_modes(body.get("auto"), body.get("loop"))
    store.set(auto=engine.auto, loop=engine.loop)
    return jsonify(ok=True)


@app.post("/api/playlist")
def set_playlist():
    engine.set_playlist(request.get_json(silent=True) or [])
    return jsonify(engine.snapshot()["playlist"])


# ---- named playlists ---------------------------------------------------
@app.get("/api/playlists")
def playlists():
    return jsonify(store.list_names())


@app.post("/api/playlists/save")
def playlist_save():
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        abort(400)
    store.save_list(name, [{k: v for k, v in it.items() if k != "id"} for it in engine.playlist])
    return jsonify(store.list_names())


@app.post("/api/playlists/load")
def playlist_load():
    body = request.get_json(silent=True) or {}
    items = store.load_list(body.get("name", ""))
    if body.get("append"):
        items = engine.playlist + items
    engine.set_playlist(items)
    return jsonify(ok=True)


@app.post("/api/playlists/delete")
def playlist_delete():
    store.delete_list((request.get_json(silent=True) or {}).get("name", ""))
    return jsonify(store.list_names())


# ---- preview -----------------------------------------------------------
def _jpeg(width=640):
    img = Image.frombuffer("RGBA", (engine.width, engine.height), bytes(engine.last_frame), "raw", "BGRA", 0, 1)
    img = img.convert("RGB").resize((width, int(width * engine.height / engine.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


@app.get("/api/preview.jpg")
def preview():
    resp = Response(_jpeg(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/preview.mjpg")
def preview_stream():
    def gen():
        while True:
            frame = _jpeg()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.1)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/output.mjpg")
def output_stream():
    """Full-size program pictures at the output frame rate, for the full screen output window."""
    w = max(320, min(engine.width, int(request.args.get("w") or engine.width)))
    q = max(50, min(95, int(request.args.get("q") or 85)))

    def gen():
        last = None
        period = 1.0 / float(engine.fps)
        while True:
            frame = engine.last_frame
            if frame is last:
                time.sleep(period / 4)
                continue
            last = frame
            img = Image.frombuffer("RGB", (engine.width, engine.height), frame, "raw", "BGRX", 0, 1)
            if w != engine.width:
                img = img.resize((w, int(w * engine.height / engine.width)), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.getvalue() + b"\r\n"
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/screens")
def screens():
    try:
        import webview
        return jsonify([{"index": i, "width": sc.width, "height": sc.height, "x": sc.x, "y": sc.y}
                        for i, sc in enumerate(webview.screens)])
    except Exception:
        return jsonify([])


@app.post("/api/fullscreen")
def fullscreen():
    """Program output full screen on a display of this PC (desktop app only)."""
    body = request.get_json(silent=True) or {}
    global fullscreen_on
    if fullscreen_hook is None:
        return jsonify(ok=False, error="browser")
    try:
        fullscreen_hook(bool(body.get("on")), body.get("screen"))
        fullscreen_on = bool(body.get("on"))
    except Exception as e:
        return jsonify(ok=False, error=str(e))
    return jsonify(ok=True, on=fullscreen_on)


# ---- recording ---------------------------------------------------------
@app.route("/api/record", methods=["GET", "POST"])
def record_info():
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        recorder.configure(b.get("resolution"), b.get("mbps"), b.get("encoder"))
        store.set(record_settings=recorder.settings())
    return jsonify(recorder.info())


@app.post("/api/record/<action>")
def record_action(action):
    if action == "start":
        return jsonify(recorder.start())
    if action == "stop":
        return jsonify(recorder.stop())
    if action == "folder":
        if request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403)
        os.makedirs(recorder.folder, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(recorder.folder)
        return jsonify(path=recorder.folder)
    abort(404)


# ---- stinger -----------------------------------------------------------
@app.route("/api/stinger", methods=["GET", "POST"])
def stinger():
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        st = engine.set_stinger(b.get("file"), b.get("cut"), b.get("auto"))
        store.set(stinger=st)
    return jsonify(engine.stinger)


# ---- live input --------------------------------------------------------
@app.get("/api/sources")
def sources():
    own = f"({cfg.get('output_name', 'PLAYOUT PGM')})"
    return jsonify([n for n in engine.finder.names() if not n.endswith(own)])   # never our own output


@app.post("/api/live_source")
def live_source():
    name = (request.get_json(silent=True) or {}).get("name", "")
    store.set(live_source=name)
    engine.set_live_source(name)
    return jsonify(ok=True)


# ---- media library -----------------------------------------------------
@app.get("/api/media")
def media():
    out = list_media(cfg["media_dir"])
    for m in out:
        path = os.path.join(cfg["media_dir"], m["name"])
        info = loudness.info(path)
        if info is None:
            loudness.request(path)
        m["lufs"] = (info or {}).get("lufs")
    return jsonify(out)


@app.get("/api/frame/<path:name>")
def clip_frame(name):
    """A picture of a clip at t seconds (trim in / out preview)."""
    import av
    path = os.path.join(cfg["media_dir"], secure_filename(name))
    if not os.path.isfile(path):
        abort(404)
    t = max(0.0, float(request.args.get("t") or 0))
    try:
        with av.open(path) as c:
            vs = c.streams.video[0]
            base = float(vs.start_time * vs.time_base) if vs.start_time is not None else 0.0
            if t > 0:
                c.seek(int((base + t) / vs.time_base), stream=vs, backward=True)
            img = None
            for fr in c.decode(vs):
                img = fr
                if fr.time is None or fr.time >= base + t - 0.02:
                    break
            if img is None:
                abort(404)
            im = img.to_image().convert("RGB")
    except Exception:
        abort(404)
    im.thumbnail((int(request.args.get("w") or 320), 400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    r = Response(buf.getvalue(), mimetype="image/jpeg")
    r.headers["Cache-Control"] = "max-age=600"
    return r


@app.post("/api/media")
def upload():
    saved = []
    for f in request.files.getlist("files"):
        name = secure_filename(f.filename or "")
        if name and os.path.splitext(name)[1].lower() in MEDIA_EXT:
            f.save(os.path.join(cfg["media_dir"], name))
            saved.append(name)
    return jsonify(saved=saved)


@app.get("/api/thumb/<path:name>")
def thumb(name):
    path = os.path.join(cfg["media_dir"], secure_filename(name))
    if not os.path.isfile(path):
        abort(404)
    try:
        return send_file(thumbnail(path, THUMBS), mimetype="image/jpeg", max_age=3600)
    except Exception:
        abort(404)


# ---- as-run log --------------------------------------------------------
def _day():
    d = request.args.get("date")
    return dt.date.fromisoformat(d) if d else dt.date.today()


@app.get("/api/asrun")
def asrun():
    return jsonify(engine.asrun.read(_day()))


@app.get("/api/asrun.csv")
def asrun_csv():
    path = engine.asrun.path_for(_day())
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True)


# ---- ad report -------------------------------------------------------------
def _range():
    a = request.args
    d1 = dt.date.fromisoformat(a.get("from")) if a.get("from") else dt.date.today()
    d2 = dt.date.fromisoformat(a.get("to")) if a.get("to") else d1
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > 400:
        abort(400)
    return d1, d2


@app.get("/api/report")
def report_json():
    d1, d2 = _range()
    rows = report.collect(engine.asrun, d1, d2, request.args.get("client"), request.args.get("q"))
    return jsonify(rows=rows, summary=report.summarize(rows), clients=report.clients(engine.asrun, d1, d2))


@app.get("/api/report.<kind>")
def report_file(kind):
    d1, d2 = _range()
    client = request.args.get("client", "")
    rows = report.collect(engine.asrun, d1, d2, client, request.args.get("q"))
    station = store.data.get("station_name", "")
    if kind == "xlsx":
        data, mime = report.to_xlsx(rows, station, d1, d2, client), \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif kind == "pdf":
        data, mime = report.to_pdf(rows, station, d1, d2, client), "application/pdf"
    elif kind == "csv":
        data, mime = report.to_csv(rows), "text/csv"
    else:
        abort(404)
    name = report.filename(kind, d1, d2, client)
    if request.args.get("save") and is_local():
        # desktop app: save into Documents\Playout\reports and open it
        folder = os.path.join(ROOT, "reports")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(data)
        if sys.platform == "win32":
            try:
                os.startfile(path)
            except OSError:
                pass
        return jsonify(path=path)
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=name)


# ---- playlist import (Excel / CSV) and day playlists ------------------------------
@app.post("/api/import")
def import_playlist():
    f = request.files.get("file")
    if not f:
        abort(400)
    try:
        items, warn = importer.parse(f.filename or "", f.read(), [m["name"] for m in list_media(cfg["media_dir"])])
    except Exception as e:
        return jsonify(error=f"Could not read the file: {e}"), 400
    if not items:
        return jsonify(error="No playlist rows found in the file", warnings=warn), 400
    mode = request.form.get("mode", "replace")
    save_as = (request.form.get("save_as") or "").strip()
    if save_as:
        store.save_list(save_as, items)
    if mode == "append":
        engine.set_playlist(engine.playlist + items)
    elif mode == "replace":
        engine.set_playlist(items)
    return jsonify(ok=True, count=len(items), warnings=warn, saved=save_as)


@app.get("/api/import/template.csv")
def import_template():
    if request.args.get("save") and is_local():
        folder = os.path.join(ROOT, "reports")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "playlist_template.csv")
        with open(path, "wb") as f:
            f.write(importer.template_csv())
        if sys.platform == "win32":
            os.startfile(path)
        return jsonify(path=path)
    return send_file(io.BytesIO(importer.template_csv()), mimetype="text/csv", as_attachment=True,
                     download_name="playlist_template.csv")


@app.route("/api/schedule", methods=["GET", "POST"])
def schedule_api():
    """Day playlists (auto-load each day) and scheduled recordings."""
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        if "day_lists" in b:
            d = b["day_lists"] or {}
            store.set(day_lists={"enabled": bool(d.get("enabled", True)), "time": d.get("time") or "00:00",
                                 "dates": {k: v for k, v in (d.get("dates") or {}).items() if v},
                                 "weekdays": {k: v for k, v in (d.get("weekdays") or {}).items() if v}})
        if "rec_times" in b:
            clean = []
            for e in b["rec_times"] or []:
                if e.get("start") and e.get("end"):
                    clean.append({"id": e.get("id") or secrets.token_hex(4), "name": e.get("name", ""),
                                  "start": e["start"][:5], "end": e["end"][:5], "days": e.get("days") or [],
                                  "date": e.get("date") or "", "enabled": bool(e.get("enabled", True))})
            store.set(rec_times=clean)
    today = dt.date.today()
    return jsonify(day_lists=planner.day_lists, rec_times=planner.rec_times, playlists=store.list_names(),
                   today=today.isoformat(), today_list=planner.list_for(today),
                   loaded=store.data.get("day_list_loaded"), rec_by_schedule=planner.rec_by_schedule)


@app.post("/api/schedule/load_today")
def schedule_load_today():
    name = planner.list_for(dt.date.today())
    if not name:
        return jsonify(ok=False, error="No playlist is set for today")
    store.set(day_list_loaded=dt.date.today().isoformat())
    return jsonify(ok=planner.load(name), name=name)


# ---- updates -------------------------------------------------------------------
@app.get("/api/update")
def update_info():
    return jsonify(updater.info())


@app.post("/api/update/<action>")
def update_action(action):
    if action == "check":
        return jsonify(updater.check())
    if not is_local():
        abort(403)
    if action == "download":
        return jsonify(updater.download())
    if action == "install":
        return jsonify(updater.install())
    abort(404)


# ---- live streaming (3 destinations) -----------------------------------
@app.get("/api/stream")
def stream_info():
    return jsonify(streamer.info())


@app.post("/api/stream/settings")
def stream_settings():
    body = request.get_json(silent=True) or {}
    result = streamer.apply_settings(body.get("resolution"), body.get("video_kbps"),
                                     body.get("audio_kbps"), body.get("encoder"))
    store.set(stream_settings=result)
    return jsonify(streamer.info())


@app.post("/api/stream/<int:slot>")
def stream_config(slot):
    if not 0 <= slot < len(streamer.dests):
        abort(404)
    body = request.get_json(silent=True) or {}
    streamer.configure(slot, body.get("name"), body.get("url"), body.get("key"))
    store.set(stream_destinations=streamer.dump())
    return jsonify(streamer.info())


@app.post("/api/stream/<int:slot>/<action>")
def stream_toggle(slot, action):
    if not 0 <= slot < len(streamer.dests) or action not in ("start", "stop"):
        abort(404)
    streamer.set_enabled(slot, action == "start")
    return jsonify(streamer.info())


@app.post("/api/stream/all/<action>")
def stream_all(action):
    for d in streamer.dests:
        if action == "stop" or (d.url.strip() and action == "start"):
            streamer.set_enabled(d.slot, action == "start")
    return jsonify(streamer.info())


# ---- simple GET triggers for vMix scripts / Stream Deck / Companion ----
@app.get("/trigger/<action>")
def trigger(action):
    actions = {"play": engine.play, "pause": engine.pause, "stop": engine.stop,
               "live": engine.take_live, "next": engine.take_next, "stinger": engine.stinger_take}
    if action not in actions:
        return "Unknown action. Use play, pause, stop, live, next or stinger.", 404
    actions[action]()
    return "OK"


@app.get("/trigger/item/<path:name>")
def trigger_item(name):
    for it in engine.playlist:
        if it["name"].lower() == name.lower():
            engine.play(it["id"])
            return "OK"
    return f"No playlist item called {name}", 404


# ---- system load -------------------------------------------------------
@app.route("/api/audio_out", methods=["GET", "POST"])
def audio_out_api():
    """Program sound on this PC's speakers (Windows default output or a chosen device)."""
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        audio_out.configure(b.get("enabled"), b.get("device"), b.get("volume"))
        store.set(audio_out={"enabled": audio_out.enabled, "device": audio_out.device, "volume": audio_out.volume})
    return jsonify(audio_out.info())


@app.get("/api/system")
def system_stats():
    return jsonify(sysmon.stats)


# ---- live card sources -------------------------------------------------
@app.get("/api/inputs")
def inputs():
    own = f"({cfg.get('output_name', 'PLAYOUT PGM')})"
    ndi = [n for n in engine.finder.names() if not n.endswith(own)]
    return jsonify(ndi=ndi, devices=list_capture_devices(streamer.ffmpeg), main=engine.live.source_name)


# ---- CG ----------------------------------------------------------------
@app.get("/api/cg/files")
def cg_files():
    return jsonify(list_cg(cfg["cg_dir"]))


@app.post("/api/cg/files")
def cg_upload():
    saved = []
    for f in request.files.getlist("files"):
        name = secure_filename(f.filename or "")
        ext = os.path.splitext(name)[1].lower()
        if name and ext in CG_EXT:
            f.save(os.path.join(cfg["cg_dir"], name))
            saved.append(name)
        elif name and ext == PRESET_EXT:      # CG Composer preset: unpack its media next to it
            path = os.path.join(cfg["cg_dir"], name)
            f.save(path)
            try:
                read_cgp(path, cfg["cg_dir"])
                saved.append(name)
            except Exception:
                os.remove(path)
    return jsonify(saved=saved)


@app.get("/api/cg/preview/<path:name>")
def cg_preview_png(name):
    path = os.path.join(cfg["cg_dir"], secure_filename(name))
    if not os.path.isfile(path):
        abort(404)
    try:
        return send_file(cg_preview(path, THUMBS), mimetype="image/png", max_age=3600)
    except Exception:
        abort(404)


@app.route("/api/cg/manual", methods=["GET", "POST"])
def cg_manual():
    if request.method == "POST":
        cgm.set_manual(request.get_json(silent=True) or [])
    return jsonify(list=cgm.manual, **cgm.status())


@app.post("/api/cg/manual/<cid>/<action>")
def cg_manual_action(cid, action):
    if action == "in":
        cgm.take_in(cid)
    elif action == "out":
        cgm.take_out(cid)
    else:
        abort(404)
    return jsonify(cgm.status())


@app.post("/api/cg/all_out")
def cg_all_out():
    cgm.all_out()
    return jsonify(cgm.status())


@app.post("/api/open_media_folder")
def open_media_folder():
    """Opens the media folder in Explorer (only from the playout PC itself)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    if sys.platform == "win32":
        os.startfile(cfg["cg_dir"] if request.args.get("cg") else cfg["media_dir"])
    return jsonify(path=cfg["cg_dir"] if request.args.get("cg") else cfg["media_dir"])


_started = False


def start_backend():
    """Load saved state and start the output engine and scheduler (no web server)."""
    global _started
    if _started:
        return
    _started = True
    crashed = os.path.exists(RUN_FLAG)
    with open(RUN_FLAG, "w") as f:
        f.write(str(os.getpid()))
    fo = store.data.get("failover") or {}
    engine.set_failover(fo.get("enabled"), fo.get("after"), fo.get("mode"), fo.get("file"))
    an = store.data.get("audio_norm") or {}
    loudness.configure(an.get("normalize"), an.get("target"))
    up = store.data.get("update") or {}
    updater.configure(up.get("repo"), up.get("token"), up.get("auto_check"))
    if sec()["lock_on_start"]:
        store.set(locked=True)
    engine.set_modes(store.data.get("auto", True), store.data.get("loop", False))
    engine.set_playlist(store.data.get("playlist", []))
    engine.on_change = lambda items: store.set(playlist=items)
    engine.set_live_source(cfg.get("live_source") or store.data.get("live_source"))
    streamer.load(store.data.get("stream_destinations"))
    rs = store.data.get("record_settings") or {}
    recorder.configure(rs.get("resolution"), rs.get("mbps"), rs.get("encoder"))
    st = store.data.get("stinger") or {}
    engine.set_stinger(st.get("file"), st.get("cut"), st.get("auto"))
    saved = store.data.get("stream_settings") or {}
    if saved:
        streamer.apply_settings(saved.get("resolution"), saved.get("video_kbps"),
                                saved.get("audio_kbps"), saved.get("encoder"))
    engine.streamer = streamer
    cgm.set_manual(store.data.get("manual_cgs", []))
    cgm.on_change = lambda items: store.set(manual_cgs=items)
    sysmon.start()
    ao = store.data.get("audio_out") or {}
    audio_out.configure(ao.get("enabled", True), ao.get("device", ""), ao.get("volume", 1.0))
    engine.start()
    scheduler.start()
    planner.start()
    updater.start_auto()
    air = store.data.get("air") or {}
    if crashed and air.get("id"):
        # Playout closed unexpectedly (crash / power): put the item that was on air back, where it was
        offset = max(0.0, time.time() - float(air.get("since") or time.time()))
        if engine.resume(air["id"], offset):
            print(f"resumed {air['id']} at {offset:.1f}s after an unexpected stop")
    elif crashed:
        engine.message = "Playout restarted after an unexpected stop"
    threading.Thread(target=_air_saver, daemon=True, name="air state").start()
    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()


def _air_saver():
    """Remembers what is on air (and since when), so a restart can carry on."""
    last = None
    while True:
        time.sleep(1)
        try:
            on = engine.on_air if engine.state == "PLAYING" else None
            if on != last:
                since = time.time() - engine.item_ticks / float(engine.fps) if on else None
                store.set(air={"id": on, "since": since})
                last = on
        except Exception:
            pass


def _watchdog():
    """If the output loop freezes, quit so the Playout launcher restarts it (and it resumes)."""
    while True:
        time.sleep(2)
        if engine._running and time.monotonic() - engine.heartbeat > 15:
            print(f"watchdog: output loop stuck for {time.monotonic() - engine.heartbeat:.0f}s, restarting", flush=True)
            import faulthandler
            faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
            os._exit(3)


def shutdown():
    planner.stop()
    try:
        os.remove(RUN_FLAG)
    except OSError:
        pass
    sysmon.stop()
    recorder.stop()
    audio_out.close()
    scheduler.stop()
    streamer.stop_all()
    engine.shutdown()


def serve(host="0.0.0.0", port=None):
    port = port or int(cfg.get("web_port", 8080))
    app.run(host=host, port=port, threaded=True, use_reloader=False)


def main():
    """Server only; open http://localhost:8080 in a browser. Playout.exe uses desktop.py."""
    start_backend()

    def on_signal(*_):
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    port = int(cfg.get("web_port", 8080))
    print(f"Playout running. NDI output: '{cfg.get('output_name')}'. Control screen: http://localhost:{port}")
    serve(port=port)


if __name__ == "__main__":
    main()
