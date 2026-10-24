"""CapCut MCP server.

CapCut has no public API. This server automates CapCut by generating and editing real
draft projects on disk (the same files the app reads), and by focusing/launching the
desktop app.

Edits are written atomically and guarded against clobbering changes CapCut made in the
meantime: if the draft changed on disk since it was loaded, the write is refused with a
conflict error until the project is reloaded (or forced). CapCut itself only shows
changes when the project is reopened.

Run stdio mode (how MCP clients start it):

    python server.py

Print a diagnostic report without starting the server:

    python server.py --check
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python server.py` from the project folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastmcp import FastMCP  # noqa: E402

from capcut_mcp import app, draft as drafts, fonts, paths  # noqa: E402
from capcut_mcp.draft import Draft, DraftError  # noqa: E402

mcp = FastMCP(
    name="CapCut",
    instructions=(
        "Create and edit CapCut projects by writing real draft files. Typical flow: "
        "create_project -> add_video/add_audio/add_text -> animate_segment / apply_mask / "
        "add_transition / apply_filter -> inspect_project. CapCut does not reload an open project, so edits "
        "appear after reopening it (or restarting the app). Each edit re-reads the project, so "
        "changes CapCut saved are picked up automatically; sync_status reports who wrote last. "
        "Export stays in CapCut's UI."
    ),
)

RELOAD_HINT = (
    "Reopen the project in CapCut to see this change. If the project is open in CapCut, "
    "CapCut's own save can overwrite these edits; reload_project picks up its changes."
)


def _saved(draft: Draft, payload: dict) -> dict:
    draft.save()
    return {
        "ok": True,
        "project": draft.data.get("name"),
        "folder": str(draft.folder),
        "duration_s": round(draft.duration_us / 1_000_000, 3),
        **payload,
        "note": RELOAD_HINT,
    }


def _open(project: str) -> Draft:
    return Draft.open(project)


# ------------------------------------------------------------------ projects


@mcp.tool
def capcut_status() -> dict:
    """Report whether CapCut is installed and running, plus the drafts folder in use."""
    return app.status()


@mcp.tool
def list_projects(limit: int = 25) -> dict:
    """List existing CapCut projects, newest first, with duration and canvas size."""
    projects = drafts.list_projects(limit=limit)
    return {"count": len(projects), "drafts_root": str(paths.drafts_root()), "projects": projects}


@mcp.tool
def inspect_project(project: str) -> dict:
    """Show a project's timeline: tracks, clips, text, animations, timings and sources."""
    return _open(project).summary()


@mcp.tool
def create_project(name: str, width: int = 1080, height: int = 1920, fps: float = 30.0) -> dict:
    """Create a new empty CapCut project.

    Common canvas sizes: 1080x1920 (vertical), 1920x1080 (landscape), 1080x1080 (square).
    """
    draft = Draft.create(name=name, width=width, height=height, fps=fps)
    return _saved(draft, {"segment_hint": "Add clips with add_video / add_audio / add_text."})


@mcp.tool
def sync_status(project: str) -> dict:
    """Check whether the project changed on disk (CapCut saved it) and if writing is safe."""
    status = _open(project).sync_status()
    status["capcut_running"] = app.is_running()
    if status["last_edit_at"] is None:
        status["advice"] = (
            "No MCP edits recorded for this project yet. inspect_project shows what CapCut last "
            "wrote; the first edit starts from that state."
        )
    elif status["changed_since_last_edit"]:
        status["advice"] = (
            "CapCut saved this project after the last MCP edit. inspect_project shows the "
            "current timeline, and the next edit starts from it. Reopen the project in CapCut "
            "after editing to see the new version."
        )
    else:
        status["advice"] = (
            "The file matches this server's last edit. Reopen the project in CapCut to see "
            "the change; if the project is open there, CapCut's own save can overwrite it."
        )
    return status


@mcp.tool
def reload_project(project: str) -> dict:
    """Re-read the project from disk and report its current state.

    Use after sync_status reports that CapCut saved the project, to confirm the file parses
    and see the timeline the next edit will build on.
    """
    draft = _open(project)
    summary = draft.reload()
    return {
        "ok": True,
        "project": summary["name"],
        "duration_s": summary["duration_s"],
        "track_count": summary["track_count"],
        "note": "Re-read from disk; this is the state the next edit starts from.",
    }


# --------------------------------------------------------------------- media


