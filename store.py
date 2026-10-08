"""Saved state (data/state.json) and named playlists (data/playlists/*.json)."""
import json
import os
import re
import threading

DEFAULT = {"live_source": "", "playlist": [], "auto": True, "loop": False}


class Store:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "state.json")
        self.lists_dir = os.path.join(data_dir, "playlists")
        os.makedirs(self.lists_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.data = dict(DEFAULT)
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def set(self, **kw):
        self.data.update(kw)
        self.save()

    # named playlists
    def _list_path(self, name):
        safe = re.sub(r"[^\w\- ]", "_", name).strip() or "playlist"
        return os.path.join(self.lists_dir, safe + ".json")

    def list_names(self):
        return sorted(os.path.splitext(n)[0] for n in os.listdir(self.lists_dir) if n.endswith(".json"))

    def save_list(self, name, items):
        with open(self._list_path(name), "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)

    def load_list(self, name):
        with open(self._list_path(name), encoding="utf-8") as f:
            return json.load(f)

    def delete_list(self, name):
        p = self._list_path(name)
        if os.path.exists(p):
            os.remove(p)
