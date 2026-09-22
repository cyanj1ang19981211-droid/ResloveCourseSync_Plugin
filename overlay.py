# -*- coding: utf-8 -*-
"""
悬浮窗启动器（Windows）。

用 Edge 的 --app 模式打开 overlay.html，得到一个无地址栏的独立小窗。
窗口初始尺寸按屏幕工作区的比例算（见下方 WINDOW_SIZE_RATIO），并可选「始终置顶」
（见下方说明；对应前端左上角的图钉按钮）。

**悬浮窗用的是自己的浏览器档案（独立 user-data-dir）**，见 EDGE_PROFILE_DIR ——
这样它和用户自己开的 Edge 完全不相干：用户关掉自己的 Edge 不会把悬浮窗带走，
用户开自己的网页也不会挤进悬浮窗这个窗口，退出时我们也能干净地只关掉自己那一个
浏览器实例（不会误杀用户正在用的 Edge）。

注意：Edge 对 --app 窗口会恢复「上次记住的尺寸」，经常完全无视 --window-size，
所以开窗后还会用 Win32 SetWindowPos 再强制一次（见 _enforce_window_layout）。

用法：
    python overlay.py            # 只开窗（后端需另行启动）
    python launcher.py           # 一键启动：后台起后端 + 开窗 + 关窗自动收尾

本模块只负责「把窗户开出来」，进程编排交给 launcher.py。
"""
import ctypes
import json
import os
import subprocess
import time
import webbrowser
from ctypes import wintypes

from version import VERSION

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.path.join(BASE_DIR, "frontend", "overlay.html")
RUNTIME_DIR = os.path.join(BASE_DIR, ".runtime")

# 悬浮窗专用的 Edge 档案目录（cookie/缓存/窗口尺寸记忆都在里面，不进 git）。
#
# 为什么必须独立（否则就是「用户自己的 Edge 和插件打架」）：
#   · 共用默认档案时，悬浮窗其实是**用户那个 Edge 实例**里的一个窗口 —— 用户
#     关掉自己的浏览器，悬浮窗跟着消失；用户按 Ctrl+Shift+W 之类也会带走它；
#   · 反过来，我们用 taskkill 清理自己的悬浮窗时，杀的就是用户那个浏览器进程，
#     会把用户所有标签页一起关掉；
#   · 独立档案之后，它是另一个 Edge 实例：互不干扰，也能精确地只关掉自己。
EDGE_PROFILE_DIR = os.path.join(RUNTIME_DIR, "edge-profile")

# 记录「我开的那个悬浮窗是谁」（pid + 窗口所属进程），退出时据此精确清理。
# 放在 .runtime/ 里（已 gitignore）。
OVERLAY_STATE = os.path.join(RUNTIME_DIR, "overlay.json")

# ---------------------------------------------------------------- 悬浮窗尺寸
# 初始尺寸按「屏幕工作区」的比例计算，而不是写死像素——这样 1080p 和 4K 上
# 看起来占比一致。
#
# 默认 13.5% 宽 × 22% 高，对应的就是用户参考图里的观感（1920x1080 下约
# 260x230，约占屏幕 1/7 宽、1/5 高）。之前写死的 400x400 在小屏上会占到
# 屏幕一半，太大。
#
# 可在 config.json 里覆盖：
#   "overlay_window_ratio": [0.135, 0.22]   # 按屏幕比例（推荐）
#   "overlay_window_size":  [300, 260]      # 直接指定像素（优先级更高）
WINDOW_SIZE_RATIO = (0.135, 0.22)
WINDOW_SIZE_MIN = (260, 220)      # 太小的屏幕上别缩到看不清
WINDOW_SIZE_MAX = (720, 620)      # 太大的屏幕上别铺满

# 悬浮窗标题（overlay.html 的 <title>）。launcher.py 靠它判断窗口是否被关掉，
# 改动时两处要一起改。
#
# 可以用环境变量 RESOLVE_SYNC_TITLE 覆盖（连 overlay.html 也要改），用途只有一个：
# **自动化测试/沙箱**。同一台机器上如果要开两个实例，两个窗口标题一模一样的话，
# 先开的那个 launcher 会把后开的窗口当成自己的，测试就会互相误杀。
OVERLAY_TITLE = os.environ.get("RESOLVE_SYNC_TITLE") or "课程强度同步"