@mcp.tool
def add_video(
    project: str,
    file_path: str,
    start: float | None = None,
    duration: float | None = None,
    trim_start: float = 0.0,
    volume: float = 1.0,
    speed: float = 1.0,
) -> dict:
    """Add a video clip or still image (png/jpg/webp) to a project at `start` seconds.

    `start` defaults to the end of the video track. `trim_start` skips into the source,
    `duration` limits the clip length, `speed` retimes it (2.0 = twice as fast).
    """
    draft = _open(project)
    return _saved(
        draft,
        {
            "added": draft.add_video(
                file_path,
                start=start,
                duration=duration,
                trim_start=trim_start,
                volume=volume,
                speed=speed,
            )
        },
    )


@mcp.tool
def add_audio(
    project: str,
    file_path: str,
    start: float | None = None,
    duration: float | None = None,
    trim_start: float = 0.0,
    volume: float = 1.0,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
) -> dict:
    """Add music or a sound effect to a project's audio track.

    `start` defaults to the end of the audio track; `fade_in`/`fade_out` are seconds.
    """
    draft = _open(project)
    return _saved(
        draft,
        {
            "added": draft.add_audio(
                file_path,
                start=start,
                duration=duration,
                trim_start=trim_start,
                volume=volume,
                fade_in=fade_in,
                fade_out=fade_out,
            )
        },
    )


# ---------------------------------------------------------------------- text


@mcp.tool
def list_fonts(query: str = "", limit: int = 40) -> dict:
    """List installed fonts usable for CapCut text (Windows fonts plus CapCut's own).

    Filter with `query` (matches family or style), e.g. "arial", "condensed", "bold".
    """
    found = fonts.search(query, limit)
    return {
        "count": len(found),
        "total_installed": len(fonts.catalogue()),
        "fonts": [font.as_dict() for font in found],
    }


@mcp.tool
def add_text(
    project: str,
    text: str,
    start: float = 0.0,
    duration: float = 3.0,
    size: float = 15.0,
    color: str = "#FFFFFF",
    align: int = 1,
    x: float = 0.0,
    y: float = 0.0,
    font_family: str = "",
    font_style: str = "",
    font_path: str = "",
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    tracking: float = 0.0,
    line_spacing: float = 0.02,
    outline_width: float = 0.0,
    outline_color: str = "#000000",
    shadow: bool = False,
    shadow_color: str = "#000000",
    shadow_distance: float = 5.0,
    background: str = "",
    background_alpha: float = 1.0,
    opacity: float = 1.0,
    new_track: bool = False,
) -> dict:
    """Add a title or caption on a text track.

    Font: pass `font_family` (see list_fonts) or an explicit `font_path`.
    Styling: `color`, `size`, `bold`, `italic`, `underline`, `tracking` (letter spacing),
    `outline_width`/`outline_color`, `shadow`, `background` (box colour), `opacity`.
    `align` is 0=left / 1=centre / 2=right; `x`/`y` shift the text (0 = centred).
    Set `new_track` to layer text over existing titles at the same time.
    """
    draft = _open(project)
    return _saved(
        draft,
        {
            "added": draft.add_text(
                text,
                start=start,
                new_track=new_track,
                duration=duration,
                size=size,
                color=color,
                align=align,
                x=x,
                y=y,
                font_path=font_path,
                font_family=font_family,
                font_style=font_style,
                bold=bold,
                italic=italic,
                underline=underline,
                tracking=tracking,
                line_spacing=line_spacing,
                outline_width=outline_width,
                outline_color=outline_color,
                shadow=shadow,
                shadow_color=shadow_color,
                shadow_distance=shadow_distance,
                background=background,
                background_alpha=background_alpha,
                opacity=opacity,
            )
        },
    )


@mcp.tool
def style_text(
    project: str,
    segment_id: str,
    text: str | None = None,
    size: float | None = None,
    color: str | None = None,
    align: int | None = None,
    font_family: str | None = None,
    font_style: str | None = None,
    font_path: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    tracking: float | None = None,
    line_spacing: float | None = None,
    outline_width: float | None = None,
    outline_color: str | None = None,
    shadow: bool | None = None,
    shadow_color: str | None = None,
    shadow_distance: float | None = None,
    background: str | None = None,
    background_alpha: float | None = None,
    opacity: float | None = None,
) -> dict:
    """Restyle an existing text clip (or change its words) without re-timing it."""
    draft = _open(project)
    return _saved(
        draft,
        {
            "styled": draft.style_text(
                segment_id,
                text=text,
                size=size,
                color=color,
                align=align,
                font_family=font_family,
                font_style=font_style,
                font_path=font_path,
                bold=bold,
                italic=italic,
                underline=underline,
                tracking=tracking,
                line_spacing=line_spacing,
                outline_width=outline_width,
                outline_color=outline_color,
                shadow=shadow,
                shadow_color=shadow_color,
                shadow_distance=shadow_distance,
                background=background,
                background_alpha=background_alpha,
                opacity=opacity,
            )
        },
    )


