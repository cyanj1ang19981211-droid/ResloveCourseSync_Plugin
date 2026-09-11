# -*- coding: utf-8 -*-
"""
悬浮窗启动器（Windows）。

用 Edge 的 --app 模式打开 overlay.html，得到一个无地址栏的独立小窗。
窗口可通过 --window-size 控制尺寸，并用置顶工具（见下方说明）保持最前。

用法：
    python overlay.py            # 只开窗（后端需另行启动）
    python launcher.py           # 一键启动：后台起后端 + 开窗 + 关窗自动收尾

本模块只负责「把窗户开出来」，进程编排交给 launcher.py。
"""
import os
import subprocess
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.path.join(BASE_DIR, "frontend", "overlay.html")

# 悬浮窗标题（overlay.html 的 <title>）。launcher.py 靠它判断窗口是否被关掉，
# 改动时两处要一起改。
OVERLAY_TITLE = "课程强度同步"

# 找到 Edge 浏览器
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge():
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    # 兜底：从注册表/环境里再找一次（Edge 装在非默认位置的情况）
    for env_key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env_key)
        if not root:
            continue
        p = os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.exists(p):
            return p
    return None


def overlay_url():
    """overlay.html 用 file:// 打开（页面内部自己 fetch 127.0.0.1 的后端）。"""
    return "file:///" + OVERLAY.replace("\\", "/")


def launch_overlay(detached=True):
    """打开悬浮窗，返回 (是否成功, 是否为独立 app 窗口)。

    第二个返回值很关键：launcher 只在「确实开出了独立 app 窗口」时才去监控
    窗口关闭；退回默认浏览器（变成普通标签页）时不做窗口监控。
    """
    edge = find_edge()
    url = overlay_url()

    if edge:
        cmd = [
            edge,
            "--app=" + url,
            "--window-size=400,400",
            "--disable-features=msEdgeSidebarV2",
        ]
        flags = 0x00000008 if detached else 0  # DETACHED_PROCESS
        try:
            subprocess.Popen(cmd, creationflags=flags)
            return True, True
        except OSError:
            pass  # 启动失败，落到下面的默认浏览器

    # 没有 Edge 时退回默认浏览器（会是普通标签页，非独立窗口）
    try:
        webbrowser.open(url)
        return True, False
    except Exception:
        return False, False


def launch():
    ok, app_mode = launch_overlay()
    if not ok:
        print("启动悬浮窗失败：没找到可用的浏览器。")
        return
    if app_mode:
        print("已启动悬浮窗（Edge app 模式）。")
        print("若需始终置顶，可安装 PowerToys 的 Always On Top，或用第三方置顶工具。")
    else:
        print("未找到 Edge，已用默认浏览器打开（非置顶窗口）。")


if __name__ == "__main__":
    launch()
