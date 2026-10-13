"""Ad booking (traffic): what each client bought, against what really went on air.

A booking (saved in data/bookings.json):
    id, client, campaign
    match        commercial name(s) or file name(s), comma separated (empty = every spot of the client)
    start, end   "YYYY-MM-DD" (inclusive)
    per_day      spots booked per day
    total        spots booked in all (0 = per_day x days)
    rate         price per spot
    notes
Aired spots come from the air logs (client + commercial / file), so the numbers are proof of play.
"""
import datetime as dt
import json
import os
import threading
import uuid

from report import AIRED, _days, is_commercial


class Bookings:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "bookings.json")
        self._lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = json.load(f)
        except Exception:
            self.items = []

    def save(self, items):
        clean = []
        for b in items:
            if not (b.get("client") or "").strip():
                continue
            try:
                start = dt.date.fromisoformat(b.get("start"))
                end = dt.date.fromisoformat(b.get("end") or b.get("start"))
            except (TypeError, ValueError):
                continue
            clean.append({"id": b.get("id") or uuid.uuid4().hex[:8], "client": b["client"].strip(),
                          "campaign": (b.get("campaign") or "").strip(), "match": (b.get("match") or "").strip(),
                          "start": start.isoformat(), "end": max(start, end).isoformat(),
                          "per_day": max(0, int(float(b.get("per_day") or 0))),
                          "total": max(0, int(float(b.get("total") or 0))),
                          "rate": max(0.0, float(b.get("rate") or 0)), "notes": (b.get("notes") or "").strip()})
        with self._lock:
            self.items = clean
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=1)
            os.replace(tmp, self.path)
        return clean

    @staticmethod
    def _matches(b, row):
        if (row.get("client") or "").strip().lower() != b["client"].lower():
            return False
        if not b["match"]:
            return True
        name = (row.get("item") or "").lower()
        file = os.path.splitext(row.get("file") or "")[0].lower()
        return any(m.strip().lower() in (name, file) or (m.strip() and m.strip().lower() in name)
                   for m in b["match"].split(","))

    def booked_for(self, b, d1, d2):
        """Spots booked between two dates (inside the booking's own dates)."""
        s, e = dt.date.fromisoformat(b["start"]), dt.date.fromisoformat(b["end"])
        a, z = max(s, d1), min(e, d2)
        if a > z:
            return 0
        days = (z - a).days + 1
        if b["per_day"]:
            return b["per_day"] * days
        return b["total"] if (a == s and z == e) else round(b["total"] * days / ((e - s).days + 1))

    def status(self, asrun, d1=None, d2=None, today=None):
        """Per booking: booked / aired in the period, aired today, aired in all, remaining, amount."""
        today = today or dt.date.today()
        out = []
        cache = {}

        def rows(day):
            if day not in cache:
                cache[day] = [r for r in asrun.read(day) if r.get("type") == "clip" and is_commercial(r)
                              and (r.get("status") or "").startswith(AIRED)]
            return cache[day]

        for b in self.items:
            s, e = dt.date.fromisoformat(b["start"]), dt.date.fromisoformat(b["end"])
            p1, p2 = d1 or s, d2 or e
            total_booked = b["total"] or b["per_day"] * ((e - s).days + 1)
            aired_all = aired_period = aired_today = 0
            for day in _days(s, min(e, today)):
                n = sum(1 for r in rows(day) if self._matches(b, r))
                aired_all += n
                if p1 <= day <= p2:
                    aired_period += n
                if day == today:
                    aired_today += n
            booked_today = b["per_day"] if (s <= today <= e and b["per_day"]) else 0
            out.append({**b, "booked_total": total_booked, "aired_total": aired_all,
                        "remaining": max(0, total_booked - aired_all),
                        "booked_period": self.booked_for(b, p1, p2), "aired_period": aired_period,
                        "booked_today": booked_today, "aired_today": aired_today,
                        "amount_period": round(aired_period * b["rate"], 2),
                        "active": s <= today <= e})
        return out

    def in_playlist(self, playlist):
        """Spots of each booking in the current playlist (to check today's playlist has enough)."""
        out = {}
        for b in self.items:
            out[b["id"]] = sum(1 for it in playlist if it.get("type") == "clip" and is_commercial(it) and self._matches(
                b, {"client": it.get("client"), "item": it.get("name"), "file": it.get("file")}))
        return out


def invoice_pdf(status_rows, client, d1, d2, station, number, currency="LKR"):
    """Invoice for one client and period: aired spots x rate per booking."""
    from fpdf import FPDF

    def clean(s):
        return str(s if s is not None else "").encode("latin-1", "replace").decode("latin-1")

    rows = [r for r in status_rows if r["client"].lower() == client.lower()]
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, clean(station or "Playout"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "INVOICE - commercial air time", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 6, clean(f"Invoice no: {number}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, clean(f"Date: {dt.date.today():%d %b %Y}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, clean(f"Client: {client}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, clean(f"Period: {d1:%d %b %Y} - {d2:%d %b %Y}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    widths = [58, 30, 20, 20, 28, 30]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(43, 46, 50)
    pdf.set_text_color(255)
    for h, w in zip(["Campaign / commercial", "Dates", "Booked", "Aired", f"Rate ({currency})",
                     f"Amount ({currency})"], widths):
        pdf.cell(w, 7, h, fill=True)
    pdf.ln()
    pdf.set_text_color(0)
    pdf.set_font("Helvetica", size=9)
    total = 0.0
    for i, r in enumerate(rows):
        amount = r["aired_period"] * r["rate"]
        total += amount
        name = r["campaign"] or r["match"] or "All spots"
        cells = [name, f"{r['start'][5:]} - {r['end'][5:]}", r["booked_period"], r["aired_period"],
                 f"{r['rate']:,.2f}", f"{amount:,.2f}"]
        for v, w in zip(cells, widths):
            txt = clean(v)
            while txt and pdf.get_string_width(txt) > w - 1.5:
                txt = txt[:-1]
            pdf.cell(w, 6.5, txt, fill=i % 2 == 1)
        pdf.ln()
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(sum(widths[:5]), 9, "Total", align="R")
    pdf.cell(widths[5], 9, clean(f"{total:,.2f}"))
    pdf.ln(14)
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(90)
    pdf.multi_cell(0, 5, "Aired spots are counted from the playout air log (proof of play). "
                         "The ad report for the same period lists every spot with its air time.")
    return bytes(pdf.output())
