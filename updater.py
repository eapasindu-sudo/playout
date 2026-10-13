"""Auto-update: checks GitHub Releases for a newer Playout-Setup.exe, downloads and installs it.

Every build on GitHub publishes a release "v1.0.<build>" with Playout-Setup.exe. Playout checks
at start (and every 6 hours), shows "Update available" and installs only when the operator presses
Install (never by itself while on air). A private repository needs a read-only access token.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from version import VERSION

DEFAULT_REPO = "eapasindu-sudo/playout"
ASSET = "Playout-Setup.exe"


def _ver(v):
    out = []
    for p in str(v or "0").lstrip("vV").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out + [0] * (3 - len(out)))


class Updater:
    def __init__(self):
        self.repo = DEFAULT_REPO
        self.token = ""
        self.auto_check = True
        self.current = VERSION
        self.latest = None
        self.notes = ""
        self.asset_url = None
        self.size = 0
        self.checked = None
        self.error = ""
        self.state = "idle"        # idle | checking | downloading | ready | installing
        self.progress = 0
        self.file = None
        self.notice = ""           # result of the last install, shown once after the restart
        self.on_install = None     # app callback: shut down so the installer can replace files
        self._lock = threading.Lock()

    def configure(self, repo=None, token=None, auto_check=None):
        if repo:
            self.repo = repo.strip().strip("/").replace("https://github.com/", "")
        if token is not None and token != "********":
            self.token = token.strip()
        if auto_check is not None:
            self.auto_check = bool(auto_check)
        return self.info()

    def settings(self):
        return {"repo": self.repo, "token": self.token, "auto_check": self.auto_check}

    def info(self):
        return {"current": self.current, "latest": self.latest, "available": self.available,
                "notes": self.notes, "checked": self.checked, "error": self.error, "state": self.state,
                "progress": self.progress, "repo": self.repo, "token_set": bool(self.token),
                "auto_check": self.auto_check, "can_install": sys.platform == "win32", "notice": self.notice}

    @property
    def available(self):
        return bool(self.latest and _ver(self.latest) > _ver(self.current) and self.asset_url)

    def _req(self, url, accept="application/vnd.github+json"):
        r = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Playout-Updater"})
        if self.token:
            r.add_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(r, timeout=30)

    def check(self):
        with self._lock:
            if self.state in ("downloading", "installing"):
                return self.info()
            self.state = "checking"
        try:
            with self._req(f"https://api.github.com/repos/{self.repo}/releases/latest") as r:
                rel = json.load(r)
            self.latest = (rel.get("tag_name") or "").lstrip("vV")
            self.notes = (rel.get("body") or "")[:2000]
            asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
            self.asset_url = asset.get("url") if asset else None
            self.size = asset.get("size", 0) if asset else 0
            self.error = "" if asset else "The latest release has no Playout-Setup.exe"
        except urllib.error.HTTPError as e:
            self.error = ("Update server not reachable: the repository is private, add an access token in Settings"
                          if e.code == 404 and not self.token else f"Update check failed: HTTP {e.code}")
        except Exception as e:
            self.error = f"Update check failed (no internet?): {e}"
        self.checked = time.time()
        self.state = "ready" if self.file and os.path.isfile(self.file) and self.available else "idle"
        return self.info()

    def start_auto(self):
        def loop():
            time.sleep(20)
            while True:
                if self.auto_check:
                    self.check()
                time.sleep(6 * 3600)
        threading.Thread(target=loop, daemon=True, name="update check").start()

    def download(self):
        if not self.available or self.state in ("downloading", "installing"):
            return self.info()
        self.state, self.progress, self.error = "downloading", 0, ""
        threading.Thread(target=self._download, daemon=True, name="update download").start()
        return self.info()

    def _download(self):
        dest = os.path.join(tempfile.gettempdir(), f"Playout-Setup-{self.latest}.exe")
        try:
            with self._req(self.asset_url, accept="application/octet-stream") as r, open(dest + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or self.size or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    self.progress = int(100 * got / total) if total else 0
            os.replace(dest + ".part", dest)
            self.file = dest
            self.progress = 100
            self.state = "ready"
        except Exception as e:
            self.error = f"Download failed: {e}"
            self.state = "idle"

    def install(self):
        """Hand over to update.ps1 (next to Playout.exe), which runs on its own: it waits for Playout to
        close, stops what still runs from the install folder, installs silently, and starts Playout again
        (also when the install did not work). Windows asks the admin Yes / No question for the install."""
        if self.state != "ready" or not self.file or not os.path.isfile(self.file):
            return {"ok": False, "error": "Download the update first"}
        if sys.platform != "win32":
            return {"ok": False, "error": "Install works on Windows only"}
        app_dir = os.path.dirname(sys.executable)
        script = os.path.join(app_dir, "update.ps1")
        if not os.path.isfile(script):
            return {"ok": False, "error": "update.ps1 is missing: install this version by hand once"}
        with open(REQUEST_FILE, "w", encoding="utf-8") as f:
            json.dump({"setup": self.file, "pid": os.getpid(), "version": self.latest}, f)
        args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-File", script]
        flags = 0x00000008 | 0x00000200 | 0x08000000      # detached, own process group, no window
        try:                                              # also break out of a job Playout may be in
            subprocess.Popen(args, close_fds=True, creationflags=flags | 0x01000000)
        except OSError:
            try:
                subprocess.Popen(args, close_fds=True, creationflags=flags)
            except OSError as e:
                return {"ok": False, "error": f"Could not start the installer: {e}"}
        self.state = "installing"
        if self.on_install:
            threading.Timer(1.5, self.on_install).start()
        return {"ok": True}

    def last_result(self):
        """What happened to the last update (read once, at start)."""
        try:
            with open(RESULT_FILE, encoding="utf-8-sig") as f:
                text = f.read().strip()
            os.remove(RESULT_FILE)
        except OSError:
            return
        if not text:
            return
        code, _, want = text.partition(" ")
        if code != "0" or (want and _ver(want) > _ver(self.current)):
            why = {"97": "the Windows admin question was answered No", "98": "the installer file was not found"}
            self.notice = (f"The last update ({want or '?'}) did not install "
                           f"({why.get(code, f'setup code {code}')}). Details: {SETUP_LOG}")
        else:
            self.notice = f"Updated to {self.current}."
        t = threading.Timer(300, lambda: setattr(self, "notice", ""))     # shown after the restart only
        t.daemon = True
        t.start()


REQUEST_FILE = os.path.join(tempfile.gettempdir(), "endora-update-request.json")
RESULT_FILE = os.path.join(tempfile.gettempdir(), "endora-update-result.txt")
SETUP_LOG = os.path.join(tempfile.gettempdir(), "endora-update-setup.log")
