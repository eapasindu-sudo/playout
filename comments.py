"""Live comments from YouTube and Facebook, shown on air as a comment box.

YouTube: the live chat of a stream link (watch?v=, youtu.be/, /live/ID, or a channel's /live page),
read the way the YouTube chat window reads it; no key or login needed.
Facebook: the comments of a live video on your Page, read with a Page access token
(Meta for Developers > Graph API Explorer > your Page > pages_read_engagement, pages_read_user_content).

Comments are listed in Playout (CG > Comments). Click one to put it on air, or turn on Auto to show each
new comment for a few seconds. Comments can also be typed in by hand (e.g. from WhatsApp).
"""
import collections
import json
import re
import threading
import time
import certs
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
# y / width below 1 = part of the screen
DEFAULT_LOOK = {"x": 60, "y": 0.62, "width": 0.45, "size": 34, "font": "Nirmala UI (Sinhala / Tamil)",
                "color": "#ffffff", "bg": "#0f2252", "bg_alpha": 88, "accent": "#e0261b", "fade": 8,
                "live_only": False}


def _get(url, data=None, headers=None, timeout=15):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+1; SOCS=CAI"}
    h.update(headers or {})
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with certs.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---- YouTube --------------------------------------------------------------
def youtube_id(link):
    """Video id of a YouTube link, or None (a channel /live page is looked up)."""
    link = (link or "").strip()
    if re.fullmatch(r"[\w-]{11}", link):
        return link
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})", link)
    if m:
        return m.group(1)
    if "youtube.com/" in link:                         # youtube.com/@channel/live
        url = link if link.rstrip("/").endswith("/live") else link.rstrip("/") + "/live"
        html = _get(url)
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html) \
            or re.search(r'"videoId":"([\w-]{11})"', html)
        return m.group(1) if m else None
    return None


def _yt_text(runs):
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            out.append(e.get("emojiId") if not e.get("isCustomEmoji") else (e.get("shortcuts") or [""])[0])
    return "".join(out).strip()


def youtube_parse(data):
    """Comments and the next (continuation, wait seconds) from a get_live_chat answer."""
    lc = (data.get("continuationContents") or {}).get("liveChatContinuation") or {}
    msgs = []
    for a in lc.get("actions") or []:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        text = _yt_text((r.get("message") or {}).get("runs"))
        paid = (r.get("purchaseAmountText") or {}).get("simpleText")
        if paid:
            text = f"[{paid}] {text}".strip()
        if not text:
            continue
        msgs.append({"id": "yt:" + r.get("id", ""), "src": "YouTube",
                     "author": (r.get("authorName") or {}).get("simpleText") or "Viewer",
                     "text": text, "t": int(r.get("timestampUsec") or 0) / 1e6 or time.time()})
    nxt, wait = None, 5.0
    for c in lc.get("continuations") or []:
        for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            if k in c:
                nxt = c[k].get("continuation")
                wait = max(1.0, min(10.0, int(c[k].get("timeoutMs") or 5000) / 1000))
                break
        if nxt:
            break
    return msgs, nxt, wait


def youtube_viewers(vid, key=""):
    """People watching a YouTube live right now (None if not live / not found)."""
    url = "https://www.youtube.com/youtubei/v1/updated_metadata" + (f"?key={key}" if key else "")
    text = _get(url, {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}}, "videoId": vid})
    return parse_viewers(text)


def parse_viewers(text):
    m = re.search(r'"originalViewCount"\s*:\s*"(\d+)"', text)
    if m:
        return int(m.group(1))
    m = re.search(r'"(?:text|simpleText)"\s*:\s*"([\d,.]+)\s*(?:watching|waiting)', text)
    return int(re.sub(r"[^\d]", "", m.group(1))) if m else None


def facebook_viewers(vid, token):
    q = urllib.parse.urlencode({"access_token": token, "fields": "live_views"})
    data = json.loads(_get(f"https://graph.facebook.com/v19.0/{vid}?{q}"))
    v = data.get("live_views")
    return int(v) if v is not None else None


