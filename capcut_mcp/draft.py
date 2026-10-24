"""Read and write CapCut draft projects.

A CapCut project is a folder containing ``draft_content.json`` (the timeline) and
``draft_meta_info.json`` (metadata plus the imported-media registration that decides
whether CapCut asks the user to relink files).

The shapes written here are cloned from templates extracted from projects written by
the locally installed CapCut build (see ``tools/extract_templates.py``), because the
format is private and changes between releases. Segments reference materials by id;
companion materials (speed, canvas, audio fade, ...) are referenced through
``extra_material_refs``.

Timeline values are microseconds. Editing a draft requires CapCut to be restarted (or
the project reopened) before the change appears in the app.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

from . import anim, fonts, media, paths, state, subtitles

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# CapCut's default still-image length and the fallback when a duration cannot be probed.
DEFAULT_PHOTO_US = 5_000_000
FALLBACK_US = 5_000_000
DEFAULT_TEXT_US = 3_000_000

US = 1_000_000

# Companion materials that CapCut attaches to each segment type.
COMPANIONS = {
    "video": ["speeds", "placeholder_infos", "canvases", "sound_channel_mappings", "material_colors", "vocal_separations"],
    "audio": ["speeds", "placeholder_infos", "audio_fades", "sound_channel_mappings", "vocal_separations"],
}

TRACK_RENDER_BASE = {"video": 0, "text": 1, "audio": 4}
TEXT_SEGMENT_RENDER_INDEX = 14000

# CapCut's built-in mask shapes (parametric, so no downloaded asset is needed).
MASK_SHAPES = {
    "linear": {"name": "Linear", "resource_type": "line", "resource_id": "6791652175668843016", "aspect": 1.0},
    "mirror": {"name": "Mirror", "resource_type": "mirror", "resource_id": "6791699060140020232", "aspect": 1.0},
    "circle": {"name": "Circle", "resource_type": "circle", "resource_id": "6791700663249146381", "aspect": 1.0},
    "rectangle": {"name": "Rectangle", "resource_type": "rectangle", "resource_id": "6791700809454195207", "aspect": 1.0},
    "heart": {"name": "Heart", "resource_type": "geometric_shape", "resource_id": "6794051276482023949", "aspect": 1.115},
    "star": {"name": "Star", "resource_type": "geometric_shape", "resource_id": "6794051169434997255", "aspect": 1.05},
}


class DraftError(RuntimeError):
    """A draft could not be created, read, or modified."""


class DraftConflict(DraftError):
    """draft_content.json changed on disk (CapCut saved it) since this copy was loaded."""


def cached_transitions(root: Path | None = None) -> list[dict]:
    """Transitions whose effect asset is present locally.

    CapCut transitions are downloaded assets; only ones already cached can be attached
    to a generated project without the app fetching them first.
    """
    found: dict[str, dict] = {}
    for folder in paths.project_dirs(root):
        try:
            content = json.loads((folder / paths.DRAFT_JSON).read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in content.get("materials", {}).get("transitions") or []:
            path = str(item.get("path") or "")
            if not path or not Path(path).exists():
                continue
            key = str(item.get("resource_id") or item.get("name") or path)
            found.setdefault(
                key,
                {
                    "name": item.get("name", ""),
                    "resource_id": item.get("resource_id", ""),
                    "effect_id": item.get("effect_id", ""),
                    "category_name": item.get("category_name", ""),
                    "default_duration_s": round(int(item.get("duration", 400000)) / US, 3),
                    "path": path,
                    # CapCut's own material, so the new transition carries the same
                    # fields (including the cached asset path).
                    "material": {k: v for k, v in item.items() if k != "id"},
                },
            )
    return sorted(found.values(), key=lambda item: item["name"].casefold())


def cached_filters(root: Path | None = None) -> list[dict]:
    """Filters whose effect asset is present locally.

    Like transitions, CapCut filters are downloaded assets: a generated project can only
    use filters the app has already cached, otherwise the clip shows an offline effect.
    """
    found: dict[str, dict] = {}
    for folder in paths.project_dirs(root):
        try:
            content = json.loads((folder / paths.DRAFT_JSON).read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in content.get("materials", {}).get("filters") or []:
            path = str(item.get("path") or "")
            if not path or not Path(path).exists():
                continue
            key = str(item.get("resource_id") or item.get("name") or path)
            found.setdefault(
                key,
                {
                    "name": item.get("name", ""),
                    "resource_id": item.get("resource_id", ""),
                    "effect_id": item.get("effect_id", ""),
                    "path": path,
                    "material": {k: v for k, v in item.items() if k != "id"},
                },
            )
    return sorted(found.values(), key=lambda item: item["name"].casefold())


@lru_cache(maxsize=1)
def _base_template() -> dict:
    return json.loads((TEMPLATE_DIR / "draft_base.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _meta_template() -> dict:
    return json.loads((TEMPLATE_DIR / "draft_meta_info.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _prototypes() -> dict:
    return json.loads((TEMPLATE_DIR / "prototypes.json").read_text(encoding="utf-8"))


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _uid_lower() -> str:
    return str(uuid.uuid4()).lower()


def _now_us() -> int:
    return int(time.time() * US)


def _win(path: Path | str) -> str:
    """CapCut stores Windows paths with forward slashes."""
    return str(path).replace("\\", "/")


def _seconds(value: float) -> int:
    return max(0, int(round(float(value) * US)))


def _ratio(width: int, height: int) -> str:
    known = {(1920, 1080): "16:9", (1080, 1920): "9:16", (1080, 1080): "1:1", (1080, 1350): "4:5"}
    return known.get((width, height), "original")


def _parse_color(value: str) -> list[float]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise DraftError(f"Colour must be a hex string like #RRGGBB, got {value!r}")
    try:
        return [round(int(text[i : i + 2], 16) / 255, 4) for i in (0, 2, 4)]
    except ValueError as exc:
        raise DraftError(f"Invalid colour {value!r}") from exc


def list_projects(limit: int = 50, root: Path | None = None) -> list[dict]:
    """Projects in the drafts folder, newest first."""
    out: list[dict] = []
    for folder in paths.project_dirs(root)[:limit]:
        entry: dict = {"name": folder.name, "folder": str(folder)}
        try:
            content = json.loads((folder / paths.DRAFT_JSON).read_text(encoding="utf-8"))
            entry["duration_s"] = round(content.get("duration", 0) / US, 2)
            entry["canvas"] = {
                "width": content.get("canvas_config", {}).get("width"),
                "height": content.get("canvas_config", {}).get("height"),
            }
            entry["tracks"] = len(content.get("tracks", []))
        except Exception as exc:  # unreadable or encrypted draft
            entry["error"] = f"unreadable draft_content.json: {exc}"
        try:
            meta = json.loads((folder / paths.META_JSON).read_text(encoding="utf-8"))
            entry["name"] = meta.get("draft_name") or folder.name
            entry["modified"] = meta.get("tm_draft_modified") or None
        except Exception:
            pass
        out.append(entry)
    return out


class Draft:
    """A single CapCut project folder."""

    def __init__(self, folder: Path, data: dict, meta: dict):
        self.folder = Path(folder)
        self.data = data
        self.meta = meta
        # Hash of the file we loaded, so writes can detect CapCut saving underneath us.
        self._loaded_hash = self._disk_hash()
        self._new_project = (self.folder / "Timelines").is_dir()
        self._project_id_value = ""
        try:
            project = json.loads((self.folder / "Timelines" / "project.json").read_text(encoding="utf-8"))
            self._project_id_value = str(project.get("id", ""))
        except Exception:
            pass

    # ---------------------------------------------------------------- loading

    @classmethod
    def open(cls, name_or_path: str, root: Path | None = None) -> "Draft":
        folder = paths.resolve_project(name_or_path, root)
        try:
            data = json.loads((folder / paths.DRAFT_JSON).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DraftError(
                f"{folder / paths.DRAFT_JSON} is not plain JSON (encrypted drafts are not supported)"
            ) from exc
        try:
            meta = json.loads((folder / paths.META_JSON).read_text(encoding="utf-8"))
        except Exception:
            meta = copy.deepcopy(_meta_template())
        return cls(folder, data, meta)

    @classmethod
    def create(
        cls,
        name: str,
        width: int = 1080,
        height: int = 1920,
        fps: float = 30.0,
        root: Path | None = None,
    ) -> "Draft":
        base = Path(root) if root else paths.drafts_root()
        if not base.is_dir():
            raise DraftError(
                f"CapCut drafts folder not found: {base}. Open CapCut once, or set CAPCUT_DRAFTS_DIR."
            )
        folder = paths.unique_folder(base, paths.safe_folder_name(name))
        folder.mkdir(parents=True)

        data = copy.deepcopy(_base_template())
        meta = copy.deepcopy(_meta_template())
        now = _now_us()
        timeline_id = _uid()

        data.update(
            id=timeline_id,
            name=name,
            fps=float(fps),
            duration=0,
            create_time=now,
            update_time=now,
            canvas_config={"ratio": _ratio(width, height), "width": int(width), "height": int(height), "background": None},
        )
        version = paths.app_version()
        if version:
            for block in ("platform", "last_modified_platform"):
                data.setdefault(block, {})["app_version"] = version

        meta.update(
            draft_fold_path=_win(folder),
            draft_id=_uid(),
            draft_name=name,
            draft_root_path=_win(base),
            draft_cover="",
            tm_draft_create=now,
            tm_draft_modified=now,
            tm_duration=0,
            draft_new_version=data.get("new_version", ""),
        )

        draft = cls(folder, data, meta)
        draft._new_project = True  # CapCut 8+ mirrors the main timeline under Timelines/
        draft._project_id_value = _uid()
        draft._write_scaffolding()
        draft.save()
        return draft

    def _write_scaffolding(self) -> None:
        """Static sidecar files that CapCut expects in a project folder."""
        now_s = int(time.time())
        (self.folder / "draft_settings").write_text(
            "[General]\r\n"
            f"draft_create_time={now_s}\r\n"
            f"draft_last_edit_time={now_s}\r\n"
            "real_edit_seconds=0\r\n"
            "real_edit_keys=0\r\n",
            encoding="utf-8",
        )
        (self.folder / "attachment_pc_common.json").write_text(
            json.dumps(
                {
                    "ai_packaging_infos": [],
                    "ai_packaging_report_info": {
                        "caption_id_list": [],
                        "commercial_material": "",
                        "material_source": "",
                        "method": "",
                        "page_from": "",
                        "style": "",
                        "task_id": "",
                        "text_style": "",
                        "tos_id": "",
                        "video_category": "",
                    },
                    "broll": {"ai_packaging_infos": [], "ai_packaging_report_info": {}},
                    "commercial_music_category_ids": [],
                    "pc_feature_flag": 0,
                    "recognize_tasks": [],
                    "reference_lines_config": {
                        "horizontal_lines": [],
                        "is_lock": False,
                        "is_visible": False,
                        "vertical_lines": [],
                    },
                    "safe_area_type": 0,
                    "template_item_infos": [],
                    "unlock_template_ids": [],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (self.folder / "draft_agency_config.json").write_text(
            json.dumps(
                {
                    "is_auto_agency_enabled": False,
                    "is_auto_agency_popup": False,
                    "is_single_agency_mode": False,
                    "marterials": None,
                    "use_converter": False,
                    "video_resolution": 720,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (self.folder / "performance_opt_info.json").write_text(
            json.dumps(
                {"manual_cancle_precombine_segs": None, "need_auto_precombine_segs": None},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (self.folder / "key_value.json").write_text(
            json.dumps({"draft_common_info_key": {}}, separators=(",", ":")), encoding="utf-8"
        )

    # ----------------------------------------------------------------- saving

    def _disk_hash(self) -> str:
        try:
            return hashlib.sha256((self.folder / paths.DRAFT_JSON).read_bytes()).hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        """Replace a file in one step so CapCut never reads a half-written draft."""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def sync_status(self) -> dict:
        """Whether this copy still matches disk, and whether CapCut saved after us."""
        disk = self._disk_hash()
        changed = bool(disk and self._loaded_hash and disk != self._loaded_hash)
        last = state.last_write(self.folder)
        external = bool(last and disk and disk != last.get("hash"))
        try:
            modified_ago = round(time.time() - (self.folder / paths.DRAFT_JSON).stat().st_mtime, 1)
        except OSError:
            modified_ago = None
        return {
            "project": self.data.get("name") or self.meta.get("draft_name", ""),
            "folder": str(self.folder),
            "changed_on_disk": changed,
            "safe_to_write": not changed,
            "changed_since_last_edit": external,
            "last_edit_at": last.get("at") if last else None,
            "file_modified_ago_s": modified_ago,
            "loaded_hash": self._loaded_hash[:12],
            "disk_hash": disk[:12],
        }

    def reload(self) -> dict:
        """Re-read the project from disk, picking up changes CapCut saved."""
        try:
            self.data = json.loads((self.folder / paths.DRAFT_JSON).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DraftError(
                f"{self.folder / paths.DRAFT_JSON} is not plain JSON (encrypted drafts are not supported)"
            ) from exc
        try:
            self.meta = json.loads((self.folder / paths.META_JSON).read_text(encoding="utf-8"))
        except Exception:
            pass
        self._loaded_hash = self._disk_hash()
        self._new_project = (self.folder / "Timelines").is_dir()
        return self.summary()

    def save(self, force: bool = False) -> None:
        current = self._disk_hash()
        if not force and self._loaded_hash and current and current != self._loaded_hash:
            raise DraftConflict(
                "draft_content.json changed on disk since it was loaded (CapCut saved this "
                "project). Call reload_project to pick up those changes, then retry."
            )
        self._recompute()
        now = _now_us()
        self.data["update_time"] = now
        self.meta["tm_draft_modified"] = now
        self.meta["draft_name"] = self.data.get("name", self.meta.get("draft_name", ""))
        self.meta["draft_new_version"] = self.data.get("new_version", "")
        self.meta["tm_duration"] = self.data.get("duration", 0)

        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        encoded = payload.encode("utf-8")
        self._write_atomic(self.folder / paths.DRAFT_JSON, encoded)
        self._write_atomic(self.folder / (paths.DRAFT_JSON + ".bak"), encoded)
        self._write_atomic(
            self.folder / paths.META_JSON,
            json.dumps(self.meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        self._write_atomic(
            self.folder / "timeline_layout.json",
            json.dumps(
                {
                    "dockItems": [
                        {
                            "dockIndex": 0,
                            "ratio": 1,
                            "timelineIds": [self.data["id"]],
                            "timelineNames": ["Timeline 01"],
                        }
                    ],
                    "layoutOrientation": 1,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        self._sync_timelines(payload, now)
        self._loaded_hash = hashlib.sha256(encoded).hexdigest()
        state.record_write(self.folder, self._loaded_hash)

    def _sync_timelines(self, payload: str, now: int) -> None:
        """CapCut 8+ mirrors the main timeline under Timelines/<id>/.

        Legacy drafts have no Timelines folder; adding one there would create a second
        source of truth, so those stay single-file.
        """
        timelines = self.folder / "Timelines"
        if not timelines.is_dir() and not self._new_project:
            return
        if not self._project_id_value:
            self._project_id_value = _uid()
        timelines.mkdir(exist_ok=True)
        timeline_dir = timelines / self.data["id"]
        timeline_dir.mkdir(exist_ok=True)
        encoded = payload.encode("utf-8")
        self._write_atomic(timeline_dir / paths.DRAFT_JSON, encoded)
        self._write_atomic(timeline_dir / (paths.DRAFT_JSON + ".bak"), encoded)
        (timelines / "project.json").write_text(
            json.dumps(
                {
                    "config": {
                        "color_space": -1,
                        "render_index_track_mode_on": False,
                        "use_float_render": False,
                    },
                    "create_time": int(self.data.get("create_time") or now),
                    "id": self._project_id_value,
                    "main_timeline_id": self.data["id"],
                    "timelines": [
                        {
                            "create_time": int(self.data.get("create_time") or now),
                            "id": self.data["id"],
                            "is_marked_delete": False,
                            "name": "Timeline 01",
                            "update_time": now,
                        }
                    ],
                    "update_time": now,
                    "version": 0,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    # --------------------------------------------------------------- editing

    @property
    def duration_us(self) -> int:
        self._recompute()
        return int(self.data.get("duration", 0))

    def _materials(self, kind: str) -> list[dict]:
        return self.data.setdefault("materials", {}).setdefault(kind, [])

    def _material(self, material_id: str) -> dict | None:
        for items in self.data.get("materials", {}).values():
            for item in items:
                if item.get("id") == material_id:
                    return item
        return None

    def _register_media(
        self, info: media.MediaInfo, metetype: str, duration_us: int | None = None
    ) -> str:
        """Record the file in draft_meta_info so CapCut does not ask to relink it."""
        groups = self.meta.setdefault("draft_materials", [])
        group = next((g for g in groups if g.get("type") == 0), None)
        if group is None:
            group = {"type": 0, "value": []}
            groups.insert(0, group)
        now_s = int(time.time())
        entry = {
            "ai_group_type": "",
            "create_time": now_s,
            "duration": int(duration_us if duration_us is not None else (info.duration_us or 0)),
            "enter_from": 0,
            "extra_info": info.path.name,
            "file_Path": _win(info.path),
            "height": int(info.height or 0),
            "id": _uid_lower(),
            "import_time": now_s,
            "import_time_ms": _now_us(),
            "item_source": 1,
            "md5": "",
            "metetype": metetype,
            "roughcut_time_range": {"duration": -1, "start": -1},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0,
            "width": int(info.width or 0),
        }
        group.setdefault("value", []).append(entry)
        return entry["id"]

    def _companions(self, kind: str, speed: float, fade_in: float, fade_out: float) -> list[str]:
        protos = _prototypes().get("companions", {})
        refs: list[str] = []
        for companion_kind in COMPANIONS.get(kind, []):
            proto = protos.get(companion_kind)
            if not proto:
                continue
            item = copy.deepcopy(proto)
            item["id"] = _uid()
            if companion_kind == "speeds":
                item["speed"] = float(speed)
            elif companion_kind == "audio_fades":
                item["fade_in_duration"] = _seconds(fade_in)
                item["fade_out_duration"] = _seconds(fade_out)
            self._materials(companion_kind).append(item)
            refs.append(item["id"])
        return refs

    def _track(self, track_type: str) -> dict:
        for track in self.data.setdefault("tracks", []):
            if track.get("type") == track_type:
                return track
        return self._new_track(track_type)

    def _new_track(self, track_type: str) -> dict:
        """Append another track of this type, for clips that overlap the first one."""
        proto = _prototypes().get("tracks", {}).get(track_type) or {
            "id": "",
            "type": track_type,
            "flag": 0,
            "attribute": 0,
            "name": "",
            "is_default_name": True,
        }
        track = copy.deepcopy(proto)
        track.update(id=_uid(), type=track_type, segments=[])
        self.data.setdefault("tracks", []).append(track)
        return track

    def _track_end(self, track: dict) -> int:
        return max(
            (
                seg["target_timerange"]["start"] + seg["target_timerange"]["duration"]
                for seg in track.get("segments", [])
            ),
            default=0,
        )

    def _free_start(self, track: dict, desired: int, duration: int) -> int:
        """First gap at or after ``desired`` that fits ``duration`` without overlap."""
        cursor = max(0, desired)
        for seg in sorted(track.get("segments", []), key=lambda s: s["target_timerange"]["start"]):
            start = seg["target_timerange"]["start"]
            end = start + seg["target_timerange"]["duration"]
            if cursor + duration <= start:
                break
            if cursor < end:
                cursor = end
        return cursor

    def add_video(
        self,
        file_path: str,
        start: float | None = None,
        duration: float | None = None,
        trim_start: float = 0.0,
        volume: float = 1.0,
        speed: float = 1.0,
    ) -> dict:
        """Add a video clip or a still image to the main video track."""
        info = media.probe(file_path)
        if info.kind == "music":
            raise DraftError(f"{info.path.name} is audio; use add_audio instead")

        is_photo = info.kind == "photo"
        if speed <= 0:
            raise DraftError("speed must be greater than 0")

        source_start = _seconds(trim_start)
        available = info.duration_us
        if is_photo:
            seg_duration = _seconds(duration) if duration is not None else DEFAULT_PHOTO_US
            source_duration = seg_duration
            estimated = False
        else:
            if available is None:
                if duration is None:
                    raise DraftError(
                        f"Could not read the duration of {info.path.name}; pass duration explicitly"
                    )
                available = _seconds(duration)
                estimated = True
            else:
                estimated = info.estimated
            source_duration = _seconds(duration) if duration is not None else max(0, available - source_start)
            source_duration = min(source_duration, max(0, available - source_start))
            if source_duration <= 0:
                raise DraftError("trim_start is at or past the end of the clip")
            seg_duration = max(1, int(round(source_duration / float(speed))))

        material_protos = _prototypes()["materials"]
        material = copy.deepcopy(
            material_protos.get("photo" if is_photo else "video") or material_protos["video"]
        )
        material.update(
            id=_uid(),
            type="photo" if is_photo else "video",
            path=_win(info.path),
            media_path="",
            material_name=info.path.name,
            duration=source_duration if not is_photo else seg_duration,
            width=int(info.width or 0),
            height=int(info.height or 0),
            local_material_id=self._register_media(
                info,
                "photo" if is_photo else "video",
                duration_us=seg_duration if is_photo else None,
            ),
            material_id="",
            unique_id="",
            local_id="",
            has_audio=bool(info.has_audio and not is_photo),
            has_sound_separated=False,
        )
        if is_photo:
            material["has_audio"] = False
        self._materials("videos").append(material)

        track = self._track("video")
        desired = self._track_end(track) if start is None else _seconds(start)
        target_start = self._free_start(track, desired, seg_duration)

        segment = copy.deepcopy(_prototypes()["segments"]["video"])
        segment.update(
            id=_uid(),
            material_id=material["id"],
            extra_material_refs=self._companions("video", speed, 0.0, 0.0),
            target_timerange={"start": target_start, "duration": seg_duration},
            source_timerange=None if is_photo else {"start": source_start, "duration": source_duration},
            speed=float(speed),
            volume=max(0.0, min(1.0, float(volume))),
            last_nonzero_volume=max(0.0, min(1.0, float(volume))),
            render_timerange={"start": 0, "duration": 0},
        )
        track.setdefault("segments", []).append(segment)

        return {
            "segment_id": segment["id"],
            "material_id": material["id"],
            "kind": material["type"],
            "start_s": round(target_start / US, 3),
            "duration_s": round(seg_duration / US, 3),
            "source": info.as_dict(),
            "duration_estimated": estimated,
        }

    def add_audio(
        self,
        file_path: str,
        start: float | None = None,
        duration: float | None = None,
        trim_start: float = 0.0,
        volume: float = 1.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
    ) -> dict:
        """Add a music/SFX clip to an audio track."""
        info = media.probe(file_path)
        if info.kind != "music":
            raise DraftError(f"{info.path.name} looks like video; use add_video instead")

        source_start = _seconds(trim_start)
        available = info.duration_us
        if available is None:
            if duration is None:
                raise DraftError(
                    f"Could not read the duration of {info.path.name}; pass duration explicitly"
                )
            available = _seconds(duration)
        seg_duration = _seconds(duration) if duration is not None else max(0, available - source_start)
        seg_duration = min(seg_duration, max(0, available - source_start))
        if seg_duration <= 0:
            raise DraftError("trim_start is at or past the end of the clip")

        material = copy.deepcopy(_prototypes()["materials"]["audio"])
        material.update(
            id=_uid(),
            path=_win(info.path),
            material_name=info.path.name,
            duration=available,
            local_material_id=self._register_media(info, "music"),
            material_id="",
            unique_id="",
            local_id="",
        )
        self._materials("audios").append(material)

        track = self._track("audio")
        desired = self._track_end(track) if start is None else _seconds(start)
        target_start = self._free_start(track, desired, seg_duration)

        segment = copy.deepcopy(_prototypes()["segments"]["audio"])
        segment.update(
            id=_uid(),
            material_id=material["id"],
            clip=None,
            uniform_scale=None,
            extra_material_refs=self._companions("audio", 1.0, fade_in, fade_out),
            target_timerange={"start": target_start, "duration": seg_duration},
            source_timerange={"start": source_start, "duration": seg_duration},
            speed=1.0,
            volume=max(0.0, min(1.0, float(volume))),
            last_nonzero_volume=max(0.0, min(1.0, float(volume))),
            render_timerange={"start": 0, "duration": 0},
        )
        track.setdefault("segments", []).append(segment)

        return {
            "segment_id": segment["id"],
            "material_id": material["id"],
            "kind": "music",
            "start_s": round(target_start / US, 3),
            "duration_s": round(seg_duration / US, 3),
            "source": info.as_dict(),
            "duration_estimated": bool(info.estimated),
        }

    def _resolve_text_font(self, font_path: str, font_family: str, font_style: str) -> tuple[str, str]:
        """(absolute font path, display name) for the requested font."""
        if font_path:
            path = Path(font_path).expanduser()
            if not path.is_file():
                raise DraftError(f"Font file not found: {path}")
            return _win(path), path.stem
        match = fonts.resolve(font_family, font_style)
        if match:
            return _win(match.path), match.family
        if font_family or font_style:
            raise DraftError(
                f"No installed font matches family={font_family!r} style={font_style!r}. "
                "Use list_fonts to see the available families."
            )
        return "", ""

    def _set_text_fields(
        self,
        material: dict,
        *,
        text: str | None,
        size: float,
        color: str,
        align: int,
        font_path: str,
        font_name: str,
        bold: bool,
        italic: bool,
        underline: bool,
        tracking: float,
        line_spacing: float,
        outline_width: float,
        outline_color: str,
        shadow: bool,
        shadow_color: str,
        shadow_distance: float,
        background: str,
        background_alpha: float,
        opacity: float,
    ) -> None:
        if size <= 0:
            raise DraftError("size must be greater than 0")
        if align not in (0, 1, 2):
            raise DraftError("align must be 0 (left), 1 (centre) or 2 (right)")
        if not 0.0 <= opacity <= 1.0:
            raise DraftError("opacity must be between 0 and 1")
        if not 0.0 <= background_alpha <= 1.0:
            raise DraftError("background_alpha must be between 0 and 1")

        rgb = _parse_color(color)
        hex_color = "#" + color.strip().lstrip("#").upper()
        style = {
            "fill": {"content": {"render_type": "solid", "solid": {"color": rgb}}},
            "font": {"path": font_path, "id": ""},
            "size": float(size),
            "range": [0, 100],
            "bold": bool(bold),
            "italic": bool(italic),
        }
        if text is None:
            try:
                text = json.loads(material.get("content") or "{}").get("text", "")
            except Exception:
                text = material.get("content", "")

        material.update(
            content=json.dumps(
                {"text": text, "styles": [style], "layer_weight": 1, "effect": []},
                ensure_ascii=False,
            ),
            base_content="",
            font_size=float(size),
            font_path=font_path,
            font_name=font_name,
            # Never leave a template project's font resource id behind.
            font_title="none",
            font_resource_id="",
            font_id="",
            font_third_resource_id="",
            font_source_platform=0,
            fonts=[],
            text_color=hex_color,
            text_alpha=float(opacity),
            global_alpha=float(opacity),
            alignment=int(align),
            letter_spacing=float(tracking),
            line_spacing=float(line_spacing),
            bold_width=0.06 if bold else 0.0,
            italic_degree=30 if italic else 0,
            underline=bool(underline),
            border_width=float(outline_width),
            border_color=outline_color if outline_width > 0 else "",
            has_shadow=bool(shadow),
            shadow_color=shadow_color if shadow else "",
            shadow_distance=float(shadow_distance),
            background_color=background if background else "",
            background_alpha=float(background_alpha),
            background_style=1 if background else 0,
            text_size=30,
        )

    def add_text(
        self,
        text: str,
        start: float = 0.0,
        duration: float = 3.0,
        size: float = 15.0,
        color: str = "#FFFFFF",
        align: int = 1,
        x: float = 0.0,
        y: float = 0.0,
        font_path: str = "",
        font_family: str = "",
        font_style: str = "",
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
        target_track: dict | None = None,
    ) -> dict:
        """Add a text (title/caption) clip above the video."""
        if not text.strip():
            raise DraftError("text must not be empty")
        seg_duration = _seconds(duration)
        if seg_duration <= 0:
            raise DraftError("duration must be greater than 0")

        resolved_path, resolved_name = self._resolve_text_font(font_path, font_family, font_style)
        material = copy.deepcopy(_prototypes()["materials"]["text"])
        material.update(id=_uid(), type="text")
        self._set_text_fields(
            material,
            text=text,
            size=size,
            color=color,
            align=align,
            font_path=resolved_path,
            font_name=resolved_name,
            bold=bold,
            italic=italic,
            underline=underline,
            tracking=tracking,
            line_spacing=line_spacing,
            outline_width=outline_width,
            outline_color=("#" + outline_color.strip().lstrip("#").upper()) if outline_width > 0 else "",
            shadow=shadow,
            shadow_color=("#" + shadow_color.strip().lstrip("#").upper()) if shadow else "",
            shadow_distance=shadow_distance,
            background=("#" + background.strip().lstrip("#").upper()) if background else "",
            background_alpha=background_alpha,
            opacity=opacity,
        )
        self._materials("texts").append(material)

        track = target_track or (self._new_track("text") if new_track else self._track("text"))
        target_start = self._free_start(track, _seconds(start), seg_duration)

        segment = copy.deepcopy(_prototypes()["segments"]["text"])
        segment.update(
            id=_uid(),
            material_id=material["id"],
            extra_material_refs=[],
            source_timerange=None,
            target_timerange={"start": target_start, "duration": seg_duration},
            clip={
                "scale": {"x": 1.0, "y": 1.0},
                "rotation": 0.0,
                "transform": {"x": float(x), "y": float(y)},
                "flip": {"vertical": False, "horizontal": False},
                "alpha": 1.0,
            },
            uniform_scale={"on": True, "value": 1.0},
            render_timerange={"start": 0, "duration": 0},
        )
        track.setdefault("segments", []).append(segment)

        return {
            "segment_id": segment["id"],
            "material_id": material["id"],
            "kind": "text",
            "start_s": round(target_start / US, 3),
            "duration_s": round(seg_duration / US, 3),
            "text": text,
            "font": material.get("font_path", ""),
            "font_name": material.get("font_name", ""),
        }

    def style_text(
        self,
        segment_id: str,
        *,
        text: str | None = None,
        size: float | None = None,
        color: str | None = None,
        align: int | None = None,
        font_path: str | None = None,
        font_family: str | None = None,
        font_style: str | None = None,
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
        """Restyle an existing text clip without re-timing it."""
        track, segment = self._find_segment(segment_id)
        if track.get("type") != "text":
            raise DraftError("style_text only applies to text segments")
        material = self._material(segment["material_id"])
        if material is None:
            raise DraftError("Text material not found for this segment")

        if font_path is not None:
            resolved = self._resolve_text_font(font_path, "", "")
        elif font_family is not None or font_style is not None:
            resolved = self._resolve_text_font("", font_family or "", font_style or "")
        else:
            resolved = (material.get("font_path", ""), material.get("font_name", ""))

        self._set_text_fields(
            material,
            text=text,
            size=size if size is not None else float(material.get("font_size", 15.0) or 15.0),
            color=color if color is not None else (material.get("text_color") or "#FFFFFF"),
            align=align if align is not None else int(material.get("alignment", 1) or 1),
            font_path=resolved[0],
            font_name=resolved[1],
            bold=bold if bold is not None else float(material.get("bold_width", 0) or 0) > 0,
            italic=italic if italic is not None else float(material.get("italic_degree", 0) or 0) > 0,
            underline=underline if underline is not None else bool(material.get("underline")),
            tracking=tracking if tracking is not None else float(material.get("letter_spacing", 0) or 0),
            line_spacing=line_spacing if line_spacing is not None else float(material.get("line_spacing", 0.02) or 0.02),
            outline_width=outline_width if outline_width is not None else float(material.get("border_width", 0) or 0),
            outline_color=outline_color if outline_color is not None else (material.get("border_color") or "#000000"),
            shadow=shadow if shadow is not None else bool(material.get("has_shadow")),
            shadow_color=shadow_color if shadow_color is not None else (material.get("shadow_color") or "#000000"),
            shadow_distance=shadow_distance if shadow_distance is not None else float(material.get("shadow_distance", 5.0) or 5.0),
            background=background if background is not None else (material.get("background_color") or ""),
            background_alpha=background_alpha if background_alpha is not None else float(material.get("background_alpha", 1.0) or 1.0),
            opacity=opacity if opacity is not None else float(material.get("global_alpha", 1.0) or 1.0),
        )
        return {
            "segment_id": segment_id,
            "material_id": material["id"],
            "text": json.loads(material["content"])["text"],
            "font": material.get("font_path", ""),
            "size": material.get("font_size"),
        }

    def trim_segment(
        self,
        segment_id: str,
        start: float | None = None,
        duration: float | None = None,
        source_start: float | None = None,
    ) -> dict:
        """Move or re-time an existing segment."""
        track, segment = self._find_segment(segment_id)
        target = segment["target_timerange"]
        if start is not None:
            target["start"] = _seconds(start)
        if duration is not None:
            new_duration = _seconds(duration)
            if new_duration <= 0:
                raise DraftError("duration must be greater than 0")
            target["duration"] = new_duration
        if source_start is not None:
            if segment.get("source_timerange") is None:
                raise DraftError("This segment has no source range to trim (still image or text)")
            segment["source_timerange"]["start"] = _seconds(source_start)
        if duration is not None and segment.get("source_timerange") is not None:
            speed = float(segment.get("speed", 1.0)) or 1.0
            segment["source_timerange"]["duration"] = int(round(target["duration"] * speed))
        return {
            "segment_id": segment_id,
            "track": track["type"],
            "start_s": round(target["start"] / US, 3),
            "duration_s": round(target["duration"] / US, 3),
        }

    def delete_segment(self, segment_id: str) -> dict:
        """Remove a segment and any materials that only it referenced."""
        track, segment = self._find_segment(segment_id)
        track["segments"] = [s for s in track["segments"] if s["id"] != segment_id]
        removed = [segment["material_id"]] + list(segment.get("extra_material_refs", []))

        still_used = set()
        for other_track in self.data.get("tracks", []):
            for other in other_track.get("segments", []):
                still_used.add(other.get("material_id"))
                still_used.update(other.get("extra_material_refs", []))

        dropped = 0
        for kind, items in self.data.get("materials", {}).items():
            keep = [m for m in items if m.get("id") not in removed or m.get("id") in still_used]
            dropped += len(items) - len(keep)
            self.data["materials"][kind] = keep

        return {"segment_id": segment_id, "track": track["type"], "materials_removed": dropped}

    # ------------------------------------------------------------ animation

    def animate_segment(
        self,
        segment_id: str,
        preset: str | None = None,
        duration: float | None = None,
        keyframes: list[dict] | None = None,
    ) -> dict:
        """Attach keyframe animation to a clip, text or audio segment."""
        if preset and keyframes:
            raise DraftError("Pass either a preset or explicit keyframes, not both")
        if not preset and not keyframes:
            raise DraftError("Pass a preset name (fade_in, slide_in_left, zoom_in, ...) or keyframes")

        track, segment = self._find_segment(segment_id)
        segment_s = int(segment["target_timerange"]["duration"]) / US
        try:
            tracks = anim.preset(preset, segment_s, duration) if preset else anim.custom(keyframes, segment_s)
        except anim.AnimationError as exc:
            raise DraftError(str(exc)) from exc

        existing = {
            item.get("property_type"): item for item in segment.get("common_keyframes", []) if item
        }
        merged = []
        for property_key, points in tracks.items():
            merged.append(anim.keyframe_list(property_key, points, segment_s))
            existing.pop(anim.PROPERTY_TYPES[property_key], None)
        segment["common_keyframes"] = list(existing.values()) + merged

        return {
            "segment_id": segment_id,
            "track": track["type"],
            "preset": preset or "custom",
            "properties": sorted(tracks),
            "keyframe_count": sum(len(points) for points in tracks.values()),
            "duration_s": round(segment_s, 3),
        }

    # ----------------------------------------------------------- transitions

    def add_transition(
        self, segment_id: str, name: str | None = None, duration: float | None = None
    ) -> dict:
        """Attach a cached CapCut transition to the end of a video clip."""
        track, segment = self._find_segment(segment_id)
        if track["type"] != "video":
            raise DraftError("Transitions apply to video/image clips")

        catalogue = cached_transitions()
        if not catalogue:
            raise DraftError(
                "No cached CapCut transitions found. CapCut downloads transition assets on "
                "first use, so add the transition once in CapCut, then retry."
            )
        entry = None
        if name:
            wanted = name.strip().casefold()
            entry = next((item for item in catalogue if item["name"].casefold() == wanted), None)
            if entry is None:
                entry = next((item for item in catalogue if wanted in item["name"].casefold()), None)
            if entry is None:
                names = ", ".join(item["name"] for item in catalogue)
                raise DraftError(f"No cached transition named {name!r}. Available: {names}")
        else:
            entry = catalogue[0]

        transition_duration = (
            _seconds(duration)
            if duration is not None
            else max(1, int(round(entry["default_duration_s"] * US)))
        )

        # A segment carries at most one transition: drop any existing one first.
        for ref in list(segment.get("extra_material_refs", [])):
            material = self._material(ref)
            if material and material.get("type") == "transition":
                segment["extra_material_refs"].remove(ref)
                self.data["materials"]["transitions"] = [
                    item for item in self._materials("transitions") if item["id"] != ref
                ]

        # Clone the cached CapCut material so the asset path and effect ids match what
        # the app itself writes; a bare material would show up as an offline effect.
        material = copy.deepcopy(entry.get("material") or {})
        material.update(
            id=_uid(),
            type="transition",
            name=entry["name"],
            duration=transition_duration,
            resource_id=entry["resource_id"],
            effect_id=entry["effect_id"],
            path=entry.get("path", material.get("path", "")),
        )
        self._materials("transitions").append(material)
        segment.setdefault("extra_material_refs", []).append(material["id"])
        return {
            "segment_id": segment_id,
            "transition": entry["name"],
            "duration_s": round(transition_duration / US, 3),
            "applies_at": "end of this clip (into the next one)",
        }

    # --------------------------------------------------------------- filters

    def apply_filter(
        self, segment_id: str, name: str | None = None, intensity: float = 1.0
    ) -> dict:
        """Attach a cached CapCut filter to a video/image clip.

        `intensity` is 0-1 (CapCut's UI shows it as 0-100%).
        """
        if not 0.0 <= intensity <= 1.0:
            raise DraftError("filter intensity must be between 0 and 1")
        track, segment = self._find_segment(segment_id)
        if track["type"] != "video":
            raise DraftError("Filters apply to video/image clips")

        catalogue = cached_filters()
        if not catalogue:
            raise DraftError(
                "No cached CapCut filters found. CapCut downloads filter assets on first "
                "use: apply the filter once in CapCut, then retry."
            )
        if name:
            wanted = name.strip().casefold()
            entry = next((item for item in catalogue if item["name"].casefold() == wanted), None)
            if entry is None:
                entry = next((item for item in catalogue if wanted in item["name"].casefold()), None)
            if entry is None:
                names = ", ".join(item["name"] for item in catalogue)
                raise DraftError(f"No cached filter named {name!r}. Available: {names}")
        else:
            entry = catalogue[0]

        # One filter per segment: drop whatever is attached now.
        for ref in list(segment.get("extra_material_refs", [])):
            material = self._material(ref)
            if material and material.get("type") == "filter":
                segment["extra_material_refs"].remove(ref)
                self.data["materials"]["filters"] = [
                    item for item in self._materials("filters") if item["id"] != ref
                ]

        material = copy.deepcopy(entry.get("material") or {})
        material.update(
            id=_uid(),
            type="filter",
            name=entry["name"],
            resource_id=entry["resource_id"],
            effect_id=entry["effect_id"],
            path=entry.get("path", material.get("path", "")),
            value=float(intensity),
        )
        self._materials("filters").append(material)
        segment.setdefault("extra_material_refs", []).append(material["id"])
        return {
            "segment_id": segment_id,
            "filter": entry["name"],
            "intensity": float(intensity),
        }

    # ----------------------------------------------------------------- masks

    def apply_mask(
        self,
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
        """Apply one of CapCut's built-in parametric masks to a video/image clip."""
        track, segment = self._find_segment(segment_id)
        if track["type"] != "video":
            raise DraftError("Masks apply to video/image clips")
        key = shape.strip().lower()
        if key not in MASK_SHAPES:
            raise DraftError(f"Unknown mask {shape!r}; use one of {', '.join(sorted(MASK_SHAPES))}")
        if not 0.0 <= feather <= 1.0:
            raise DraftError("feather must be between 0 and 1")
        if width <= 0 or height <= 0:
            raise DraftError("mask width and height must be greater than 0")

        meta = MASK_SHAPES[key]
        # Replace any mask already on this segment.
        for ref in list(segment.get("extra_material_refs", [])):
            material = self._material(ref)
            if material and material.get("type") == "mask":
                segment["extra_material_refs"].remove(ref)
                self.data["materials"]["common_mask"] = [
                    item for item in self._materials("common_mask") if item["id"] != ref
                ]

        mask = {
            "config": {
                "aspectRatio": meta["aspect"],
                "centerX": float(center_x),
                "centerY": float(center_y),
                "expansion": float(expansion),
                "feather": float(feather),
                "height": float(height),
                "invert": bool(invert),
                "rotation": float(rotation),
                "roundCorner": float(round_corner),
                "width": float(width),
            },
            "id": _uid(),
            "name": meta["name"],
            "platform": "all",
            "position_info": "",
            "resource_type": meta["resource_type"],
            "resource_id": meta["resource_id"],
            "type": "mask",
            "category": "video",
            "category_id": "jichu",
            "category_name": "基础",
            "is_old_version": False,
            "loader_work_space": "",
            "panel": "",
            "source_platform": 0,
            "track_segment": "",
            "text_config": {
                "align_type": 15,
                "bold_width": 0.0,
                "char_spacing": 0.0,
                "content": "",
                "font_name": "",
                "font_path": "",
                "font_resource_id": "",
                "font_size": 15.0,
                "has_underline": False,
                "italic_degree": 0,
                "line_gap": 0.0,
                "scale": 1.0,
            },
        }
        self._materials("common_mask").append(mask)
        segment.setdefault("extra_material_refs", []).append(mask["id"])
        segment["enable_video_mask"] = True
        return {
            "segment_id": segment_id,
            "shape": meta["name"],
            "mask_id": mask["id"],
            "width": float(width),
            "height": float(height),
        }

    # --------------------------------------------------------- speed / timing

    def set_speed(self, segment_id: str, speed: float) -> dict:
        """Change constant playback speed; the clip's timeline length follows.

        The footage used is unchanged (source range stays), so at 2x the clip occupies
        half the timeline and later clips on that track move earlier to avoid overlap.
        Gaps that appear when slowing a clip down are left alone; move clips with
        trim_segment if you need them tight.
        """
        if speed <= 0:
            raise DraftError("speed must be greater than 0")
        if speed > 10:
            raise DraftError("speed must be 10 or less")
        track, segment = self._find_segment(segment_id)
        if track["type"] not in ("video", "audio"):
            raise DraftError("set_speed applies to video and audio clips")

        source = segment.get("source_timerange")
        if not source or int(source.get("duration", 0)) <= 0:
            raise DraftError(
                "This segment has no source footage to retime (still images and text "
                "have no speed); adjust its duration instead."
            )
        source_duration = int(source["duration"])

        target = segment["target_timerange"]
        old_duration = int(target["duration"])
        old_start = int(target["start"])
        new_duration = max(1, int(round(source_duration / float(speed))))
        delta = new_duration - old_duration

        segment["speed"] = float(speed)
        for ref in segment.get("extra_material_refs", []):
            material = self._material(ref)
            if material and material.get("type") == "speed":
                material["speed"] = float(speed)

        target["duration"] = new_duration
        shifted = 0
        if delta:
            for other in track.get("segments", []):
                if other is segment:
                    continue
                start = int(other["target_timerange"]["start"])
                if start >= old_start + old_duration:
                    other["target_timerange"]["start"] = max(0, start + delta)
                    shifted += 1

        return {
            "segment_id": segment_id,
            "speed": float(speed),
            "source_duration_s": round(source_duration / US, 3),
            "timeline_duration_s": round(new_duration / US, 3),
            "segment_shifted_s": round(delta / US, 3),
            "later_segments_shifted": shifted,
        }

    def _clone_companions(self, refs: list[str]) -> list[str]:
        """Deep-copy companion materials so two segments do not share mutable state."""
        cloned: list[str] = []
        for ref in refs:
            original = self._material(ref)
            if not original:
                continue
            clone = copy.deepcopy(original)
            clone["id"] = _uid()
            for items in self.data.get("materials", {}).values():
                if any(item.get("id") == ref for item in items):
                    items.append(clone)
                    break
            cloned.append(clone["id"])
        return cloned

    def split_segment(self, segment_id: str, at: float) -> dict:
        """Split a clip in two at an absolute timeline position (seconds)."""
        track, segment = self._find_segment(segment_id)
        start = int(segment["target_timerange"]["start"])
        duration = int(segment["target_timerange"]["duration"])
        cut = _seconds(at)
        if not start < cut < start + duration:
            raise DraftError(
                f"split time {at}s must be inside the clip "
                f"({start / US:.2f}s - {(start + duration) / US:.2f}s)"
            )

        first_duration = cut - start
        second_duration = duration - first_duration
        source = segment.get("source_timerange")
        second_source = None
        if source:
            speed = float(segment.get("speed", 1.0)) or 1.0
            first_source_duration = int(round(first_duration * speed))
            second_source = {
                "start": int(source["start"]) + first_source_duration,
                "duration": int(round(second_duration * speed)),
            }
            segment["source_timerange"] = {
                "start": int(source["start"]),
                "duration": first_source_duration,
            }

        second = copy.deepcopy(segment)
        second["id"] = _uid()
        second["target_timerange"] = {"start": cut, "duration": second_duration}
        second["source_timerange"] = second_source
        second["extra_material_refs"] = self._clone_companions(segment.get("extra_material_refs", []))
        second["common_keyframes"] = []
        segment["target_timerange"]["duration"] = first_duration
        segment["common_keyframes"] = []

        index = track["segments"].index(segment)
        track["segments"].insert(index + 1, second)
        return {
            "first_segment_id": segment["id"],
            "second_segment_id": second["id"],
            "first_duration_s": round(first_duration / US, 3),
            "second_duration_s": round(second_duration / US, 3),
            "note": "keyframes were cleared on both halves of the split clip",
        }

    def duplicate_segment(self, segment_id: str, start: float | None = None) -> dict:
        """Copy a clip (sharing its source material) to the end of its track or `start`."""
        track, segment = self._find_segment(segment_id)
        duration = int(segment["target_timerange"]["duration"])
        clone = copy.deepcopy(segment)
        clone["id"] = _uid()
        desired = self._track_end(track) if start is None else _seconds(start)
        new_start = self._free_start(track, desired, duration)
        clone["target_timerange"] = {"start": new_start, "duration": duration}
        clone["extra_material_refs"] = self._clone_companions(segment.get("extra_material_refs", []))
        clone["common_keyframes"] = []
        track["segments"].append(clone)
        return {
            "source_segment_id": segment_id,
            "segment_id": clone["id"],
            "start_s": round(new_start / US, 3),
            "duration_s": round(duration / US, 3),
        }

    def ripple_delete_segment(self, segment_id: str) -> dict:
        """Delete a clip and pull later clips on that track back to close the gap."""
        track, segment = self._find_segment(segment_id)
        start = int(segment["target_timerange"]["start"])
        duration = int(segment["target_timerange"]["duration"])
        self.delete_segment(segment_id)
        shifted = 0
        for other in track["segments"]:
            other_start = int(other["target_timerange"]["start"])
            if other_start > start:
                other["target_timerange"]["start"] = max(0, other_start - duration)
                shifted += 1
        return {
            "segment_id": segment_id,
            "ripple_s": round(duration / US, 3),
            "segments_shifted": shifted,
        }

    # ------------------------------------------------------------- subtitles

    def import_srt(
        self,
        srt_path: str,
        *,
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
        """Turn an .srt file into one text clip per cue, on their own text track.

        `x`/`y` position every cue the same way (``y=-0.6`` is a typical lower third),
        and `align` is 0=left / 1=centre / 2=right.
        """
        cues = subtitles.parse_srt(srt_path)
        # A dedicated track keeps cue times exact even when titles already occupy the
        # first text track (captions must not be pushed later to avoid a collision).
        track = self._new_track("text") if separate_track else None
        created = []
        for cue in cues:
            created.append(
                self.add_text(
                    cue.text,
                    start=cue.start,
                    duration=max(0.20, cue.duration),
                    size=size,
                    color=color,
                    font_family=font_family,
                    font_style=font_style,
                    bold=bold,
                    outline_width=outline_width,
                    outline_color=outline_color,
                    shadow=shadow,
                    align=align,
                    x=x,
                    y=y,
                    target_track=track,
                )
            )
        return {
            "cues": len(created),
            "first_start_s": round(cues[0].start, 3),
            "last_end_s": round(cues[-1].end, 3),
            "text_track": str(track["id"]) if track else "existing",
            "segments": created,
        }

    def _find_segment(self, segment_id: str) -> tuple[dict, dict]:
        for track in self.data.get("tracks", []):
            for segment in track.get("segments", []):
                if segment.get("id") == segment_id:
                    return track, segment
        raise DraftError(f"No segment with id {segment_id}")

    def set_canvas(self, width: int, height: int) -> dict:
        if width <= 0 or height <= 0:
            raise DraftError("width and height must be positive")
        self.data["canvas_config"] = {
            "ratio": _ratio(int(width), int(height)),
            "width": int(width),
            "height": int(height),
            "background": self.data.get("canvas_config", {}).get("background"),
        }
        return dict(self.data["canvas_config"])

    def rename(self, name: str) -> dict:
        self.data["name"] = name
        self.meta["draft_name"] = name
        return {"name": name}

    def _recompute(self) -> None:
        end = 0
        counters = {"video": 0, "text": 0, "audio": 0}
        for track in self.data.get("tracks", []):
            track_type = track.get("type", "video")
            base = TRACK_RENDER_BASE.get(track_type, 0)
            track_render_index = base + counters.get(track_type, 0)
            counters[track_type] = counters.get(track_type, 0) + 1
            for segment in track.get("segments", []):
                segment["track_render_index"] = track_render_index
                segment["render_index"] = TEXT_SEGMENT_RENDER_INDEX if track_type == "text" else 0
                timing = segment.get("target_timerange") or {}
                end = max(end, int(timing.get("start", 0)) + int(timing.get("duration", 0)))
        self.data["duration"] = end

    # -------------------------------------------------------------- reporting

    def summary(self) -> dict:
        self._recompute()
        tracks = []
        for track in self.data.get("tracks", []):
            segments = []
            for segment in track.get("segments", []):
                material = self._material(segment.get("material_id", "")) or {}
                timing = segment.get("target_timerange") or {}
                entry = {
                    "segment_id": segment["id"],
                    "material_id": segment.get("material_id"),
                    "start_s": round(int(timing.get("start", 0)) / US, 3),
                    "duration_s": round(int(timing.get("duration", 0)) / US, 3),
                    "label": material.get("material_name") or material.get("name") or "",
                    "source": material.get("path") or None,
                }
                if track.get("type") == "text" and material.get("content"):
                    try:
                        entry["text"] = json.loads(material["content"]).get("text", "")
                    except Exception:
                        entry["text"] = material.get("content", "")
                    entry["font"] = material.get("font_name") or material.get("font_path") or "default"
                    entry["size"] = material.get("font_size")
                if float(segment.get("speed", 1.0) or 1.0) != 1.0:
                    entry["speed"] = float(segment["speed"])
                animations = [
                    item.get("property_type", "?")
                    for item in segment.get("common_keyframes", [])
                    if item and item.get("keyframe_list")
                ]
                if animations:
                    entry["keyframes"] = animations
                for ref in segment.get("extra_material_refs", []):
                    companion = self._material(ref) or {}
                    if companion.get("type") == "mask":
                        entry["mask"] = companion.get("name", "")
                    elif companion.get("type") == "transition":
                        entry["transition"] = companion.get("name", "")
                    elif companion.get("type") == "filter":
                        entry["filter"] = companion.get("name", "")
                segments.append(entry)
            tracks.append({"track_id": track["id"], "type": track.get("type"), "segments": segments})

        return {
            "name": self.data.get("name") or self.meta.get("draft_name", ""),
            "folder": str(self.folder),
            "duration_s": round(int(self.data.get("duration", 0)) / US, 3),
            "fps": self.data.get("fps"),
            "canvas": self.data.get("canvas_config"),
            "capcut_version": (self.data.get("platform") or {}).get("app_version"),
            "schema_version": self.data.get("new_version"),
            "track_count": len(tracks),
            "tracks": tracks,
        }
