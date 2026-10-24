"""SubRip (.srt) parsing for timed caption clips."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TIMECODE = re.compile(
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")


class SubtitleError(ValueError):
    """The subtitle file could not be parsed."""


@dataclass
class Cue:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _seconds(hours: str, minutes: str, secs: str, frac: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(frac.ljust(3, "0")[:3]) / 1000


def parse_srt(source: str | Path) -> list[Cue]:
    path = Path(source).expanduser()
    if not path.is_file():
        raise SubtitleError(f"Subtitle file not found: {path}")
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    cues: list[Cue] = []
    lines = raw.split("\n")
    index = 0
    while index < len(lines):
        match = TIMECODE.search(lines[index])
        if not match:
            index += 1
            continue
        start = _seconds(*match.groups()[:4])
        end = _seconds(*match.groups()[4:])
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(TAG.sub("", lines[index]).strip())
            index += 1
        text = "\n".join(line for line in body if line).strip()
        if text and end > start:
            cues.append(Cue(start=start, end=end, text=text))
        index += 1

    if not cues:
        raise SubtitleError(f"No subtitle cues found in {path}")
    cues.sort(key=lambda cue: cue.start)
    return cues