# 是否让悬浮窗「始终置顶」（替代手动按 Win+Ctrl+T，效果同 PowerToys 的
# Always On Top）。config.json 里可用 "always_on_top": false 关掉。
ALWAYS_ON_TOP = True

# 找 Edge 浏览器。别只认 C 盘默认位置 —— 用户的 Program Files 有可能在别的盘，
# 也可能把 Edge 装到自定义目录，所以按「注册表 → 环境变量 → 常见路径」的顺序找。
_EDGE_REL = ("Microsoft", "Edge", "Application", "msedge.exe")
_EDGE_MEMO = []


def _edge_from_registry():
    """Windows 会给已安装程序登记 App Paths\\msedge.exe，这是最权威的一条。"""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                h = winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                    0, winreg.KEY_READ | view)
            except OSError:
                continue
            try:
                v = winreg.QueryValueEx(h, "")[0]
                if v and os.path.isfile(v):
                    return v
            except OSError:
                pass
    return None


def find_edge():
    if _EDGE_MEMO:
        return _EDGE_MEMO[0]
    cands = [_edge_from_registry()]
    for env_key in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432",
                    "LOCALAPPDATA", "SystemDrive"):
        root = os.environ.get(env_key)
        if not root:
            continue
        if env_key == "SystemDrive":
            # SystemDrive 是 "C:"，补成根目录再拼默认的 Program Files 位置
            for sub in ("Program Files (x86)", "Program Files"):
                cands.append(os.path.join(root + "\\", sub, *_EDGE_REL))
            continue
        cands.append(os.path.join(root, *_EDGE_REL))
    # 最后再试最常见的默认位置（少数机器上环境变量是空的）
    cands += [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    for p in cands:
        if p and os.path.exists(p):
            _EDGE_MEMO.append(p)
            return p
    return None


def overlay_url():
    """overlay.html 用 file:// 打开（页面内部自己 fetch 127.0.0.1 的后端）。

    后端端口通过 ?port= 传给页面：页面是 file:// 打开的，读不到 config.json，
    只能靠 URL 参数带过去。默认 8765，与 server.py / config.json 一致。
    也认环境变量 RESOLVE_SYNC_PORT（与 launcher.py 保持一致，便于跑第二个实例）。
    """
    env_port = os.environ.get("RESOLVE_SYNC_PORT")
    port = env_port if (env_port and env_port.isdigit()) else _read_config().get("port") or 8765
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8765
    return "file:///" + OVERLAY.replace("\\", "/") + f"?port={port}"


def _read_config():
    """读 config.json（读不到就返回空字典，一切走默认值）。"""
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _screen_work_area():
    """主屏工作区（去掉任务栏后的可用区域）像素尺寸。失败回退 1920x1080。

    本进程是 DPI-unaware 的，Windows 会把返回值按缩放比例折算成「逻辑像素」，
    与 Edge --window-size 使用同一套单位，所以两边比例天然一致。
    """
    try:
        import ctypes
        from ctypes import wintypes
        SPI_GETWORKAREA = 0x0030
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        )
        if ok:
            w = int(rect.right - rect.left)
            h = int(rect.bottom - rect.top)
            if w >= 640 and h >= 480:
                return w, h
    except Exception:
        pass
    return 1920, 1080


