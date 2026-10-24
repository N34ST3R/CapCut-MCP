"""Dependency-free media probing.

CapCut drafts store durations and pixel sizes inline, so generating a draft needs
them up front. ffprobe is not guaranteed to be installed, and drafts are small, so
this module reads the containers directly: MP4/MOV/M4A boxes, WAV/RIFF chunks,
MP3 frame headers, and PNG/JPEG/WebP image headers.

Durations are returned in microseconds, matching CapCut's timeline units.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
CONTAINER_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT


class UnsupportedMedia(ValueError):
    """The file cannot be placed on a CapCut timeline."""


@dataclass
class MediaInfo:
    path: Path
    kind: str  # "video" | "photo" | "music"
    duration_us: int | None = None
    width: int = 0
    height: int = 0
    has_audio: bool = False
    estimated: bool = False  # duration is approximate or defaulted

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "duration_s": round(self.duration_us / 1_000_000, 3) if self.duration_us else None,
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "duration_estimated": self.estimated,
        }


def _be32(buf: bytes) -> int:
    return struct.unpack(">I", buf)[0]


def _mp4_atoms(fh, start: int, end: int):
    """Yield (type, payload_start, payload_end) for atoms in a byte range."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size = _be32(header[:4])
        kind = header[4:8].decode("latin-1", "replace")
        header_len = 8
        if size == 1:  # 64-bit extended size
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:  # extends to end of file
            size = end - pos
        if size < header_len or pos + size > end:
            return
        yield kind, pos + header_len, pos + size
        pos += size


def _mp4_find(fh, start: int, end: int, path: tuple[str, ...]):
    """Depth-first search for a nested atom path, e.g. ("moov", "mvhd")."""
    if not path:
        return None
    want = path[0]
    for kind, payload_start, payload_end in _mp4_atoms(fh, start, end):
        if kind != want:
            continue
        if len(path) == 1:
            return payload_start, payload_end
        found = _mp4_find(fh, payload_start, payload_end, path[1:])
        if found:
            return found
    return None


def _mp4_duration(fh, file_size: int) -> int | None:
    found = _mp4_find(fh, 0, file_size, ("moov", "mvhd"))
    if not found:
        return None
    start, _ = found
    fh.seek(start)
    version = fh.read(1)
    # mvhd: version/flags, creation, modification, then timescale and duration.
    if version == b"\x01":
        fh.seek(start + 20)  # 4 + 8 + 8
        timescale, duration = struct.unpack(">IQ", fh.read(12))
    else:
        fh.seek(start + 12)  # 4 + 4 + 4
        timescale, duration = struct.unpack(">II", fh.read(8))
    if not timescale or not duration:
        return None
    return int(duration * 1_000_000 / timescale)


def _mp4_tracks(fh, file_size: int) -> tuple[int, int, bool, bool]:
    """Return (width, height, has_video, has_audio) from tkhd/hdlr atoms."""
    width = height = 0
    has_video = has_audio = False
    moov = _mp4_find(fh, 0, file_size, ("moov",))
    if not moov:
        return width, height, has_video, has_audio
    for kind, start, end in _mp4_atoms(fh, *moov):
        if kind != "trak":
            continue
        handler = _mp4_find(fh, start, end, ("mdia", "hdlr"))
        if handler:
            fh.seek(handler[0] + 8)
            name = fh.read(4).decode("latin-1", "replace")
            has_video = has_video or name == "vide"
            has_audio = has_audio or name == "soun"
        tkhd = _mp4_find(fh, start, end, ("tkhd",))
        if tkhd:
            payload_start = tkhd[0]
            fh.seek(payload_start)
            version = fh.read(1)
            # tkhd: width/height are the last two 16.16 fixed-point fields.
            offset = payload_start + (88 if version == b"\x01" else 76)
            fh.seek(offset)
            raw = fh.read(8)
            if len(raw) == 8:
                w_fixed, h_fixed = struct.unpack(">II", raw)
                if w_fixed and h_fixed:
                    width, height = w_fixed >> 16, h_fixed >> 16
    return width, height, has_video, has_audio


def _wav_info(fh) -> tuple[int | None, int, bool]:
    fh.seek(0)
    header = fh.read(12)
    if len(header) < 12 or header[:4] != b"RIFF":
        return None, 0, True
    byte_rate = 0
    data_size = 0
    while True:
        chunk = fh.read(8)
        if len(chunk) < 8:
            break
        chunk_id = chunk[:4]
        size = struct.unpack("<I", chunk[4:])[0]
        if chunk_id == b"fmt ":
            body = fh.read(size)
            if len(body) >= 12:
                byte_rate = struct.unpack("<I", body[8:12])[0]
            fh.seek(size % 2, 1)
        elif chunk_id == b"data":
            data_size = size
            break
        else:
            fh.seek(size + (size % 2), 1)
    if byte_rate and data_size:
        return int(data_size / byte_rate * 1_000_000), 0, True
    return None, 0, True


