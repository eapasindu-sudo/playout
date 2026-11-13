"""Endora licence key for Endora Playout: checked with the Endora server, 14-day free trial,
7 days offline grace. Same rules as the other Endora apps (see licence.js there)."""
import hashlib
import json
import os
import platform
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid

API = "https://www.endoraweddingfilms.com/perfex_crm/endora_bridge/shop/check"
BUY = "https://tools.endoraweddingfilms.com/#playout"
DAY = 86400
GRACE_DAYS = 7
RECHECK = 12 * 3600


def machine_id():
    raw = "|".join([socket.gethostname(), platform.system(), platform.machine(), platform.processor() or "", hex(uuid.getnode())])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Licence:
    def __init__(self, data_dir, product="playout", trial_days=14, now=time.time, opener=None, machine=None):
        self.file = os.path.join(data_dir, "licence.json")
        self.product = product
        self.trial_days = trial_days
        self.now = now
        self.opener = opener or (lambda url: urllib.request.urlopen(url, timeout=15).read())
        self.machine = machine or machine_id()
        self.lock = threading.Lock()
        self.s = self._load()
        if not self.s.get("first_run"):
            self.s["first_run"] = self.now()
            self._save()

    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(self.s, f)
        except Exception:
            pass

    def status(self):
        s, now = self.s, self.now()
        if s.get("key") and s.get("valid"):
            exp = s.get("expires")
            if exp and time.strftime("%Y-%m-%d", time.localtime(now)) > exp:
                return {"state": "expired", "ok": False, "key": s["key"], "message": f"Your licence ended on {exp}. Buy a new licence to keep using Playout."}
            if now - s.get("last_ok", 0) > GRACE_DAYS * DAY:
                return {"state": "offline", "ok": False, "key": s["key"], "message": "Connect to the internet once so Playout can check your licence."}
            return {"state": "licensed", "ok": True, "key": s["key"], "expires": exp}
        if s.get("key") and s.get("valid") is False:
            return {"state": "revoked", "ok": False, "key": s["key"], "message": s.get("reason") or "This licence key is not valid for Playout."}
        left = int((s["first_run"] + self.trial_days * DAY - now + DAY - 1) // DAY)
        if left > 0:
            return {"state": "trial", "ok": True, "days_left": left}
        return {"state": "none", "ok": False, "message": "Your 14-day free trial has ended. Enter a licence key to keep using Playout."}

    def _check(self, key):
        url = API + "?" + urllib.parse.urlencode({"key": key, "product": self.product, "machine": self.machine})
        j = json.loads(self.opener(url))
        if not j or not j.get("ok"):
            raise RuntimeError("The licence server did not answer. Try again in a moment.")
        return j

    def activate(self, key):
        key = str(key or "").strip().upper()
        j = self._check(key)
        if not j.get("valid"):
            return {"state": "invalid", "ok": False, "message": j.get("reason") or "This licence key is not valid for Playout."}
        with self.lock:
            self.s.update(key=key, valid=True, last_ok=self.now(), expires=j.get("expires"), reason="")
            self._save()
        return self.status()

    def refresh(self, force=False):
        key = self.s.get("key")
        if not key or (not force and self.s.get("valid") and self.now() - self.s.get("last_ok", 0) < RECHECK):
            return self.status()
        try:
            j = self._check(key)
            with self.lock:
                if j.get("valid"):
                    self.s.update(valid=True, last_ok=self.now(), expires=j.get("expires"), reason="")
                else:
                    self.s.update(valid=False, reason=j.get("reason", ""))
                self._save()
        except Exception:
            pass  # offline: grace period applies
        return self.status()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Endora Playout – Licence</title>
<style>body{margin:0;background:#0b0a09;color:#f5f0e8;font:15px/1.5 system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}.b{width:min(380px,90vw);text-align:center}
h1{font:500 28px Georgia,serif;color:#c8a96a;margin:0 0 6px}p{color:#bbb}input{width:100%;box-sizing:border-box;background:#161412;color:#f5f0e8;border:1px solid #3a332c;border-radius:4px;padding:12px;font:16px monospace;text-align:center;text-transform:uppercase}
button{width:100%;margin-top:10px;background:#c8a96a;color:#0b0a09;border:0;border-radius:4px;padding:12px;font-weight:700;cursor:pointer}#e{color:#ff8a80;min-height:22px;margin-top:8px}a{color:#c8a96a}</style></head>
<body><div class="b"><h1>Endora Playout</h1><p>__MSG__</p><input id="k" placeholder="EWF-PLY-XXXX-XXXX-XXXX"><button id="g">Activate</button><div id="e"></div>
<p style="font-size:12px">No key yet? <a href="__BUY__" target="_blank">Buy a licence</a></p></div>
<script>g.onclick=async()=>{e.textContent='Checking…';const r=await fetch('/api/licence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k.value})}).then(r=>r.json()).catch(()=>({message:'Could not reach Playout.'}));if(r.ok)location.href='/';else e.textContent=r.message||'This key did not work.'}</script></body></html>"""


def page(message):
    import html
    return PAGE.replace("__MSG__", html.escape(message)).replace("__BUY__", BUY)