def compute_window_size():
    """算出悬浮窗初始尺寸 (宽, 高)（像素 / 逻辑像素）。

    优先级：config 的 overlay_window_size（固定像素）
          > config 的 overlay_window_ratio（屏幕比例）
          > WINDOW_SIZE_RATIO（默认比例）
    最后统一用 WINDOW_SIZE_MIN / MAX 钳制，避免极端分辨率下不可用。
    """
    cfg = _read_config()

    # 1) 固定像素
    fixed = cfg.get("overlay_window_size")
    if isinstance(fixed, (list, tuple)) and len(fixed) == 2:
        try:
            fw, fh = int(fixed[0]), int(fixed[1])
            if fw >= 200 and fh >= 160:
                return fw, fh
        except (TypeError, ValueError):
            pass

    # 2) 屏幕比例
    ratio = cfg.get("overlay_window_ratio")
    if not (isinstance(ratio, (list, tuple)) and len(ratio) == 2):
        ratio = WINDOW_SIZE_RATIO
    try:
        rw, rh = float(ratio[0]), float(ratio[1])
        if not (0.05 <= rw <= 0.9 and 0.05 <= rh <= 0.9):
            rw, rh = WINDOW_SIZE_RATIO
    except (TypeError, ValueError):
        rw, rh = WINDOW_SIZE_RATIO

    sw, sh = _screen_work_area()
    w = int(round(sw * rw))
    h = int(round(sh * rh))
    w = max(WINDOW_SIZE_MIN[0], min(w, WINDOW_SIZE_MAX[0]))
    h = max(WINDOW_SIZE_MIN[1], min(h, WINDOW_SIZE_MAX[1]))
    return w, h


def always_on_top_enabled():
    """config.json 里 always_on_top 的**初始**值（默认开）。

    写错类型/读不到配置都按默认走，不让用户因为一个配置项打不开窗口。
    注意这只是「默认」；用户在前端点图钉后的选择会记到 .runtime/topmost.json，
    优先级更高（见 read_topmost_pref）。
    """
    val = _read_config().get("always_on_top", ALWAYS_ON_TOP)
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off")
    return bool(val)


# 用户在前端点图钉产生的「是否置顶」偏好。放 .runtime/（已 gitignore）：
# 只影响这台机器上的观感，不属于要提交的项目内容。
PREF_FILE = os.path.join(BASE_DIR, ".runtime", "topmost.json")


def read_topmost_pref():
    """悬浮窗要不要置顶：优先用用户上次点图钉的选择，没有就按 config.json 默认。

    这样「取消置顶」是一次性的选择 —— 用户不想让它压在达芬奇上面，关掉一次，
    下次打开就不会又被强制置顶。
    """
    try:
        with open(PREF_FILE, "r", encoding="utf-8") as f:
            v = json.load(f)
        if isinstance(v, dict) and isinstance(v.get("on"), bool):
            return v["on"]
    except Exception:
        pass
    return always_on_top_enabled()


def write_topmost_pref(on):
    """记下用户的置顶选择（best-effort：写不了也不影响本次生效）。"""
    try:
        os.makedirs(os.path.dirname(PREF_FILE), exist_ok=True)
        tmp = PREF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"on": bool(on)}, f)
        os.replace(tmp, PREF_FILE)
        return True
    except Exception:
        return False


def launch_overlay(detached=True):
    """打开悬浮窗，返回 (是否成功, 是否为独立 app 窗口, 进程对象或 None)。

    第二个返回值很关键：launcher 只在「确实开出了独立 app 窗口」时才去监控
    窗口关闭；退回默认浏览器（变成普通标签页）时不做窗口监控。

    第三个返回值是刚起的 msedge 进程，交给 launcher 做退出清理（见 stop_overlay）。
    """
    edge = find_edge()
    url = overlay_url()
    size = compute_window_size()
    topmost = read_topmost_pref()   # 用户上次点图钉的选择优先于 config 默认值

    if edge:
        try:
            os.makedirs(EDGE_PROFILE_DIR, exist_ok=True)
        except Exception:
            pass
        cmd = [
            edge,
            "--app=" + url,
            f"--window-size={size[0]},{size[1]}",
            # 独立档案目录 —— 与用户自己的 Edge 彻底隔开（见 EDGE_PROFILE_DIR 说明）
            "--user-data-dir=" + EDGE_PROFILE_DIR,
            # 新档案第一次启动别弹「首次运行引导 / 设为默认浏览器」这些向导，
            # 否则用户看到的是一个向导页而不是悬浮窗。
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2",
        ]
        flags = 0x00000008 if detached else 0  # DETACHED_PROCESS
        proc = None
        try:
            proc = subprocess.Popen(cmd, creationflags=flags)
        except OSError:
            proc = None  # 启动失败，落到下面的默认浏览器
        if proc is not None:
            # Edge 对 --app 窗口会恢复「上次记住的尺寸」，常常完全无视
            # --window-size（实测请求 415x370，实际开出 1522x1660 = 半屏）。
            # 所以等窗口出现后再用 SetWindowPos 强制一次，同时把它置顶。
            _enforce_window_layout(size, topmost=topmost)
            reminder_overlay_state(proc)
            return True, True, proc

    # 没有 Edge 时退回默认浏览器（会是普通标签页，非独立窗口）
    try:
        webbrowser.open(url)
        return True, False, None
    except Exception:
        return False, False, None


