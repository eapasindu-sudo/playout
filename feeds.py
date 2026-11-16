"""Ticker text from the internet: a news RSS / Atom feed, a Google Sheet, or a web page with plain text.

A ticker's "text file" can be a link (http:// or https://) instead of a .txt file:
  - RSS / Atom feed (news sites, Google News): the headline of each story, newest first
  - Google Sheet: File > Share > Publish to web > the sheet as CSV, paste that link. Each row is one item;
    the cells of a row are joined with " - " (e.g. "Gold 22K | Rs. 185,000" -> "Gold 22K - Rs. 185,000").
    A normal Google Sheet link (.../edit) is turned into its CSV link by itself (the sheet must be shared
    as "anyone with the link can view").
  - anything else: the text, one item per line
The link is read again every minute in the background; the ticker shows the last good text meanwhile
(so a slow or broken internet never stops the ticker).
"""
import csv
import io
import logging
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

import certs  # noqa: F401  (HTTPS certificates from the Windows store)

REFRESH = 60          # seconds between reads of the same link
MAX_ITEMS = 40
_cache = {}           # url -> {"text", "at", "busy", "error"}
_lock = threading.Lock()


def is_url(s):
    return isinstance(s, str) and s.strip().lower().startswith(("http://", "https://"))


def sheet_csv_url(url):
    """A Google Sheet edit / view link -> its CSV export link (the tab in gid= if there is one)."""
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m or "output=csv" in url or "format=csv" in url:
        return url
    g = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={g.group(1)}" if g else "")


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse(body, content_type=""):
    """Feed / CSV / text -> the ticker items (list of strings)."""
    text = body.decode("utf-8-sig", errors="replace") if isinstance(body, bytes) else str(body)
    head = text.lstrip()[:300].lower()
    if head.startswith("<?xml") or "<rss" in head or "<feed" in head or "xml" in content_type:
        try:
            root = ET.fromstring(text.encode("utf-8"))
            items = []
            for el in root.iter():
                if _local(el.tag) in ("item", "entry"):
                    title = next((c.text for c in el if _local(c.tag) == "title" and c.text), None)
                    if title and title.strip():
                        items.append(re.sub(r"\s+", " ", title).strip())
            if items:
                return items[:MAX_ITEMS]
        except ET.ParseError:
            pass
    if "csv" in content_type or ("," in text and "<" not in head):
        rows = []
        for r in csv.reader(io.StringIO(text)):
            cells = [c.strip() for c in r if c and c.strip()]
            if cells:
                rows.append(" - ".join(cells))
        if rows:
            return rows[:MAX_ITEMS]
    if "<html" in head:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
    return [ln.strip() for ln in text.splitlines() if ln.strip()][:MAX_ITEMS]


def _fetch(url):
    try:
        req = urllib.request.Request(sheet_csv_url(url), headers={"User-Agent": "Playout ticker"})
        with certs.urlopen(req, timeout=15) as r:
            body = r.read(2_000_000)
            ctype = r.headers.get("Content-Type", "")
        items = parse(body, ctype.lower())
        with _lock:
            _cache[url].update(text="\n".join(items), error="", at=time.time())
    except Exception as e:
        logging.info("ticker feed %s: %s", url, e)
        with _lock:
            _cache[url].update(error=str(e), at=time.time())
    finally:
        with _lock:
            _cache[url]["busy"] = False


def get_text(url, refresh=REFRESH):
    """The feed's items, one per line (the last good read). Starts a read in the background when it is due."""
    with _lock:
        c = _cache.setdefault(url, {"text": "", "at": 0.0, "busy": False, "error": ""})
        due = not c["busy"] and time.time() - c["at"] >= refresh
        if due:
            c["busy"] = True
        text = c["text"]
    if due:
        threading.Thread(target=_fetch, args=(url,), daemon=True, name="ticker feed").start()
    return text


def status(url):
    with _lock:
        c = _cache.get(url) or {}
        return {"items": len([x for x in (c.get("text") or "").splitlines() if x]), "error": c.get("error", "")}


def read_now(url):
    """Read the link now (Test button): -> (items, error)."""
    try:
        req = urllib.request.Request(sheet_csv_url(url), headers={"User-Agent": "Playout ticker"})
        with certs.urlopen(req, timeout=15) as r:
            items = parse(r.read(2_000_000), r.headers.get("Content-Type", "").lower())
        with _lock:
            _cache[url] = {"text": "\n".join(items), "at": time.time(), "busy": False, "error": ""}
        return items, ""
    except Exception as e:
        return [], str(e)
