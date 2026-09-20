# -*- coding: utf-8 -*-
"""
课程强度同步服务（主程序）。

职责：
    1. 连接达芬奇，轮询当前播放头时间码（~100ms）。
    2. 根据当前时间线名称，匹配同名的课程数据文件。
    3. 把「当前时刻的环节名 + 各指标值 + 曲线预览数据」通过本地 HTTP 提供给悬浮窗。

运行：
    python server.py
    （悬浮窗 frontend/overlay.html 会从 http://127.0.0.1:8765 拉取数据）

配置：
    见同目录 config.json（可指定达芬奇 scripting 路径、数据目录、端口等）。
"""

import json
import os
import subprocess
import sys
import threading
import multiprocessing as mp
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 确保脚本所在目录在 sys.path（兼容 embedded Python 的 ._pth 机制不自动加脚本目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_io   # config.json 的容错读取（用户手改的文件，写坏了不能崩）
from course_data import CourseData, load_course, list_course_files
from resolve_connection import ResolveConnection, timecode_to_seconds
from equipment_config import EQUIPMENTS
import overlay   # 借用它的「置顶」实现（同一目录，纯 Win32 小工具）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _force_utf8_stdio():
    """日志被重定向到文件/管道时，把 stdout/stderr 改成 UTF-8 输出。

    为什么必须做，而且必须做在**模块级**：
        launcher 把 server.py 的 stdout 重定向到 `.runtime/server.log`，并且是按
        UTF-8 读回来的（启动失败时要弹给用户看）。但 Python 一发现 stdout 不是
        控制台，就退回按「系统代码页」编码 —— 简体中文机器上是 GBK —— 于是中文
        日志写进去是 GBK、读出来是乱码，用户看到的报错信息全成了问号。

        坑在于：**光给子进程设 PYTHONIOENCODING=utf-8 是不够的**。实测（本机
        Python 3.11.9）用 multiprocessing 起的 worker 子进程，环境变量明明继承到了
        （os.environ 里就是 utf-8），sys.stdout.encoding 仍然是 gbk。所以修复放在
        这里最保险：worker 子进程会重新执行本模块的模块级代码，这段会跟着生效，
        父进程和子进程写出来的日志编码就一致了。

    只在**非控制台**时才动：控制台输出交给 Python 自己（3.6+ 在 Windows 上走
    WriteConsoleW，与代码页无关），拿代码页去 reconfigure 真控制台反而会把中文写坏。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:                 # pythonw 下可能是 None
            continue
        try:
            if stream.isatty():
                continue
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()


# 器械中文名前缀映射：与 equipment_config.EQUIPMENTS 各 key 一一对应。
# 用于从「椭圆机-间歇燃脂训练」这种 timeline 名里提取器械维度，避免不同器械但
# 课程名相近时互相误匹配（如椭圆机的「间歇燃脂训练」 vs 划船机的「间歇燃脂训练」）。
# 注意：每个 key 的前缀列表互不为前缀（如「单车」不会被「单车内」之类误匹配，
# 因为这种词不是合法器械名；这里按 key 列出顺序不重要）。
EQUIPMENT_PREFIX_MAP = {
    "treadmill":    ["跑步机"],
    "stairclimber": ["爬楼机", "楼梯机", "登楼机"],
    "bike":         ["动感单车", "室内单车", "单车"],
    "rower":        ["划船机"],
    "elliptical":   ["椭圆机"],
    "bodyweight":   ["徒手"],
}


# ---------- 配置 ----------

def load_config():
    """读配置。**绝不抛异常** —— 配置文件是用户手改的，写坏了不能让插件死。

    返回 (配置字典, 配置问题说明)。问题说明为空串时表示一切正常。
    """
    cfg_path = os.path.join(BASE_DIR, "config.json")
    default = {
        # 达芬奇路径。三项都留空 = 全自动探测（推荐）。
        # 只有当自动探测失败（比如达芬奇装在很偏的位置）时才需要手动填：
        #   resolve_install_dir  —— 达芬奇安装目录（里面同时有 Resolve.exe 和
        #                           fusionscript.dll），例："D:\\软件\\达芬奇"
        #   resolve_script_path  —— …\\Support\\Developer\\Scripting\\Modules
        #   resolve_script_lib   —— fusionscript.dll 的完整路径（最精确）
        "resolve_install_dir": None,
        "resolve_script_path": None,
        "resolve_script_lib": None,
        "data_dir": os.path.join(BASE_DIR, "data"),
        "port": 8765,
        "poll_interval": 0.1,               # 轮询间隔（秒）
        # 关掉悬浮窗后是否自动结束后端进程（配合 start.bat 后台启动用）。
        # 手动 `python server.py` 调试时可以设成 false，让服务一直留着。
        "auto_exit_on_overlay_close": True,
        # 兜底：前端超过这么多秒没有任何请求，就认为它已经没了（应对浏览器崩溃等
        # 收不到关闭信号的极端情况）。取 90s 是因为浏览器对最小化/后台的页面会把
        # 定时器降频到每分钟一次，超时太短会误杀。
        "overlay_idle_timeout": 90,
        # 悬浮窗初始尺寸：按屏幕工作区比例算（宽, 高）。默认接近屏宽 1/7、屏高 1/5，
        # 可在小屏上不至于占掉半个屏幕。也可以在 overlay_window_size 里直接写像素。
        "overlay_window_ratio": [0.135, 0.22],
        "overlay_window_size": None,
        # 悬浮窗「始终置顶」（等价 PowerToys 的 Always On Top，免手动 Win+Ctrl+T）。
        # 由 overlay.py 读取并生效，这里只是跟 config.json 保持同一份默认值。
        "always_on_top": True,
    }

    # 容错读取：BOM / ANSI(GBK) / 尾逗号 / 单反斜杠路径 都会在这里被修好，
    # 修不好也只降级成默认值 + 一条人话说明，不会让 import 阶段就崩掉。
    user, problem, notes = config_io.load_config_file(cfg_path)
    default.update(user)

    # 允许用环境变量临时改端口（跑第二个实例 / 自动化测试时用）
    env_port = os.environ.get("RESOLVE_SYNC_PORT")
    if env_port and env_port.isdigit():
        default["port"] = int(env_port)

    # data_dir 允许写相对路径（配置里就是 "data"）。统一按项目目录解析，
    # 这样从任意工作目录启动（比如 launcher 拉起的子进程）都能找到 data/。
    dd = default.get("data_dir")
    if dd and not os.path.isabs(dd):
        default["data_dir"] = os.path.join(BASE_DIR, dd)

    return default, problem, notes


CONFIG, CONFIG_PROBLEM, CONFIG_NOTES = load_config()

# 后端实例的自述信息，挂在 /state 和 /diag 里。
#
# 用途：排查「悬浮窗一直不同步」时，能一眼看出端口上跑的到底是**哪一个副本**。
# 典型坑：换电脑/换版本后没关掉旧的悬浮窗，旧后端还在 8765 上活着，
# 新双击的 start.bat 起的进程要么被清掉要么绑不上端口，用户看到的其实是
# 另一个文件夹里的旧实例 —— 那样「改了设置却没变化」怎么都说不通。
SERVER_META = {
    "server_pid": os.getpid(),
    "server_started_at": time.time(),
    "server_code_dir": BASE_DIR,
    "server_data_dir": os.path.abspath(CONFIG.get("data_dir") or ""),
    "server_python": sys.executable,
    # 配置文件本身的问题（编码/语法/路径写错）。非空时悬浮窗会把它顶到状态栏，
    # 免得用户「明明改了 config.json 却毫无反应」还以为是插件坏了。
    "config_problem": CONFIG_PROBLEM,
    # 已经自动处理好、不影响使用的小提醒（BOM / ANSI / 自动修复语法）
    "config_notes": CONFIG_NOTES,
    # 用户到底有没有在 config.json 里手填达芬奇路径。排查时很有用：
    # 「我明明指定了目录」和「配置根本没被读进去」是两回事。
    "config_resolve_install_dir": CONFIG.get("resolve_install_dir") or "",
    "config_resolve_script_lib": CONFIG.get("resolve_script_lib") or "",
    "config_resolve_script_path": CONFIG.get("resolve_script_path") or "",
}

# 配置相关的日志只在主进程打。
# worker 子进程是用 multiprocessing spawn 起来的，会**重新执行本模块的模块级代码**，
# 于是这些 [配置] 行会被一模一样地打印第二遍（子进程的 stdout 也是同一份
# server.log）。配置是全进程共用的，说一遍就够了，重复行只会干扰看日志。
if mp.current_process().name == "MainProcess":
    if CONFIG_PROBLEM:
        print("[配置] 有问题：%s" % CONFIG_PROBLEM.replace("\n", " / "), flush=True)
    for _n in CONFIG_NOTES:
        print("[配置] %s" % str(_n).replace("\n", " / "), flush=True)
    if not os.path.isfile(os.path.join(BASE_DIR, "config.json")):
        print("[配置] 没有 config.json，全部用默认值（自动探测达芬奇）。", flush=True)


# ---------- 前端存活检测（关掉悬浮窗后自动退出后端） ----------

class Liveness:
    """跟前端「对表」：前端还在拉数据就活着，前端关窗就结束后端进程。

    背景：一键启动（start.bat）时后端是**后台无窗口**进程。用户关掉悬浮窗后如果
    后端还赖着不走，就会变成看不见的僵尸进程，下次启动还占着 8765 端口。

    双保险：
        1. 前端真正关窗时，overlay.html 用 sendBeacon 打 /shutdown → 立刻退出；
        2. 万一信号没送到（浏览器崩溃/被强杀），超过 idle_timeout 没有前端请求
           也自动退出。

    注意：只有「曾经有前端连过」才会因空闲退出。手动跑 `python server.py` 调试、
    还没打开过悬浮窗时，进程会一直留着，不会被误杀。
    """

    def __init__(self, enabled: bool, idle_timeout: float):
        self._lock = threading.Lock()
        self._last_seen = 0.0
        self._seen = False
        self._shutdown_at = 0.0     # 收到关闭信号的时间；0 表示没有待处理的退出
        self.enabled = bool(enabled)
        self.idle_timeout = float(idle_timeout)
        # 收到关闭信号后先等这么久再退：页面按 F5 刷新时 pagehide 也会触发，
        # 刷新后立刻又有 /state 心跳进来 → 撤销退出，不会把后端误杀。
        self.shutdown_grace = 2.5

    def beat(self):
        """记录一次前端心跳（前端每次拉 /state 都算）。"""
        with self._lock:
            self._seen = True
            self._last_seen = time.time()
            if self._shutdown_at:
                # 页面又活了（多半是刷新/重新加载）→ 撤销刚才的退出请求
                self._shutdown_at = 0.0

    def request_shutdown(self):
        """前端关窗时调用：标记待退出，交给 watchdog 在宽限期后执行。"""
        with self._lock:
            if not self._shutdown_at:
                self._shutdown_at = time.time()

    def watchdog(self):
        """后台线程：处理「前端关窗」与「前端失联」两种情况下的退出。"""
        while True:
            time.sleep(0.5)
            if not self.enabled:
                continue
            now = time.time()
            with self._lock:
                seen = self._seen
                last = self._last_seen
                pending = self._shutdown_at
            if pending and (now - pending) > self.shutdown_grace:
                self._exit("收到前端关闭信号")
            if seen and (now - last) > self.idle_timeout:
                self._exit(f"前端已 {self.idle_timeout:.0f} 秒无响应")

    @staticmethod
    def _exit(reason: str):
        print(f"[退出] {reason}，后端结束。", flush=True)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)   # 直接退出：HTTP 线程/worker 子进程都由它带走


LIVENESS = Liveness(
    enabled=CONFIG.get("auto_exit_on_overlay_close", True),
    idle_timeout=CONFIG.get("overlay_idle_timeout", 90),
)


# ---------- 全局状态（线程安全） ----------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.timeline_name = ""
        self.course_name = ""
        self.title = ""
        self.course_loaded = False
        self.equipment = ""
        self.segment = ""
        self.action = ""
        self.keyword = ""
        self.intensity = ""       # 徒手课强度标签 "high"/"mid"/"low"/""
        self.intensity_score = 0.0  # 徒手课连续强度分（0~1）
        self.values = {}            # {field_key: display_value}
        self.segment_remaining = 0.0   # 当前环节剩余秒数（end - t）
        self.next_step = None           # 下一个小动作信息 {"name","keyword","start"}，无则 None
        self.time_seconds = 0.0
        self.connected = False
        self.message = "正在连接达芬奇..."
        self.mode = "init"  # worker 当前模式: 直传/本地推算/停止推算/init
        self.curve = {"times": [], "main": [], "sub": [], "main_label": "", "sub_label": ""}
        self.field_meta = []        # 字段显示元信息 [{key,label,unit}]

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return {
                "connected": self.connected,
                "timeline_name": self.timeline_name,
                "course_name": self.course_name,
                "title": self.title,
                "course_loaded": self.course_loaded,
                "equipment": self.equipment,
                "segment": self.segment,
                "action": self.action,
                "keyword": self.keyword,
                "intensity": self.intensity,
                "intensity_score": self.intensity_score,
                "values": dict(self.values),
                "segment_remaining": self.segment_remaining,
                "next_step": self.next_step,
                "time_seconds": self.time_seconds,
                "message": self.message,
                "mode": self.mode,
                "curve": self.curve,
                "field_meta": self.field_meta,
            }


STATE = State()


# ---------- 悬浮窗「始终置顶」开关（前端左上角图钉按钮） ----------

class PinState:
    """悬浮窗要不要一直压在达芬奇上面。

    以前是启动时写死的（config.json 的 always_on_top），用户没得选，只能被动接受；
    现在前端左上角有个图钉按钮：点亮 = 置顶，熄灭 = 普通窗口（可以正常最小化、
    也会被别的窗口盖住）。

    三件事：
        1. 立即生效 —— set() 里直接调 Win32 改 Z 序，不用等 launcher 下一秒复查；
        2. 记住选择 —— 落盘到 .runtime/topmost.json，下次打开沿用；
        3. 对外可见 —— /state 里带 pin 字段，前端据此渲染图钉的亮/灭。
    """

    def __init__(self, on):
        self._lock = threading.Lock()
        self._on = bool(on)

    def get(self):
        with self._lock:
            return self._on

    def set(self, on):
        on = bool(on)
        with self._lock:
            if self._on == on:
                return on          # 状态没变就别去动窗口
            self._on = on
        overlay.write_topmost_pref(on)
        try:
            overlay.apply_topmost(on)   # 立刻生效（找不到窗口就算了，launcher 会兜底）
        except Exception:
            pass
        print(f"[置顶] {'开启' if on else '关闭'}悬浮窗始终置顶", flush=True)
        return on


PIN = PinState(overlay.read_topmost_pref())


# ---------- 课程数据管理 ----------

class CourseManager:
    """按时间线名称匹配课程数据文件。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._cache = {}  # {course_name: CourseData}
        self.file_count = 0     # data_dir 下的课程 JSON 文件数（前端按钮状态用）
        self._last_reload = 0.0

    def _reload(self):
        files = list_course_files(self.data_dir)
        self.file_count = len(files)
        self._cache = {}
        for f in files:
            try:
                c = load_course(f)
                self._cache[c.course_name] = c
            except Exception:
                continue

    def count(self):
        """data_dir 下现有课程 JSON 文件数。

        悬浮窗右下角按钮靠它决定显示「清除课件缓存」（>0）还是「选择课件」（=0）。
        """
        return self.file_count

    def reload_now(self):
        """立刻重新扫描 data_dir（转换/清空后调用，不必等 5 秒的定时重载）。"""
        self._reload()
        self._last_reload = time.time()
        return self.file_count

    @staticmethod
    def _strip_punct(s: str) -> str:
        """只去掉标点和空白/连接符，保留中文和数字；不再剥离器械前缀（防止跨器械误匹配）。"""
        if not s:
            return ""
        for ch in "-_ —\t\n":
            s = s.replace(ch, "")
        return s.strip()

    @staticmethod
    def _strip_training_suffix(s: str) -> str:
        """去掉常见尾缀如"训练"，便于宽松匹配。"""
        if not s:
            return ""
        for suf in ("训练",):
            if s.endswith(suf):
                s = s[:-len(suf)]
        return s.strip()

    @staticmethod
    def _extract_equipment(name: str):
        """从时间线/课程名里提取器械前缀，返回 (equipment_key, 去掉前缀后的剩余部分)。

        器械前缀映射（与 equipment_config.EQUIPMENTS 对齐）：
            跑步机  -> treadmill
            爬楼机  -> stairclimber（兼容「楼梯机」「登楼机」）
            单车    -> bike（兼容「动感单车」「室内单车」等变体，但以「单车」为最短前缀）
            划船机  -> rower
            椭圆机  -> elliptical
            徒手    -> bodyweight

        返回 (None, name) 表示没识别到器械前缀（不会胡乱猜）。
        """
        if not name:
            return None, ""
        # 各器械前缀互不为前缀（如「单车」不会匹配「单车内」之外的东西），
        # 所以顺序不重要；但保持按 key 列出更清晰。
        for eq_key, prefixes in EQUIPMENT_PREFIX_MAP.items():
            for p in prefixes:
                if name.startswith(p):
                    rest = name[len(p):]
                    # 剥掉可能紧跟的连接符 " -" / "-" / "_" / " "
                    while rest and rest[0] in "-_ — ":
                        rest = rest[1:]
                    return eq_key, rest
        return None, name

    def clear_all(self):
        """清除 data_dir 下所有课程 JSON 文件，并清空内存缓存。

        返回 (删除数量, 剩余数量)。只删除 data_dir 内的 .json 文件（不递归子目录、
        不碰其他目录），删除后立即重载缓存（此时应为空）。

        为什么叫"清缓存"：data/ 里的 JSON 是 xlsx_to_json.py 转换出来的课程数据，
        用久了会累积；而且若不同课件重名，旧文件可能让同名 timeline 匹配到错误的
        强度数据。清除后需重新运行 convert 生成干净的数据。
        """
        removed = 0
        files = list_course_files(self.data_dir)
        for f in files:
            try:
                os.remove(f)
                removed += 1
            except OSError:
                continue
        # 清空内存缓存并重载（此刻 data 已空，重载后 _cache 应为空）
        remaining = self.reload_now()
        return removed, remaining

    def find(self, timeline_name: str):
        """根据时间线名查找课程；每 5 秒重载一次数据目录以支持热更新。

        匹配原则：**器械一致优先**。
            不同器械但课程名相近（如「椭圆机-间歇燃脂训练」 vs 「划船机-间歇燃脂训练」）
            不会互相误匹配——只要 timeline 名里有器械前缀，就只在「同器械」课程里找。

        匹配优先级（从高到低）：
          A. 精确匹配：timeline_name == course_name
          B. timeline 含器械前缀：
             B1. 同器械下精确匹配（去前缀后的剩余名 == 同器械课程的剩余名）
             B2. 同器械下去标点精确匹配
             B3. 同器械下去标点+去"训练"后缀精确匹配
             B4. 同器械下归一化包含匹配（剩余名长度 >= 2 时）
          C. timeline 不含器械前缀（用户没在名字里标器械）：
             C1. 跨器械精确匹配（course_name == timeline_name）
             C2. 跨器械包含匹配
             C3. 跨器械归一化匹配（保留原行为兜底）

        返回第一个命中；B 系列匹配不上时**不**退化到跨器械匹配（避免椭圆机匹配到划船机）。
        """
        now = time.time()
        if now - getattr(self, "_last_reload", 0) > 5:
            self._reload()
            self._last_reload = now
        if not timeline_name:
            return None

        # A. 精确匹配
        if timeline_name in self._cache:
            return self._cache[timeline_name]

        # 从 timeline 提取器械前缀
        tl_eq, tl_rest = self._extract_equipment(timeline_name)

        # B. timeline 含器械前缀 → 只在同器械里找
        if tl_eq is not None:
            same_eq = [(name, c) for name, c in self._cache.items() if c.equipment == tl_eq]
            # B1. 同器械精确匹配（course_name == timeline_name）—— 已在 A 中覆盖
            # B2. 同器械去前缀后剩余名精确匹配
            if tl_rest:
                for name, c in same_eq:
                    _, rest = self._extract_equipment(name)
                    if rest == tl_rest:
                        return c
            # B3. 同器械下去标点精确匹配
            tl_norm = self._strip_punct(tl_rest) if tl_rest else ""
            if tl_norm:
                for name, c in same_eq:
                    _, rest = self._extract_equipment(name)
                    if self._strip_punct(rest) == tl_norm:
                        return c
                # B4. 同器械下归一化精确匹配（含去"训练"后缀）
                tl_no_train = self._strip_training_suffix(tl_norm)
                for name, c in same_eq:
                    _, rest = self._extract_equipment(name)
                    rest_no_train = self._strip_training_suffix(self._strip_punct(rest))
                    if rest_no_train and tl_no_train and rest_no_train == tl_no_train:
                        return c
                # B5. 同器械下归一化包含匹配（剩余名长度 >= 2 才用，避免误匹配）
                if len(tl_no_train) >= 2:
                    for name, c in same_eq:
                        _, rest = self._extract_equipment(name)
                        rest_no_train = self._strip_training_suffix(self._strip_punct(rest))
                        if rest_no_train and (rest_no_train in tl_no_train or tl_no_train in rest_no_train):
                            return c
            # 有器械但同器械下没匹配上 → 返回 None，不退化到跨器械（核心改动）
            return None

        # C. timeline 不含器械前缀 → 跨器械兜底匹配（保留原行为）
        # C1. 包含关系
        for name, c in self._cache.items():
            if name and (name in timeline_name or timeline_name in name):
                return c
        # C2. 归一化精确匹配
        tl_norm = self._strip_punct(timeline_name)
        if tl_norm:
            for name, c in self._cache.items():
                if self._strip_punct(name) == tl_norm:
                    return c
        # C3. 归一化包含匹配
        tl_no_train = self._strip_training_suffix(tl_norm)
        if tl_no_train and len(tl_no_train) >= 2:
            for name, c in self._cache.items():
                cn = self._strip_training_suffix(self._strip_punct(name))
                if cn and (cn in tl_no_train or tl_no_train in cn):
                    return c
        return None