# ---------------------------------------------------------------- 进程/窗口工具
#
# 都是「退出时要把自己开的东西收干净」用的。放在 overlay.py 里是因为它本来就是
# 这个项目的 Win32 小工具模块（server.py 借它的置顶实现，launcher.py 借它的
# 窗口枚举），进程清理和窗口枚举是同一类东西。
#
# 注意：ctypes 调 Win32 **必须先设 argtypes/restype**。不设的话 HANDLE 会被当成
# 32 位 int 传，64 位下句柄失真（这个坑在本文件上面的窗口部分已经踩过一次）。

def _procs():
    """返回 (kernel32, user32)，都设好了 argtypes/restype。失败返回 (None, None)。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)

        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        return k32, u32
    except Exception:
        return None, None


def pid_alive(pid) -> bool:
    """进程还活着吗。

    用途：父进程看门狗（server.py）判断 launcher 还在不在。
    打不开句柄时**保守地认为还活着**（宁可多活一会儿，也不要误杀），
    唯一例外是错误码 87（参数错误 = 没这个进程）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    k32, _u32 = _procs()
    if not k32:
        return True
    SYNCHRONIZE = 0x00100000
    h = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return ctypes.get_last_error() != 87      # 87 = ERROR_INVALID_PARAMETER
    try:
        # WaitForSingleObject 返回 0 = 对象已 Signaled = 进程已退出
        return k32.WaitForSingleObject(h, 0) != 0
    finally:
        k32.CloseHandle(h)


def proc_image_path(pid):
    """进程的 exe 完整路径（拿不到返回空串）。用来确认「这个 PID 确实是 msedge」。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    k32, _u32 = _procs()
    if not k32:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
    except Exception:
        pass
    finally:
        k32.CloseHandle(h)
    return ""


def window_owner_pid(hwnd):
    """窗口所属进程的 PID（拿不到返回 0）。"""
    u32_ = ctypes.WinDLL("user32", use_last_error=True)
    try:
        u32_.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        pid = wintypes.DWORD(0)
        u32_.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def kill_tree(pid, expect_prefix="msedge") -> bool:
    """杀掉进程及其整棵子进程树（`/T`：Edge 和 Python 都会带一串子进程）。

    只对「exe 名字以 expect_prefix 开头」的进程生效 —— 这是防误杀的最后一道闸：
    万一 pid 已经被系统复用成了别的程序，这里会拒绝动手。
        · 悬浮窗 → expect_prefix="msedge"
        · 后端   → expect_prefix="python"（python.exe 或 pythonw.exe 都算）
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    image = proc_image_path(pid)
    if not image:
        return False
    if expect_prefix and not os.path.basename(image).lower().startswith(expect_prefix.lower()):
        return False
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)   # CREATE_NO_WINDOW
        return True
    except Exception:
        return False


def _overlay_window_pids():
    """标题是本插件悬浮窗的窗口，其所属进程 PID 列表。"""
    u32_, wintypes_ = _win32()
    if not u32_:
        return []
    pids = []
    try:
        own = ctypes.windll.kernel32.GetConsoleWindow()

        def _cb(hwnd, _lparam):
            if hwnd == own or not u32_.IsWindowVisible(hwnd):
                return True
            n = u32_.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32_.GetWindowTextW(hwnd, buf, n + 1)
            if OVERLAY_TITLE in buf.value:
                p = window_owner_pid(hwnd)
                if p:
                    pids.append(p)
            return True

        ENUM = ctypes.WINFUNCTYPE(wintypes_.BOOL, wintypes_.HWND, wintypes_.LPARAM)
        u32_.EnumWindows(ENUM(_cb), 0)
    except Exception:
        return []
    return pids


