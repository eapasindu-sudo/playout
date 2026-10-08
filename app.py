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

from engine import Engine
from media import MEDIA_EXT, list_media, thumbnail
from scheduler import Scheduler
from store import Store
from streamer import Streamer

if getattr(sys, "frozen", False):   # Playout.exe: user files next to the exe, web page inside it
    ROOT = os.path.dirname(sys.executable)
    BUNDLE = getattr(sys, "_MEIPASS", ROOT)
else:
    ROOT = BUNDLE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "output_name": "PLAYOUT PGM", "live_source": "", "width": 1920, "height": 1080, "fps": "25",
    "web_port": 8080, "media_dir": "media", "data_dir": "data", "log_dir": "logs",
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
    for key in ("media_dir", "data_dir", "log_dir"):
        cfg[key] = os.path.join(ROOT, cfg.get(key, key.split("_")[0]))
        os.makedirs(cfg[key], exist_ok=True)
    return cfg


cfg = load_config()
store = Store(cfg["data_dir"])
engine = Engine(cfg, cfg["media_dir"], cfg["log_dir"])
scheduler = Scheduler(engine)
streamer = Streamer(engine, cfg)
THUMBS = os.path.join(cfg["data_dir"], "thumbs")
app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_from_directory(os.path.join(BUNDLE, "web"), "index.html")


# ---- state & transport -------------------------------------------------
@app.get("/api/state")
def state():
    return jsonify(engine.snapshot())


@app.post("/api/transport/<action>")
def transport(action):
    body = request.get_json(silent=True) or {}
    if action == "play":
        engine.play(body.get("id"))
    elif action == "pause":
        engine.pause()
    elif action == "stop":
        engine.stop()
    elif action == "next":
        engine.next()
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
               "live": engine.stop, "next": engine.next}
    if action not in actions:
        return "Unknown action. Use play, pause, stop, live or next.", 404
    actions[action]()
    return "OK"


@app.get("/trigger/item/<path:name>")
def trigger_item(name):
    for it in engine.playlist:
        if it["name"].lower() == name.lower():
            engine.play(it["id"])
            return "OK"
    return f"No playlist item called {name}", 404


def main():
    engine.set_modes(store.data.get("auto", True), store.data.get("loop", False))
    engine.set_playlist(store.data.get("playlist", []))
    engine.on_change = lambda items: store.set(playlist=items)
    engine.set_live_source(cfg.get("live_source") or store.data.get("live_source"))
    streamer.load(store.data.get("stream_destinations"))
    engine.streamer = streamer
    engine.start()
    scheduler.start()

    def shutdown(*_):
        scheduler.stop()
        streamer.stop_all()
        engine.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    port = int(cfg.get("web_port", 8080))
    print(f"Playout running. NDI output: '{cfg.get('output_name')}'. Control screen: http://localhost:{port}")
    print("Keep this window open while on air. Close it to stop the playout.")
    if getattr(sys, "frozen", False) or "--open" in sys.argv:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
