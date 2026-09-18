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
| `overlay.html` / `overlay.py` | Dark overlay window + Edge app-mode launcher (pin-button topmost, auto size fix) |
| `xlsx_to_json.py` | **Converts course `.xlsx` files into plugin JSON** (command-line entry) |
| `convert_course.py` / `convert.bat` | Graphical conversion: file-picker dialog → JSON |
| `launcher.py` | What `start.bat` actually runs: pre-flight environment check + hidden backend + overlay + topmost keep-alive + auto-shutdown |
| `start.bat` | One-click startup (no console window; closing the overlay exits everything) |
| `env_check.py` / `检查环境.bat` | **Environment check**: Python version, Resolve, Edge, course files and port, with a Chinese report |
| `_find_python.bat` | Shared interpreter discovery used by all `.bat` files (portable → py 3.11 → 3.10 → common folders → PATH) |
| `check_bat.py` | Developer lint: keeps the `.bat` files ASCII-only so they cannot break on a different code page |
| `SETUP-GUIDE.txt` | Chinese setup guide; `start.bat` opens it in Notepad when no Python is found |
| `config.json` | Configuration (port, data dir, Resolve scripting path) |
| `data/` | Course JSON data (private; not version-controlled) |

## Requirements

- Windows 10 / 11 + **DaVinci Resolve Studio** (paid edition).
  ⚠️ **The free edition will not work**: since Resolve 19.1, external processes may only
  drive the Scripting API on Studio (the free edition can only run scripts from the
  in-app *Workspace → Scripts* menu). On the free edition `scriptapp("Resolve")` always
  returns `None`, no matter what you configure.