def reminder_overlay_state(proc=None):
    """记下「这次的悬浮窗是谁」，供退出清理与下一次启动清场用。

    为什么不能只靠 `proc.pid`：Edge 的多进程结构里，我们 Popen 出来的那个进程
    不一定是**开着窗口**的那个（同档案再开一个窗口时，新进程会把请求交给已有
    实例然后自己退出）。所以窗口出现后，按窗口反查一次真正的宿主 PID 更可靠。
    """
    info = read_overlay_state() or {}
    if proc is not None:
        try:
            info["pid"] = int(proc.pid)
        except Exception:
            pass
    info["profile_dir"] = EDGE_PROFILE_DIR
    # 窗口可能还没出现（Edge 要几百毫秒），稍等一下再反查，免得记了个空值
    for _ in range(20):
        pids = _overlay_window_pids()
        if pids:
            info["window_pid"] = pids[0]
            break
        time.sleep(0.2)
    info["title"] = OVERLAY_TITLE
    info["version"] = VERSION
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        tmp = OVERLAY_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        os.replace(tmp, OVERLAY_STATE)
    except Exception:
        pass
    return info


def read_overlay_state():
    """读回上次记录的悬浮窗信息（没有/读坏了返回空 dict）。"""
    try:
        with open(OVERLAY_STATE, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def stop_overlay(proc=None):
    """关掉我们自己开出来的悬浮窗（只关自己的，绝不碰用户的 Edge）。

    清理三个来源，逐个都是「确认过确实是 msedge 才动手」：
        1. 这次 Popen 出来的进程；
        2. .runtime/overlay.json 里记着的上次实例（防止上一轮没清干净）；
        3. 标题正是本插件悬浮窗、且属于**我们的独立档案**的窗口。

    第 3 条为什么要额外校验档案：老版本（还没有独立档案的时候）是在用户自己的
    Edge 里开窗的，那个窗口标题跟我们一样 —— 但它属于用户的浏览器进程，
    一刀切地按标题杀会把用户所有标签页一起关掉。
    """
    cands = []
    if proc is not None:
        try:
            cands.append(int(proc.pid))
        except Exception:
            pass
    st = read_overlay_state()
    # 状态文件只认「自己写的」那一份：两个 launcher 前后脚启动时，先启动的那个
    # 可能读到后一个刚写进去的记录，照着杀就会把**别人刚开出来的窗口**关掉。
    if int(st.get("owner_pid") or 0) == os.getpid():
        for key in ("pid", "window_pid"):
            try:
                p = int(st.get(key) or 0)
                if p and p not in cands:
                    cands.append(p)
            except (TypeError, ValueError):
                pass

    profile = os.path.normcase(os.path.abspath(EDGE_PROFILE_DIR))
    for p in _overlay_window_pids():
        if p in cands:
            continue
        # 只认「用同一个独立档案」的 msedge：命令行里带我们的 user-data-dir。
        # 拿不到命令行就不动它 —— 宁可留一个死窗口让用户手动关，也不误杀用户的浏览器。
        if _proc_cmdline_has(p, profile):
            cands.append(p)

    killed = []
    for p in cands:
        if kill_tree(p):
            killed.append(p)
    try:
        if os.path.exists(OVERLAY_STATE):
            os.remove(OVERLAY_STATE)
    except Exception:
        pass
    if killed:
        print(f"[退出] 已关闭悬浮窗（msedge PID {', '.join(str(p) for p in killed)}）",
              flush=True)
    return killed


def _proc_cmdline_has(pid, needle, timeout=6.0):
    """进程命令行里是否含某个字符串（用 PowerShell 查一次，失败就返回 False）。

    只在退出清理时调用一次，慢一点无所谓；绝不为了让清理更"聪明"而冒险误杀。
    """
    if not needle:
        return False
    try:
        ps = ("(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\")"
              ".CommandLine" % int(pid))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, errors="replace", timeout=timeout,
            creationflags=0x08000000)
        return needle.lower() in (out.stdout or "").lower()
    except Exception:
        return False


