"""Shrinkit presets — high-quality focused lineup (HEVC first, H.264 for compat)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    tagline: str
    crf: int
    max_height: int  # scale down if taller, keep aspect. 0 = keep original
    audio_kbps: int
    vcodec: str = "libx265"
    ffmpeg_preset: str = "slow"  # x264/x265 speed: slower = better quality/size
    hint: str = ""

    @property
    def codec_label(self) -> str:
        return "HEVC" if "265" in self.vcodec or "hevc" in self.vcodec.lower() else "H.264"

    @property
    def res_label(self) -> str:
        return "Original" if not self.max_height else f"{self.max_height}p"


PRESETS: dict[str, Preset] = {
    "pristine": Preset(
        id="pristine",
        name="Pristine HEVC",
        tagline="Transparent archive copy",
        crf=18,
        max_height=0,
        audio_kbps=192,
        vcodec="libx265",
        ffmpeg_preset="slow",
        hint="≈25–35% smaller",
    ),
    "high": Preset(
        id="high",
        name="High Quality HEVC",
        tagline="Near-identical, half the size",
        crf=21,
        max_height=0,
        audio_kbps=160,
        vcodec="libx265",
        ffmpeg_preset="slow",
        hint="≈45–60% smaller",
    ),
    "balanced": Preset(
        id="balanced",
        name="Balanced HEVC",
        tagline="Great look, much smaller",
        crf=24,
        max_height=0,
        audio_kbps=128,
        vcodec="libx265",
        ffmpeg_preset="medium",
        hint="≈60–75% smaller",
    ),
    "compatible": Preset(
        id="compatible",
        name="Universal H.264",
        tagline="Plays everywhere, near-original",
        crf=19,
        max_height=0,
        audio_kbps=160,
        vcodec="libx264",
        ffmpeg_preset="slow",
        hint="≈30–45% smaller",
    ),
}

DEFAULT_PRESET_ID = "high"
