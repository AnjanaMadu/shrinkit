"""ADB wrapper for Shrinkit. Single USB device, MediaStore discovery."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass


ADB_EXE = "adb"


class AdbError(RuntimeError):
    pass


@dataclass
class Device:
    serial: str
    state: str
    detail: str = ""


@dataclass
class Video:
    phone_path: str
    name: str
    size: int = 0
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    modified: int = 0
    folder: str = ""  # friendly folder name, e.g. "Movies", "Camera"

    @property
    def thumb_key(self) -> str:
        import hashlib
        raw = f"{self.phone_path}|{self.size}|{self.modified}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


@dataclass
class Folder:
    name: str           # friendly display name
    dir: str            # phone directory, e.g. /storage/emulated/0/Movies
    videos: list[Video]
    total_bytes: int = 0
    total_ms: int = 0
    newest: int = 0

    @property
    def count(self) -> int:
        return len(self.videos)


def _friendly_folder(phone_dir: str) -> str:
    """Last meaningful path segment, prettified: 'WhatsApp Video', 'Camera'."""
    seg = phone_dir.rstrip("/").rsplit("/", 1)[-1] if phone_dir else ""
    seg = seg.replace("_", " ").replace("-", " ").strip()
    # keep well-known names tidy
    low = seg.lower()
    if low in ("camera",):
        return "Camera"
    if not seg:
        return "Videos"
    return " ".join(w.capitalize() for w in seg.split())


def group_by_folder(videos: list[Video]) -> list[Folder]:
    """Group videos by phone directory, newest-folder-first."""
    by_dir: dict[str, list[Video]] = {}
    for v in videos:
        d = v.phone_path.rsplit("/", 1)[0] if "/" in v.phone_path else ""
        by_dir.setdefault(d, []).append(v)
    folders: list[Folder] = []
    for d, vs in by_dir.items():
        vs.sort(key=lambda v: v.modified, reverse=True)
        folders.append(Folder(
            name=_friendly_folder(d), dir=d, videos=vs,
            total_bytes=sum(v.size for v in vs),
            total_ms=sum(v.duration_ms for v in vs),
            newest=max((v.modified for v in vs), default=0),
        ))
    folders.sort(key=lambda f: f.newest, reverse=True)
    return folders


def run_adb(args: list[str], timeout: int = 60) -> str:
    try:
        p = subprocess.run(
            [ADB_EXE, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise AdbError("adb not found in PATH. Install Android Platform Tools.")
    if p.returncode != 0:
        raise AdbError((p.stderr or p.stdout or "adb failed").strip()[:500])
    return p.stdout


def check_adb() -> tuple[bool, str]:
    try:
        out = run_adb(["version"], timeout=10)
        return True, out.strip().splitlines()[0] if out else "adb ok"
    except AdbError as e:
        return False, str(e)


def list_devices() -> list[Device]:
    out = run_adb(["devices", "-l"], timeout=10)
    devices: list[Device] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "daemon" in line.lower():
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append(Device(serial=parts[0], state=parts[1],
                                  detail=" ".join(parts[2:])))
    return devices


def get_single_device() -> Device:
    devices = [d for d in list_devices() if d.serial]
    ready = [d for d in devices if d.state == "device"]
    if not ready:
        if any(d.state == "unauthorized" for d in devices):
            raise AdbError("Device unauthorized — accept the USB debugging prompt on your phone.")
        if any(d.state == "offline" for d in devices):
            raise AdbError("Device offline — unplug and replug USB, then retry.")
        raise AdbError("No device found — connect via USB with USB debugging on.")
    return ready[0]


# Matches key=value pairs where value may contain spaces (paths).
_PAIR_RE = re.compile(r"(\w+)=((?:(?!\s\w+=).)*)", re.S)


def _parse_content_row(line: str) -> dict[str, str]:
    line = line.strip()
    if line.startswith("Row:"):
        line = line.split("Row:", 1)[1]
        line = re.sub(r"^\s*\d+\s*", "", line)
    return {m.group(1): m.group(2).strip().rstrip(",")
            for m in _PAIR_RE.finditer(line)}


def _shell_content_query(projection: str, sort: str | None) -> str:
    # NOTE: must be a SINGLE remote-shell arg so 'date_modified DESC' stays
    # quoted on-device. Passing --sort as separate argv breaks (adb joins
    # args with spaces and the device shell splits DESC into its own arg).
    cmd = f"content query --uri content://media/external/video/media --projection {projection}"
    if sort:
        cmd += f" --sort '{sort}'"
    return run_adb(["shell", cmd], timeout=60)


def query_videos() -> list[Video]:
    """Fast MediaStore query. No pull needed. Sorted newest-first in Python."""
    projection = "_data:_display_name:_size:duration:width:height:date_modified:relative_path"
    out = _shell_content_query(projection, "date_modified DESC")
    if "Row:" not in out:
        # fallback: some shells mangle quotes — retry without sort
        out = _shell_content_query(projection, None)
    videos: list[Video] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        f = _parse_content_row(line)
        path = f.get("_data", "").strip()
        if path in ("", "null", "NULL"):
            continue  # scoped-storage row without path; skip in v1
        try:
            size = int(float(f.get("_size", "0") or 0))
        except ValueError:
            size = 0
        if size <= 0:
            continue
        try:
            dur = int(float(f.get("duration", "0") or 0))
        except ValueError:
            dur = 0
        try:
            w = int(float(f.get("width", "0") or 0))
            h = int(float(f.get("height", "0") or 0))
        except ValueError:
            w = h = 0
        try:
            mod = int(float(f.get("date_modified", "0") or 0))
        except ValueError:
            mod = 0
        name = f.get("_display_name", "").strip() or os.path.basename(path)
        phone_dir = path.rsplit("/", 1)[0] if "/" in path else ""
        videos.append(Video(phone_path=path, name=name, size=size,
                            duration_ms=dur, width=w, height=h, modified=mod,
                            folder=_friendly_folder(phone_dir)))
    videos.sort(key=lambda v: v.modified, reverse=True)
    return videos


def phone_path_exists(phone_path: str) -> bool:
    # quote for shell; use single-quote escaping
    q = "'" + phone_path.replace("'", "'\"'\"'") + "'"
    out = run_adb(["shell", f"test -e {q} && echo YES || echo NO"], timeout=15)
    return "YES" in out


def unique_push_path(phone_dir: str, stem: str, ext: str) -> str:
    candidate = f"{phone_dir}/{stem}{ext}"
    i = 2
    while phone_path_exists(candidate):
        candidate = f"{phone_dir}/{stem}_{i}{ext}"
        i += 1
        if i > 50:
            break
    return candidate


def pull(phone_path: str, local_path: str) -> None:
    run_adb(["pull", phone_path, local_path], timeout=60 * 60)


def push(local_path: str, phone_path: str) -> None:
    run_adb(["push", local_path, phone_path], timeout=60 * 60)


def trigger_media_scan(phone_path: str) -> None:
    try:
        run_adb(["shell", "am", "broadcast",
                 "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                 "-d", f"file://{phone_path}"], timeout=15)
    except AdbError:
        pass  # non-fatal; Gallery will pick it up on reboot/rescan
