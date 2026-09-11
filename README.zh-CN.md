# 课程强度同步插件（DaVinci Resolve 辅助剪辑）

让剪辑师在达芬奇时间线上拖动播放头时，**实时看到当前时间点对应课件的运动强度信息**——环节名称、动作、强度关键词、速度/配速/坡度（按器械自动调整）——从而根据课程当前强度自主决定切镜节奏。

> 中文说明。英文版见 [README.md](README.md)。

## 它能做什么

- 读取达芬奇当前播放头时间码，**自动跟随**（拖动 / 播放时毫秒级刷新）。
- 根据**当前时间线名称**自动匹配**同名课程数据文件**。
- 在**始终置顶的暗色小窗**里显示：
  - 当前**课程环节**（如「快速跑」「动作教学1」）
  - 当前**动作名称**（如「慢跑」「中速跑」）+ **强度关键词**（如「有氧输出」「持续消耗」）
  - 当前**强度指标**（速度/配速/坡度，按器械类型自动调整）
  - 整节课的强度曲线 + 当前播放头位置竖线

## 架构

```
达芬奇 (Scripting API)
        │  GetCurrentTimecode() / GetCurrentTimeline().GetName()
        ▼
server.py  ── 轮询时间码 → 匹配课程数据 → 换算当前强度
        │  (本地 HTTP, 127.0.0.1:8765)
        ▼
overlay.html  ── 暗色悬浮窗，实时渲染
```

| 文件 | 作用 |
|---|---|
| `server.py` | 后端：连达芬奇 + 轮询时间码 + 匹配课程 + 本地 HTTP |
| `resolve_connection.py` | 封装达芬奇 Scripting API |
| `course_data.py` | 课程数据加载 + 按时间阶梯查询 |
| `equipment_config.py` | 器械字段配置（跑步机/单车/划船机/椭圆机/徒手） |
| `overlay.html` / `overlay.py` | 暗色悬浮窗 + Edge app 模式启动器 |
| `xlsx_to_json.py` | **把课件 xlsx 转成插件 JSON**（命令行入口） |
| `convert_course.py` / `convert.bat` | 图形化转换：弹文件选择框 → 生成 JSON |
| `launcher.py` | `start.bat` 背后真正干活的：后台起后端 + 开悬浮窗 + 关窗收尾 |
| `start.bat` | 一键启动（无黑框；关掉悬浮窗即全部退出） |
| `config.json` | 配置（端口、数据目录、达芬奇脚本路径） |
| `data/` | 课程 JSON 数据（私密业务数据，不纳入版本管理） |

## 环境要求

- Windows + DaVinci Resolve（Studio 或免费版均可，需支持 Scripting API）。
- Python **3.10 或 3.11**（必须——达芬奇的 `fusionscript` 模块只支持这两个版本，3.12+ 会崩）。
- 无需第三方 Python 库（纯标准库实现）。

## 使用步骤

### 1. 开启达芬奇外部脚本

达芬奇菜单：**DaVinci Resolve → 偏好设置 → 系统 → 常规**，把
**"External scripting using"** 设为 **Local**（或 Network），重启达芬奇。

### 2. 准备课程数据（两种方式）

**方式 A（推荐）：双击 `convert.bat`，在弹出的文件选择框里挑课件**

双击后会弹出 Windows 原生的「打开文件」对话框（默认定位到桌面），选中课件表格即可转换，不用记路径、也不用拖拽。

也可以把任意位置的 xlsx **直接拖到 `convert.bat` 上**，会跳过选择框、直接转换该文件。

转换脚本会解析所有工作表（跑步机/单车/划船机/椭圆机/徒手均支持），生成对应 JSON 到 `data/` 目录。

**命令行方式**（调试用）：

```bat
python convert_course.py "C:\path\to\冠军课程课件.xlsx"
python xlsx_to_json.py "C:\path\to\冠军课程课件.xlsx"          # 等价的纯命令行入口
```

**方式 B：手写 JSON**（格式见下文「数据格式」）。

关键点：JSON 里的 **`course_name` 字段必须和达芬奇时间线名称一致**（或互为包含），插件按**器械 + 课程名**匹配（详见下文）。

### 3. 启动

双击 **`start.bat`** 即可（一键启动后端 + 悬浮窗）。

- 后端在**后台静默运行，不会弹出黑色命令行窗口**；日志写在 `.runtime/server.log`。
- 稍等片刻会自动弹出悬浮窗，**关掉悬浮窗后端就会自动退出**，不会残留后台进程占着端口。

或手动分两步（调试时用，此时后端有可见输出、且不随前端退出）：
```bat
python server.py     # 启动后端
python overlay.py    # 启动悬浮窗
```

### 4. 使用

- 在达芬奇里打开与课程同名的时间线，拖动播放头，悬浮窗即实时显示当前强度。
- 悬浮窗默认用 Edge 的 `--app` 模式打开（无边框小窗）。
- 需要「始终置顶」时，可用 **Microsoft PowerToys → Always On Top**（Win+Ctrl+T）或任意置顶工具。