_MP3_BITRATES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3_duration(fh, file_size: int) -> int | None:
    """Estimate duration from the first MPEG frame header (good enough for CBR)."""
    fh.seek(0)
    head = fh.read(10)
    offset = 0
    if head[:3] == b"ID3" and len(head) >= 10:
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        offset = 10 + size
    fh.seek(offset)
    window = fh.read(64 * 1024)
    for index in range(len(window) - 4):
        if window[index] != 0xFF or (window[index + 1] & 0xE0) != 0xE0:
            continue
        second = window[index + 1]
        version_bits = (second >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer_bits = (second >> 1) & 0x03  # 1=Layer III
        third = window[index + 2]
        bitrate_index = (third >> 4) & 0x0F
        rate_index = (third >> 2) & 0x03
        if version_bits == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
            continue
        layer = 4 - layer_bits
        version = 1 if version_bits == 3 else 2
        table = _MP3_BITRATES.get((version, layer))
        if not table:
            continue
        bitrate = table[bitrate_index] * 1000
        audio_bytes = file_size - offset
        if bitrate <= 0 or audio_bytes <= 0:
            return None
        return int(audio_bytes * 8 / bitrate * 1_000_000)
    return None


def _png_size(fh) -> tuple[int, int]:
    fh.seek(16)
    raw = fh.read(8)
    return struct.unpack(">II", raw) if len(raw) == 8 else (0, 0)


def _jpeg_size(fh) -> tuple[int, int]:
    fh.seek(2)
    while True:
        marker = fh.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return 0, 0
        code = marker[1]
        if code in (0xD8, 0xD9) or 0xD0 <= code <= 0xD7:
            continue
        raw = fh.read(2)
        if len(raw) < 2:
            return 0, 0
        length = struct.unpack(">H", raw)[0]
        if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
            body = fh.read(5)
            if len(body) < 5:
                return 0, 0
            height, width = struct.unpack(">HH", body[1:5])
            return width, height
        fh.seek(length - 2, 1)


def _webp_size(fh) -> tuple[int, int]:
    fh.seek(12)
    chunk = fh.read(4)
    if chunk == b"VP8X":
        fh.seek(24)
        raw = fh.read(6)
        if len(raw) == 6:
            width = 1 + (raw[0] | raw[1] << 8 | raw[2] << 16)
            height = 1 + (raw[3] | raw[4] << 8 | raw[5] << 16)
            return width, height
    return 0, 0


def probe(path: str | Path) -> MediaInfo:
    """Detect kind, duration and frame size for a media file."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise UnsupportedMedia(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in CONTAINER_EXT:
        raise UnsupportedMedia(
            f"Unsupported media type {suffix or '(none)'}; supported: "
            + ", ".join(sorted(CONTAINER_EXT))
        )

    size = file_path.stat().st_size
    if size == 0:
        raise UnsupportedMedia(f"File is empty: {file_path}")

    if suffix in IMAGE_EXT:
        info = MediaInfo(path=file_path, kind="photo")
        with open(file_path, "rb") as fh:
            if suffix == ".png":
                info.width, info.height = _png_size(fh)
            elif suffix in {".jpg", ".jpeg"}:
                info.width, info.height = _jpeg_size(fh)
            elif suffix == ".webp":
                info.width, info.height = _webp_size(fh)
        return info

    if suffix in {".mp4", ".mov", ".m4v", ".m4a"}:
        with open(file_path, "rb") as fh:
            duration = _mp4_duration(fh, size)
            width, height, has_video, has_audio = _mp4_tracks(fh, size)
        is_audio = suffix == ".m4a" or (has_audio and not has_video)
        return MediaInfo(
            path=file_path,
            kind="music" if is_audio else "video",
            duration_us=duration,
            width=width,
            height=height,
            has_audio=has_audio,
            estimated=duration is None,
        )

    if suffix == ".wav":
        with open(file_path, "rb") as fh:
            duration, _, has_audio = _wav_info(fh)
        return MediaInfo(
            path=file_path,
            kind="music",
            duration_us=duration,
            has_audio=has_audio,
            estimated=duration is None,
        )

    if suffix == ".mp3":
        with open(file_path, "rb") as fh:
            duration = _mp3_duration(fh, size)
        return MediaInfo(
            path=file_path,
            kind="music",
            duration_us=duration,
            has_audio=True,
            estimated=duration is None,
        )

    # Container we cannot parse (mkv/webm/avi/flac/ogg/...): let the caller supply a duration.
    is_audio = suffix in {".flac", ".ogg", ".opus", ".aac"}
    return MediaInfo(
        path=file_path,
        kind="music" if is_audio else "video",
        has_audio=is_audio,
        estimated=True,
    )
