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
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from version import VERSION


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
SERVER_JSON = os.path.join(RUNTIME_DIR, "server.json")

CREATE_NO_WINDOW = 0x08000000


class _QuietStream:
    """写不进去就当没写 —— stdout 断了不许把启动器打死。

    为什么需要：launcher 是**无窗口**进程，它的 stdout 可能是调用方给的管道。
    调用方中途退出（或控制台被关掉）时管道就断了，此后任何一次 print 都会抛
    BrokenPipeError：轻则启动器直接死掉、悬浮窗和后端全留在原地没人管，重则被
    最外层的兜底抓到、弹一个「插件启动失败（未预料的错误）」—— 用户看到的是
    「插件自己报了个莫名其妙的错」，而真正的原因只是「没人看日志了」。

    实测确实踩到过：一个自动化脚本读了几行输出就退出，launcher 下一行 print
    立刻炸掉。
    """

    def __init__(self, real):
        self._real = real

    def write(self, s):
        try:
            if self._real is None:
                return len(s)
            return self._real.write(s)
        except Exception:
            return len(s)

    def flush(self):
        try:
            if self._real is not None:
                self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_quiet_stdio():
    global _STDIO_GUARDED
    if _STDIO_GUARDED:
        return
    _STDIO_GUARDED = True
    sys.stdout = _QuietStream(sys.stdout)
    sys.stderr = _QuietStream(sys.stderr)


_STDIO_GUARDED = False
_install_quiet_stdio()


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
        # 必须和 server.py 用同一套容错读法：用户把 config.json 存成带 BOM 的
        # UTF-8 时老写法读不出来，launcher 会在 8765 上干等，而后端其实听在
        # 用户指定的端口上 —— 表现就是「后端启动失败」。
        import config_io
        cfg, _p, _n = config_io.load_config_file(os.path.join(BASE_DIR, "config.json"))
        port = int(cfg.get("port") or port)
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


def _explain_log(text):
    """把 server.log 的尾巴翻译成人话（用户拿到的应该是"怎么办"，不是 traceback）。"""
    low = (text or "").lower()
    tips = []
    if "jsondecodeerror" in low or "invalid \\escape" in low or "expecting value" in low:
        tips.append("· config.json 内容有语法错误（旧版本遇到这种情况后端会直接崩）。"
                    "新版会自动修好；实在不想管，把 config.json 删掉即可 —— "
                    "插件会全自动找达芬奇。")
    if "address already in use" in low or "10048" in low or "无法监听端口" in (text or ""):
        tips.append("· 端口被占用。新版会自动改用别的端口，不会再因此启动失败；"
                    "如果你看到这条，说明用的还是旧版。")
    if "modulenotfound" in low or "importerror" in low:
        tips.append("· Python 环境不完整：确认用的是插件自带的 Python（用 start.bat 启动），"
                    "并且整个文件夹都拷全了。")
    if "permissionerror" in low or "winerror 5" in low or "拒绝访问" in (text or ""):
        tips.append("· 权限不足：插件文件夹可能被安全软件挡住，或解压到了只读位置。")
    return tips