def youtube_start(vid):
    """(api key, client version, first continuation) from the chat window page."""
    html = _get(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html)
    cont = re.search(r'"continuations":\[\{"(?:invalidation|timed|reload)ContinuationData":\{(?:[^{}]|\{[^{}]*\})*?"continuation":"([^"]+)"', html)
    if not cont:
        raise RuntimeError("no live chat on this video (is it live, with chat on?)")
    return (key.group(1) if key else ""), (ver.group(1) if ver else "2.20240101.00.00"), cont.group(1)


# ---- Facebook -------------------------------------------------------------
def facebook_id(link):
    link = (link or "").strip()
    if re.fullmatch(r"\d{6,}", link):
        return link
    m = re.search(r"(?:/videos/(?:[^/?]+/)?|[?&]v=|/live/)(\d{6,})", link)
    return m.group(1) if m else None


def facebook_parse(data):
    out = []
    for c in data.get("data") or []:
        text = (c.get("message") or "").strip()
        if text:
            out.append({"id": "fb:" + str(c.get("id")), "src": "Facebook",
                        "author": (c.get("from") or {}).get("name") or "Facebook user", "text": text,
                        "t": time.time()})
    return out


class Comments:
    def __init__(self, cgm, get_cfg, save_cfg):
        self.cgm = cgm
        self._get_cfg, self._save_cfg = get_cfg, save_cfg   # the "comments" part of config.json
        self.items = collections.deque(maxlen=300)
        self._seen = set()
        self._lock = threading.Lock()
        self.status = {"YouTube": "", "Facebook": ""}
        self.viewers = {}              # {"YouTube": 120, "Facebook": 35}: people watching now (stream health page)
        self._gen = 0                  # bumped when the sources change: old reader threads stop
        self._queue = collections.deque()
        self._shown_at = 0.0
        self._last = 0
        self.apply()
        threading.Thread(target=self._auto_loop, daemon=True, name="comments auto").start()

    @property
    def cfg(self):
        c = dict(self._get_cfg() or {})
        look = {**DEFAULT_LOOK, **(c.get("look") or {})}
        for k, full in (("y", self.cgm.height), ("width", self.cgm.width)):
            if 0 < float(look[k] or 0) < 1:
                look[k] = int(float(look[k]) * full)
        c["look"] = look
        return c

    def set_cfg(self, changes):
        c = {**(self._get_cfg() or {}), **changes}
        self._save_cfg(c)
        if {"youtube", "facebook", "facebook_token"} & set(changes):
            self.apply()
        if "auto" in changes:
            self._queue.clear()

    def apply(self):
        """(Re)start the readers for the links in the settings."""
        self._gen += 1
        gen, c = self._gen, self.cfg
        self.status = {"YouTube": "", "Facebook": ""}
        self.viewers = {}
        if (c.get("youtube") or "").strip() or (c.get("facebook") or "").strip():
            threading.Thread(target=self._count_viewers, args=(gen, c), daemon=True, name="viewers").start()
        if (c.get("youtube") or "").strip():
            threading.Thread(target=self._youtube, args=(gen, c["youtube"]), daemon=True, name="yt chat").start()
        if (c.get("facebook") or "").strip():
            threading.Thread(target=self._facebook, args=(gen, c["facebook"], c.get("facebook_token") or ""),
                             daemon=True, name="fb comments").start()

    def _count_viewers(self, gen, c):
        """Viewer counts every 30 s while the links are set (shown on the stream health page)."""
        yt, fb, token = (c.get("youtube") or "").strip(), (c.get("facebook") or "").strip(), c.get("facebook_token") or ""
        vid = None
        while gen == self._gen:
            if yt:
                try:
                    vid = vid or youtube_id(yt)
                    if vid:
                        self.viewers["YouTube"] = youtube_viewers(vid)
                except Exception:
                    self.viewers.pop("YouTube", None)
            if fb and token:
                try:
                    fid = facebook_id(fb)
                    if fid:
                        self.viewers["Facebook"] = facebook_viewers(fid, token)
                except Exception:
                    self.viewers.pop("Facebook", None)
            for _ in range(30):
                if gen != self._gen:
                    return
                time.sleep(1)

    # ---- incoming -----------------------------------------------------------
    def add(self, msgs, queue=True):
        new = []
        with self._lock:
            for m in msgs:
                if m["id"] in self._seen:
                    continue
                self._seen.add(m["id"])
                self._last += 1
                m["n"] = self._last
                self.items.append(m)
                new.append(m)
            if len(self._seen) > 5000:
                self._seen = {m["id"] for m in self.items}
        if queue and self.cfg.get("auto"):
            self._queue.extend(new)
        return new

    def add_manual(self, author, text):
        return self.add([{"id": f"me:{time.time()}", "src": "Typed", "author": author.strip() or "Viewer",
                          "text": text.strip(), "t": time.time()}])

    def list(self, since=0):
        with self._lock:
            return [m for m in self.items if m["n"] > since]

    def _youtube(self, gen, link):
        first = True
        while gen == self._gen:
            try:
                vid = youtube_id(link)
                if not vid:
                    raise RuntimeError("not a YouTube link")
                key, ver, cont = youtube_start(vid)
                self.status["YouTube"] = "connected"
                while gen == self._gen and cont:
                    url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat" + (f"?key={key}" if key else "")
                    data = json.loads(_get(url, {"context": {"client": {"clientName": "WEB", "clientVersion": ver}},
                                                 "continuation": cont}))
                    msgs, cont, wait = youtube_parse(data)
                    self.add(msgs, queue=not first)   # the first answer is old chat: list it, don't auto-show it
                    first = False
                    time.sleep(wait)
                self.status["YouTube"] = "chat ended"
            except Exception as e:
                self.status["YouTube"] = f"error: {e}"
            for _ in range(15):                       # try again
                if gen != self._gen:
                    return
                time.sleep(1)

    def _facebook(self, gen, link, token):
        first = True
        while gen == self._gen:
            try:
                vid = facebook_id(link)
                if not vid:
                    raise RuntimeError("not a Facebook video link")
                if not token:
                    raise RuntimeError("Page access token needed")
                q = urllib.parse.urlencode({"access_token": token, "order": "reverse_chronological",
                                            "filter": "stream", "limit": 50,
                                            "fields": "id,from{name},message,created_time"})
                data = json.loads(_get(f"https://graph.facebook.com/v19.0/{vid}/comments?{q}"))
                if "error" in data:
                    raise RuntimeError(data["error"].get("message", "Facebook error"))
                msgs = list(reversed(facebook_parse(data)))
                self.status["Facebook"] = "connected"
                self.add(msgs, queue=not first)
                first = False
                wait = 3
            except urllib.error.HTTPError as e:
                try:
                    msg = json.loads(e.read().decode()).get("error", {}).get("message") or str(e)
                except Exception:
                    msg = str(e)
                self.status["Facebook"] = f"error: {msg}"
                wait = 15
            except Exception as e:
                self.status["Facebook"] = f"error: {e}"
                wait = 15
            for _ in range(wait):
                if gen != self._gen:
                    return
                time.sleep(1)

    # ---- on air ---------------------------------------------------------------
    def show(self, n):
        with self._lock:
            m = next((m for m in self.items if m["n"] == n), None)
        if m is None:
            return "comment not found"
        c = self.cfg
        look = c["look"]
        secs = float(c.get("show_secs") or 0)
        cfg = {**look, "line1": m["author"], "text": m["text"],
               "label": m["src"] if m["src"] != "Typed" else "", "duration": secs if c.get("auto") else 0}
        m["shown"] = True
        self._shown_at = time.time()
        return self.cgm.show_comment(cfg, n)

    def hide(self):
        self._queue.clear()
        self.cgm.hide_comment()

    def _auto_loop(self):
        while True:
            time.sleep(0.5)
            try:
                c = self.cfg
                if not c.get("auto") or not self._queue:
                    continue
                secs = max(2.0, float(c.get("show_secs") or 8))
                if time.time() - self._shown_at < secs + 1.0:
                    continue
                m = self._queue.popleft()
                self.show(m["n"])
            except Exception:
                pass
