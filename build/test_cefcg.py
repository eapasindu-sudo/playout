"""CI: the built-in browser (build/cef/cefcg.exe) draws a test page into Playout's web CG: a red box on a
see-through background, at its place and with straight alpha."""
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import webcg  # noqa: E402

assert webcg.cef_exe(), "build/cef/cefcg.exe missing"
page = pathlib.Path(tempfile.gettempdir()) / "cefcg-test.html"
page.write_text("<html><body style='margin:0;background:transparent'>"
                "<div style='position:absolute;left:100px;top:50px;width:200px;height:80px;background:rgba(255,0,0,.5)'></div>"
                "<div id='t' style='position:absolute;left:400px;top:50px;width:40px;height:40px;background:#00f'></div>"
                "<script>let n=0;setInterval(()=>{document.getElementById('t').style.left=(400+(n++%50))+'px'},40)</script>"
                "</body></html>", encoding="utf-8")
r = webcg.WebRenderer(page.as_uri(), 1280, 720, rate=25)
assert isinstance(r, webcg.CefRenderer), type(r)
try:
    end = time.monotonic() + 40
    while time.monotonic() < end and r.frames < 3:
        time.sleep(0.2)
    print("loaded", r.loaded, "frames", r.frames, "error", r.error)
    assert r.frame is not None, "no picture from the built-in browser: " + r.error
    n0, t0 = r.frames, time.monotonic()
    time.sleep(4)
    rate = (r.frames - n0) / (time.monotonic() - t0)
    print(f"pictures a second: {rate:.1f}")
    img, (dx, dy) = r.frame
    print("picture", img.shape, "at", dx, dy)
    assert (dx, dy) == (100, 50), (dx, dy)
    b, g, red, a = (int(v) for v in img[40, 100])
    print("red box BGRA", b, g, red, a)
    assert abs(a - 128) <= 3 and red >= 250 and g <= 3, "red box colour / straight alpha wrong"
    assert rate >= 10, "the moving page should give many pictures a second"
    assert r._fallback is None, "fell back to Edge"
finally:
    r.close()
print("built-in browser OK")
