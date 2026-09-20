"""ffmpeg helpers for Shrinkit."""

from __future__ import annotations

import re
import subprocess

from presets import Preset

FFMPEG_EXE = "ffmpeg"
FFPROBE_EXE = "ffprobe"


class FfmpegError(RuntimeError):
    pass


def check_ffmpeg() -> tuple[bool, str]:
    try:
        p = subprocess.run([FFMPEG_EXE, "-version"], capture_output=True,
                           text=True, timeout=10)
    except FileNotFoundError:
        return False, "ffmpeg not found in PATH. Install it from ffmpeg.org."
    if p.returncode != 0:
        return False, "ffmpeg failed to run."
    return True, p.stdout.strip().splitlines()[0][:80] if p.stdout else "ffmpeg ok"


def build_command(in_path: str, out_path: str, preset: Preset,
                  crf: int | None = None, max_height: int | None = None,
                  audio_kbps: int | None = None,
                  mute: bool = False) -> list[str]:
    crf = preset.crf if crf is None else crf
    maxh = preset.max_height if max_height is None else max_height
    ab = preset.audio_kbps if audio_kbps is None else audio_kbps
    vcodec = preset.vcodec
    # High-quality lanczos downscale, never upscale.
    vf = f"scale=-2:'min(ih,{maxh})':flags=lanczos" if maxh and maxh > 0 else None
    cmd = [FFMPEG_EXE, "-hide_banner", "-v", "error", "-y", "-i", in_path,
           "-map_metadata", "0",
           "-vcodec", vcodec, "-crf", str(crf),
           "-preset", preset.ffmpeg_preset,
           "-pix_fmt", "yuv420p",
           "-movflags", "+faststart"]
    # hvc1 tag = required for iPhone / QuickTime / many Android galleries
    # to recognise HEVC in MP4.
    if "265" in vcodec or "hevc" in vcodec.lower():
        cmd += ["-tag:v", "hvc1"]
    if vf:
        cmd += ["-vf", vf]
    if mute:
        cmd += ["-an"]
    else:
        cmd += ["-acodec", "aac", "-b:a", f"{ab}k", "-ar", "48000"]
    # NB: output options must precede the output file they apply to.
    cmd += ["-nostats", "-progress", "pipe:1", out_path]
    return cmd


def extract_frame(in_path: str, out_jpg: str, duration_ms: int = 0,
                  width: int = 320) -> None:
    """Extract one frame (~10% in, at least 1s) scaled to `width` px."""
    if duration_ms <= 0:
        duration_ms = probe_duration_ms(in_path)
    ss = min(max(duration_ms / 1000 * 0.1, 1.0), max(duration_ms / 1000 - 0.5, 0.5)) \
        if duration_ms > 0 else 1.0
    cmd = [FFMPEG_EXE, "-y", "-ss", f"{ss:.1f}", "-i", in_path,
           "-frames:v", "1", "-vf", f"scale={width}:-2",
           "-q:v", "4", out_jpg]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise FfmpegError("ffmpeg not found in PATH.")
    if p.returncode != 0:
        raise FfmpegError(f"frame extract failed (exit {p.returncode}).")


def probe_duration_ms(path: str) -> int:
    """Duration in ms via ffprobe, fallback to ffmpeg -i parse. 0 if unknown."""
    try:
        p = subprocess.run(
            [FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and p.stdout.strip():
            return int(float(p.stdout.strip()) * 1000)
    except (FileNotFoundError, ValueError):
        pass
    try:
        p = subprocess.run([FFMPEG_EXE, "-i", path], capture_output=True,
                           text=True, timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", p.stderr)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return int((h * 3600 + mi * 60 + s) * 1000)
    except (FileNotFoundError, ValueError):
        pass
    return 0
