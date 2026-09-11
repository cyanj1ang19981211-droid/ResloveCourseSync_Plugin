# Course Intensity Sync Plugin (DaVinci Resolve Editing Assistant)

A helper tool for video editors working in DaVinci Resolve: while scrubbing or playing the timeline, it shows **real-time exercise-intensity information for the course segment at the current playhead position** — segment name, movement/action, intensity keywords, and equipment-specific metrics (speed/pace/incline) — so editors can decide cut pacing based on the course's current intensity.

> English README. For the Chinese version, see [README.zh-CN.md](README.zh-CN.md).

## What It Does

- Reads DaVinci Resolve's current playhead timecode and **follows it automatically** (refreshes on scrub / play).
- Automatically matches a **course data file by the current timeline name**.
- Shows in an **always-on-top dark overlay window**:
  - The current **course segment** (e.g. "Fast Run", "Movement Tutorial 1")
  - The current **movement/action** (e.g. "Jog", "Tempo Run") + **intensity keywords** (e.g. "Aerobic Output", "Sustained Burn")
  - The current **intensity metrics** (speed / pace / incline, auto-adjusted by equipment type)
  - The full-course intensity curve with a vertical line at the current playhead

## Architecture

```
DaVinci Resolve (Scripting API)
        │  GetCurrentTimecode() / GetCurrentTimeline().GetName()
        ▼
server.py  ── polls timecode → matches course data → computes current intensity
        │  (local HTTP, 127.0.0.1:8765)
        ▼
overlay.html  ── dark overlay window, real-time rendering
```

| File | Purpose |
|---|---|
| `server.py` | Backend: connects to Resolve + polls timecode + matches course + local HTTP server |
| `resolve_connection.py` | Wrapper around the DaVinci Resolve Scripting API |
| `course_data.py` | Course data loading + stepped (time-based) queries |
| `equipment_config.py` | Equipment field configuration (treadmill / bike / rower / elliptical / bodyweight) |
| `overlay.html` / `overlay.py` | Dark overlay window + Edge app-mode launcher (auto always-on-top, auto size fix) |
| `xlsx_to_json.py` | **Converts course `.xlsx` files into plugin JSON** (command-line entry) |
| `convert_course.py` / `convert.bat` | Graphical conversion: file-picker dialog → JSON |
| `launcher.py` | What `start.bat` actually runs: hidden backend + overlay + topmost keep-alive + auto-shutdown |
| `start.bat` | One-click startup (no console window; closing the overlay exits everything) |
| `config.json` | Configuration (port, data dir, Resolve scripting path) |
| `data/` | Course JSON data (private; not version-controlled) |

## Requirements

