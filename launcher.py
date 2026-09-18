# -*- coding: utf-8 -*-
"""
launcher.py —— 一键启动（start.bat 调用的就是它）。

对普通用户来说只有两步：双击 start.bat → 出现悬浮窗；关掉悬浮窗 → 全部退出。

背后做四件事：
    1. 以「后台无窗口」方式启动 server.py（不再弹命令行黑框）；
    2. 轮询后端 /ping，等它真正就绪再开窗（避免悬浮窗先开、一直显示连不上）；
    3. 用 Edge 的 --app 模式打开悬浮窗（默认置顶，前端左上角图钉按钮可随时开关）；
    4. 盯着悬浮窗：一旦窗口消失，就结束后端进程，然后自己退出。

后端自己也有一套「前端失联就退出」的兜底（见 server.py 的 Liveness），
两边谁先发现都行，不会出现「关了窗后端还在后台赖着」的僵尸进程。
"""

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _raw_message_box(text, title="课程强度同步", warn=True):
    """最底层、零依赖的弹窗（只用 ctypes）。

    为什么单独留一个：下面的 import 万一失败（文件没拷全、Python 版本不对），
    也得有办法让用户看到原因。否则「双击了但什么都不发生」——这正是用户
    反馈过的问题。
    """
    try:
        MB_ICONWARNING = 0x30
        MB_ICONINFORMATION = 0x40
        ctypes.windll.user32.MessageBoxW(
            None, str(text), str(title),
            (MB_ICONWARNING if warn else MB_ICONINFORMATION) | 0x40000,  # MB_TOPMOST
        )
    except Exception:
        pass


try:
    import overlay  # noqa: E402
except Exception as _e:                       # pragma: no cover - 兜底路径
    _raw_message_box(
        "启动失败：读不到 overlay.py。\n\n"
        "多半是文件夹没有完整复制 —— 请把整个项目文件夹一起拷过来，"
        "不要只拷一部分文件。\n\n"
        "具体错误：" + repr(_e),
        title="课程强度同步 - 启动失败",
    )
    raise SystemExit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(BASE_DIR, ".runtime")
SERVER_LOG = os.path.join(RUNTIME_DIR, "server.log")
SERVER_PY = os.path.join(BASE_DIR, "server.py")

CREATE_NO_WINDOW = 0x08000000


# ---------- 小工具 ----------

def _message_box(text, title="课程强度同步", warn=True):
    """GUI 提示框（launcher 没有控制台，出错只能靠弹窗告诉用户）。"""
    try:
        MB_ICONWARNING = 0x30
        MB_ICONINFORMATION = 0x40
        ctypes.windll.user32.MessageBoxW(
            None, str(text), str(title),
            (MB_ICONWARNING if warn else MB_ICONINFORMATION) | 0x40000,  # MB_TOPMOST
        )
    except Exception:
        pass


def _hide_own_console():
    """把自己带出来的控制台窗口藏掉。

    用 pythonw 启动时本来就没有控制台；但如果没有 pythonw（或用 python 直接跑
    launcher），start.bat 的控制台会一直挂着——这里主动隐藏，保证「后台运行」。
    """
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _read_port():
    """从 config.json 读端口（读不到就用 8765，与 server.py 的默认值一致）。

    也认环境变量 RESOLVE_SYNC_PORT，方便跑第二个实例或自动化测试。
    """
    env_port = os.environ.get("RESOLVE_SYNC_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    port = 8765
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            port = int(json.load(f).get("port") or port)
    except Exception:
        pass
    return port


# ---------- 窗口枚举（判断悬浮窗还在不在） ----------

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_kernel32.GetConsoleWindow.restype = wintypes.HWND


def _own_console_hwnd():
    try:
        return _kernel32.GetConsoleWindow()
    except Exception:
        return None


def overlay_window_exists(title=overlay.OVERLAY_TITLE):
    """当前是否存在标题包含 title 的可见窗口（排除本进程自己的控制台）。"""
    own = _own_console_hwnd()
    found = []

    def _cb(hwnd, _lparam):
        if hwnd == own:
            return True
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        if title in buf.value:
            found.append(buf.value)
            return False
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(_cb), 0)
    except Exception:
        return True   # 枚举失败时保守认为窗口还在，别误杀后端
    return bool(found)


