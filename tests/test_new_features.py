"""Tests for the on-air safety / automation / ad features (run: python -m unittest discover tests).

They need the packages in requirements.txt (NDI is not needed: nothing is sent)."""
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation  # noqa: E402
import feeds  # noqa: E402
import onair  # noqa: E402
import textcg  # noqa: E402


def frame(w, h, value):
    f = np.full((h, w, 4), value, np.uint8)
    f[..., 3] = 255
    return f.reshape(-1)


class DetectorTest(unittest.TestCase):
    def test_black_after_its_seconds(self):
        d = onair.Detector({"black_secs": 3, "freeze_secs": 0, "silence_secs": 0})
        black = onair.luma_sample(frame(320, 180, 0), 320, 180)
        self.assertEqual(d.feed(0.0, black, -10), [])
        self.assertEqual(d.feed(2.5, black, -10), [])
        self.assertEqual(d.feed(3.0, black, -10), [("black", True)])
        self.assertEqual(d.feed(3.5, black, -10), [])              # once, not every sample
        grey = onair.luma_sample(frame(320, 180, 128), 320, 180)
        self.assertEqual(d.feed(4.0, grey, -10), [("black", False)])

    def test_small_logo_over_black_is_still_black(self):
        f = frame(320, 180, 0).reshape(180, 320, 4)
        f[5:15, 280:310, :3] = 255                                  # a bug in the corner
        self.assertTrue(onair.is_black(onair.luma_sample(f.reshape(-1), 320, 180)))

    def test_freeze_and_moving_picture(self):
        d = onair.Detector({"black_secs": 0, "freeze_secs": 2, "silence_secs": 0})
        still = onair.luma_sample(frame(320, 180, 100), 320, 180)
        d.feed(0.0, still, -10)
        d.feed(1.0, still, -10)
        self.assertEqual(d.feed(2.0, still, -10), [])               # frozen since the 2nd sample (t=1)
        self.assertEqual(d.feed(3.0, still, -10), [("freeze", True)])
        rng = np.random.default_rng(1)
        moving = rng.integers(0, 255, still.shape).astype(np.float32)
        self.assertEqual(d.feed(3.5, moving, -10), [("freeze", False)])

    def test_silence_and_quiet_sources(self):
        d = onair.Detector({"black_secs": 0, "freeze_secs": 0, "silence_secs": 2, "silence_db": -50})
        img = onair.luma_sample(frame(320, 180, 100), 320, 180)
        d.feed(0.0, img, -80)
        self.assertEqual(d.feed(2.0, img, -80), [("silence", True)])
        self.assertEqual(d.feed(2.5, img, -80, quiet=True), [("silence", False)])   # bars / slate: no alarm
        self.assertEqual(d.feed(3.0, img, -80), [])

    def test_uyvy_sample(self):
        class U(np.ndarray):
            uyvy = True
        raw = np.zeros(320 * 180 * 2, np.uint8).view(U)
        raw.w, raw.h = 320, 180
        raw[1::2] = 16                                               # Y = 16 (video black)
        self.assertTrue(onair.is_black(onair.luma_sample(raw, 1920, 1080)))


class AutomationTest(unittest.TestCase):
    def test_expiry(self):
        m = automation.clean_expiry({"a.mp4": "2026-11-15", "b.mp4": "nonsense", "": "2026-01-01"})
        self.assertEqual(m, {"a.mp4": "2026-11-15"})
        self.assertTrue(automation.is_expired(m, "a.mp4", dt.date(2026, 11, 16)))
        self.assertFalse(automation.is_expired(m, "a.mp4", dt.date(2026, 11, 15)))   # its last day still plays
        self.assertFalse(automation.is_expired(m, "c.mp4", dt.date(2030, 1, 1)))

    def test_check_items(self):
        d = tempfile.mkdtemp()
        try:
            open(os.path.join(d, "here.mp4"), "w").close()
            open(os.path.join(d, "old.mp4"), "w").close()
            items = [{"type": "clip", "file": "here.mp4"}, {"type": "clip", "file": "gone.mp4"},
                     {"type": "clip", "file": "old.mp4"}, {"type": "live"}]
            r = automation.check_items(items, d, {"old.mp4": "2020-01-01"}, dt.date(2026, 1, 1))
            self.assertEqual(r, {"missing": ["gone.mp4"], "expired": ["old.mp4"], "clips": 3})
        finally:
            shutil.rmtree(d)

    def test_ad_conflicts_and_fix(self):
        ad = lambda i, c: {"id": str(i), "type": "clip", "name": f"ad{i}", "file": f"{i}.mp4", "client": c,
                           "category": "Commercial"}
        prog = {"id": "p", "type": "clip", "file": "p.mp4", "category": "Program", "name": "show"}
        items = [ad(1, "Dialog"), ad(2, "Dialog"), ad(3, "Mobitel"), prog, ad(4, "Keells"), ad(5, "Keells")]
        c = automation.ad_conflicts(items)
        self.assertEqual([x["index"] for x in c], [1, 5])
        fixed, left = automation.ad_fix(items)
        self.assertEqual(len(left), 1)                       # Keells + Keells alone in a break: cannot be split
        self.assertEqual([x["client"] for x in fixed[:3]], ["Dialog", "Mobitel", "Dialog"])
        self.assertEqual(fixed[3]["id"], "p")                # breaks stay where they are
        # a break with a fixed-start item is left as it is
        items[0]["fixed"] = "10:00:00"
        fixed, left = automation.ad_fix(items)
        self.assertEqual([x["id"] for x in fixed[:3]], ["1", "2", "3"])

    def test_pick_filler(self):
        dur = {"a": 30, "b": 10, "c": 0}
        self.assertEqual(automation.pick_filler(["a", "b", "c"], dur, 15), "b")
        self.assertIsNone(automation.pick_filler(["a"], dur, 15))
        self.assertEqual(automation.pick_filler(["a", "b"], dur, None, last="a"), "b")
        self.assertIsNone(automation.pick_filler(["c"], dur, None))       # no length known: not used

    def test_due_inserts(self):
        e = automation.clean_inserts([{"time": "19:00", "file": "news intro.mp4", "days": ["mon", "xx"]},
                                      {"time": "bad", "file": "x.mp4"}])
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["days"], ["mon"])
        mon = dt.datetime(2026, 11, 16, 18, 59, 0)          # a Monday
        due = automation.due_inserts(e, mon, set())
        self.assertEqual(len(due), 1)
        self.assertEqual(automation.due_inserts(e, mon, {due[0][1]}), [])
        self.assertEqual(automation.due_inserts(e, mon - dt.timedelta(minutes=5), set()), [])   # not yet
        self.assertEqual(automation.due_inserts(e, mon + dt.timedelta(days=1), set()), [])      # Tuesday