# ----------------------------------------------------------------- animation


@mcp.tool
def animate_segment(
    project: str,
    segment_id: str,
    preset: str | None = None,
    duration: float | None = None,
    keyframes: list[dict] | None = None,
) -> dict:
    """Animate a clip with keyframes.

    Presets: fade_in, fade_out, slide_in_left, slide_in_right, slide_in_top,
    slide_in_bottom, zoom_in, zoom_out, pop, rotate_in, ken_burns.
    Or pass `keyframes`: [{"property": "scale", "time": 1.0, "value": 1.2}, ...] with
    properties x, y, rotation, scale, scale_x, scale_y, alpha, volume, saturation,
    contrast, brightness. Times are seconds from the clip's start; interpolation is linear.
    """
    draft = _open(project)
    return _saved(draft, {"animated": draft.animate_segment(segment_id, preset=preset, duration=duration, keyframes=keyframes)})


# --------------------------------------------------------------- transitions


@mcp.tool
def list_transitions() -> dict:
    """List transitions whose CapCut asset is already cached on this machine.

    CapCut downloads transition assets on first use, so this is the set that can be
    attached to a generated project without the app fetching anything.
    """
    found = [{k: v for k, v in item.items() if k != "material"} for item in drafts.cached_transitions()]
    return {
        "count": len(found),
        "transitions": found,
        "note": "Add a transition once in CapCut to cache more.",
    }


@mcp.tool
def add_transition(
    project: str, segment_id: str, name: str | None = None, duration: float | None = None
) -> dict:
    """Attach a cached transition to the end of a video clip (into the next clip)."""
    draft = _open(project)
    return _saved(draft, {"transition": draft.add_transition(segment_id, name=name, duration=duration)})


# ------------------------------------------------------------------- filters


@mcp.tool
def list_filters() -> dict:
    """List CapCut filters whose asset is cached locally.

    Filters are downloaded effects, so only ones already used in CapCut can be attached
    to a generated project.
    """
    found = [{k: v for k, v in item.items() if k != "material"} for item in drafts.cached_filters()]
    return {
        "count": len(found),
        "filters": found,
        "note": "Apply a filter once in CapCut to cache its asset, then it appears here.",
    }


@mcp.tool
def apply_filter(
    project: str, segment_id: str, name: str | None = None, intensity: float = 1.0
) -> dict:
    """Attach a cached CapCut filter to a video/image clip.

    `intensity` is 0-1 (CapCut shows 0-100%). Leave `name` empty to use the first one.
    """
    draft = _open(project)
    return _saved(
        draft, {"filter": draft.apply_filter(segment_id, name=name, intensity=intensity)}
    )


# --------------------------------------------------------------------- masks


@mcp.tool
def apply_mask(
    project: str,
    segment_id: str,
    shape: str = "circle",
    center_x: float = 0.0,
    center_y: float = 0.0,
    width: float = 0.6,
    height: float = 0.6,
    rotation: float = 0.0,
    feather: float = 0.0,
    invert: bool = False,
    round_corner: float = 0.0,
    expansion: float = 0.0,
) -> dict:
    """Mask a video/image clip with CapCut's built-in shapes.

    Shapes: linear, mirror, circle, rectangle, heart, star. `center_x`/`center_y` and
    `width`/`height` are fractions of the frame; `feather` and `round_corner` are 0-1.
    """
    draft = _open(project)
    return _saved(
        draft,
        {
            "mask": draft.apply_mask(
                segment_id,
                shape=shape,
                center_x=center_x,
                center_y=center_y,
                width=width,
                height=height,
                rotation=rotation,
                feather=feather,
                invert=invert,
                round_corner=round_corner,
                expansion=expansion,
            )
        },
    )


# ------------------------------------------------------------------- timing