# ---------- 后端进程 ----------

def _tail(path, lines=12):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except Exception:
        return ""


def start_server(port):
    """后台无窗口启动 server.py，日志写到 .runtime/server.log。"""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    log = open(SERVER_LOG, "w", encoding="utf-8")
    log.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 server.py =====\n")
    log.flush()

    py = sys.executable or "python"
    proc = subprocess.Popen(
        [py, SERVER_PY],
        cwd=BASE_DIR,                 # 保证相对路径（data/ 等）能解析
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc, log


def wait_server_ready(proc, port, timeout=25.0):
    """轮询 /ping 等后端就绪。返回 True/False。

    用 /ping 而不是 /state：/ping 不算「前端心跳」，不会干扰后端的自动退出判断。
    """
    url = f"http://127.0.0.1:{port}/ping"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False          # 后端已经挂了，不用再等
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def fetch_pin(port, timeout=0.8):
    """问后端「现在该不该置顶」（前端图钉按钮的状态）。

    返回 True / False；问不到（后端没了）返回 None —— 调用方沿用上一次的值，
    不要因为一次网络抖动就把窗口的置顶状态改掉。
    """
    url = f"http://127.0.0.1:{port}/pin"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("on"))
    except Exception:
        return None


def stop_server(proc):
    """结束后端进程（先礼后兵）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_quiet(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=15, creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return ""


def _find_listener_pid(port):
    """找出正在监听指定端口的进程 PID（没有则 None）。"""
    out = _run_quiet(["netstat", "-aon"])
    for line in out.splitlines():
        if f":{port} " not in line + " ":
            continue
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            return parts[-1]
    return None


def _is_python_process(pid):
    """确认这个 PID 是 Python 进程 —— 避免误杀别人占着同一端口的程序。"""
    out = _run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).lower()
    return "python" in out


def kill_stale_server(port):
    """清掉上次没退干净的旧后端（否则端口被占，新后端起不来）。

    只杀「监听该端口 + 进程名是 python」的进程；不是 Python 就不动，交给
    start_server 去报错，避免误伤用户的其它程序。
    """
    pid = _find_listener_pid(port)
    if not pid or not _is_python_process(pid):
        return None
    _run_quiet(["taskkill", "/F", "/PID", pid])
    print(f"[启动] 已清理占用 {port} 端口的旧后端进程 PID={pid}", flush=True)
    time.sleep(0.4)
    return pid


# ---------- 主流程 ----------

def _preflight_message():
    """启动前的环境自检。返回要弹给用户的文字；一切正常返回 None。

    目的：把「双击了但什么都没发生」变成「弹个框直接告诉你缺什么」。
    自检本身出问题（文件缺失、导入失败等）一律放行 —— 不能因为体检程序
    自己坏了就把插件拦住。
    """
    try:
        import env_check
        fatal = env_check.fatal_problems(env_check.run_checks())
        if not fatal:
            return None
        lines = ["启动前的环境检查没有通过，插件现在跑不起来：", ""]
        for r in fatal:
            lines.append("【" + str(r.get("title") or "") + "】")
            for l in (r.get("lines") or []):
                lines.append("  " + str(l).strip())
            advice = r.get("advice")
            if advice:
                lines.append("  怎么办：")
                for l in str(advice).split("\n"):
                    lines.append("    " + l.strip())
            lines.append("")
        lines.append("双击文件夹里的「检查环境.bat」可以看到完整报告。")
        return "\n".join(lines)
    except Exception:
        return None


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    monitor_ui = "--no-monitor" not in argv

    _hide_own_console()

    # 启动前先体检：有问题就弹框说清楚，而不是悄无声息地什么都不发生
    pre = _preflight_message()
    if pre:
        _message_box(pre, title="课程强度同步 - 无法启动")
        return 1

    port = _read_port()
    print(f"[启动] 后端端口 {port}，日志：{SERVER_LOG}", flush=True)

    # 先清掉上次残留的旧后端，避免端口被占
    kill_stale_server(port)

    proc, log = start_server(port)

    if not wait_server_ready(proc, port):
        reason = _tail(SERVER_LOG)
        stop_server(proc)
        try:
            log.close()
        except Exception:
            pass
        _message_box(
            "后端启动失败。\n\n"
            "常见原因：端口被占用，或 Python 环境跑不起来。\n\n"
            f"日志（{SERVER_LOG}）：\n{reason}",
            title="课程强度同步 - 启动失败",
        )
        return 1

    print("[启动] 后端已就绪，正在打开悬浮窗…", flush=True)
    ok, app_mode = overlay.launch_overlay()
    if not ok:
        stop_server(proc)
        _message_box(
            "后端已启动，但打开悬浮窗失败：没找到可用的浏览器。\n\n"
            "请确认系统里装了 Microsoft Edge。",
            title="课程强度同步 - 悬浮窗打不开",
        )
        return 1

    if not monitor_ui or not app_mode:
        # 没有独立 app 窗口（退回默认浏览器）就不做窗口监控，
        # 交给后端自己的「前端失联/收到关闭信号」兜底逻辑收尾。
        print("[运行] 等待后端退出（关掉网页后自动结束）…", flush=True)
        try:
            proc.wait()
        except KeyboardInterrupt:
            stop_server(proc)
        print("[退出] 后端已结束。", flush=True)
        return 0

    # 盯着悬浮窗：先等它出现，出现后再等它消失
    print("[运行] 悬浮窗已打开，关闭它即可退出插件。", flush=True)
    state = "wait_appear"
    appear_deadline = time.time() + 40
    misses = 0
    pin = overlay.read_topmost_pref()   # 期望的置顶状态，之后每秒跟后端对齐
    last_keep = 0.0

    while True:
        if proc.poll() is not None:
            print("[退出] 后端已自行结束。", flush=True)
            break

        if state == "wait_appear":
            if overlay_window_exists():
                state = "monitor"
                print("[运行] 已捕获悬浮窗，开始监控。", flush=True)
            elif time.time() > appear_deadline:
                # 一直没等到窗口（可能被系统拦截/开了普通标签页）→
                # 不再做窗口监控，改为只等后端退出
                print("[运行] 未捕获到独立窗口，改为等待后端退出。", flush=True)
                state = "passive"
        elif state == "monitor":
            if overlay_window_exists():
                misses = 0
                # 每秒跟后端的图钉状态对齐一次：用户点了图钉（开/关置顶）后，
                # 这里负责把窗口真正调成那个状态 —— 即使置顶被别的程序顶掉，
                # 只要图钉还亮着，1 秒内就会自动恢复。
                # 注意：只动置顶，**不碰尺寸/位置**，用户手动拉大窗口或最小化
                # 都不会被我们拽回去。
                if time.time() - last_keep >= 1.0:
                    last_keep = time.time()
                    now = fetch_pin(port)
                    if now is not None:
                        pin = now
                    overlay.apply_topmost(pin)
            else:
                misses += 1
                if misses >= 3:      # 连续 3 秒找不到，认定用户关掉了
                    print("[退出] 悬浮窗已关闭。", flush=True)
                    break
        # state == "passive"：什么都不做，只等 proc 自己结束

        time.sleep(1.0)

    stop_server(proc)
    try:
        log.close()
    except Exception:
        pass
    print("[退出] 已全部结束。", flush=True)
    return 0


if __name__ == "__main__":
    # 最后一道防线：任何没预料到的异常都弹窗说清楚。
    # 没有这个的话，pythonw 下异常只会写进一个不存在的控制台 ——
    # 用户看到的现象就是「双击了，什么反应都没有」。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _raw_message_box(
            "插件启动失败（未预料的错误）。\n\n"
            "把下面这段内容发给开发者可以定位问题：\n\n"
            + traceback.format_exc()[-1500:],
            title="课程强度同步 - 启动失败",
        )
        sys.exit(1)
