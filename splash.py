"""Loading screen shown while Playout / CG Composer start (the Endora logo, a moving bar and what is loading)."""
import base64
import json
import os

import paths
from version import VERSION

SIZE = (760, 400)


def html(kind="playout"):
    """kind: playout | composer"""
    name = "logo-composer.png" if kind == "composer" else "logo-playout.png"
    try:
        with open(os.path.join(paths.bundle_dir(), "web", name), "rb") as f:
            logo = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        logo = ""
    title = "Endora CG Composer" if kind == "composer" else "Endora Playout"
    img = f'<img src="{logo}" alt="{title}">' if logo else f"<h1>{title}</h1>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;height:100%;background:#0c0c0e;color:#e9eaec;font:13px "Segoe UI",system-ui,sans-serif;
  overflow:hidden;user-select:none;cursor:default}}
body{{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:34px;
  border:1px solid #26272b;box-sizing:border-box}}
img{{width:560px;opacity:0;animation:in .7s ease-out forwards}}
h1{{font-size:40px;margin:0}}
.bar{{width:420px;height:4px;background:#1d1e22;border-radius:2px;overflow:hidden;position:relative}}
.bar i{{position:absolute;top:0;left:-35%;width:35%;height:100%;border-radius:2px;
  background:linear-gradient(90deg,#ff2a14,#ff8a00,#ffd000);animation:run 1.3s ease-in-out infinite}}
.st{{color:#8d9299;font-size:12px;letter-spacing:.04em;height:16px;margin-top:-20px}}
.ver{{position:absolute;bottom:12px;right:16px;color:#55595f;font-size:11px}}
@keyframes run{{0%{{left:-35%}}100%{{left:100%}}}}
@keyframes in{{from{{opacity:0;transform:scale(.97)}}to{{opacity:1;transform:none}}}}
</style></head><body>{img}<div class="bar"><i></i></div><div class="st" id="st">Starting...</div>
<div class="ver">Version {VERSION}</div></body></html>"""


def status(win, text):
    """Show what is loading under the bar (ignored if the window is gone)."""
    try:
        win.evaluate_js("document.getElementById('st').textContent = " + json.dumps(text))
    except Exception:
        pass