@mcp.tool
def set_speed(project: str, segment_id: str, speed: float) -> dict:
    """Change playback speed of a video or audio clip (1.0 = normal, 2.0 = double)."""
    draft = _open(project)
    return _saved(draft, {"speed": draft.set_speed(segment_id, speed)})


@mcp.tool
def trim_segment(
    project: str,
    segment_id: str,
    start: float | None = None,
    duration: float | None = None,
    source_start: float | None = None,
) -> dict:
    """Move or shorten a clip. Get `segment_id` values from inspect_project."""
    draft = _open(project)
    return _saved(
        draft,
        {"segment": draft.trim_segment(segment_id, start=start, duration=duration, source_start=source_start)},
    )


@mcp.tool
def split_segment(project: str, segment_id: str, at: float) -> dict:
    """Split a clip in two at an absolute timeline position (seconds)."""
    draft = _open(project)
    return _saved(draft, {"split": draft.split_segment(segment_id, at)})


@mcp.tool
def duplicate_segment(project: str, segment_id: str, start: float | None = None) -> dict:
    """Copy a clip on the same track, at `start` or after the track's last clip."""
    draft = _open(project)
    return _saved(draft, {"duplicate": draft.duplicate_segment(segment_id, start=start)})


@mcp.tool
def ripple_delete_segment(project: str, segment_id: str) -> dict:
    """Delete a clip and pull later clips on that track back to close the gap."""
    draft = _open(project)
    return _saved(draft, {"deleted": draft.ripple_delete_segment(segment_id)})


@mcp.tool
def delete_segment(project: str, segment_id: str) -> dict:
    """Delete a clip, text, or audio segment and its unused materials."""
    draft = _open(project)
    return _saved(draft, {"deleted": draft.delete_segment(segment_id)})


# ---------------------------------------------------------------- subtitles


@mcp.tool
def import_srt(
    project: str,
    srt_path: str,
    size: float = 14.0,
    color: str = "#FFFFFF",
    font_family: str = "",
    font_style: str = "",
    bold: bool = False,
    outline_width: float = 0.0,
    outline_color: str = "#000000",
    shadow: bool = False,
    separate_track: bool = True,
    align: int = 1,
    x: float = 0.0,
    y: float = 0.0,
) -> dict:
    """Import an .srt file as one timed text clip per subtitle cue.

    Cues go on their own text track so they keep exact times and can sit under titles.
    `x`/`y` shift every cue (for example y=-0.6 for a lower third).
    """
    draft = _open(project)
    return _saved(
        draft,
        {
            "subtitles": draft.import_srt(
                srt_path,
                size=size,
                color=color,
                font_family=font_family,
                font_style=font_style,
                bold=bold,
                outline_width=outline_width,
                outline_color=outline_color,
                shadow=shadow,
                separate_track=separate_track,
                align=align,
                x=x,
                y=y,
            )
        },
    )


# ------------------------------------------------------------------ project


@mcp.tool
def set_canvas(project: str, width: int, height: int) -> dict:
    """Change a project's canvas resolution (for example 1080x1920 vertical)."""
    draft = _open(project)
    return _saved(draft, {"canvas": draft.set_canvas(width, height)})


@mcp.tool
def rename_project(project: str, new_name: str) -> dict:
    """Rename a project. The folder on disk keeps its current name."""
    draft = _open(project)
    return _saved(draft, {"renamed": draft.rename(new_name)})


@mcp.tool
def open_project_folder(project: str) -> dict:
    """Open the project's folder in Explorer (useful before exporting or backing up)."""
    draft = _open(project)
    return {"opened": app.reveal(draft.folder), "folder": str(draft.folder)}


@mcp.tool
def focus_capcut(launch_if_needed: bool = True) -> dict:
    """Bring the CapCut window to the front, optionally starting it if it is closed."""
    if app.is_running():
        return {"running": True, "focused": app.focus(), "launched": False}
    if not launch_if_needed:
        return {"running": False, "focused": False, "launched": False}
    result = app.launch()
    return {"running": True, **result}


def main() -> None:
    if "--check" in sys.argv:
        print(json.dumps(app.status(), indent=2))
        try:
            print(json.dumps({"projects": drafts.list_projects(limit=5)}, indent=2))
            print(json.dumps({"fonts": len(fonts.catalogue())}, indent=2))
        except DraftError as exc:  # pragma: no cover - diagnostic path
            print(f"project listing failed: {exc}", file=sys.stderr)
        return
    mcp.run()


if __name__ == "__main__":
    main()
