"""Playlist automation and checks.

- check_items: a playlist's clips that are missing from the media folder or past their end date
  (used for "tomorrow's playlist" the day before, and the Check button in Day Schedule).
- Clip end dates (expiry): a commercial whose campaign is over is not played (skipped, air log "expired").
- Ad separation: two commercials of the same client (or the same clip) one after the other in a break.
  ad_conflicts lists them, ad_fix moves them apart inside the same break when it can.
- Gap filler: picks filler clips (promos / station IDs) for the time before a fixed-start item or after
  the end of the playlist, so the screen does not go to black / live without a plan.
- Daily inserts: a clip that goes into the playlist at the same time every day (or on chosen weekdays),
  e.g. the 7.00 news intro or a daily sponsor spot.
"""
import datetime as dt
import logging
import os
import random
import secrets
import threading
import time

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---- clip end dates ----------------------------------------------------------------
def parse_day(v):
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def is_expired(expiry, file, day=None):
    """expiry: {file name: "YYYY-MM-DD" last day it may play}."""
    last = parse_day((expiry or {}).get(file))
    return last is not None and (day or dt.date.today()) > last


def clean_expiry(m):
    out = {}
    for k, v in (m or {}).items():
        d = parse_day(v)
        if k and d:
            out[os.path.basename(str(k))] = d.isoformat()
    return out


# ---- a playlist's files ---------------------------------------------------------------
def check_items(items, media_dir, expiry=None, day=None):
    """-> {"missing": [files], "expired": [files], "clips": n}"""
    missing, expired, n = [], [], 0
    for it in items or []:
        if it.get("type") != "clip" or not it.get("file"):
            continue
        n += 1
        f = it["file"]
        if not os.path.isfile(os.path.join(media_dir, f)):
            if f not in missing:
                missing.append(f)
        elif is_expired(expiry, f, day) and f not in expired:
            expired.append(f)
    return {"missing": missing, "expired": expired, "clips": n}


# ---- ad separation ------------------------------------------------------------------
def _is_ad(it):
    return it.get("type") == "clip" and (it.get("category") or "Commercial") == "Commercial"


def _key(it):
    c = (it.get("client") or "").strip().lower()
    return ("client", c) if c else ("file", (it.get("file") or "").lower())


def breaks(items):
    """Runs of commercials one after the other: [(start index, end index exclusive)]."""
    out, i = [], 0
    while i < len(items):
        if _is_ad(items[i]):
            j = i
            while j < len(items) and _is_ad(items[j]):
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def ad_conflicts(items):
    """Commercials of the same client (or the same clip) back to back: [{index, name, other, why}]."""
    out = []
    for a, b in breaks(items):
        for i in range(a + 1, b):
            if _key(items[i]) == _key(items[i - 1]):
                k = _key(items[i])
                out.append({"index": i, "id": items[i].get("id"), "name": items[i].get("name"),
                            "other": items[i - 1].get("name"),
                            "why": f"same client: {items[i].get('client')}" if k[0] == "client" else "same clip twice"})
    return out


def _spread(group, fixed_first=None):
    """Order a break so that no two neighbours share a client, when that is possible (most frequent first)."""
    left = list(group)
    out = []
    last = fixed_first
    while left:
        counts = {}
        for it in left:
            counts[_key(it)] = counts.get(_key(it), 0) + 1
        pick = None
        for it in sorted(left, key=lambda x: -counts[_key(x)]):
            if last is None or _key(it) != last:
                pick = it
                break
        if pick is None:          # only the same client is left: it has to go here
            pick = left[0]
        left.remove(pick)
        out.append(pick)
        last = _key(pick)
    return out


def ad_fix(items, keep=()):
    """Reorder commercials inside each break so the same client is not back to back.
    Items in keep (on air / fixed start) stay where they are. -> (new items, conflicts left)"""
    items = list(items)
    for a, b in breaks(items):
        group = items[a:b]
        if any(it.get("id") in keep or it.get("fixed") for it in group):
            continue
        if not any(_key(group[i]) == _key(group[i - 1]) for i in range(1, len(group))):
            continue
        items[a:b] = _spread(group, _key(items[a - 1]) if a and _is_ad(items[a - 1]) else None)
    return items, ad_conflicts(items)


# ---- gap filler ----------------------------------------------------------------------
FILLER_DEFAULTS = {"enabled": False, "files": [], "before_fixed": True, "at_end": False, "min_gap": 5}


