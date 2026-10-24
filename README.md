# CapCut MCP

A local [Model Context Protocol](https://modelcontextprotocol.io) server that builds and edits
**CapCut desktop projects**: clips, stills, music, rich typography, keyframe animation, masks,
transitions, filters, captions and timeline surgery — with a safe live-edit loop.

## Why this design

CapCut has no public desktop API, no automation server, and no documented deep link for opening a
specific draft. What it *does* have is projects stored as plain JSON on disk. This server writes
those files — the same ones CapCut reads — and uses window-level calls only to launch or focus the
app.

| Capability | How it works |
| --- | --- |
| Create projects, add clips/stills/audio/text, trim, split, duplicate, ripple-delete, retime | Generates `draft_content.json` + `draft_meta_info.json` matching the installed build's schema |
| Animation | Keyframes (`x`, `y`, `rotation`, `scale`, `alpha`, `volume`, `saturation`, `contrast`, `brightness`) or presets (`fade_in`, `slide_in_*`, `zoom_in/out`, `pop`, `rotate_in`, `ken_burns`) |
| Typography | Any installed font file — Windows fonts and CapCut's own — with bold/italic/underline, letter spacing, line spacing, outline, shadow, background box and opacity |
| Masks | CapCut's built-in parametric shapes (circle, rectangle, heart, star, linear, mirror) with feather, rotation, invert |
| Transitions & filters | Clones CapCut's cached effect materials, including the asset path, so they render offline |
| Captions | `.srt` import as one timed text clip per cue, on their own track |
| Live editing | Every edit re-reads the project; atomic writes; conflict detection; `sync_status` reports who wrote last |
| No relink prompt | Registers every media file in the `draft_materials` sidecar |
| Open the result | You reopen the project (or restart the app); export stays manual |

Draft schemas are **private and version-sensitive**, so the JSON shapes in
`capcut_mcp/templates/` were extracted from projects written by the CapCut build installed on
this machine (see `tools/extract_templates.py`). Re-run that tool after a CapCut upgrade.

## How "real time" works (and its one hard limit)

CapCut never reloads a draft while the project is open, and it exposes no reload hook. The server
therefore makes editing *safe and current*, not magically visible:

1. **Every tool call re-reads the project from disk**, so the base state always includes whatever
   CapCut last saved — including edits you made in the app.
2. **Writes are atomic** (`os.replace`) and update both `draft_content.json` and the
   `Timelines/<id>/` mirror CapCut 8+ reads, so the app never sees a half-written file.
3. **Concurrent saves are refused, not clobbered.** If the file changes between the read and the
   write, the edit raises a conflict instead of overwriting CapCut's work.
4. **`sync_status` tells you who wrote last** — `changed_since_last_edit`, `last_edit_at`,
   `file_modified_ago_s`, `capcut_running`, plus an `advice` string.
5. **The app shows the change after reopening the project** (or restarting CapCut).

The limit is CapCut's, not the server's: if a project is open in CapCut and CapCut saves from its
in-memory copy, it can overwrite edits made meanwhile. `sync_status` reports that as
`changed_since_last_edit: true`, and the next edit starts from CapCut's version. Close the project
in CapCut (or reopen it) when you want MCP edits to be authoritative.

Typical loop:

```text
sync_status("Launch Teaser")      # who wrote last?
inspect_project("Launch Teaser")  # current timeline + segment ids
animate_segment(...)              # edit
                                   # reopen the project in CapCut to view
```

## Requirements

- Windows with **CapCut desktop installed** (built and verified against CapCut 8.5.0, schema 167.0.0)
- Python 3.10+
- `fastmcp` and `pillow` (Pillow reads font family/style names; without it no fonts are listed)

## Install

```bash
cd CapCut-MCP
pip install -r requirements.txt
python server.py --check     # diagnostic: app version, drafts folder, project list, font count
```

## Register with an MCP client

`python tools/print_config.py` prints a ready-to-paste entry with absolute paths for this machine.
For Claude Desktop, merge the `capcut` entry into `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "capcut": {
      "command": "C:\\Python312\\python.exe",
      "args": ["C:\\path\\to\\CapCut-MCP\\server.py"],
      "env": {
        "CAPCUT_DRAFTS_DIR": "C:\\Users\\<you>\\AppData\\Local\\CapCut\\User Data\\Projects\\com.lveditor.draft"
      }
    }
  }
}
```

`CAPCUT_DRAFTS_DIR` is optional — it defaults to CapCut's standard projects folder and only needs
setting if you moved the drafts location in CapCut's settings. Restart the client after editing.

## Tools

### Projects and live sync

| Tool | Purpose |
| --- | --- |
| `capcut_status` | Is CapCut installed/running, version, drafts folder, project count |
| `list_projects` | Projects, newest first, with duration and canvas |
| `inspect_project` | Tracks, clips, text, speed, keyframes, masks, transitions, filters, segment ids |
| `create_project` | New empty project (vertical 1080x1920 by default) |
| `sync_status` | Whether the file changed since the last MCP edit, and whether writing is safe |
| `reload_project` | Re-read from disk and report the current summary |
| `set_canvas` / `rename_project` | Resize / rename |
| `open_project_folder` / `focus_capcut` | Reveal the folder / bring the app to the front |

### Media and text

| Tool | Purpose |
| --- | --- |
| `add_video` | Video **or** still image, with trim / duration / speed / volume |
| `add_audio` | Music or SFX with volume, fade in/out |
| `add_text` | Title/caption with font family/style or font file, weights, outline, shadow, box, spacing, opacity |
| `style_text` | Restyle or rewrite an existing text clip without moving it |
| `list_fonts` | Searchable font catalogue (family, style, source, path) |
| `import_srt` | One timed text clip per subtitle cue, on a dedicated track |

### Animation, effects, timeline

| Tool | Purpose |
| --- | --- |
| `animate_segment` | Keyframe presets or explicit keyframes on any clip, text or audio |
| `apply_mask` | Parametric mask on a video/image clip |
| `list_transitions` / `add_transition` | Cached CapCut transitions, attached at a clip's end |
| `list_filters` / `apply_filter` | Cached CapCut filters with 0-1 intensity |
| `trim_segment` | Move or shorten a clip (optionally into its source) |
| `split_segment` | Split at an absolute timeline position |
| `duplicate_segment` | Copy a clip on the same track |
| `ripple_delete_segment` / `delete_segment` | Delete and close the gap / delete in place |
| `set_speed` | Retime a clip; the timeline length follows and later clips shift to stay valid |

### Typical agent flow

```text
create_project("Launch Teaser", 1080, 1920)
add_video("C:/clips/hero.mp4")
add_text("LAUNCHING FRIDAY", font_family="Arial", font_style="Black", size=26,
         bold=True, outline_width=0.6, shadow=True)
animate_segment(text_segment_id, preset="slide_in_bottom")
apply_mask(clip_segment_id, shape="circle", feather=0.2)
add_transition(clip_segment_id, name="Mix")
import_srt("C:/captions/teaser.srt", font_family="Arial", bold=True)
inspect_project("Launch Teaser")     # verify everything before opening CapCut
```

Then: **reopen the project in CapCut** (or restart CapCut) and press Export yourself.

## Limits

- **CapCut must reload the draft.** No reload API, and no scripted UI automation: edits appear after
  reopening the project or restarting the app. See "How real time works" above.
- **No export automation.** Rendering happens only through CapCut's UI.
- **Downloadable effects.** Keyframes are pure data and always work. CapCut's *library* animations,
  filters and transitions are downloaded assets: this server can only reuse ones already cached on
  the machine (apply each once in CapCut to cache it). No local animation catalogue exists, so
  library animations are not reproduced — use the keyframe presets or explicit keyframes.
- **One transition and one filter per clip**, matching CapCut's own model; adding another replaces
  it.
- **Text templates/effects** (CapCut's animated text presets) and **stickers/audio effects** are
  adaptive, asset-backed features with no local schema sample; they are not offered.
- **No cloud/mobile features**, no AI tools — only what the local draft format expresses.
- **Still images and text have no speed** (nothing to retime); change their duration instead.
- Drafts the app wrote with encryption (some non-Windows/JianYing builds) are rejected with a clear
  error instead of being corrupted.

## Verified against this machine

- `python -m unittest discover -s tests -t .` — **41 tests**: draft engine, MP4/WAV/PNG probing,
  material-reference integrity, overlap handling, relink registration, keyframes, fonts, masks,
  transitions, filters, speed/split/ripple, SRT import, conflict detection and live-sync state.
- `python tools/mcp_smoke.py` — drives the server through a real in-process MCP client on a
  throwaway drafts folder and checks every core tool's output.
- End-to-end over the stdio transport on a temporary drafts root: 28 tools listed; a project built
  with Arial Black text (outline/shadow/box), zoom-in and alpha/x keyframes, a circle mask, a cached
  transition, 2x retiming, split, duplicate and three SRT cues was re-read and checked
  independently (no dangling references, mirror copy identical, every font and effect asset
  present, no NaN).
- `python server.py --check` — reads the real drafts folder and the running app (204 fonts and 2
  cached transitions on this machine).

Automated checks cannot confirm that CapCut's UI renders a generated project; open it in the app to
confirm. Projects are ordinary folders, so a generated one can be deleted at any time.

## Layout

```text
CapCut-MCP/
  server.py                  MCP stdio server (tool definitions)
  capcut_mcp/
    draft.py                 draft read/write engine, keyframes, masks, transitions, filters
    anim.py                  animation presets and keyframe serialization
    fonts.py                 font catalogue (Windows + CapCut fonts, via Pillow)
    subtitles.py             SRT parsing
    state.py                 last-write bookkeeping for live sync
    media.py                 dependency-free duration/size probing (MP4, WAV, MP3, PNG, JPEG, WebP)
    app.py                   window detection, focus, launch, Explorer reveal
    paths.py                 draft folder + install discovery
    templates/               schema skeletons extracted from the installed build
  tools/
    extract_templates.py     re-derive templates after a CapCut upgrade
    mcp_smoke.py             end-to-end MCP client test
    print_config.py          print client config with absolute paths
  teaser/build_teaser.py     15s vertical demo: keyframes, fonts, SRT captions
  tests/test_draft.py
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `CapCut drafts folder not found` | Open CapCut once, or set `CAPCUT_DRAFTS_DIR` |
| New project does not appear in CapCut | Restart CapCut; confirm the folder is under the same drafts root it uses |
| Clips show as missing/relink | The media file moved after it was added — put it back or re-add it |
| `changed on disk since it was loaded` | CapCut saved the project mid-edit: call `reload_project`, then repeat the edit |
| Edits disappeared after editing in CapCut | CapCut saved its in-memory copy over them; `sync_status` flags this. Reopen the project before MCP edits, or re-apply them |
| `No cached ... transitions/filters` | Apply that effect once in CapCut so its asset downloads, then retry |
| `No installed font matches family=...` | Call `list_fonts` and use an exact family/style, or pass `font_path` |
| `not plain JSON (encrypted drafts are not supported)` | That project was written by an encrypted-schema build; this server only edits plain drafts |
| Changes do not show up | Reopen the project; CapCut caches open timelines in memory |