COURSES = CourseManager(CONFIG["data_dir"])


# ---------- 前端触发的课件转换（POST /convert） ----------

def _last_lines(text, lines=1):
    """取文本最后若干非空行（用于把子进程的报错回显给用户）。"""
    rows = [r.strip() for r in (text or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:]) if rows else ""


class ConvertJob:
    """在后台线程里跑 convert_course.py，实现「在前端点按钮选课件」。

    为什么是「后台线程 + 子进程」而不是在本进程里自己弹框转换：
        1. 选文件框要等用户操作（可能几十秒），绝不能占住 HTTP 请求线程；
        2. convert_course.py 就是双击 convert.bat 跑的那个入口，直接复用它，
           前端按钮和 convert.bat 的行为就天然一致（同样的系统文件框、同样的
           出错提示框、同样的 data/ 输出），不用维护两套逻辑。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.ok = None         # None = 还没跑过；True/False = 上一次的结果
        self.message = ""      # 给悬浮窗显示的一句话
        self.count = 0         # 转换结束后 data/ 里的课件数

    def snapshot(self):
        with self._lock:
            return {
                "converting": self.running,
                "convert_ok": self.ok,
                "convert_message": self.message,
            }

    def start(self):
        """发起一次转换；已经在跑就返回 False（避免弹两个文件框）。"""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.ok = None
            self.message = "已弹出选择窗口，请在窗口里选择课件表格…"
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        script = os.path.join(BASE_DIR, "convert_course.py")
        env = dict(os.environ)
        # 子进程 stdout 是管道时，Python 默认按系统代码页（GBK）编码，中文日志
        # 会在回读时乱码，这里强制 UTF-8。
        env["PYTHONIOENCODING"] = "utf-8"
        out, code = "", -1
        try:
            proc = subprocess.run(
                [sys.executable or "python", script],
                cwd=BASE_DIR, env=env,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=0x08000000,   # CREATE_NO_WINDOW：不要多弹一个黑框
            )
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            code = proc.returncode
        except Exception as e:
            out = f"{type(e).__name__}: {e}"

        # 转换完立刻重扫 data/：按钮状态和课程匹配都马上跟着更新，不用等 5 秒
        count = COURSES.reload_now()
        if count > 0:
            ok, msg = True, f"已导入 {count} 个课件"
        elif "已取消" in out:
            ok, msg = False, "已取消：没有选择课件"
        else:
            ok, msg = False, (_last_lines(out) or "没有转换出任何课程数据")

        with self._lock:
            self.running = False
            self.ok = ok
            self.message = msg
            self.count = count
        print(f"[转换] ok={ok} count={count} msg={msg}", flush=True)
        if code != 0:
            print(f"[转换] 子进程退出码 {code}，输出尾部：\n{_last_lines(out, 6)}",
                  flush=True)


CONVERT = ConvertJob()


# ---------- 达芬奇 worker（独立子进程，避免 fusionscript 的 GIL 阻塞主进程 HTTP） ----------

# ---------- 折线图数据生成 ----------

def build_curve(course: CourseData):
    """根据器械类型返回对应的曲线预览数据。

    - 器械课（跑步机/爬楼机/单车/划船机/椭圆机）：从 points 里抽 speed/incline 等数值字段
      画出连续折线。
    - 徒手课：没有速度/阻力等数值指标，但每个小动作有自己的强度（high/low）
      和个数（reps），用阶梯图能清晰看到「这节课哪里是高强度、哪里是休息」。
    """
    eq = course.equipment
    if eq == "bodyweight":
        return build_intensity_curve(course)
    return build_metric_curve(course)


def build_metric_curve(course: CourseData):
    """器械课：从 points 里抽主指标+副指标，画连续折线。"""
    main_key = None
    sub_key = None
    for f in course.field_order:
        if f["required"] and main_key is None:
            main_key = f["key"]
        elif f["key"] in ("incline", "resistance") and sub_key is None:
            sub_key = f["key"]

    def label_of(key):
        for f in course.field_order:
            if f["key"] == key:
                return f["label"]
        return key or ""

    times = []
    main = []
    sub = []
    for p in course.points:
        times.append(float(p.get("time", 0)))
        main.append(p.get(main_key) if main_key else None)
        sub.append(p.get(sub_key) if sub_key else None)

    return {
        "kind": "metric",         # 给前端区分绘制方式
        "times": times,
        "main": main,
        "sub": sub,
        "main_label": label_of(main_key),
        "sub_label": label_of(sub_key),
    }


def build_intensity_curve(course: CourseData):
    """徒手课：把每段小动作画成连续强度阶梯图，并叠 reps 副线。

    徒手课没有速度/阻力等数值指标，强度来自每个动作的「连续强度分」
    （0~1，由动作类型规则表打分，见 equipment_config.score_action）：
        - 爆发跳跃（波比/开合跳）≈ 0.95
        - 多关节抗阻（深蹲/俯卧撑）≈ 0.70
        - 核心稳定（平板/死虫/臀桥）≈ 0.50
        - 孤立局部（卷腹/弯举）≈ 0.30
        - 拉伸放松 ≈ 0.15
        - 休息/介绍/总结 = 0.00

    输出字段：
        kind            : "intensity"（前端据此切换绘制模式）
        steps           : [{start, end, intensity, score, reps, action}, ...]（连续阶梯段）
        intensity_times : 强度阶梯转折点（每段起止两点，构成水平+垂直阶梯）
        intensity_vals  : 与 times 等长的连续强度分（0~1）
        reps_times      : reps 对应时间点（每个 point 的 time）
        reps_vals       : reps 对应个数（None 表示无 reps）
        main_label      : "强度"
        sub_label       : "个数"
    """
    segs = course.intensity_segments()
    if not segs:
        return {
            "kind": "intensity",
            "steps": [],
            "intensity_times": [],
            "intensity_vals": [],
            "reps_times": [],
            "reps_vals": [],
            "main_label": "强度",
            "sub_label": "个数",
        }

    # 强度阶梯：每段起止画两个点，构成水平横线 + 到下段的垂直竖线
    intensity_times = [segs[0]["start"]]
    intensity_vals = [segs[0]["score"]]
    for s in segs:
        intensity_times.append(s["end"])
        intensity_vals.append(s["score"])

    reps_times = []
    reps_vals = []
    for s in segs:
        reps_times.append(s["start"])
        reps_vals.append(s["reps"])

    return {
        "kind": "intensity",
        "steps": segs,
        "intensity_times": intensity_times,
        "intensity_vals": intensity_vals,
        "reps_times": reps_times,
        "reps_vals": reps_vals,
        "main_label": "强度",
        "sub_label": "个数",
    }


def _resolve_worker(q, module_path, lib_path):
    """独立子进程：保持与达芬奇的连接，主动循环采集时间线/时间码，put 到 q。

    为什么要走子进程：
        fusionscript 的 native 调用会长时间持有 CPython GIL，
        即使主进程用 ThreadingHTTPServer，HTTP 线程也拿不到 GIL 跑 Python 字节码，
        导致客户端 fetch 超时。把 fusionscript 调用彻底隔离到子进程后，
        子进程的 GIL 阻塞不影响主进程，HTTP 永远能秒回。

    为什么做时间码推算：
        达芬奇 fusionscript 的 GetCurrentTimecode() 在**播放时可能不更新**（返回的是
        播放起始位置，或者直接卡住）。所以 worker 不直接用 GetCurrentTimecode 的返回值，
        而是用 "上次基准时间码 + 自此经过的 wall time" 推算当前时间码。
        当 GetCurrentTimecode 跳变（拖动/开始播放/重新开始），说明基准变了，重置基准。
    """
    import os as _os
    import sys as _sys
    import time as _time
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from resolve_connection import ResolveConnection as _RC

    # 未连接时：多久重试一次连接（达芬奇可能在插件之后才启动）
    RECONNECT_INTERVAL = 3.0
    # 已连接时：多久探活一次（达芬奇被关掉后要及时发现）
    ALIVE_PROBE_INTERVAL = 2.0

    def _tc_to_sec(s, fps):
        if not s: return None
        p = s.split(":")
        if len(p) == 4:
            return int(p[0])*3600 + int(p[1])*60 + int(p[2]) + int(p[3])/fps
        return None

    def _sec_to_tc(t, fps):
        if t < 0: t = 0.0
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        f = int(round((t - int(t)) * fps))
        if f >= fps: f = 0; s += 1
        if s >= 60:  s = 0; m += 1
        if m >= 60:  m = 0; h += 1
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    conn = _RC(module_path, lib_path)
    print(f"[worker] 启动 | module={conn.module_path} | lib={conn.lib_path}", flush=True)

    def _wait_msg(err: str) -> str:
        """把连接失败原因转成前端状态栏能显示的一句人话。

        两种失败要分开说，因为**用户能做的事完全不同**：
          · 「文件没找到」→ 再怎么重开达芬奇都没用，得去指定路径；
          · 「文件在但连不上」→ 才是「达芬奇没开 / 外部脚本没设 / 免费版」那一套。
        混成一句「等待达芬奇」会让人白折腾半天。
        """
        err = (err or "").strip()
        low = err.lower()

        if ("未找到" in err or "没有找到" in err or "找不到" in err
                or "modulenotfounderror" in low or "dll load failed" in low):
            return ("连不上达芬奇：没找到它的接口文件（不是没开达芬奇）。"
                    "双击「检查环境.bat」看【2】达芬奇")

        if (not err) or ("返回 none" in low):
            # scriptapp("Resolve") 返回 None：最常见就是「达芬奇还没启动」
            return "正在等待达芬奇启动…（启动后插件会自动连接，无需重启插件）"

        first = err.splitlines()[0][:160]
        return f"正在等待达芬奇：{first}"

    connected = False
    try:
        connected = conn.connect()
    except Exception as e:
        conn.last_error = f"{type(e).__name__}: {e}"
        connected = False

    q.put(("init", connected, "" if connected else _wait_msg(conn.last_error)))
    if connected:
        print("[worker] 已连接到达芬奇", flush=True)
    else:
        print(f"[worker] 暂未连接到达芬奇（将每 {RECONNECT_INTERVAL:.0f}s 重试）："
              f"{conn.last_error}", flush=True)

    # ---- 完全透传 raw（最简方案） ----
    # 达芬奇 GetCurrentTimecode() 的真实表现：
    #   播放时约 1.5~2 秒才跳一次（API 更新间隔慢），跳完又静止；
    #   暂停/seek 时跳到真实播放头位置后静止。
    # 不再做任何外推/封顶/惯性推算。理由：
    #   - 之前用"锚点 + 匀速外推 + 封顶 2s"想解决播放时悬浮窗看起来卡顿，
    #     副作用是暂停后悬浮窗永远比达芬奇快 1~2 秒（用户截图实测：02:51 vs 02:49:33），
    #     而且封顶机制就是承认"暂停时不准，只是限制在 2 秒内"——这违背基本需求。
    #   - 现在彻底透传 raw。暂停时绝对精确等于达芬奇当前位置（一帧都不差）。
    #   - 播放时悬浮窗会"每 1.5~2 秒跳一次"——这是达芬奇 API 自身的怪癖，
    #     无法从外部绕过，只能让用户接受。
    #   - 如果未来达芬奇修复此问题或使用其他 API（如 GetCurrentVideoItem 的实时偏移），
    #     再考虑做平滑。当前最优解就是信任源。
    last_retry = _time.time()      # 上次尝试重连的时间
    last_probe = _time.time()      # 上次探活的时间
    last_print = 0.0
    fps = 50.0

    while True:
        try:
            # ============ 未连接：按节奏重试 ============
            # 关键修复：用户完全可能「先开插件，再开达芬奇」。以前连不上就永远
            # 连不上了（只 connect 一次），悬浮窗会一直显示没有时间线数据。
            if not connected:
                if _time.time() - last_retry >= RECONNECT_INTERVAL:
                    last_retry = _time.time()
                    try:
                        if conn.connect():
                            connected = True
                            last_probe = _time.time()
                            q.put(("init", True, ""))
                            print("[worker] 已连接到达芬奇（重试成功）", flush=True)
                        else:
                            q.put(("init", False, _wait_msg(conn.last_error)))
                    except Exception as e:
                        q.put(("init", False, _wait_msg(f"{type(e).__name__}: {e}")))
                _time.sleep(0.5)
                continue

            # ============ 已连接：定时探活 ============
            # 达芬奇被关掉后，API 往往只是「持续返回空值」而不是抛异常，
            # 所以主动每 2 秒问一句「你还在吗」，掉线就回到重连分支。
            if _time.time() - last_probe >= ALIVE_PROBE_INTERVAL:
                last_probe = _time.time()
                if not conn.is_alive():
                    connected = False
                    conn.disconnect()
                    q.put(("init", False, "达芬奇已关闭，正在等待它重新启动…"))
                    print("[worker] 与达芬奇的连接已断开，回到等待重连状态", flush=True)
                    _time.sleep(0.3)
                    continue

            t0 = _time.time()
            tl = conn.current_timeline_name()
            tc_raw = conn.current_timecode()
            try:
                cur_fps = float(conn.current_framerate() or fps)
            except Exception:
                cur_fps = fps
            fps = cur_fps

            computed_tc = tc_raw if tc_raw else "00:00:00:00"
            mode = "raw"
            q.put(("snapshot", tl, computed_tc, fps, mode))

            # 调试：每秒打印一次
            if _time.time() - last_print > 1.0:
                print(f"[worker] raw={tc_raw} out={computed_tc} mode={mode} tl={tl}", flush=True)
                last_print = _time.time()

            dt = _time.time() - t0
            if dt < 0.1:
                _time.sleep(0.1 - dt)
        except Exception as e:
            q.put(("error", str(e)))
            _time.sleep(1.0)


def _spawn_worker(module_path, lib_path):
    """起一个新的达芬奇 worker 子进程，返回 (queue, process)。"""
    q = mp.Queue()  # 不限大小，避免 worker put 阻塞
    p = mp.Process(target=_resolve_worker, args=(q, module_path, lib_path), daemon=True)
    p.start()
    return q, p


def resolve_loop():
    """主进程后台线程：从 worker 子进程的队列里读数据，更新 STATE。

    GIL 永远不阻塞这里（我们不调用 fusionscript），HTTP 永远能响应。

    另外负责「看住」worker：
        - worker 因为达芬奇关闭时 fusionscript 原生崩溃而退出 → 自动重启；
        - worker 卡死在原生调用里（一直不给消息）→ 杀掉重启。
      重启后 worker 会自己重新连接达芬奇，用户不需要做任何事。
    """
    module_path = CONFIG.get("resolve_script_path")
    lib_path = CONFIG.get("resolve_script_lib")

    HANG_TIMEOUT = 12.0   # worker 超过这么久没任何消息 → 判定卡死，重启它

    _q, _proc = _spawn_worker(module_path, lib_path)
    print(f"[主进程] 启动 worker 子进程 (pid={_proc.pid})", flush=True)

    # 连接状态去重：状态没变就不反复写 STATE，避免前端文字闪烁
    conn_state = {"ok": None, "msg": None}

    def _set_conn(ok: bool, msg: str):
        if conn_state["ok"] == ok and conn_state["msg"] == msg:
            return
        conn_state["ok"] = ok
        conn_state["msg"] = msg
        if ok:
            STATE.update(connected=True, message="")
        else:
            # 断开时把课程画面一并清掉：让悬浮窗显示「等待达芬奇」，
            # 而不是停留在上一次的旧数据上（用户会以为还在同步）
            STATE.update(
                connected=False,
                course_loaded=False,
                timeline_name="",
                course_name="",
                title="",
                segment="",
                action="",
                keyword="",
                intensity="",
                intensity_score=0.0,
                values={},
                segment_remaining=0.0,
                next_step=None,
                message=msg,
            )

    # 等待 worker 的 init 结果
    last_msg = time.time()
    try:
        kind, ok, msg = _q.get(timeout=15)
        last_msg = time.time()
        if kind == "init" and ok:
            print("[已连接] 成功连上达芬奇（worker 子进程）", flush=True)
            _set_conn(True, "")
        else:
            print(f"[未连接] {str(msg)[:200]}", flush=True)
            _set_conn(False, str(msg or "正在等待达芬奇启动…"))
    except Exception as e:
        print(f"[主进程] 等待 worker init 超时: {e}", flush=True)
        _set_conn(False, "正在连接达芬奇…")

    # 主循环：从队列取最新 snapshot，更新 STATE
    dbg_last = 0.0
    while True:
        try:
            item = _q.get(timeout=2)
            last_msg = time.time()
        except Exception:
            # 队列空：worker 要么挂了，要么卡在原生调用里
            now = time.time()
            dead = not _proc.is_alive()
            if dead or (now - last_msg) > HANG_TIMEOUT:
                why = "已退出" if dead else f"超过 {HANG_TIMEOUT:.0f} 秒无响应"
                print(f"[主进程] worker 子进程{why}，正在重启…", flush=True)
                try:
                    _proc.kill()
                except Exception:
                    pass
                try:
                    _proc.join(timeout=2)
                except Exception:
                    pass
                try:
                    _q.close()
                except Exception:
                    pass
                _q, _proc = _spawn_worker(module_path, lib_path)
                last_msg = time.time()
                print(f"[主进程] worker 已重启 (pid={_proc.pid})", flush=True)
            _set_conn(False, "正在连接达芬奇…")
            continue

        if item[0] == "init":
            # worker 的（重）连接结果：连上/断开/仍在等待
            ok = bool(item[1])
            msg = item[2] if len(item) > 2 else ""
            if ok:
                print("[已连接] worker 连上达芬奇", flush=True)
                _set_conn(True, "")
            else:
                _set_conn(False, msg or "正在等待达芬奇启动…")
            continue
        if item[0] == "error":
            _set_conn(False, f"worker 错误: {item[1]}")
            time.sleep(0.5)
            continue
        if item[0] != "snapshot":
            continue

        # 正常 snapshot（兼容旧版 4-tuple 和新版 5-tuple）
        _set_conn(True, "")
        tl_name = item[1]
        tc = item[2]
        fps = item[3] if len(item) > 3 else 25.0
        mode = item[4] if len(item) > 4 else "unknown"
        # 匹配课程
        course = COURSES.find(tl_name)

        STATE.update(connected=True, message="", mode=mode)

        if course is None:
            STATE.update(
                timeline_name=tl_name,
                course_loaded=False,
                course_name="",
                title="",
                segment="",
                action="",
                keyword="",
                intensity="",
                intensity_score=0.0,
                values={},
                segment_remaining=0.0,
                next_step=None,
                message=("未找到对应课程的强度数据（按器械+课程名匹配；如确认有对应 JSON，"
                         "请检查时间线名是否含器械前缀，如'爬楼机-XXX'、'椭圆机-XXX'、'跑步机-XXX'）"
                         if tl_name else "已连接达芬奇，但当前没有打开的时间线"),
            )
        else:
            # 时间码 -> 秒
            t = timecode_to_seconds(tc, fps) if tc else 0.0
            # 调试：每秒打印一次 t（用独立计数器，避免 dbg_last 被前面的 print 抢占）
            if time.time() - dbg_last > 1.0:
                print(f"[main] tc={tc} fps={fps} t={t:.2f} seg={course.segment_at(t)}", flush=True)
                dbg_last = time.time()

            segment = course.segment_at(t)
            action = course.action_at(t)
            keyword = course.keyword_at(t)
            intensity = course.intensity_at(t)
            intensity_score = course.intensity_score_at(t)
            values = course.values_at(t)

            seg_info = course.segment_info_at(t)
            segment_remaining = seg_info["remaining"] if seg_info else 0.0
            next_step = course.next_point_at(t)

            field_meta = [
                {"key": f["key"], "label": f["label"], "unit": f["unit"]}
                for f in course.field_order if f["key"] in values
            ]

            STATE.update(
                timeline_name=tl_name,
                course_name=course.course_name,
                title=getattr(course, "title", "") or course.course_name,
                course_loaded=True,
                equipment=course.equipment,
                segment=segment,
                action=action,
                keyword=keyword,
                intensity=intensity,
                intensity_score=intensity_score,
                values=values,
                segment_remaining=segment_remaining,
                next_step=next_step,
                time_seconds=t,
                mode=mode,
                field_meta=field_meta,
                curve=build_curve(course),
                message="",
            )

        # 不在这里 sleep：worker 已经按 0.1s 节奏 put，主进程紧跟 get 即可
        # （加了 sleep 会让主进程落后于 worker，导致 q 堆积或 STATE 落后）


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            # 就绪探测（launcher 启动时用）。**不算心跳**，避免「只探测没开窗」
            # 也被当成前端还活着。
            self._send_json({"ok": True})
            return

        if self.path == "/pin":
            # launcher 每秒来问一次「现在该不该置顶」。和 /ping 一样**不算心跳**，
            # 否则后端永远不会因为前端关窗而空闲退出。
            self._send_json({"on": PIN.get()})
            return

        if self.path == "/diag":
            # 给「检查环境.bat」用的诊断口：和 /state 一样的内容，但**不算心跳**。
            # 体检只是个一次性的旁观者，不能因为问了一句就把本该自动退出的
            # 旧后端留住（那正是「端口被占」这类怪现象的来源）。
            s = STATE.snapshot()
            s.update(SERVER_META)
            s["course_count"] = COURSES.count()
            s["pin"] = PIN.get()
            s.update(CONVERT.snapshot())
            self._send_json(s)
            return

        s = STATE.snapshot()
        if self.path in ("/", "/state"):
            LIVENESS.beat()   # 前端在拉数据 = 前端还活着
            s.update(SERVER_META)
            # 悬浮窗右下角按钮的状态：课件数 > 0 显示「清除课件缓存」，
            # == 0 显示「选择课件」；转换进行中则临时显示「等待选择…」。
            s["course_count"] = COURSES.count()
            s["pin"] = PIN.get()   # 左上角图钉按钮：当前是否置顶
            s.update(CONVERT.snapshot())
            self._send_json(s)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # 先把请求体读掉：sendBeacon 会带一个 body，不读干净会残留在连接里，
        # 影响这条 keep-alive 连接上的后续请求。
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(n)
        except Exception:
            pass

        # 清除课件缓存：删除 data_dir 下所有转换生成的 JSON
        if self.path == "/clear_cache":
            try:
                removed, remaining = COURSES.clear_all()
                self._send_json({
                    "ok": True,
                    "removed": removed,
                    "remaining": remaining,
                    "message": f"已清除 {removed} 个课件缓存文件",
                })
            except Exception as e:
                self._send_json({"ok": False, "message": f"清除失败: {e}"})
        elif self.path == "/convert":
            # 前端「选择课件」按钮：后台起一个 convert_course.py（会弹系统文件框），
            # 立刻返回，不让 HTTP 请求等着用户选文件；进度由 /state 的 converting
            # 和 course_count 反映，前端据此把按钮切回来。
            started = CONVERT.start()
            if started:
                self._send_json({
                    "ok": True,
                    "started": True,
                    "message": "已弹出选择窗口，请在窗口里选择课件表格",
                })
            else:
                self._send_json({
                    "ok": False,
                    "started": False,
                    "message": "上一次转换还没结束，请先在弹窗里选择文件",
                })
        elif self.path.startswith("/pin"):
            # 前端图钉按钮：POST /pin?on=1 / ?on=0
            q = ""
            if "?" in self.path:
                q = self.path.split("?", 1)[1]
            on = True
            for kv in q.split("&"):
                if kv.startswith("on="):
                    on = kv[3:].strip().lower() not in ("0", "false", "no", "off", "")
            applied = PIN.set(on)
            self._send_json({"ok": True, "on": applied})
        elif self.path == "/shutdown":
            # 前端关窗（pagehide）时用 sendBeacon 打过来 → 后端自行退出
            self._send_json({"ok": True})
            LIVENESS.request_shutdown()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # 静默日志


class _SyncHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务器，但**关掉端口复用**。

    为什么：Windows 上 allow_reuse_address（SO_REUSEADDR）的语义是「允许绑定一个
    已被别人监听的端口」。结果就是旧后端没被杀干净时，新后端会"看似启动成功"，
    而请求可能落到旧进程上——表现就是「改了代码却像没生效」「数据是旧的」。

    关掉之后端口被占就直接报错，配合 launcher 的清理逻辑，行为可预期。
    """
    allow_reuse_address = False
    daemon_threads = True