def clean_filler(s):
    s = s or {}
    out = dict(FILLER_DEFAULTS)
    out["enabled"] = bool(s.get("enabled", False))
    out["files"] = [os.path.basename(str(f)) for f in (s.get("files") or []) if f][:200]
    out["before_fixed"] = bool(s.get("before_fixed", True))
    out["at_end"] = bool(s.get("at_end", False))
    try:
        out["min_gap"] = max(1, min(600, int(float(s.get("min_gap", 5)))))
    except (TypeError, ValueError):
        pass
    return out


def pick_filler(files, durations, gap, last=None, rng=random):
    """A filler clip for a gap of `gap` seconds (None = no end, e.g. after the playlist).
    durations: {file: seconds}. Prefers one that fits the gap, not the one just played."""
    have = [f for f in files if durations.get(f)]
    if not have:
        return None
    pool = [f for f in have if f != last] or have
    if gap is None:
        return rng.choice(pool)
    fits = [f for f in pool if durations[f] <= gap + 0.5]
    if fits:
        return rng.choice(fits)
    return None            # nothing short enough: the fixed item comes on air at its time anyway


# ---- daily inserts --------------------------------------------------------------------
LEAD = 120        # seconds before its time the insert is put into the playlist (shows on screen)


def clean_inserts(entries):
    out = []
    for e in entries or []:
        try:
            t = dt.time.fromisoformat(str(e.get("time") or "")[:8])
        except ValueError:
            continue
        if not e.get("file"):
            continue
        out.append({"id": e.get("id") or secrets.token_hex(4), "time": t.strftime("%H:%M:%S"),
                    "file": os.path.basename(str(e["file"])), "name": str(e.get("name") or "")[:80],
                    "days": [d for d in (e.get("days") or []) if d in DAYS],
                    "category": (str(e.get("category") or "Program").strip()[:30] or "Program"),
                    "exact": bool(e.get("exact", True)), "enabled": bool(e.get("enabled", True))})
    return out


def due_inserts(entries, now, done, lead=LEAD):
    """Inserts whose time is within `lead` seconds (and not done today): [(entry, key)]."""
    out = []
    for e in entries:
        if not e.get("enabled", True):
            continue
        if e.get("days") and DAYS[now.weekday()] not in e["days"]:
            continue
        try:
            at = dt.datetime.combine(now.date(), dt.time.fromisoformat(e["time"]))
        except ValueError:
            continue
        key = f"{e['id']}|{now.date().isoformat()}"
        left = (at - now).total_seconds()
        if key not in done and -20 <= left <= lead:
            out.append((e, key))
    return out


class DailyInserts:
    """Puts each daily insert into the playlist right after the on-air item, LEAD seconds before its time.
    "Exact" inserts carry a fixed start (the scheduler cuts to them at that second); others play after the
    on-air item ends."""

    def __init__(self, engine, store):
        self.engine = engine
        self.store = store
        self._done = set()
        self.active = lambda: True       # off on a backup PC

    @property
    def entries(self):
        return self.store.data.get("daily_inserts") or []

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="daily inserts").start()

    def _run(self):
        while True:
            time.sleep(1)
            try:
                if self.active():
                    self.check(dt.datetime.now())
            except Exception as e:
                logging.info("daily inserts: %s", e)

    def check(self, now):
        for e, key in due_inserts(self.entries, now, self._done):
            self._done.add(key)
            self.insert(e, now)
        if len(self._done) > 2000:
            self._done = {k for k in self._done if k.endswith(now.date().isoformat())}

    def insert(self, e, now=None):
        eng = self.engine
        item = {"type": "clip", "file": e["file"], "name": e.get("name") or os.path.splitext(e["file"])[0],
                "category": e.get("category") or "Program", "insert": e["id"], "fixed": e["time"] if e.get("exact", True) else ""}
        with eng._lock:
            items = [dict(x) for x in eng.playlist]
            # already there today (e.g. it was in the day's playlist): leave it
            if any(x.get("insert") == e["id"] and x["id"] not in eng.played for x in items):
                return False
            i = eng._index(eng.on_air)
            pos = (i + 1) if i is not None else (eng._index(eng.cued) if eng.cued else len(items))
            items.insert(pos if pos is not None else len(items), item)
            eng.set_playlist(items)
            eng.message = f"Daily insert added: {item['name']} at {e['time'][:5]}"
        return True