## 配置（config.json）

| 键 | 说明 | 默认 |
|---|---|---|
| `resolve_script_path` | 达芬奇 scripting 模块路径（留空自动探测） | `null` |
| `data_dir` | 课程数据目录 | `data` |
| `port` | 本地服务端口 | `8765` |
| `poll_interval` | 轮询间隔（秒） | `0.1` |
| `auto_exit_on_overlay_close` | 关掉悬浮窗后是否自动结束后端（手动跑 `server.py` 调试时可设 `false`） | `true` |
| `overlay_idle_timeout` | 兜底：前端多少秒无请求就认定已关闭（应对浏览器崩溃） | `90` |

> 端口也可以用环境变量 `RESOLVE_SYNC_PORT` 临时覆盖（跑第二个实例或自动化测试时有用）。

达芬奇 scripting 模块常见位置：
```
C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules
```
若自动探测失败，把它填进 `resolve_script_path`。

## 数据格式

```json
{
  "course_name": "跑步机-进阶跑姿训练",   // 必须与达芬奇时间线名一致
  "title": "进阶跑姿训练",                // 课程主题（悬浮窗显示用，可选）
  "equipment": "treadmill",              // 器械类型，见 equipment_config.py
  "duration": 1050,                      // 总时长（秒）
  "segments": [                          // 课程环节（时间升序）
    { "name": "热身激活", "start": 60,  "end": 170 },
    { "name": "快速跑",   "start": 680, "end": 800 }
  ],
  "points": [                            // 各环节起点处的强度数据（分段恒定）
    { "time": 60,  "speed": 2, "pace": "30:00", "action": "机上热身" },
    { "time": 170, "speed": 8, "pace": "7:30",  "action": "慢跑", "keyword": "有氧输出" }
  ]
}
```

**关键规则：**
- `time` 单位是**秒**（从课程开始算）。
- 健身课件的强度在「一个环节内恒定」，所以 `points` 只需在每个环节起点记录一次；
  查询时取「最后一个 time <= t 的点」（阶梯查询）。
- `speed` / `incline` / `distance` 等数值字段，值为 `null` 表示该环节无此指标
  （如冷身/拉伸环节没有跑步机速度），前端显示 "—"。
- `pace` 配速用 `"mm:ss"` 字符串，由速度自动换算（8 km/h = "7:30"）。
- `action`（动作名称）、`keyword`（强度标签）为可选文本字段。
- **可选字段**：课程数据里没有的字段（如跑步机不带坡度）会自动隐藏。

## 支持多器械（可扩展）

`equipment_config.py` 里已内置多种器械的字段定义，后续加新器械只需加一个条目：

| 器械 key | 名称 | 字段 |
|---|---|---|
| `treadmill` | 跑步机 | 速度 km/h · 配速 · 坡度 % · 距离 |
| `bike` | 动感单车 | 踏频 rpm · 阻力 · 功率 |
| `rower` | 划船机 | 桨频 spm · 阻力 · 配速 |
| `elliptical` | 椭圆机 | 转速 rpm · 阻力 |
| `bodyweight` | 徒手 | （无器械指标，仅动作/环节） |

**真实课件字段映射**（来自「冠军课程课件.xlsx」）：
- 跑步机 D 列「建议速度(km/h)」→ `speed`，E 列「建议阻力/坡度」→ `incline`
- 单车 D 列「RPM」→ `rpm`，E 列「阻力」→ `resistance`
- 划船机 D 列「SPM」→ `spm`，E 列「阻力」→ `resistance`

已随项目转换 21 节器械类课程（跑步机 10 / 单车 5 / 划船机 3 / 椭圆机 3）。

## 常见问题

- **悬浮窗显示「无法连接服务」**：先运行 `server.py`。
- **状态一直「正在连接达芬奇」**：确认达芬奇已启动、外部脚本已开启为 Local。
- **找不到同名课程**：检查 `course_name` 与时间线名称是否一致（或互为包含）。
- **窗口不置顶**：Edge `--app` 模式本身不保证置顶，用 PowerToys 的 Always On Top 快捷键。
- **徒手类课件未转换**：徒手训练按「个数」计数、无器械指标且时间轴不完整，暂不支持自动转换，需另行设计。

## 版本说明

本项目遵循[语义化版本](https://semver.org/)。

- **v0.1.0** —— 初始版本：器械类课程适配（跑步机/单车/划船机/椭圆机/徒手）+ 基础悬浮窗。

## 迭代规划

- **不同语言课件适配**：可配置的表头/标签识别 + 器械名前缀匹配，支持非中文课件。
- **徒手课件适配**：为「按个数/组数」计数的训练设计独立数据模型（无器械指标、时间轴不连续）。
- **前端优化 + 清除课件缓存**：内存课程缓存、`data/` 重载、"清除缓存"端点。

## 许可

专有软件，保留所有权利。课程数据（`data/`）为私密业务数据，已刻意排除在版本管理之外。