class FeedsTest(unittest.TestCase):
    def test_rss(self):
        rss = b"""<?xml version="1.0"?><rss><channel><title>Site</title>
        <item><title>Rain in Colombo</title></item><item><title> Gold price up </title></item></channel></rss>"""
        self.assertEqual(feeds.parse(rss), ["Rain in Colombo", "Gold price up"])

    def test_atom(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title><entry><title>One</title></entry></feed>"""
        self.assertEqual(feeds.parse(atom), ["One"])

    def test_csv_sheet(self):
        self.assertEqual(feeds.parse(b"Gold 22K,Rs. 185000\nUSD,Rs. 298\n,\n", "text/csv"),
                         ["Gold 22K - Rs. 185000", "USD - Rs. 298"])

    def test_text(self):
        self.assertEqual(feeds.parse("one\n\n two \n".encode()), ["one", "two"])

    def test_sheet_link(self):
        self.assertEqual(feeds.sheet_csv_url("https://docs.google.com/spreadsheets/d/AbC-1/edit#gid=42"),
                         "https://docs.google.com/spreadsheets/d/AbC-1/export?format=csv&gid=42")
        u = "https://docs.google.com/spreadsheets/d/e/X/pub?output=csv"
        self.assertEqual(feeds.sheet_csv_url(u), u)


class FieldsTest(unittest.TestCase):
    def test_fill(self):
        textcg.FIELDS = lambda: {"now": "News", "next": "Teledrama", "next_time": "20:30", "next_in": "5 min"}
        try:
            c = textcg.fill_fields({"line1": "Coming up next: {next}", "line2": "at {next_time} ({next_in})", "x": 1})
            self.assertEqual(c["line1"], "Coming up next: Teledrama")
            self.assertEqual(c["line2"], "at 20:30 (5 min)")
            plain = {"text": "no fields"}
            self.assertIs(textcg.fill_fields(plain), plain)
        finally:
            textcg.FIELDS = None


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


@unittest.skipUnless(_ffmpeg(), "needs ffmpeg")
class EngineTest(unittest.TestCase):
    """The engine without its output loop: items are started / finished by calling it directly."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        for name, secs in (("ad1.mp4", 2), ("ad2.mp4", 2), ("promo.mp4", 3), ("show.mp4", 4)):
            subprocess.run([_ffmpeg(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"testsrc=size=320x180:rate=25:duration={secs}", "-pix_fmt", "yuv420p",
                            os.path.join(cls.dir, name)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        import engine
        engine.Engine._new_output = lambda self: None          # no NDI sender in tests
        self.e = engine.Engine({"width": 320, "height": 180, "fps": "25"}, self.dir, os.path.join(self.dir, "logs"))

    def tearDown(self):
        with self.e._lock:
            self.e._finish("stopped")
            self.e._stop_to_live()

    def test_expired_commercial_is_skipped(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "ad1.mp4"}, {"type": "clip", "file": "ad2.mp4"}])
        e.expiry = {"ad1.mp4": "2020-01-01"}
        e.play()
        self.assertEqual(e._item(e.on_air)["file"], "ad2.mp4")
        rows = e.asrun.read(dt.date.today())
        self.assertTrue(any(r["status"].startswith("expired") for r in rows))

    def test_gap_filler_before_fixed_item(self):
        e = self.e
        at = (dt.datetime.now() + dt.timedelta(seconds=60)).strftime("%H:%M:%S")
        e.set_playlist([{"type": "clip", "file": "ad1.mp4"}, {"type": "clip", "file": "show.mp4", "fixed": at}])
        e.filler = automation.clean_filler({"enabled": True, "files": ["promo.mp4"], "before_fixed": True})
        e.play()
        with e._lock:
            e._item_done()
        cur = e._item(e.on_air)
        self.assertTrue(cur.get("filler"))
        self.assertEqual(cur["file"], "promo.mp4")
        self.assertEqual([x["file"] for x in e.playlist], ["ad1.mp4", "promo.mp4", "show.mp4"])
        with e._lock:                     # the next filler replaces the one that played
            e._item_done()
        self.assertEqual([x["file"] for x in e.playlist], ["ad1.mp4", "promo.mp4", "show.mp4"])
        self.assertEqual(sum(1 for x in e.playlist if x.get("filler")), 1)

    def test_no_filler_without_gap(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "ad1.mp4"}, {"type": "clip", "file": "show.mp4"}])
        e.filler = automation.clean_filler({"enabled": True, "files": ["promo.mp4"]})
        e.play()
        with e._lock:
            e._item_done()
        self.assertEqual(e._item(e.on_air)["file"], "show.mp4")

    def test_filler_at_end(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "ad1.mp4"}])
        e.filler = automation.clean_filler({"enabled": True, "files": ["promo.mp4"], "at_end": True})
        e.play()
        with e._lock:
            e._item_done()
        self.assertEqual(e._item(e.on_air)["file"], "promo.mp4")

    def test_breaking_news_and_back(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "show.mp4"}, {"type": "clip", "file": "ad1.mp4"}])
        e.play()
        show = e.on_air
        e.item_ticks = 50                                  # 2 s into the show
        card = e.breaking_on("", "BREAKING NEWS")
        self.assertEqual(e.on_air, card)
        self.assertEqual(e._item(card)["type"], "live")
        self.assertTrue(e.breaking_off(True))
        self.assertEqual(e.on_air, show)                   # the show carries on ...
        self.assertEqual(e.item_ticks, 50)                 # ... from where it was
        self.assertIsNone(e._item(card))                   # the card is gone from the playlist

    def test_slate(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "show.mp4"}])
        e.play()
        e.slate_take(True)
        self.assertEqual(e.state, "PAUSED")
        v, a = e._slate_frame(1920)
        self.assertEqual(np.asarray(v).size, 320 * 180 * 4)
        e.slate_take(False)
        self.assertEqual(e.state, "PLAYING")

    def test_up_next_skips_commercials(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "ad1.mp4"}, {"type": "clip", "file": "ad2.mp4"},
                        {"type": "clip", "file": "show.mp4", "category": "Program", "name": "Teledrama"}])
        e.play()
        u = e.up_next()
        self.assertEqual(u["next"], "Teledrama")
        self.assertTrue(u["next_time"])

    def test_daily_insert(self):
        e = self.e
        e.set_playlist([{"type": "clip", "file": "show.mp4"}, {"type": "clip", "file": "ad1.mp4"}])
        e.play()

        class S:
            data = {}
        ins = automation.DailyInserts(e, S())
        entry = automation.clean_inserts([{"time": "19:00", "file": "promo.mp4", "name": "News intro"}])[0]
        self.assertTrue(ins.insert(entry))
        self.assertEqual([x["file"] for x in e.playlist], ["show.mp4", "promo.mp4", "ad1.mp4"])
        self.assertEqual(e.playlist[1]["fixed"], "19:00:00")
        self.assertFalse(ins.insert(entry))                # not twice


@unittest.skipUnless(_ffmpeg(), "needs ffmpeg")
class ProofTest(unittest.TestCase):
    def test_proof_clip(self):
        import adproof
        d = tempfile.mkdtemp()
        try:
            class E:
                width, height, fps = 1280, 720, 25

                def _item(self, i):
                    return {"id": i, "name": "Soap ad", "client": "Unilever", "category": "Commercial", "duration": 2}
            p = adproof.AdProof(E(), d, _ffmpeg())
            p.configure({"enabled": True, "secs": 1})
            f = frame(1280, 720, 90)
            for _ in range(40):
                p.push(f, "CLIP:x1")
            p.push(f, "LIVE")
            deadline = time.time() + 20
            rows = []
            while time.time() < deadline:
                rows = p.list(dt.date.today(), dt.date.today())
                if rows and os.path.getsize(os.path.join(d, rows[0]["day"], rows[0]["clip"])) > 1000:
                    break
                time.sleep(0.3)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["client"], "Unilever")
            import io
            import zipfile
            buf = io.BytesIO()
            p.zip_to(buf, rows)
            names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
            self.assertIn("proof-list.csv", names)
            self.assertEqual(len(names), 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
