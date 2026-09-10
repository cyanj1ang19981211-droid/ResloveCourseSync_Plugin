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
import sys
import threading
import multiprocessing as mp
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 确保脚本所在目录在 sys.path（兼容 embedded Python 的 ._pth 机制不自动加脚本目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from course_data import CourseData, load_course, list_course_files
from resolve_connection import ResolveConnection, timecode_to_seconds

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------- 配置 ----------

def load_config():
    cfg_path = os.path.join(BASE_DIR, "config.json")
    default = {
        "resolve_script_path": None,        # 达芬奇 scripting 模块路径（留空则自动探测）
        "data_dir": os.path.join(BASE_DIR, "data"),
        "port": 8765,
        "poll_interval": 0.1,               # 轮询间隔（秒）
    }
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            user = json.load(f)
        default.update(user)
    return default


CONFIG = load_config()


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


# ---------- 课程数据管理 ----------

class CourseManager:
    """按时间线名称匹配课程数据文件。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._cache = {}  # {course_name: CourseData}

    def _reload(self):
        self._cache = {}
        for f in list_course_files(self.data_dir):
            try:
                c = load_course(f)
                self._cache[c.course_name] = c
            except Exception:
                continue

    @staticmethod
    def _normalize(name: str) -> str:
        """把课程/时间线名归一化：去掉器械前缀、标点、'训练'后缀等，方便宽松匹配。

        例如 "跑步机-基础跑姿训练" -> "基础跑姿"
             "基础跑训练"            -> "基础跑"
        """
        if not name:
            return ""
        s = str(name)
        for prefix in ("跑步机", "单车", "划船机", "椭圆机", "徒手"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        for ch in "-_ —\t\n":
            s = s.replace(ch, "")
        for suf in ("训练",):
            if s.endswith(suf):
                s = s[:-len(suf)]
        return s.strip()

    def find(self, timeline_name: str):
        """根据时间线名查找课程；每 5 秒重载一次数据目录以支持热更新。

        匹配优先级：
          1) 精确匹配
          2) 包含关系（time 含 course 或 course 含 time）
          3) 归一化后精确匹配（去器械前缀/标点/'训练'后缀）
          4) 归一化后包含关系（任一方含另一方）
        """
        now = time.time()
        if now - getattr(self, "_last_reload", 0) > 5:
            self._reload()
            self._last_reload = now
        if not timeline_name:
            return None
        # 1) 精确匹配
        if timeline_name in self._cache:
            return self._cache[timeline_name]
        # 2) 包含关系
        for name, c in self._cache.items():
            if name and (name in timeline_name or timeline_name in name):
                return c
        # 3) 归一化后精确匹配
        tn = self._normalize(timeline_name)
        if tn:
            for name, c in self._cache.items():
                if tn == self._normalize(name):
                    return c
        # 4) 归一化后包含关系
        if tn and len(tn) >= 2:
            for name, c in self._cache.items():
                cn = self._normalize(name)
                if cn and (cn in tn or tn in cn):
                    return c
        return None


COURSES = CourseManager(CONFIG["data_dir"])


# ---------- 达芬奇 worker（独立子进程，避免 fusionscript 的 GIL 阻塞主进程 HTTP） ----------

# ---------- 折线图数据生成 ----------

def build_curve(course: CourseData):
    """根据器械类型返回对应的曲线预览数据。

    - 器械课（跑步机/单车/划船机/椭圆机）：从 points 里抽 speed/incline 等数值字段
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
    ok = conn.connect()
    if ok:
        q.put(("init", True, ""))
    else:
        q.put(("init", False, conn.last_error or "未知原因"))

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
    last_print = 0.0
    fps = 50.0

    while True:
        try:
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


def resolve_loop():
    """主进程后台线程：从 worker 子进程的队列里读数据，更新 STATE。

    GIL 永远不阻塞这里（我们不调用 fusionscript），HTTP 永远能响应。
    """
    # 启动 worker 子进程
    module_path = CONFIG.get("resolve_script_path")
    lib_path = CONFIG.get("resolve_script_lib")
    _q = mp.Queue()  # 不限大小，避免 worker put 阻塞
    _proc = mp.Process(target=_resolve_worker, args=(_q, module_path, lib_path), daemon=True)
    _proc.start()
    print(f"[主进程] 启动 worker 子进程 (pid={_proc.pid})")

    # 等待 worker 的 init 结果
    try:
        kind, ok, msg = _q.get(timeout=15)
        if kind == "init" and ok:
            print("[已连接] 成功连上达芬奇（worker 子进程）")
        else:
            print(f"[未连接] {msg[:200]}")
    except Exception as e:
        print(f"[主进程] 等待 worker init 超时: {e}")
        kind, ok, msg = ("init", False, "worker 启动超时")

    STATE.update(connected=bool(ok), message=msg.splitlines()[0][:200] if msg else "")

    # 主循环：从队列取最新 snapshot，更新 STATE
    last_snapshot = ("", None, 25.0)  # (tl_name, tc, fps)
    poll_interval = CONFIG.get("poll_interval", 0.1)
    dbg_last = 0.0
    while True:
        try:
            item = _q.get(timeout=3)
        except Exception:
            # worker 卡住或死了，标记同步中断（但不动 STATE 的 data，让画面保留）
            STATE.update(connected=False, message="达芬奇通信超时")
            time.sleep(0.5)
            continue

        if item[0] == "init":
            # worker 重新连接成功（init 在 connect 重试时也会 put）
            STATE.update(connected=bool(item[1]), message=item[2] if len(item) > 2 else "")
            if item[1]:
                print("[已连接] worker 重新连上达芬奇")
            continue
        if item[0] == "error":
            STATE.update(connected=False, message=f"worker 错误: {item[1]}")
            time.sleep(0.5)
            continue
        if item[0] != "snapshot":
            continue

        # 正常 snapshot（兼容旧版 4-tuple 和新版 5-tuple）
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
                message=("未找到同名课程数据" if tl_name else "当前无时间线") + "（数据目录见 config.json）",
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
        s = STATE.snapshot()
        if self.path in ("/", "/state"):
            self._send_json(s)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # 静默日志


def start_server():
    # 用多线程 HTTP 服务器：fusionscript 的 native 调用会长时间持有 GIL，
    # 单线程模式会导致 HTTP 响应被阻塞，客户端 fetch 失败。
    server = ThreadingHTTPServer(("127.0.0.1", CONFIG["port"]), Handler)
    print(f"[课程强度同步] HTTP 服务已启动: http://127.0.0.1:{CONFIG['port']}")
    server.serve_forever()


if __name__ == "__main__":
    # 后台线程：达芬奇轮询
    t = threading.Thread(target=resolve_loop, daemon=True)
    t.start()

    # 前台：HTTP 服务
    try:
        start_server()
    except KeyboardInterrupt:
        print("已退出。")
