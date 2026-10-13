"""Playlist import from Excel (.xlsx) or CSV, e.g. a traffic / sales department's daily schedule.

One row per item. Column titles (any order, upper or lower case):
    Time      fixed start "HH:MM" or "HH:MM:SS" (empty = plays in order)
    File      clip file name in the media folder (with or without .mp4)
    Name      name on screen (default: file name)
    Client    advertiser, for the ad report
    Type      "clip" (default) or "live"  (a live card, uses the main vMix input)
    Duration  live cards only: "mm:ss" (empty = until Next)
    In, Out   clip trim points "mm:ss" or seconds
A sheet with no title row is read as a plain list of file names.
"""
import csv
import datetime as dt
import io
import os

ALIASES = {
    "time": "time", "start": "time", "fixed": "time", "fixed start": "time", "air time": "time",
    "file": "file", "clip": "file", "media": "file", "filename": "file", "file name": "file", "spot": "file",
    "name": "name", "title": "name", "commercial": "name", "item": "name",
    "client": "client", "advertiser": "client", "customer": "client", "brand": "client",
    "type": "type", "duration": "duration", "length": "duration",
    "in": "in", "in point": "in", "mark in": "in", "out": "out", "out point": "out", "mark out": "out",
}


def _rows_from(name, data):
    ext = os.path.splitext(name)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if ext in (".csv", ".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [r for r in csv.reader(io.StringIO(text), dialect)]
    raise ValueError("Use an Excel .xlsx or a .csv file")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        return v.strftime("%H:%M:%S")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, dt.timedelta):
        s = int(v.total_seconds())
        return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _seconds(text):
    t = (text or "").strip()
    if not t:
        return None
    try:
        parts = [float(p) for p in t.replace(",", ".").split(":")]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _time(text):
    t = (text or "").strip().replace(".", ":")
    if not t:
        return ""
    try:
        f = float(t)            # Excel time stored as a day fraction
        if 0 <= f < 1:
            s = int(round(f * 86400))
            return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
    except ValueError:
        pass
    parts = t.split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return ""
    while len(nums) < 3:
        nums.append(0)
    h, m, s = nums[:3]
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return ""
    return f"{h:02d}:{m:02d}:{s:02d}"


def _match_file(want, files):
    """Media file for a name in the sheet: exact, then without extension, case-insensitive."""
    if not want:
        return None
    w = want.strip().replace("\\", "/").split("/")[-1].lower()
    lower = {f.lower(): f for f in files}
    if w in lower:
        return lower[w]
    stem = {os.path.splitext(f)[0].lower(): f for f in files}
    return stem.get(os.path.splitext(w)[0]) or stem.get(w)


def parse(name, data, media_files):
    """-> (items, warnings)"""
    rows = [[_cell(c) for c in r] for r in _rows_from(name, data)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], ["The file is empty"]
    head = [ALIASES.get(c.strip().lower()) for c in rows[0]]
    if any(head):
        cols = {k: i for i, k in reversed(list(enumerate(head))) if k}
        body = rows[1:]
    else:
        cols = {"file": 0}
        body = rows
    get = lambda r, k: r[cols[k]] if k in cols and cols[k] < len(r) else ""
    items, warn = [], []
    for n, r in enumerate(body, start=2 if any(head) else 1):
        kind = (get(r, "type") or "clip").strip().lower()
        fixed = _time(get(r, "time"))
        if kind.startswith("live"):
            dur = _seconds(get(r, "duration"))
            items.append({"type": "live", "name": get(r, "name") or "LIVE", "duration": dur, "fixed": fixed,
                          "source": "", "cgs": []})
            continue
        want = get(r, "file") or get(r, "name")
        if not want:
            continue
        f = _match_file(want, media_files)
        if not f:
            warn.append(f"Row {n}: '{want}' is not in the media folder")
            f = want if os.path.splitext(want)[1] else want + ".mp4"
        it = {"type": "clip", "file": f, "name": get(r, "name") or os.path.splitext(f)[0], "fixed": fixed,
              "client": get(r, "client")}
        tin, tout = _seconds(get(r, "in")), _seconds(get(r, "out"))
        if tin:
            it["in"] = tin
        if tout:
            it["out"] = tout
        items.append(it)
    return items, warn


def template_csv():
    rows = [["Time", "File", "Name", "Client", "Type", "Duration", "In", "Out"],
            ["", "", "Morning show", "", "live", "", "", ""],
            ["", "ad_softlogic.mp4", "Softlogic 30s", "Softlogic", "clip", "", "", ""],
            ["", "ad_dialog.mp4", "Dialog 15s", "Dialog", "clip", "", "00:02", "00:17"],
            ["08:00:00", "news_open.mp4", "News open", "", "clip", "", "", ""]]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8-sig")
