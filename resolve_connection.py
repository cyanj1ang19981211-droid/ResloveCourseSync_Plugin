# -*- coding: utf-8 -*-
"""
达芬奇连接模块 —— 通过 DaVinci Resolve Scripting API 读取当前播放头时间码。

原理：
    - 达芬奇提供官方 Scripting API（需在 偏好设置 -> 系统 -> 常规 中启用
      "External scripting using" 为 Local/Network）。
    - 通过 import 达芬奇安装目录下的 scripting 模块连接，调用
      GetCurrentTimecode() 获取当前播放头时间码（Cut/Edit/Color/Fairlight/Deliver 均可用）。
    - 还可用 GetCurrentTimeline().GetName() 获取当前时间线名称，用于和课程名匹配。

连接方式说明：
    - app-level（"resolve" 全局）：适合「连接已打开的达芬奇」，本插件主要用这种方式。
    - 若达芬奇没开，连接会失败，需提示用户先启动达芬奇并开启外部脚本。
"""

import os
import sys


# DaVinciResolveScript.py（纯 Python 脚本，负责 import fusionscript）所在目录
_RESOLVE_MODULE_DIRS_WIN = [
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
]

# fusionscript.dll 常见位置（达芬奇安装目录下）
_FUSIONSCRIPT_DLL_DIRS_WIN = [
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Fusion",
    r"E:\Davinci",
    r"D:\Davinci",
    r"C:\Davinci",
]


def _find_module_dir():
    """定位 DaVinciResolveScript.py 所在目录（用于 sys.path）。"""
    # 环境变量
    for k in ("RESOLVE_SCRIPT_API",):
        v = os.environ.get(k)
        if v and os.path.exists(v):
            return v
    for d in _RESOLVE_MODULE_DIRS_WIN:
        if os.path.isfile(os.path.join(d, "DaVinciResolveScript.py")):
            return d
    return None


def _find_fusionscript_lib():
    """定位 fusionscript.dll 的完整路径（用于设置 RESOLVE_SCRIPT_LIB）。"""
    # 环境变量
    v = os.environ.get("RESOLVE_SCRIPT_LIB")
    if v and os.path.exists(v):
        return v
    # 常见安装目录
    for d in _FUSIONSCRIPT_DLL_DIRS_WIN:
        p = os.path.join(d, "fusionscript.dll")
        if os.path.isfile(p):
            return p
    # 扫描所有盘符下的 Davinci 目录
    for drive in "CDEFGH":
        for root in (f"{drive}:\\Davinci", f"{drive}:\\DaVinci Resolve",
                     f"{drive}:\\Program Files\\Blackmagic Design\\DaVinci Resolve"):
            p = os.path.join(root, "fusionscript.dll")
            if os.path.isfile(p):
                return p
    return None