- Windows + DaVinci Resolve (Studio or Free, with Scripting API support).
- Python **3.10 or 3.11** (required — DaVinci Resolve's `fusionscript` module only supports these versions; 3.12+ will crash).
- No third-party Python packages required (pure standard library).

## Usage

### 1. Enable Resolve External Scripting

In Resolve: **DaVinci Resolve → Preferences → System → General**, set
**"External scripting using"** to **Local** (or Network), then restart Resolve.

### 2. Prepare Course Data (two ways)

**Method A (recommended): double-click `convert.bat` and pick the course file in the dialog**

A native Windows "Open file" dialog appears (starting at your Desktop) — just select the course workbook. No paths to remember, no drag-and-drop required.

You can still **drag an `.xlsx` onto `convert.bat`** to skip the dialog and convert that file directly.

**Method B: use the button in the overlay window** (no need to quit the plugin)

The button in the bottom-right corner has two roles, decided by how many course JSONs exist in `data/`:

| State | Button label | On click |
|---|---|---|
| `data/` has course files | **清除课件缓存** (clear cache) | Deletes every JSON under `data/` (and clears the in-memory cache) |
| After clearing (or nothing there yet) | **选择课件** (pick course file) | Opens the very same file dialog as `convert.bat`, then converts |
| Waiting for your file selection | 等待选择… (disabled) | — |

So after clearing the cache you can re-import a course file **without restarting the plugin**; once
the conversion finishes the button flips back to "clear cache".

The converter parses every worksheet (treadmill / bike / rower / elliptical / bodyweight all supported) and writes the JSON files into `data/`.

**Command line** (for scripting/debugging):

```bat
python convert_course.py "C:\path\to\course.xlsx"
python xlsx_to_json.py "C:\path\to\course.xlsx"          # equivalent CLI-only entry point
```

**Method C: hand-write JSON** (format below).

Key point: the JSON's **`course_name` field must match the DaVinci timeline name** (or contain it); the plugin matches by **equipment + course name** (see below).

### 3. Start

Double-click **`start.bat`** — that's it.

- The backend runs **silently in the background with no console window**; its log goes to `.runtime/server.log`.
- The overlay window appears a moment later. **Closing the overlay shuts the backend down automatically** — no leftover process holding the port.

Or manually, in two steps (useful for debugging; the backend then stays visible and does not exit with the overlay):
```bat
python server.py     # start backend
python overlay.py    # start overlay
```

### 4. Use

- In Resolve, open a timeline named the same as the course, scrub the playhead, and the overlay shows the current intensity in real time.
- The overlay opens in Edge's `--app` mode (frameless small window) by default.
- The overlay is **always-on-top by default** (same effect as PowerToys Always On Top) — no
  `Win+Ctrl+T` needed; turn it off with `always_on_top: false`.

## Deploying to Another Machine

This project is **portable** — no hard-coded user paths. Copy the whole folder to the target machine and ensure the following:

1. **Python 3.10 or 3.11** is installed (the `fusionscript` module only works on these versions).
2. **DaVinci Resolve** is installed and **External scripting** is enabled (`Preferences → System → General → External scripting using = Local`).
3. The Resolve scripting module is normally at the fixed system path `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules` — this is **independent of which drive** Resolve is installed on. If auto-detection fails, set `resolve_script_path` in `config.json`.
4. Run `start.bat` — it auto-locates Python via the `py` launcher (prefers 3.11, then 3.10, then falls back to `python` on PATH).

## Configuration (`config.json`)

| Key | Description | Default |
|---|---|---|
| `resolve_script_path` | Path to the Resolve scripting module (empty = auto-detect) | `null` |
| `data_dir` | Course data directory | `data` |
| `port` | Local server port | `8765` |
| `poll_interval` | Polling interval (seconds) | `0.1` |
| `auto_exit_on_overlay_close` | Shut the backend down when the overlay window closes (set `false` when running `server.py` manually for debugging) | `true` |
| `overlay_idle_timeout` | Fallback: seconds without any frontend request before assuming the overlay is gone (covers browser crashes) | `90` |
| `overlay_window_ratio` | Initial overlay size = screen work area × this ratio (`[width, height]`, about 1/7 of screen width) | `[0.135, 0.22]` |
| `overlay_window_size` | Explicit initial overlay size in pixels (`[width, height]`; takes priority over the ratio) | `null` |
| `always_on_top` | Keep the overlay always on top (built in — no PowerToys needed) | `true` |

> The port can also be overridden with the `RESOLVE_SYNC_PORT` environment variable (useful for a second instance or automated tests).

The Resolve scripting module is commonly located at:
```
C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules
```
If auto-detection fails, set `resolve_script_path` to this path.

## Data Format

```json
{
  "course_name": "Treadmill - Advanced Running Form",  // must match the timeline name
  "title": "Advanced Running Form",                      // course topic (shown in overlay, optional)
  "equipment": "treadmill",                              // equipment type, see equipment_config.py
  "duration": 1050,                                      // total duration (seconds)
  "segments": [                                          // course segments (ascending by time)
    { "name": "Warm Up", "start": 60,  "end": 170 },
    { "name": "Fast Run", "start": 680, "end": 800 }
  ],
  "points": [                                            // intensity data at segment start points (constant within a segment)
    { "time": 60,  "speed": 2, "pace": "30:00", "action": "Warm Up on Machine" },
    { "time": 170, "speed": 8, "pace": "7:30",  "action": "Jog", "keyword": "Aerobic Output" }
  ]
}
```

**Key rules:**
- `time` is in **seconds** (from course start).
- Fitness-course intensity is **constant within a segment**, so `points` only records once at each segment's start; queries take the last point where `time <= t` (stepped query).
- Numeric fields like `speed` / `incline` / `distance` are `null` when the segment has no such metric (e.g. cool-down / stretching has no treadmill speed); the overlay shows "—".
- `pace` is a `"mm:ss"` string, converted from speed (8 km/h = "7:30").
- `action` (movement name) and `keyword` (intensity tag) are optional text fields.
- **Optional fields**: fields absent from course data (e.g. a treadmill course without incline) are hidden automatically.

## Supported Equipment (extensible)

`equipment_config.py` ships with several equipment field definitions; to add a new equipment type, just add one entry:

| Equipment key | Name | Fields |
|---|---|---|
| `treadmill` | Treadmill | speed km/h · pace · incline % · distance |
| `bike` | Spin Bike | cadence rpm · resistance · power |
| `rower` | Rowing Machine | stroke rate spm · resistance · split pace |
| `elliptical` | Elliptical | rotation rpm · resistance |
| `bodyweight` | Bodyweight | (no equipment metrics; action/segment only) |

**Real course-field mapping** (from the original `冠军课程课件.xlsx`):
- Treadmill: col D "建议速度(km/h)" → `speed`, col E "建议阻力/坡度" → `incline`
- Bike: col D "RPM" → `rpm`, col E "阻力" → `resistance`
- Rower: col D "SPM" → `spm`, col E "阻力" → `resistance`

## FAQ

- **Overlay shows "cannot connect to service"**: run `server.py` first.
- **Status stuck on "connecting to Resolve"**: make sure Resolve is running and external scripting is set to Local.
- **Cannot find a matching course**: check that `course_name` matches the timeline name (or contains it).
- **Window won't stay on top**: Edge `--app` mode doesn't guarantee top-most; use PowerToys' Always On Top shortcut.
- **Bodyweight courses not converted**: bodyweight training counts "reps", has no equipment metrics, and its timeline is incomplete, so it isn't auto-converted yet and needs a separate design.

## Versioning

This project follows [Semantic Versioning](https://semver.org/).

- **v0.1.0** — Initial release: equipment-based course adaptation (treadmill / bike / rower / elliptical / bodyweight) + basic overlay.

## Roadmap

- **Multi-language course adaptation** — configurable header/label detection and equipment-name prefix matching for non-Chinese course files.
- **Bodyweight course adaptation** — a dedicated data model for rep/set-based training (no equipment metrics, non-continuous timeline).
- **Frontend polish + course cache invalidation** — in-memory course caching, `data/` reload, and a "clear cache" endpoint.

## License

Proprietary. All rights reserved. Course data (`data/`) is confidential business data and is intentionally excluded from version control.