def kill_stale_overlay():
    """启动前清场：上一次留下的悬浮窗先关掉。

    不清的话会出现两个悬浮窗叠着（旧的那个连着一个已经死掉的后端，还显示着上次
    的数据），用户会以为「插件乱了」。

    两步走，都只对**确认是我们自己**的 msedge 动手（overlay.kill_tree 会核对 exe 名）：
        1. .runtime/overlay.json 里记着的 pid / window_pid（上一次自己写的，最准）；
        2. 兜底：标题正是悬浮窗、且命令行里带我们的独立档案目录的窗口。
    """
    dead = []
    st = read_overlay_state()
    for key in ("pid", "window_pid"):
        try:
            p = int(st.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if p and p not in dead and kill_tree(p):
            dead.append(p)

    profile = os.path.normcase(os.path.abspath(EDGE_PROFILE_DIR))
    for p in _overlay_window_pids():
        if p in dead:
            continue
        if _proc_cmdline_has(p, profile) and kill_tree(p):
            dead.append(p)

    if dead:
        print(f"[启动] 已关掉上一次留下的悬浮窗（PID {', '.join(map(str, dead))}）",
              flush=True)
    return dead


def apply_topmost(on):
    """把悬浮窗设为 / 取消「始终置顶」，返回是否找到了窗口。

    给 launcher 周期性调用（每秒一次）：只纠正置顶状态，**不碰尺寸和位置** ——
    用户手动把窗口拉大了、或者最小化了，都不该被我们拽回来。
    已经是目标状态就什么都不做（避免每秒无谓地去动窗口）。
    """
    hwnd = _find_overlay_hwnd()
    if not hwnd:
        return False
    if on and not _is_topmost(hwnd):
        _set_topmost(hwnd, True)
    elif (not on) and _is_topmost(hwnd):
        _set_topmost(hwnd, False)
    return True


# ------------------------------------------------------- 强制窗口尺寸（Win32）
#
# 注意：ctypes 调用 Win32 API **必须**先设 argtypes/restype。不设的话 ctypes 默认
# 按 C int 传参，64 位下 HWND 会被截断成 32 位，句柄失真 → IsWindowVisible 直接
# 返回 False，枚举永远匹配不到窗口（这个坑踩过一次）。

def _win32():
    """返回 (user32, wintypes)；非 Windows 或初始化失败返回 (None, None)。"""
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32   # GetConsoleWindow 在 kernel32，不在 user32
        u32.EnumWindows.argtypes = [
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        ]
        u32.EnumWindows.restype = wintypes.BOOL
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetWindowTextLengthW.restype = ctypes.c_int
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u32.GetWindowRect.restype = wintypes.BOOL
        u32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.IsIconic.restype = wintypes.BOOL
        u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.GetWindowLongW.restype = ctypes.c_long
        k32.GetConsoleWindow.restype = wintypes.HWND
        return u32, wintypes
    except Exception:
        return None, None


def _find_overlay_hwnd():
    """找标题含 OVERLAY_TITLE 的可见窗口，返回 hwnd（找不到返回 None）。"""
    u32, wintypes = _win32()
    if not u32:
        return None
    try:
        import ctypes

        own = ctypes.windll.kernel32.GetConsoleWindow()  # 排除自己所在的控制台窗口
        found = []

        def _cb(hwnd, _lparam):
            if hwnd == own or not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if OVERLAY_TITLE in buf.value:
                found.append(hwnd)
                return False
            return True

        ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows(ENUM(_cb), 0)
        return found[0] if found else None
    except Exception:
        return None


def _window_rect(hwnd):
    """返回窗口外框 (left, top, width, height)；失败返回 None。"""
    u32, wintypes = _win32()
    if not u32:
        return None
    try:
        import ctypes

        r = wintypes.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


def _apply_window_size(hwnd, w, h):
    """把窗口还原成普通状态 + 设成指定尺寸，并保证不超出屏幕工作区。

    位置尽量沿用 Edge 记忆的位置（用户可能习惯放在右边），只有越界时才往里收。
    """
    u32, _wintypes = _win32()
    if not u32:
        return False
    try:
        if u32.IsIconic(hwnd):
            # 窗口最小化了：不要动它（ShowWindow(SW_RESTORE) 会把它弹回来，
            # 用户会以为"最小化不管用"）。
            return True

        u32.ShowWindow(hwnd, 9)  # SW_RESTORE：万一被记忆成最大化，先还原

        rect = _window_rect(hwnd)
        x, y = (rect[0], rect[1]) if rect else (0, 0)

        sw, sh = _screen_work_area()
        if x + w > sw:
            x = sw - w
        if y + h > sh:
            y = sh - h
        if x < 0:
            x = 0
        if y < 0:
            y = 0

        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        return bool(u32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                                     SWP_NOZORDER | SWP_NOACTIVATE))
    except Exception:
        return False


