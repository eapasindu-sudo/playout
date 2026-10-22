"""CI: Playout's native GPU engine (build/cef/pgpu.exe) mixes a program frame with a CG picture and a web page,
sends the mixed picture back, and encodes a stream (MPEG-TS through its pipe) and a recording (MP4) from it.
The CI machine has no graphics card: Windows' software Direct3D (WARP) and the CPU encoder stand in, which
also tests the fallbacks."""
import io
import re
import os
import pathlib
import sys
import tempfile
import threading
import time

import av
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gpuengine  # noqa: E402

W, H, FPS = 1280, 720, 25
assert gpuengine.exe(), "build/cef/pgpu.exe missing"
eng = gpuengine.start(W, H, FPS)
assert eng is not None, "pgpu.exe did not start: " + gpuengine.STATE["error"]
print("GPU engine:", eng.gpu)

page = pathlib.Path(tempfile.gettempdir()) / "pgpu-test.html"
page.write_text("<html><body style='margin:0;background:transparent'>"
                "<div style='position:absolute;left:100px;top:50px;width:200px;height:80px;background:rgba(255,0,0,.5)'></div>"
                "<div id='t' style='position:absolute;left:0;top:400px;width:300px;height:100px;background:rgba(0,0,255,.5)'></div>"
                "<script>let n=0;setInterval(()=>{document.getElementById('t').style.left=(n++%50)+'px'},40)</script>"
                "</body></html>", encoding="utf-8")

base = np.zeros((H, W, 4), np.uint8)
base[...] = (128, 128, 128, 255)
cg = np.zeros((40, 60, 4), np.uint8)
cg[...] = (0, 0, 255, 128)                       # straight alpha red, half see-through
silence = np.zeros((2, 1920), np.float32)
tone = (0.2 * np.sin(np.arange(1920) * 2 * np.pi * 1000 / 48000)).astype(np.float32)
sound = np.stack([tone, tone])


def near(px, want, tol=6):
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(px, want))


