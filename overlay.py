# -*- coding: utf-8 -*-
"""
悬浮窗启动器（Windows）。

用 Edge 的 --app 模式打开 overlay.html，得到一个无地址栏的独立小窗。
窗口可通过 --window-size 控制尺寸，并用置顶工具（见下方说明）保持最前。

用法：
    python overlay.py
"""
import os
import subprocess
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.path.join(BASE_DIR, "frontend", "overlay.html")
OVERLAY_URL = "http://127.0.0.1:8765/"  # 由 server.py 提供，但 overlay.html 本身可本地打开

# 找到 Edge 浏览器
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge():
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def launch():
    edge = find_edge()
    # 直接以 file:// 打开本地 overlay.html（它内部会 fetch 127.0.0.1:8765）
    url = "file:///" + OVERLAY.replace("\\", "/")

    if edge:
        cmd = [
            edge,
            "--app=" + url,
            "--window-size=400,400",
            "--disable-features=msEdgeSidebarV2",
        ]
        subprocess.Popen(cmd, creationflags=0x00000008)  # DETACHED_PROCESS
        print("已启动悬浮窗（Edge app 模式）。")
        print("若需始终置顶，可安装 PowerToys 的 Always On Top，或用第三方置顶工具。")
    else:
        # 没有 Edge 时退回默认浏览器
        webbrowser.open(url)
        print("未找到 Edge，已用默认浏览器打开（非置顶窗口）。")


if __name__ == "__main__":
    launch()