# ---------------------------------------------------------- 始终置顶（Win32）
#
# 等价于 PowerToys 的 Always On Top：给窗口加 WS_EX_TOPMOST。
# 用 SetWindowPos(HWND_TOPMOST, ..., SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE) 实现：
#   - NOMOVE/NOSIZE：只改 Z 序，不动我们刚设好的尺寸和位置；
#   - NOACTIVATE：不要把焦点从达芬奇抢过来（否则用户在达芬奇里按空格暂停会被打断）。

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008


def _set_topmost(hwnd, on=True):
    """把窗口设为「始终置顶」/取消置顶。成功返回 True。"""
    u32, _wintypes = _win32()
    if not u32:
        return False
    try:
        return bool(u32.SetWindowPos(
            hwnd,
            HWND_TOPMOST if on else HWND_NOTOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        ))
    except Exception:
        return False


def _is_topmost(hwnd):
    """检查窗口当前是否已经是置顶状态（读 WS_EX_TOPMOST 扩展样式位）。"""
    u32, _wintypes = _win32()
    if not u32:
        return False
    try:
        ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        return bool(ex & WS_EX_TOPMOST)
    except Exception:
        return False


def _enforce_window_layout(size, topmost=True, timeout=10.0):
    """等悬浮窗出现后，把尺寸和置顶都强制成我们想要的样子。

    为什么要「反复确认」：Edge 可能在窗口显示后的一小会儿才把记忆中的尺寸套回去，
    也可能重建窗口把置顶状态弄丢，所以只在「尺寸和置顶都连续两次正确」时才收手，
    否则继续纠正。最多等 timeout 秒。
    找不到窗口（比如没装 Edge / 被系统拦住）就直接返回，不影响主流程。
    """
    w, h = int(size[0]), int(size[1])
    deadline = time.time() + timeout
    stable = 0
    while time.time() < deadline:
        hwnd = _find_overlay_hwnd()
        if hwnd:
            rect = _window_rect(hwnd)
            size_ok = bool(rect and rect[2] == w and rect[3] == h)
            top_ok = (not topmost) or _is_topmost(hwnd)
            if size_ok and top_ok:
                stable += 1
                if stable >= 2:
                    return True
            else:
                stable = 0
                if not size_ok:
                    _apply_window_size(hwnd, w, h)
                if not top_ok:
                    _set_topmost(hwnd, True)
        time.sleep(0.15)
    return False


def launch():
    ok, app_mode, _proc = launch_overlay()
    if not ok:
        print("启动悬浮窗失败：没找到可用的浏览器。")
        return
    if app_mode:
        print("已启动悬浮窗（Edge app 模式，使用独立浏览器档案，不影响你自己的 Edge）。")
        if read_topmost_pref():
            print("已置顶；点悬浮窗左上角的图钉按钮可取消（取消后下次打开也不再置顶）。")
    else:
        print("未找到 Edge，已用默认浏览器打开（非置顶窗口）。")


if __name__ == "__main__":
    launch()
