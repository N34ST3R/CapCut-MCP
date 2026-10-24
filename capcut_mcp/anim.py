"""Keyframe animation.

CapCut's downloadable animation effects are asset-backed, so they cannot be generated
offline. Keyframes are pure timeline data and work everywhere: position, rotation,
scale, opacity and volume with linear interpolation, which is what the draft format
supports (``common_keyframes`` on a segment).

Units follow the draft format:
  position_x / position_y  fraction of canvas size (0.5 = half a canvas from centre)
  rotation                 degrees, clockwise
  scale / scale_x / scale_y  1.0 = original size
  alpha                    1.0 = opaque
  volume                   1.0 = original level
"""

from __future__ import annotations

import uuid
from typing import Iterable

PROPERTY_TYPES = {
    "x": "KFTypePositionX",
    "y": "KFTypePositionY",
    "rotation": "KFTypeRotation",
    "scale_x": "KFTypeScaleX",
    "scale_y": "KFTypeScaleY",
    "scale": "UNIFORM_SCALE",
    "alpha": "KFTypeAlpha",
    "volume": "KFTypeVolume",
    "saturation": "KFTypeSaturation",
    "contrast": "KFTypeContrast",
    "brightness": "KFTypeBrightness",
}


class AnimationError(ValueError):
    pass


def _clamp_time(value: float, duration_s: float) -> int:
    return max(0, min(int(round(duration_s * 1_000_000)), int(round(value * 1_000_000))))


def keyframe_list(property_key: str, points: Iterable[tuple[float, float]], duration_s: float) -> dict:
    """Serialize one animated property into a CapCut keyframe list."""
    if property_key not in PROPERTY_TYPES:
        raise AnimationError(
            f"Unknown property {property_key!r}; use one of {', '.join(sorted(PROPERTY_TYPES))}"
        )
    cleaned = []
    for time_s, value in points:
        cleaned.append((_clamp_time(float(time_s), duration_s), float(value)))
    cleaned.sort(key=lambda item: item[0])
    if len(cleaned) < 2:
        raise AnimationError("An animation needs at least two keyframes")

    frames = []
    for offset, value in cleaned:
        frames.append(
            {
                "curveType": "Line",
                "graphID": "",
                "left_control": {"x": 0.0, "y": 0.0},
                "right_control": {"x": 0.0, "y": 0.0},
                "id": uuid.uuid4().hex,
                "time_offset": offset,
                "values": [value],
            }
        )
    return {
        "id": uuid.uuid4().hex,
        "keyframe_list": frames,
        "material_id": "",
        "property_type": PROPERTY_TYPES[property_key],
    }


PRESETS = {
    "fade_in",
    "fade_out",
    "slide_in_left",
    "slide_in_right",
    "slide_in_top",
    "slide_in_bottom",
    "zoom_in",
    "zoom_out",
    "pop",
    "rotate_in",
    "ken_burns",
}


def preset(name: str, segment_s: float, length: float | None = None) -> dict[str, list[tuple[float, float]]]:
    """Keyframe tracks for a named preset, with times relative to the segment start."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in PRESETS:
        raise AnimationError(f"Unknown preset {name!r}; use one of {', '.join(sorted(PRESETS))}")

    seg = max(0.001, segment_s)
    dur = min(length if length is not None else 0.6, seg)
    dur = max(0.05, dur)
    end = seg
    start_out = max(0.0, seg - dur)

    if key == "fade_in":
        return {"alpha": [(0.0, 0.0), (dur, 1.0)]}
    if key == "fade_out":
        return {"alpha": [(start_out, 1.0), (end, 0.0)]}
    if key == "slide_in_left":
        return {"x": [(0.0, -0.35), (dur, 0.0)]}
    if key == "slide_in_right":
        return {"x": [(0.0, 0.35), (dur, 0.0)]}
    if key == "slide_in_top":
        return {"y": [(0.0, 0.35), (dur, 0.0)]}
    if key == "slide_in_bottom":
        return {"y": [(0.0, -0.35), (dur, 0.0)]}
    if key == "zoom_in":
        return {"scale": [(0.0, 1.0), (dur, 1.15)]}
    if key == "zoom_out":
        return {"scale": [(0.0, 1.18), (dur, 1.0)]}
    if key == "pop":
        return {"scale": [(0.0, 0.86), (dur * 0.6, 1.06), (dur, 1.0)]}
    if key == "rotate_in":
        return {"rotation": [(0.0, -8.0), (dur, 0.0)]}
    # ken_burns: slow push across the whole clip
    return {
        "scale": [(0.0, 1.0), (end, 1.08)],
        "x": [(0.0, -0.02), (end, 0.02)],
    }


def custom(keyframes: list[dict], segment_s: float) -> dict[str, list[tuple[float, float]]]:
    """Group user keyframes ({property, time, value}) by property."""
    grouped: dict[str, list[tuple[float, float]]] = {}
    for item in keyframes:
        prop = str(item.get("property", "")).strip()
        if prop not in PROPERTY_TYPES:
            raise AnimationError(f"Unknown property {prop!r} in keyframes")
        try:
            time_s = float(item["time"])
            value = float(item["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnimationError("Each keyframe needs numeric 'time' and 'value'") from exc
        if not 0.0 <= time_s <= segment_s + 1e-6:
            raise AnimationError(f"Keyframe time {time_s}s is outside the segment (0-{segment_s:.2f}s)")
        grouped.setdefault(prop, []).append((time_s, value))

    for prop, points in grouped.items():
        if len(points) < 2:
            raise AnimationError(f"Property {prop!r} needs at least two keyframes")
        if prop in ("scale", "scale_x", "scale_y") and any(v <= 0 for _, v in points):
            raise AnimationError("Scale keyframes must be greater than 0")
        if prop == "alpha" and any(not 0.0 <= v <= 1.0 for _, v in points):
            raise AnimationError("Alpha keyframes must be between 0 and 1")
        if prop == "volume" and any(not 0.0 <= v <= 2.0 for _, v in points):
            raise AnimationError("Volume keyframes must be between 0 and 2")
    return grouped
