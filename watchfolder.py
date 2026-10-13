"""Watch folder: ad files dropped into a folder (or a network share) come into Playout by themselves.

Every few seconds new files in the folder are checked: once a file has stopped growing it is copied
(or moved) into the media folder, checked that it plays, its loudness is measured, and it can be
added to the end of the playlist. Files that do not play go into a "rejected" folder with a note.
"""
import os
import shutil
import threading
import time

from media import MEDIA_EXT, probe


class WatchFolder:
    def __init__(self, engine, media_dir, loudness=None):
        self.engine = engine
        self.media_dir = media_dir
        self.loudness = loudness
        self.folder = ""
        self.enabled = False
        self.move = False            # move instead of copy
        self.add_to_playlist = False
        self.log = []                # last imports, newest first
        self.error = ""
        self._seen = {}              # path -> (size, mtime, first seen stable time)
        threading.Thread(target=self._run, daemon=True, name="watch folder").start()

    def configure(self, enabled=None, folder=None, move=None, add_to_playlist=None):
        if folder is not None:
            self.folder = folder.strip().strip('"')
        if enabled is not None:
            self.enabled = bool(enabled)
        if move is not None:
            self.move = bool(move)
        if add_to_playlist is not None:
            self.add_to_playlist = bool(add_to_playlist)
        self.error = "" if (not self.enabled or os.path.isdir(self.folder)) else "Folder not found"
        return self.settings()

    def settings(self):
        return {"enabled": self.enabled, "folder": self.folder, "move": self.move,
                "add_to_playlist": self.add_to_playlist}

    def info(self):
        return {**self.settings(), "error": self.error, "log": self.log[:30]}

    def _note(self, text):
        self.log.insert(0, {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text})
        del self.log[100:]

    def _run(self):
        while True:
            time.sleep(4)
            if not self.enabled or not self.folder:
                continue
            try:
                self.scan()
                self.error = ""
            except Exception as e:
                self.error = str(e)

    def scan(self):
        if not os.path.isdir(self.folder):
            self.error = "Folder not found"
            return
        if os.path.abspath(self.folder) == os.path.abspath(self.media_dir):
            self.error = "The watch folder must not be the media folder itself"
            return
        now = time.time()
        for name in os.listdir(self.folder):
            src = os.path.join(self.folder, name)
            if not os.path.isfile(src) or os.path.splitext(name)[1].lower() not in MEDIA_EXT:
                continue
            st = os.stat(src)
            sig = (st.st_size, st.st_mtime)
            old = self._seen.get(src)
            if old is None or old[:2] != sig:
                self._seen[src] = (*sig, now)           # still being copied: wait until it stops growing
                continue
            if old[2] is None or now - old[2] < 6:
                continue
            self._seen[src] = (*sig, None)
            self._import(src, name)

    def _import(self, src, name):
        dest = os.path.join(self.media_dir, name)
        if os.path.exists(dest):
            if os.path.getsize(dest) == os.path.getsize(src):
                if self.move:
                    os.remove(src)
                return
            base, ext = os.path.splitext(name)
            i = 2
            while os.path.exists(os.path.join(self.media_dir, f"{base}_{i}{ext}")):
                i += 1
            name = f"{base}_{i}{ext}"
            dest = os.path.join(self.media_dir, name)
        tmp = dest + ".part"
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
        dur = probe(dest)
        if not dur:
            bad = os.path.join(self.folder, "rejected")
            os.makedirs(bad, exist_ok=True)
            os.remove(dest)
            shutil.move(src, os.path.join(bad, os.path.basename(src)))
            with open(os.path.join(bad, os.path.basename(src) + ".txt"), "w", encoding="utf-8") as f:
                f.write("Playout could not read this file (damaged or not a video).\n")
            self._note(f"REJECTED {name}: does not play")
            return
        if self.move:
            try:
                os.remove(src)
            except OSError:
                pass
        if self.loudness is not None:
            self.loudness.request(dest)
        text = f"Imported {name} ({dur:.1f}s)"
        if self.add_to_playlist:
            self.engine.set_playlist(self.engine.playlist + [{"type": "clip", "file": name, "fixed": ""}])
            text += ", added to the playlist"
        self._note(text)
        self.engine.message = "Watch folder: " + text