- Python **3.10 or 3.11** (required — DaVinci Resolve's `fusionscript` module only supports these versions; 3.12+ cannot talk to Resolve).
- Microsoft Edge (the overlay uses `--app` mode; bundled with Windows 10/11).
- No third-party Python packages required (pure standard library).

> **Downloaded the ZIP and it will not run on the other machine?**
> Double-click **`检查环境.bat`** (environment check) first — it prints a report of
> every requirement with a concrete fix. If that machine has no Python at all,
> `start.bat` opens `SETUP-GUIDE.txt` (a Chinese guide covering both a normal
> install and the no-admin-rights portable option).

### How Python Is Located

`_find_python.bat` picks the first interpreter it finds, in this order:

1. `runtime\python\python.exe` inside the project (**portable Python**, see below)
2. `py -3.11` → `py -3.10` → `py -3` (the `py` launcher, exact versions first)
3. Common install folders (`%LOCALAPPDATA%\Programs\Python\Python311`, …) —
   covers "Python installed but PATH was not ticked"
4. `python` on PATH

**No admin rights to install Python?** Copy a whole Python 3.11 folder from a
machine that works into `runtime\python\` here. The plugin prefers it, so the
target machine needs no install at all. Ship the entire folder (including
`runtime`) and your colleague can just unzip and run.

## Usage

### 1. Enable Resolve External Scripting

In Resolve: **DaVinci Resolve → Preferences → System → General**, set
**"External scripting using"** to **Local** (or Network), then restart Resolve.

### 2. Prepare Course Data (two ways)

**Method A (recommended): double-click `convert.bat` and pick the course file in the dialog**

A native Windows "Open file" dialog appears (starting in the folder you used last time, Desktop on first run) — just select the course workbook. No paths to remember, no drag-and-drop required.

**You can select several files at once**: hold `Ctrl` to pick individually or `Shift` to select a
range, then hit "Open" — they are all converted in one go. You can also **drag multiple `.xlsx`
files onto `convert.bat`** at the same time to skip the dialog entirely.

This is aimed at the newer "one xlsx per lesson" layout (e.g. `20min舒缓解压轻氧攀登.xlsx`):
drag a whole batch in and you're done.

**Method B: use the button in the overlay window** (no need to quit the plugin)

The button in the bottom-right corner has two roles, decided by how many course JSONs exist in `data/`:

| State | Button label | On click |
|---|---|---|
| `data/` has course files | **清除课件缓存** (clear cache) | Deletes every JSON under `data/` (and clears the in-memory cache) |
| After clearing (or nothing there yet) | **选择课件** (pick course file) | Opens the very same file dialog as `convert.bat`, then converts |
| Waiting for your file selection | 等待选择… (disabled) | — |

So after clearing the cache you can re-import a course file **without restarting the plugin**; once
the conversion finishes the button flips back to "clear cache".

The converter parses every worksheet (treadmill / stair climber / bike / rower / elliptical / bodyweight
all supported) and writes the JSON files into `data/`.

It identifies columns **by header text instead of fixed column letters**, so it handles both course
layout generations:

| Layout | Shape | Header row | Course name taken from |
|---|---|---|---|
| Legacy master sheet | many worksheets in one xlsx | row 3 | worksheet name (e.g. `跑步机-爬坡模拟训练`) |
| New per-lesson sheet | one xlsx per lesson | row 1 | worksheet name; falls back to the file name when it is a default like `Sheet1` |

Equipment type is detected from the worksheet's equipment prefix first, then from keywords in the
worksheet name / file name / folder path (e.g. `35min高阻低频力量攀登.xlsx` inside a
`20260902爬楼机课程/课程课件/` folder is recognised as a stair climber via `攀登` and the folder name).

**Command line** (for scripting/debugging):

```bat
python convert_course.py "C:\path\to\course.xlsx"
python convert_course.py a.xlsx b.xlsx c.xlsx             # convert several at once
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
  `Win+Ctrl+T` needed.
- The **pin button in the top-left corner** toggles it: lit (blue) = stays above everything;
  dim (grey) = an ordinary window that can be minimised or covered normally. The choice is
  remembered in `.runtime/topmost.json`; `always_on_top` in `config.json` is only the default
  for the very first launch.
- The window size is corrected once when it opens and never forced again afterwards, so
  resizing it by hand sticks.

## Deploying to Another Machine

This project is **portable** — no hard-coded user paths. Copy (or unzip) the whole folder on the target machine, then:

1. **Run the environment check first** — double-click `检查环境.bat`. It reports Python
   version, Resolve, Edge, course files, port and file completeness, each with a fix.
2. **Python 3.10 or 3.11** must be available to the plugin. `_find_python.bat` looks for a
   portable `runtime\python\`, then `py -3.11` / `py -3.10` / `py -3`, then the usual install
   folders, then PATH. If nothing is found, `start.bat` opens `SETUP-GUIDE.txt` (Chinese)
   in Notepad. Python 3.12+ cannot talk to Resolve — install 3.11 alongside it.
3. **DaVinci Resolve** must be installed with **External scripting** enabled
   (`Preferences → System → General → External scripting using = Local`).
4. The Resolve scripting module normally lives at the fixed system path
   `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules` —
   **independent of which drive** Resolve is installed on. If auto-detection fails, set
   `resolve_script_path` in `config.json`.
5. Run `start.bat`. `launcher.py` runs the pre-flight check and shows a message box
   (in Chinese) if anything blocking is missing — it no longer fails silently.

> **Note on `.bat` encoding:** every `.bat` in this repo is deliberately **ASCII-only** and
> CRLF. `cmd.exe` decodes a batch file using the console code page, so GBK bytes inside one
> fall apart on any machine whose code page is not 936 (e.g. with *Beta: Use Unicode UTF-8*
> enabled → 65001): comments leak out as commands and `if/for` blocks break, producing a wall
> of "*is not recognized as an internal or external command*". All Chinese messages are
> printed by Python instead (it writes through `WriteConsoleW`, which is code-page agnostic).
> Run `python check_bat.py` to verify this invariant.

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
| `always_on_top` | Keep the overlay always on top **on first launch** (afterwards the pin button decides, and the choice is remembered) | `true` |

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
| `stairclimber` | Stair Climber | speed level · resistance level · distance |
| `bike` | Spin Bike | cadence rpm · resistance · power |
| `rower` | Rowing Machine | stroke rate spm · resistance · split pace |
| `elliptical` | Elliptical | rotation rpm · resistance |
| `bodyweight` | Bodyweight | (no equipment metrics; action/segment only) |

**Real course-field mapping** (matched by header text):
- Treadmill: header "建议速度(km/h)" → `speed`, header "建议阻力/坡度" → `incline`
- Stair climber: header "建议速度" → `speed` (a **level**, not km/h), header "建议阻力/坡度"
  → `resistance` (only present on magnetically-braked models; hidden when empty)
- Bike: header "RPM" → `rpm`, header "阻力" → `resistance`
- Rower: header "SPM" → `spm`, header "阻力" → `resistance`

> Stair climber vs. incline treadmill: a treadmill's speed is real (km/h, convertible to pace),
> while a stair climber steps in place — the course sheet's "建议速度" is really a **level**, so the
> unit is "级" (level) and no pace is derived. Everything else (constant per segment, optional
> fields hidden when absent) is identical to the treadmill.

32 courses are currently converted (treadmill 10 / stair climber 6 / bike 5 / rower 3 /
elliptical 3 / bodyweight 5).

## FAQ

**On a fresh machine / after downloading the ZIP**

- **A wall of "*is not recognized as an internal or external command*" when running a `.bat`**:
  an old-version problem. Those `.bat` files used to be stored as GBK, and `cmd.exe` decodes a
  batch file with the console code page — so on a machine whose code page is not 936 (typically
  with *Beta: Use Unicode UTF-8* enabled → 65001) the bytes got mis-paired, comments leaked out
  as commands and `if/for` blocks broke. Every `.bat` is now **ASCII-only** (Chinese is printed
  by Python instead), which is code-page independent. Download the latest version;
  `python check_bat.py` verifies the invariant.
- **Double-clicking `start.bat` seems to do nothing**: that *is* the normal behaviour — the
  plugin runs silently in the background and the overlay pops up a second or two later. If no
  window ever appears, the current version shows a **Chinese message box** explaining why
  (older versions exited silently). If even that does not appear, double-click
  `检查环境.bat` and send a screenshot.
- **"Python was not found"**: that machine has no Python, or it was installed without
  *Add python.exe to PATH*. Follow the `SETUP-GUIDE.txt` that opens automatically — install
  Python 3.11, or use the portable-Python option (`runtime\python\`) if you lack admin rights.
- **Python is 3.12 / 3.13 / 3.14**: Resolve's `fusionscript` only supports 3.10 / 3.11 and cannot
  connect on newer versions. Install 3.11 alongside it — both can coexist.
- **How do I know whether this machine can run it?** Double-click `检查环境.bat` for a full
  report (Python version, Resolve, Edge, course files, port) with fixes; it is also saved to
  `.runtime\环境体检报告.txt`.
- **The overlay keeps saying "waiting for sync" even though I imported courses and built the
  timeline**: double-click `检查环境.bat` again. It now *actually connects* to Resolve
  (item **[3]**, real `scriptapp("Resolve")` call) and, if the backend is already running,
  prints what that backend sees (item **[6]**: connected? which timeline? matched any course?).
  The three usual causes, in order of likelihood:
  1. **Free edition of Resolve** — see *Environment* above. Nothing can be configured around it.
  2. **External scripting changed but Resolve not restarted** — set
     *Preferences → System → General → External scripting using* to **Local**, then fully quit
     and reopen Resolve.
  3. **The timeline name does not match any course name** — the panel matches by name; rename the
     timeline to the course name (with the equipment prefix it is safer, e.g.
     `爬楼机-20min心肺间歇突破攀登`).

**While running**

- **Overlay shows "cannot connect to service"**: run `server.py` first.
- **Status stuck on "connecting to Resolve"**: make sure Resolve is running and external scripting is set to Local.
- **Cannot find a matching course**: check that `course_name` matches the timeline name (or contains it).
- **Don't want it on top**: click the pin button in the top-left corner (the choice is remembered;
  delete `.runtime/topmost.json` to fall back to `always_on_top`).
- **The panel freezes for a second while scrubbing the timeline**: expected. DaVinci does not
  report the playhead during a drag, so the last frame is kept on screen instead of flashing
  back to the waiting state; it refreshes as soon as you let go.
- **Stair-climber "resistance" not shown**: expected. Early stair-climber sheets leave the
  resistance column at 0/empty, and empty optional fields are hidden; sheets from
  magnetically-braked models (e.g. MR2616) do carry values and will show it.
- **Converted several files but `data/` gained fewer JSONs**: some worksheets share the same name,
  so their output overwrote each other. The console lists every "worksheet -> JSON file" pair —
  compare those to spot the collision.

## Versioning

This project follows [Semantic Versioning](https://semver.org/).

- **v0.5.0** — **"It won't connect / won't sync" is no longer guesswork:** the environment check gained item **[3] "live Resolve connection test"** (spawns a subprocess that really calls `scriptapp("Resolve")` and reports the current project / timeline / whether a course matches), the port check now asks the running backend what *it* sees, and the backend gained a `/diag` endpoint plus self-reported instance info (PID, start time, code dir, data dir) so a stale instance from *another folder* is obvious. Also documents the key prerequisite that **external scripting is Studio-only since Resolve 19.1** (the free edition cannot work).
- **v0.4.0** — **No more guesswork on a fresh machine:** every `.bat` is now ASCII-only (fixes the garbled "*is not recognized as an internal or external command*" errors seen on another machine); added `检查环境.bat` + `env_check.py` environment check with a Chinese report; `start.bat` no longer fails silently (opens `SETUP-GUIDE.txt` when Python is missing, shows a Chinese message box on startup errors); portable Python support (`runtime\python\`) for machines without admin rights; added `check_bat.py` as a regression guard.
- **v0.3.0** — Stair climber support (speed shown as a level + resistance/distance); `convert.bat` accepts multiple files at once; the parser now identifies columns by header text and handles the "one xlsx per lesson" layout.
- **v0.2.0** — Graphical startup and conversion, screen-ratio overlay sizing, built-in always-on-top with a pin toggle, dual-role action button.
- **v0.1.0** — Initial release: equipment-based course adaptation (treadmill / bike / rower / elliptical / bodyweight) + basic overlay.

## Roadmap

- **Multi-language course adaptation** — configurable header/label detection and equipment-name prefix matching for non-Chinese course files.

## License

Source is publicly visible, but no open-source license is attached: copyright remains with the
author and all rights are reserved (contact the author for permission to reuse). Course data
(`data/`) is confidential business data and is intentionally excluded from version control.
