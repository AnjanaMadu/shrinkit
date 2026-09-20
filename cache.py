"""Persistent thumbnail cache for Shrinkit.

Key = sha1(phone_path|size|modified) so a changed phone file re-fetches.
Disk dir: QStandardPaths CacheLocation / Shrinkit / thumbs, ~300MB LRU cap.
Memory: small dict LRU of QPixmap (fast repaint while scrolling).
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtGui import QPixmap

MAX_DISK_BYTES = 300 * 1024 * 1024
MAX_MEM_ITEMS = 200


def cache_dir() -> str:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "Shrinkit", "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


class ThumbCache:
    def __init__(self, disk_dir: str | None = None):
        self.disk_dir = disk_dir or cache_dir()
        os.makedirs(self.disk_dir, exist_ok=True)
        self._mem: OrderedDict[str, QPixmap] = OrderedDict()
        self._evict_if_needed()

    def path_for(self, key: str) -> str:
        return os.path.join(self.disk_dir, key + ".jpg")

    def has(self, key: str) -> bool:
        if key in self._mem:
            return True
        return os.path.exists(self.path_for(key))

    def get(self, key: str) -> QPixmap | None:
        pm = self._mem.get(key)
        if pm is not None:
            self._mem.move_to_end(key)
            return pm
        p = self.path_for(key)
        if not os.path.exists(p):
            return None
        pm = QPixmap(p)
        if pm.isNull():
            return None
        self._put_mem(key, pm)
        try:
            os.utime(p, None)  # touch for LRU
        except OSError:
            pass
        return pm

    def store_file(self, key: str, src_jpg: str) -> str:
        """Move an extracted jpg into the cache. Returns cached path."""
        import shutil
        dest = self.path_for(key)
        try:
            shutil.move(src_jpg, dest)
        except OSError:
            try:
                shutil.copyfile(src_jpg, dest)
            except OSError:
                return src_jpg
        self._mem.pop(key, None)
        self._evict_if_needed()
        return dest

    def _put_mem(self, key: str, pm: QPixmap) -> None:
        self._mem[key] = pm
        self._mem.move_to_end(key)
        while len(self._mem) > MAX_MEM_ITEMS:
            self._mem.popitem(last=False)

    def _evict_if_needed(self) -> None:
        try:
            files = [(os.path.join(self.disk_dir, f), os.path.getmtime(os.path.join(self.disk_dir, f)))
                     for f in os.listdir(self.disk_dir) if f.endswith(".jpg")]
        except OSError:
            return
        total = 0
        sizes: list[tuple[str, float, int]] = []
        for p, mt in files:
            try:
                s = os.path.getsize(p)
            except OSError:
                continue
            total += s
            sizes.append((p, mt, s))
        if total <= MAX_DISK_BYTES:
            return
        sizes.sort(key=lambda t: t[1])  # oldest first
        for p, _mt, s in sizes:
            try:
                os.remove(p)
            except OSError:
                pass
            total -= s
            if total <= MAX_DISK_BYTES:
                break

    def disk_usage(self) -> int:
        total = 0
        try:
            for f in os.listdir(self.disk_dir):
                if f.endswith(".jpg"):
                    try:
                        total += os.path.getsize(os.path.join(self.disk_dir, f))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def clear(self) -> None:
        self._mem.clear()
        try:
            for f in os.listdir(self.disk_dir):
                if f.endswith(".jpg"):
                    try:
                        os.remove(os.path.join(self.disk_dir, f))
                    except OSError:
                        pass
        except OSError:
            pass