def start_server():
    # 用多线程 HTTP 服务器：fusionscript 的 native 调用会长时间持有 GIL，
    # 单线程模式会导致 HTTP 响应被阻塞，客户端 fetch 失败。
    try:
        server = _SyncHTTPServer(("127.0.0.1", CONFIG["port"]), Handler)
    except OSError as e:
        print(f"[错误] 无法监听端口 {CONFIG['port']}：{e}", flush=True)
        print("      多半是上一个后端没退干净。重新双击 start.bat 即可（它会先清理）。",
              flush=True)
        raise

    print(f"[课程强度同步] HTTP 服务已启动: http://127.0.0.1:{CONFIG['port']}")
    if LIVENESS.enabled:
        print(f"[课程强度同步] 悬浮窗关闭后自动退出已开启"
              f"（兜底空闲 {LIVENESS.idle_timeout:.0f}s）")
    server.serve_forever()


if __name__ == "__main__":
    # 后台线程：达芬奇轮询
    t = threading.Thread(target=resolve_loop, daemon=True)
    t.start()

    # 后台线程：前端存活检测（关掉悬浮窗 → 自动退出）
    tw = threading.Thread(target=LIVENESS.watchdog, daemon=True)
    tw.start()

    # 前台：HTTP 服务
    try:
        start_server()
    except KeyboardInterrupt:
        print("已退出。")
