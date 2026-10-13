"""Playout server: playlist of commercials + LIVE cards (vMix NDI) -> NDI program out.

Run:  python app.py      then open http://localhost:8080
"""
import datetime as dt
import io
import json
import os
import signal
import sys
import time

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from PIL import Image
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
from scheduler import Scheduler
from store import Store
from streamer import Streamer

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
THUMBS = os.path.join(cfg["data_dir"], "thumbs")
app = Flask(__name__, static_folder=None)
app.register_blueprint(make_blueprint(lambda: cfg["cg_dir"], lambda: (engine.width, engine.height),
                                      lambda: engine.last_frame))
fullscreen_on = False
fullscreen_hook = None   # set by the desktop app: fn(on, screen) opens / closes the full screen output window


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
    return jsonify(engine.snapshot())


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
    return jsonify(list_media(cfg["media_dir"]))


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


def shutdown():
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