class ResolveConnection:
    """封装与达芬奇的连接与时间码读取。"""

    def __init__(self, module_path: str = None, lib_path: str = None):
        self.resolve = None
        self.module_path = module_path or _find_module_dir()     # DaVinciResolveScript.py 目录
        self.lib_path = lib_path or _find_fusionscript_lib()      # fusionscript.dll 完整路径
        self._loaded = False
        self.last_error = ""

    def _ensure_module(self):
        if self._loaded:
            return
        if not self.module_path:
            raise RuntimeError(
                "未找到 DaVinciResolveScript.py（已探测以下目录均不存在：\n"
                + "\n".join(_RESOLVE_MODULE_DIRS_WIN) + "\n"
                "请把其所在 Modules 目录填入 config.json 的 resolve_script_path。"
            )
        if not self.lib_path:
            raise RuntimeError(
                "未找到 fusionscript.dll（已探测以下目录均不存在：\n"
                + "\n".join(_FUSIONSCRIPT_DLL_DIRS_WIN) + "\n"
                "请把 fusionscript.dll 的完整路径填入 config.json 的 resolve_script_lib。"
            )

        # ---- Windows 下加载 fusionscript 的关键初始化（顺序不能乱） ----
        py_home = os.path.dirname(sys.executable)   # 当前解释器目录（含 python3xx.dll）
        resolve_dir = os.path.dirname(self.lib_path)  # fusionscript.dll 所在目录（达芬奇安装目录）

        # 1) 让 fusionscript 绑定到当前 Python 运行时。
        #    不设的话它会去注册表 (HKCU/HKLM\...\PythonCore) 找最高版本 Python，
        #    加载到不兼容的 python3xx.dll 会直接 access violation 崩溃。
        os.environ["FUSION_PYTHON3_HOME"] = py_home

        # 2) 预加载当前解释器自己的 python3.dll，确保 fusionscript 绑定正确运行时
        import ctypes
        py3 = os.path.join(py_home, "python3.dll")
        if os.path.exists(py3):
            try:
                ctypes.WinDLL(py3)
            except OSError:
                pass

        # 3) 注册 DLL 搜索目录，让 fusionscript.dll 及其依赖的达芬奇 DLL 能被找到
        for d in (resolve_dir, py_home):
            if d and os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass

        # 4) Blackmagic 官方 DaVinciResolveScript.py 查找的环境变量
        os.environ["RESOLVE_SCRIPT_LIB"] = self.lib_path
        os.environ["RESOLVE_SCRIPT_API"] = os.path.dirname(self.module_path.rstrip("\\/"))

        # 5) Modules 目录加入 sys.path，才能 import DaVinciResolveScript
        sys.path.append(self.module_path)
        self._loaded = True

    def connect(self):
        """连接达芬奇，返回 True/False。失败时把原因写入 self.last_error。"""
        try:
            self._ensure_module()
            import DaVinciResolveScript as dvr_script
            self.resolve = dvr_script.scriptapp("Resolve")
        except Exception as e:
            self.resolve = None
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        if not self.resolve:
            self.last_error = (
                "scriptapp(\"Resolve\") 返回 None。达芬奇可能没启动，或 'External scripting' 没设为 Local，"
                "或本进程运行的 Python 版本与达芬奇 fusionscript 模块不兼容（需用达芬奇匹配的 Python 版本）。"
            )
            return False
        return True

    def is_connected(self):
        return self.resolve is not None

    # ---------- 内部：正确的对象层级 ProjectManager -> Project -> Timeline ----------

    def _get_timeline(self):
        """返回当前 Timeline 对象；没有则 None。

        达芬奇 API 对象层级：Resolve -> GetProjectManager() -> ProjectManager
        -> GetCurrentProject() -> Project -> GetCurrentTimeline() -> Timeline。
        """
        if not self.resolve:
            return None
        try:
            pm = self.resolve.GetProjectManager()
            proj = pm.GetCurrentProject()
            if not proj:
                return None
            return proj.GetCurrentTimeline()
        except Exception:
            return None

    def current_timeline_name(self) -> str:
        """返回当前时间线名称；失败返回空字符串。"""
        tl = self._get_timeline()
        if not tl:
            return ""
        try:
            return tl.GetName() or ""
        except Exception:
            return ""

    def current_timecode(self) -> str:
        """返回当前播放头时间码字符串（如 "00:01:23:14"）；失败返回 None。"""
        tl = self._get_timeline()
        if not tl:
            return None
        try:
            return tl.GetCurrentTimecode()
        except Exception:
            return None

    def current_framerate(self) -> float:
        """返回当前时间线帧率（fps），用于时间码->秒 的换算。失败返回 25.0。"""
        tl = self._get_timeline()
        if not tl:
            return 25.0
        try:
            setting = tl.GetSetting("timelineFrameRate")
            if setting:
                return float(setting)
        except Exception:
            pass
        return 25.0


def timecode_to_seconds(tc: str, fps: float = 25.0) -> float:
    """把 "HH:MM:SS:FF" 时间码转成秒。非丢帧时间码。"""
    if not tc:
        return 0.0
    parts = tc.split(":")
    if len(parts) == 4:
        h, m, s, f = parts
        return int(h) * 3600 + int(m) * 60 + int(s) + int(f) / fps
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0