def start_server(port):
    """后台无窗口启动 server.py，日志写到 .runtime/server.log。"""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    log = open(SERVER_LOG, "w", encoding="utf-8")
    log.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 server.py"
              f"（课程强度同步 v{VERSION}）=====\n")
    log.flush()

    py = sys.executable or "python"

    # 上面这个日志文件是按 UTF-8 写的，所以尽量让子进程也用 UTF-8 输出：
    # Python 发现 stdout 不是控制台就退回「系统代码页」编码（简体中文机器上是
    # GBK），日志里混进 GBK 字节、而 _tail() 按 UTF-8 读 → 启动失败弹窗里的中文
    # 全是乱码。这里设环境变量只是第一道；**真正兜底的是 server.py 模块级的
    # _force_utf8_stdio()**，因为 multiprocessing 起出来的 worker 子进程并不听
    # PYTHONIOENCODING（实测环境变量继承到了，sys.stdout.encoding 依旧是 gbk）。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 告诉后端「这次听哪个端口」（它可能因为被占用再自己换，换完写进 server.json）
    env["RESOLVE_SYNC_PORT"] = str(int(port))
    # 告诉后端「我是谁」：后端会盯着这个 PID，我要是被强杀了它自己跟着退出。
    # 这是「关了悬浮窗后台还有僵尸进程占着端口」的最后一道保险。
    env["RESOLVE_SYNC_PARENT_PID"] = str(os.getpid())

    proc = subprocess.Popen(
        [py, SERVER_PY],
        cwd=BASE_DIR,                 # 保证相对路径（data/ 等）能解析
        env=env,
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
    url = f"http://127.0.0.1:{int(port)}/ping"
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
    """结束后端 —— 连同它的子进程一起。

    为什么不能只 terminate 父进程：server.py 的达芬奇 worker 是 multiprocessing
    起的子进程，父进程被结束时它**不一定会跟着走**（老版本就是这样在后台留下
    一个看不见的进程），而且它握着与达芬奇的连接。所以这里最终还是按进程树杀。

    只对 python* 进程生效（overlay.kill_tree 会先核对 exe 名），不会误杀别的程序。
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    # 还没走 → 杀整棵进程树（后端 + worker）
    overlay.kill_tree(proc.pid, expect_prefix="python")
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_quiet(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return ""


# ---------- 端口：只探测，不报错 ----------
#
# 「端口被占用」以前是用户能看到的错误（弹「后端启动失败」）。其实对用户来说
# 它完全不该存在：换个端口就行了。所以这里的原则是 —— **永远能启动**，
# 端口冲突只写进日志。

def _port_listening(port, timeout=0.35):
    """端口上有没有**活的监听者**（TIME_WAIT 不算）。

    为什么区分：上一次运行留下的 TIME_WAIT 连接在 Windows 上会让 bind 失败
    （不设 SO_REUSEADDR 时），但那并不是「被占用」—— 刚关掉插件马上重开时
    属于这种情况，换了端口反而多此一举。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", int(port)))
            return True
    except OSError:
        return False


def _diag(port, timeout=0.8):
    """问端口上的服务要一份自述信息（不是本插件 / 没响应就返回空 dict）。"""
    try:
        url = "http://127.0.0.1:%d/diag" % int(port)
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _free_port():
    """让系统给一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pick_port(preferred):
    """决定后端听哪个端口，返回 (端口, 说明)。"""
    preferred = int(preferred or 8765)
    if not _port_listening(preferred):
        return preferred, ""
    d = _diag(preferred)
    if d and os.path.normcase(os.path.abspath(str(d.get("server_code_dir") or ""))) == os.path.normcase(BASE_DIR):
        why = f"端口 {preferred} 上是个还没退干净的本插件后端"
    elif d:
        why = f"端口 {preferred} 被别的程序占用了"
    else:
        why = f"端口 {preferred} 被别的程序占用了（问不出是什么）"
    port = _free_port()
    return port, f"{why}，本次自动改用端口 {port}（不影响使用）。"


def _read_server_json():
    """读 .runtime/server.json —— 后端启动后写的「我是谁、我听哪个端口」。"""
    try:
        with open(SERVER_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _same_dir(p):
    """这个路径是不是本项目目录（用来确认「是自己人」）。"""
    try:
        return os.path.normcase(os.path.abspath(str(p))) == os.path.normcase(BASE_DIR)
    except Exception:
        return False


def _kill_python_tree(pid):
    """杀一个 Python 进程树（后端 + 它 multiprocessing 起来的 worker）。"""
    # 先礼后兵：能优雅结束就优雅结束（让它把 server.log 的收尾写完）
    _run_quiet(["taskkill", "/PID", str(pid)])
    time.sleep(0.4)
    return overlay.kill_tree(pid, expect_prefix="python")


def kill_stale_server(port):
    """清掉上一次没退干净的**本插件**后端。只杀自己人。

    怎么确认是自己人（任一成立即可）：
        A. 端口上的服务自己报的代码目录（/diag）就是这个文件夹；
        B. .runtime/server.json 里记着 pid，且自报目录也是这个文件夹。
    两个都问不出来就不动手 —— 新版后端会自动换端口，绝不会因为不清场而打不开。

    老做法是「凡是占着这个端口的 python 进程就杀」。那会误杀用户自己的 Python
    程序，而且现在也不需要了。
    """
    killed = []
    if _port_listening(port):
        d = _diag(port)
        pid = d.get("server_pid")
        if pid and _same_dir(d.get("server_code_dir")):
            print(f"[启动] 端口 {port} 上是本插件上一个后端（PID {pid}），先清掉。",
                  flush=True)
            if _kill_python_tree(pid):
                killed.append(pid)
    st = _read_server_json()
    pid = st.get("pid") if _same_dir(st.get("code_dir")) else None
    if pid and int(pid) not in killed and overlay.pid_alive(pid):
        print(f"[启动] 清掉 server.json 里记着的旧后端（PID {pid}）。", flush=True)
        if _kill_python_tree(pid):
            killed.append(pid)
    if killed:
        time.sleep(0.5)
    return killed


# ---------- 主流程 ----------

def _preflight_message():
    """启动前的环境自检。返回要弹给用户的文字；一切正常返回 None。

    目的：把「双击了但什么都没发生」变成「弹个框直接告诉你缺什么」。
    自检本身出问题（文件缺失、导入失败等）一律放行 —— 不能因为体检程序
    自己坏了就把插件拦住。
    """
    try:
        import env_check
        # include_link=False：跳过「达芬奇连接实测」。那一项要起子进程真连一次，
        # 慢的时候要等好几秒，而启动前自检只关心「缺了什么致命的东西」。
        # 真连不上的话插件本来也会自己等重连，用户想细查可以双击「检查环境.bat」。
        fatal = env_check.fatal_problems(env_check.run_checks(include_link=False))
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
    print(f"[启动] 课程强度同步 v{VERSION} | 代码目录：{BASE_DIR}", flush=True)

    # 启动前先体检：有问题就弹框说清楚，而不是悄无声息地什么都不发生
    pre = _preflight_message()
    if pre:
        _message_box(pre, title="课程强度同步 - 无法启动")
        return 1

    # 先把上一次留下的悬浮窗关掉（优先按 .runtime/overlay.json 里记的 pid，
    # 标题 + 独立档案双重确认后才动手），免得两个悬浮窗叠在一起、
    # 旧的那个还显示着上一次的数据。
    overlay.kill_stale_overlay()

    preferred = _read_port()

    # **顺序很重要**：先清掉占着首选端口的「自己人」，再决定端口。
    # 反过来的话（先挑端口再清理）会出现：首选端口上有个没退干净的本插件后端 →
    # 挑端口时看到端口被占 → 于是另选一个端口，而旧后端还赖在原地 ——
    # 用户看到的是「开了两个插件」，第二次打开的窗口没把上一次替掉。
    kill_stale_server(preferred)

    port, why = pick_port(preferred)
    if why:
        print(f"[端口] {why}", flush=True)
    if port != preferred:
        # 换了端口的话，顺手再确认新端口上没有自己人的残留
        kill_stale_server(port)
    print(f"[启动] 后端端口 {port}，日志：{SERVER_LOG}", flush=True)

    proc, log = start_server(port)

    if not wait_server_ready(proc, port):
        # 后端可能因为端口在最后关头被占用而自己换了端口（它会写 server.json），
        # 所以这里再按它自报的端口试一次。**不再直接判定失败**。
        actual = _read_server_json().get("port")
        try:
            actual = int(actual)
        except (TypeError, ValueError):
            actual = 0
        if actual and actual != port and wait_server_ready(proc, actual, timeout=6.0):
            port = actual
            print(f"[启动] 后端实际监听在端口 {port}", flush=True)
        else:
            reason = _tail(SERVER_LOG)
            tips = _explain_log(reason)
            stop_server(proc)
            try:
                log.close()
            except Exception:
                pass
            body = [
                f"后端启动失败（课程强度同步 v{VERSION}）。",
                "",
                "可能的原因与处理：",
            ]
            body += tips if tips else [
                "· 常见原因是 Python 环境跑不起来（缺少组件），或文件夹没拷贝完整。",
            ]
            body += [
                "",
                "如果这个版本号是 0.7 之前的旧版，请重新从 GitHub 下载 ZIP"
                "（这些毛病在 0.7 都已经修掉）。",
                "",
                f"日志（{SERVER_LOG}）：",
                reason or "（日志是空的，后端可能根本没启动起来）",
            ]
            _message_box("\n".join(body), title="课程强度同步 - 启动失败")
            return 1

    print("[启动] 后端已就绪，正在打开悬浮窗…", flush=True)
    ok, app_mode, edge_proc = overlay.launch_overlay()
    if not ok:
        stop_server(proc)
        _message_box(
            "后端已启动，但打开悬浮窗失败：没找到可用的浏览器。\n\n"
            "请确认系统里装了 Microsoft Edge。",
            title="课程强度同步 - 悬浮窗打不开",
        )
        return 1

    def _cleanup():
        """收尾：关掉悬浮窗 + 结束后端。任何退出路径都走这里。"""
        overlay.stop_overlay(edge_proc)
        stop_server(proc)
        try:
            log.close()
        except Exception:
            pass

    if not monitor_ui or not app_mode:
        # 没有独立 app 窗口（退回默认浏览器）就不做窗口监控，
        # 交给后端自己的「前端失联/收到关闭信号」兜底逻辑收尾。
        print("[运行] 等待后端退出（关掉网页后自动结束）…", flush=True)
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        _cleanup()
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

    _cleanup()
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
    except BrokenPipeError:
        # stdout 被调用方关掉了（脚本读了就走 / 控制台被关）——这不是插件的错，
        # 静默退出就行，别弹框吓用户。见 _QuietStream。
        sys.exit(1)
    except BaseException:
        import traceback
        _raw_message_box(
            f"插件启动失败（未预料的错误）。\n\n当前版本：v{VERSION}\n\n"
            "把下面这段内容发给开发者可以定位问题：\n\n"
            + traceback.format_exc()[-1500:],
            title="课程强度同步 - 启动失败",
        )
        sys.exit(1)
