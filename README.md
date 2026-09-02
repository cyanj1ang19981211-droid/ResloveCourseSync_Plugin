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
| `overlay.html` / `overlay.py` | Dark overlay window + launcher |
| `xlsx_to_json.py` | **Converts course `.xlsx` files into plugin JSON** |
| `start.bat` | One-click startup for backend + overlay |
| `config.json` | Configuration (port, data dir, Resolve scripting path) |
| `data/` | Course JSON data (private; not version-controlled) |

## Requirements

- Windows + DaVinci Resolve (Studio or Free, with Scripting API support).
- Python 3.7+.
- No third-party Python packages required (pure standard library).

## Usage

### 1. Enable Resolve External Scripting

In Resolve: **DaVinci Resolve → Preferences → System → General**, set
**"External scripting using"** to **Local** (or Network), then restart Resolve.

### 2. Prepare Course Data (two ways)

**Method A (recommended): convert the course `.xlsx` directly**

```bat
python xlsx_to_json.py "C:\path\to\course.xlsx"
```

The script parses all equipment-based course sheets and generates corresponding JSON into the `data/` directory.
(Treadmill / bike / rower / elliptical are supported; bodyweight courses have a different structure and are not auto-converted yet.)

**Method B: hand-write JSON** (format below).

Key point: the JSON's **`course_name` field must match the DaVinci timeline name** (or contain it); the plugin matches by name automatically.

### 3. Start

Double-click **`start.bat`** (starts backend + overlay in one click).

Or manually, in two steps:
```bat
python server.py     # start backend
python overlay.py    # start overlay
```

### 4. Use

- In Resolve, open a timeline named the same as the course, scrub the playhead, and the overlay shows the current intensity in real time.
- The overlay opens in Edge's `--app` mode (frameless small window) by default.
- For "always on top", use **Microsoft PowerToys → Always On Top** (Win+Ctrl+T) or any window-pinning tool.

## Configuration (`config.json`)

| Key | Description | Default |
|---|---|---|
| `resolve_script_path` | Path to the Resolve scripting module (empty = auto-detect) | `null` |
| `data_dir` | Course data directory | `data` |
| `port` | Local server port | `8765` |
| `poll_interval` | Polling interval (seconds) | `0.1` |

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