web = gpuengine.NativeWebRenderer(page.as_uri(), W, H, rate=25)
try:
    end = time.monotonic() + 45
    out = None
    while time.monotonic() < end:
        fr = web.frame
        jobs = [(cg, 20, 30, 255)] + ([(fr[0], 600, 300, 255)] if fr else [])
        out = eng.frame(base, silence, jobs)
        if fr is not None and out is not None and web.loaded:
            break
        time.sleep(0.04)
    print("web page: loaded", web.loaded, "drawn", web.frames, "mode", web.mode, "error", web.error)
    assert out is not None, "no mixed picture back"
    assert web.frame is not None, "the web page was never drawn: " + web.error
    end = time.monotonic() + 20                   # more frames until the page has drawn its boxes
    while True:
        out = eng.frame(base, silence, [(cg, 20, 30, 255), (web.frame[0], 600, 300, 255)])
        img = out.reshape(H, W, 4)
        if near(img[390, 750], (64, 64, 192, 255)) or time.monotonic() > end:
            break
        time.sleep(0.04)
    print("page pictures", gpuengine.STATE["stats"])
    print("background", img[5, 5], "CG", img[50, 50], "web box", img[390, 750])
    assert near(img[5, 5], (128, 128, 128, 255), 1), "program picture wrong"
    assert near(img[50, 50], (64, 64, 192, 255)), "CG picture blend wrong"
    assert near(img[390, 750], (64, 64, 192, 255)), "web page blend wrong (place, alpha)"
    assert near(img[320, 620], (128, 128, 128, 255), 1), "web page see-through part not see-through"

    # stream + recording from the same frames
    rec = os.path.join(tempfile.gettempdir(), "pgpu-test.mp4")
    if os.path.exists(rec):
        os.remove(rec)
    pipe, msg = eng.stream_start(1280, 720, 3000, 128, "auto", 50)
    assert pipe, "stream did not start: " + msg
    print("stream encoder:", msg)
    ok, rmsg = eng.rec_start(rec, 1280, 720, 8000, "auto", 25)
    assert ok, "recording did not start: " + rmsg
    print("recording encoder:", rmsg)
    got = io.BytesIO()

    def read():
        with open(pipe, "rb", buffering=0) as f:
            while True:
                d = f.read(65536)
                if not d:
                    break
                got.write(d)

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t0 = time.monotonic()
    k = 0
    while k < 100:                                # 4 seconds at 25 a second
        due = t0 + k / FPS
        time.sleep(max(0.0, due - time.monotonic()))
        b = base.copy()
        b[:, (k * 8) % W:(k * 8) % W + 40] = (255, 255, 255, 255)   # something moving for the encoder
        r = eng.frame(b, sound, [(cg, 20, 30, 255), (web.frame[0], 600, 300, 255)])
        assert r is not None, f"frame {k}: no answer"
        k += 1
    took = time.monotonic() - t0
    print(f"100 frames in {took:.1f} s, stats: {gpuengine.STATE['stats']}")
    eng.stream_stop()
    eng.rec_stop()
    t.join(10)
    time.sleep(3)
    data = got.getvalue()
    print("stream bytes", len(data))
    assert len(data) > 50000 and data[0] == 0x47, "no MPEG-TS from the stream encoder"
    with av.open(io.BytesIO(data), format="mpegts") as c:
        v = sum(1 for _ in c.decode(video=0))
    with av.open(io.BytesIO(data), format="mpegts") as c:
        a = sum(1 for _ in c.decode(audio=0))
    print("stream: video frames", v, "audio frames", a)
    assert v >= 90 and a >= 100, "stream is missing pictures or sound"
    with av.open(rec) as c:
        vs = c.streams.video[0]
        print("recording:", vs.codec_context.name, vs.codec_context.width, "x", vs.codec_context.height,
              "bitrate", c.bit_rate)
        n = sum(1 for _ in c.decode(video=0))
    print("recording frames", n)
    assert n >= 90, "recording is missing pictures"

    # off air and on again: the page keeps drawing; reload: it comes back
    web.active = False
    time.sleep(1)
    web.active = True
    web.reload()
    end = time.monotonic() + 30
    while time.monotonic() < end and not web.loaded:
        eng.frame(base, silence, [])
        time.sleep(0.04)
    assert web.loaded, "the page did not come back after Reload"

    # the control screen's preview drawn by the engine into a window of another program (here a Tk window),
    # while the web page keeps drawing: nothing may lock up (1.0.103 locked up here on a real PC)
    import tkinter
    root = tkinter.Tk()
    root.geometry("800x500")
    root.update()
    parent = root.winfo_id()
    stop = {"run": True, "frames": 0, "misses": 0}

    def pump():
        while stop["run"]:
            r = eng.frame(base, silence, [(cg, 20, 30, 255), (web.frame[0], 600, 300, 255)])
            stop["frames" if r is not None else "misses"] += 1
            time.sleep(0.04)

    pt = threading.Thread(target=pump, daemon=True)
    pt.start()
    t0 = time.monotonic()
    k = 0
    while time.monotonic() - t0 < 6:
        if k % 10 == 0:
            on = (k // 10) % 3 != 2
            eng.preview(int(parent), 10 + k % 7, 10, 640 - k % 5, 360, on)
        root.update()
        time.sleep(0.03)
        k += 1
    # the preview window must show the program (grey with the red CG), not what was under it
    eng.preview(int(parent), 100, 50, 640, 360, True)
    for _ in range(40):
        root.update()
        time.sleep(0.05)
    from PIL import ImageGrab
    rx, ry = root.winfo_rootx(), root.winfo_rooty()
    shot = ImageGrab.grab(bbox=(rx, ry, rx + 800, ry + 500), all_screens=True).convert("RGB")
    inside, outside = shot.getpixel((100 + 320, 50 + 250)), shot.getpixel((20, 470))
    print("preview window: inside", inside, "outside", outside, "stats", gpuengine.STATE["stats"])
    stop["run"] = False
    pt.join(5)
    assert "preview_error" not in gpuengine.STATE["stats"], "preview: " + gpuengine.STATE["stats"]
    assert near(inside, (128, 128, 128), 8), "the preview window does not show the program"
    # minimised and back: the preview must come back live (1.0.118 came back frozen)
    root.iconify()
    for _ in range(10):
        root.update()
        time.sleep(0.05)
    n0 = int((re.search(r"preview=(\d+)", gpuengine.STATE["stats"]) or [0, 0])[1])
    root.deiconify()
    stop["run"] = True
    pt = threading.Thread(target=pump, daemon=True)
    pt.start()
    for _ in range(60):
        root.update()
        time.sleep(0.05)
    stop["run"] = False
    pt.join(5)
    n1 = int((re.search(r"preview=(\d+)", gpuengine.STATE["stats"]) or [0, 0])[1])
    print("preview after restore:", n0, "->", n1, gpuengine.STATE["stats"])
    assert n1 > n0 and gpuengine.STATE["stats"].split("preview=")[1].split()[0].endswith("on"), \
        "the preview did not come back after the window was restored"
    # turned off while minimised: it must stay hidden when the window comes back (1.0.120 showed a frozen picture)
    import ctypes
    root.iconify()
    for _ in range(10):
        root.update()
        time.sleep(0.05)
    eng.preview(int(parent), 100, 50, 640, 360, False)
    time.sleep(0.5)
    root.deiconify()
    for _ in range(20):
        root.update()
        time.sleep(0.05)
    pv = ctypes.windll.user32.FindWindowW("PlayoutGpuPreview", None)
    print("preview window after off + restore: visible", bool(pv and ctypes.windll.user32.IsWindowVisible(pv)))
    assert not (pv and ctypes.windll.user32.IsWindowVisible(pv)), "the preview window came back while turned off"
    eng.preview(int(parent), 0, 0, 0, 0, False)
    root.update()
    print("preview: frames", stop["frames"], "no answer", stop["misses"], "drawn", web.frames)
    assert stop["frames"] >= 100 and stop["misses"] <= 2, "the engine locked up with the preview window on"
    root.destroy()

    # squeeze (L-ad): the program into a box over black, a picture under it, all on the GPU
    under = np.zeros((H, W, 4), np.uint8)
    under[:, : W // 4] = (255, 0, 0, 255)                         # blue strip on the left
    sq = [{"box": {"x": W // 2, "y": 0, "w": W // 2, "h": H // 2}, "under": [(under, 0, 0, 255)], "plan": None, "over": 0}]
    img = eng.frame(base, silence, sq).reshape(H, W, 4)
    print("squeeze: box", img[100, W * 3 // 4], "outside", img[H - 10, W // 2], "under", img[H - 10, 10])
    assert near(img[100, W * 3 // 4], (128, 128, 128, 255), 2), "squeeze: program not in its box"
    assert near(img[H - 10, W // 2], (0, 0, 0, 255), 2), "squeeze: not black outside the box"
    assert near(img[H - 10, 10], (255, 0, 0, 255), 2), "squeeze: picture under the program missing"

    # a vertical (9:16) destination encoded from the GPU picture
    vp, vmsg = eng.stream_start(720, 1280, 2500, 128, "auto", 50, name="v0", layout="blur", pos=50)
    assert vp, "vertical encode did not start: " + vmsg
    vgot = io.BytesIO()

    def vread():
        with open(vp, "rb", buffering=0) as f:
            while True:
                d = f.read(65536)
                if not d:
                    break
                vgot.write(d)

    vt = threading.Thread(target=vread, daemon=True)
    vt.start()
    for k in range(50):
        eng.frame(base, sound, [])
        time.sleep(0.04)
    eng.stream_stop("v0")
    vt.join(10)
    with av.open(io.BytesIO(vgot.getvalue()), format="mpegts") as c:
        vf = [f for f in c.decode(video=0)]
    print("vertical:", len(vf), "pictures", vf[0].width if vf else 0, "x", vf[0].height if vf else 0)
    assert len(vf) >= 40 and (vf[0].width, vf[0].height) == (720, 1280), "vertical encode wrong"

    # a clip decoded by the engine (GPU decoder on a real PC, the CPU here), as the program player uses it
    import media
    clip_path = os.path.join(tempfile.gettempdir(), "pgpu-clip.mp4")
    with av.open(clip_path, "w") as c:
        st = c.add_stream("libx264", rate=25)
        st.width, st.height, st.pix_fmt = 640, 360, "yuv420p"
        for i in range(75):
            a = np.zeros((360, 640, 3), np.uint8)
            a[...] = (255, 0, 0) if i < 25 else (0, 255, 0) if i < 50 else (0, 0, 255)    # RGB: red, green, blue
            f = av.VideoFrame.from_ndarray(a, format="rgb24")
            for pk in st.encode(f):
                c.mux(pk)
        for pk in st.encode(None):
            c.mux(pk)
    p = media.ClipPlayer(clip_path, W, H, 25)
    end = time.monotonic() + 20
    while not p.ready() and time.monotonic() < end:
        time.sleep(0.05)
    pics = []
    for i in range(80):
        v, a = p.next_frame(1920)
        if v is not None:
            pics.append(np.asarray(v).reshape(H, W, 4)[H // 2, W // 2].copy())
        time.sleep(0.04)
    print("clip decode:", media.DECODE, "pictures", len(pics), "error", p.error)
    p.close()
    assert "GPU engine" in media.DECODE["codec"], "the clip was not decoded by the engine"
    assert len(pics) >= 70, "clip pictures missing"
    print("clip colours (BGRA) at 0.4 s, 1.4 s, 2.4 s:", pics[10], pics[35], pics[60])
    assert near(pics[10], (0, 0, 255, 255), 12) and near(pics[35], (0, 255, 0, 255), 12) and near(pics[60], (255, 0, 0, 255), 12), \
        "clip colours wrong"
finally:
    web.close()
    gpuengine.stop()
print("GPU engine OK")
