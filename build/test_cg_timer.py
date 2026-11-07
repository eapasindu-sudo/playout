"""CG check: a manual video CG really shows when it is put IN (warmed-up and fresh), and a CG with a timer
(duration) gives back its decoder and frames every time it goes off by itself, over 20 plays.
Run: python build/test_cg_timer.py   (needs ffmpeg on PATH to make the test clip)"""
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cg  # noqa: E402


def frames(layer, n):
    shown = 0
    for _ in range(n):
        if layer.tick():
            shown += 1
        time.sleep(0.04)
    return shown


def main():
    d = tempfile.mkdtemp()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=2",
                    "-pix_fmt", "yuv420p", os.path.join(d, "t.mp4")], check=True)
    m = cg.CGManager(d, 1920, 1080, 25)
    m.manual = [{"id": "w", "file": "t.mp4", "kind": "video", "loop": True},
                {"id": "t", "file": "t.mp4", "kind": "video", "loop": True, "duration": 1}]
    m._warm_all()
    time.sleep(3)
    w = m._manual_layers["w"]
    frames(w, 5)                                # off air for a while, as before the operator presses IN
    m.take_in("w")
    shown = frames(w, 40)
    assert shown >= 35, f"warmed CG showed {shown}/40 frames"
    m.take_out("w")
    frames(w, 20)
    m.take_in("w")
    shown = frames(m._manual_layers["w"], 40)
    assert shown >= 35, f"CG after OUT / IN showed {shown}/40 frames"
    m.take_out("w")
    frames(m._manual_layers["w"], 20)

    threads0 = threading.active_count()
    for i in range(20):
        m.take_in("t")
        layer = m._manual_layers["t"]
        shown = frames(layer, 40)                 # 1 s on, then it fades out by itself
        assert shown >= 20, f"timer play {i}: showed {shown} frames"
        assert layer.state == "off", f"timer play {i}: still {layer.state}"
        assert layer._player is None and not layer._cache, f"timer play {i}: decoder / frames kept"
    assert cg._cache_used == 0, f"{cg._cache_used} bytes of CG frames still held"
    assert threading.active_count() <= threads0 + 2, "decoder threads pile up"
    print("OK: CGs show on IN, timer plays give everything back (20 plays)")


if __name__ == "__main__":
    main()
