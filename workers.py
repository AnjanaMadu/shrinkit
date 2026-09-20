"""Background workers — keep UI responsive."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from PyQt6.QtCore import QThread, pyqtSignal

import adb
import ffmpeg
from ffmpeg import FfmpegError
from presets import Preset


class VideoLoadWorker(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def run(self):
        try:
            adb.get_single_device()
            self.loaded.emit(adb.query_videos())
        except Exception as e:  # noqa: BLE001 — surface to UI
            self.failed.emit(str(e))


class PipelineWorker(QThread):
    """Pull → compress → push → cleanup. Emits progress; supports cancel."""
    step = pyqtSignal(str)            # 'pull' | 'compress' | 'push' | 'cleanup' | 'done'
    compress_pct = pyqtSignal(int)
    log = pyqtSignal(str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, video: adb.Video, preset: Preset,
                 crf=None, max_height=None, audio_kbps=None, mute=False,
                 parent=None):
        super().__init__(parent)
        self.video = video
        self.preset = preset
        self.crf = crf
        self.max_height = max_height
        self.audio_kbps = audio_kbps
        self.mute = mute
        self._cancel = False
        self._proc: subprocess.Popen | None = None

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _check_cancel(self):
        if self._cancel:
            raise InterruptedError("Cancelled by user.")

    def run(self):
        tmpdir = tempfile.mkdtemp(prefix="shrinkit_")
        in_path = os.path.join(tmpdir, "in_" + safe_name(self.video.name))
        out_path = os.path.join(tmpdir, "out_compressed.mp4")
        try:
            # 1. pull
            self.step.emit("pull")
            self.log.emit(f"Copying from phone: {self.video.phone_path}")
            adb.pull(self.video.phone_path, in_path)
            self._check_cancel()

            # 2. compress
            self.step.emit("compress")
            cmd = ffmpeg.build_command(in_path, out_path, self.preset,
                                       self.crf, self.max_height,
                                       self.audio_kbps, self.mute)
            self.log.emit("Compressing: " + " ".join(cmd[:8]) + " …")
            total_ms = ffmpeg.probe_duration_ms(in_path)
            self._run_ffmpeg(cmd, total_ms)
            self._check_cancel()

            # 3. push back as new file
            self.step.emit("push")
            phone_dir = self.video.phone_path.rsplit("/", 1)[0] or "/sdcard/DCIM"
            base = self.video.name.rsplit(".", 1)[0]
            dest = adb.unique_push_path(phone_dir, base + "_compressed", ".mp4")
            self.log.emit(f"Copying back: {dest}")
            adb.push(out_path, dest)
            adb.trigger_media_scan(dest)
            self._check_cancel()

            self.step.emit("done")
            self.finished.emit({
                "dest": dest,
                "in_bytes": os.path.getsize(in_path) if os.path.exists(in_path) else self.video.size,
                "out_bytes": os.path.getsize(out_path) if os.path.exists(out_path) else 0,
            })
        except InterruptedError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e)[:600])
        finally:
            self.step.emit("cleanup")
            for p in (in_path, out_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            try:
                os.rmdir(tmpdir)
            except Exception:
                pass

    def _run_ffmpeg(self, cmd: list[str], total_ms: int):
        import threading
        from collections import deque
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            raise FfmpegError("ffmpeg not found in PATH.")
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        # Drain stderr on a side thread (avoids pipe deadlock) and keep a
        # tail for error messages. Needs -v error so this stays small.
        err_tail: deque[str] = deque(maxlen=30)
        def _drain():
            try:
                for line in self._proc.stderr:  # type: ignore[union-attr]
                    line = line.strip()
                    if line:
                        err_tail.append(line[-300:])
            except Exception:
                pass
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        # NOTE: despite the name, out_time_ms is MICROseconds.
        ms_re = re.compile(r"out_time_ms=(\d+)")
        last_pct = -1
        try:
            for line in self._proc.stdout:
                if self._cancel:
                    self._proc.kill()
                    raise InterruptedError("Cancelled by user.")
                s = line.strip()
                if s == "progress=end":
                    self.compress_pct.emit(100)
                    last_pct = 100
                    continue
                m = ms_re.search(s)
                if m and total_ms > 0:
                    try:
                        out_us = int(m.group(1))
                        pct = min(100, max(0, out_us // (total_ms * 10)))
                        if pct != last_pct:
                            last_pct = pct
                            self.compress_pct.emit(pct)
                    except ValueError:
                        pass
        finally:
            rc = self._proc.wait()
            self._proc = None
            t.join(timeout=5)
        if self._cancel:
            raise InterruptedError("Cancelled by user.")
        if rc != 0:
            detail = " ".join(err_tail)[-400:]
            msg = f"ffmpeg failed (exit {rc})."
            if detail:
                msg += f" {detail}"
            raise FfmpegError(msg)
        if last_pct != 100:
            self.compress_pct.emit(100)


def safe_name(name: str) -> str:
    keep = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    clean = "".join(c if c in keep else "_" for c in name).strip() or "video"
    if "." not in clean:
        clean += ".mp4"
    return clean[:80]
