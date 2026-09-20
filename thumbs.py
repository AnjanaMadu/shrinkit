"""Manual thumbnail fetching — pull video, extract frame, cache, cleanup.

Single worker thread, FIFO queue with dedupe. UI enqueues explicitly
(per-tile tap or 'load all'); nothing fetches on its own.
"""

from __future__ import annotations

import os
import tempfile
from collections import deque
from dataclasses import dataclass

from PyQt6.QtCore import QMutex, QThread, QWaitCondition, pyqtSignal

import adb
import ffmpeg
from cache import ThumbCache
from workers import safe_name


@dataclass
class ThumbJob:
    video: adb.Video
    key: str


class ThumbnailWorker(QThread):
    ready = pyqtSignal(str)          # thumb_key, now in cache
    failed = pyqtSignal(str, str)    # thumb_key, message
    started_job = pyqtSignal(str)    # thumb_key -> tile shows spinner

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self._mutex = QMutex()
        self._cond = QWaitCondition()
        self._queue: deque[ThumbJob] = deque()
        self._queued_keys: set[str] = set()
        self._stop = False

    def enqueue(self, videos: list[adb.Video]) -> int:
        """Queue videos missing from cache. Returns newly queued count."""
        added = 0
        self._mutex.lock()
        try:
            for v in videos:
                if self.cache.has(v.thumb_key) or v.thumb_key in self._queued_keys:
                    continue
                self._queue.append(ThumbJob(video=v, key=v.thumb_key))
                self._queued_keys.add(v.thumb_key)
                added += 1
        finally:
            self._mutex.unlock()
        if added:
            self._cond.wakeAll()
        return added

    def pending_count(self) -> int:
        self._mutex.lock()
        try:
            return len(self._queue)
        finally:
            self._mutex.unlock()

    def clear_pending(self) -> None:
        self._mutex.lock()
        try:
            self._queue.clear()
            self._queued_keys.clear()
        finally:
            self._mutex.unlock()

    def stop(self) -> None:
        self._mutex.lock()
        self._stop = True
        self._mutex.unlock()
        self._cond.wakeAll()
        self.wait(5000)

    def _should_stop(self) -> bool:
        self._mutex.lock()
        try:
            return self._stop
        finally:
            self._mutex.unlock()

    def _next(self) -> ThumbJob | None:
        self._mutex.lock()
        try:
            while not self._queue and not self._stop:
                self._cond.wait(self._mutex)
            if self._stop:
                return None
            job = self._queue.popleft()
            self._queued_keys.discard(job.key)
            return job
        finally:
            self._mutex.unlock()

    def run(self):
        while True:
            job = self._next()
            if job is None:
                return
            if self.cache.has(job.key):
                self.ready.emit(job.key)
                continue
            self.started_job.emit(job.key)
            tmpdir = tempfile.mkdtemp(prefix="shrinkit_thumb_")
            in_path = os.path.join(tmpdir, "in_" + safe_name(job.video.name))
            out_jpg = os.path.join(tmpdir, "thumb.jpg")
            try:
                adb.pull(job.video.phone_path, in_path)
                if self._should_stop():
                    return
                ffmpeg.extract_frame(in_path, out_jpg, job.video.duration_ms)
                self.cache.store_file(job.key, out_jpg)
                self.ready.emit(job.key)
            except Exception as e:  # noqa: BLE001 — per-tile error
                self.failed.emit(job.key, str(e)[:200])
            finally:
                for p in (in_path, out_jpg):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
                try:
                    os.rmdir(tmpdir)
                except OSError:
                    pass
