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


路径发现（本文件最容易出问题的部分，单独说明）
------------------------------------------------------------
达芬奇要靠两个文件才能被外部脚本连上，缺一不可：

    1. DaVinciResolveScript.py —— 官方封装，官方装在
       <ProgramData>\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules
    2. fusionscript.dll —— 原生库，躺在**达芬奇自己的安装目录**里。
       这个目录毫无规律：有人是 C:\\Program Files\\Blackmagic Design\\DaVinci Resolve，
       有人是 D:\\软件\\达芬奇，有人是 E:\\Davinci。

而官方那份 DaVinciResolveScript.py 只认一个**写死**的路径：
       C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll
所以「换个盘装达芬奇就连不上」这件事是必然的。

本模块的做法：**按代价从低到高列出所有可能的来源，逐个验证；全都失败才做一次
限定深度 + 限定时间的全盘搜索，找到的结果写进 .runtime 缓存 —— 一台机器只需要
付一次代价。** 优先级如下（前一条命中就不再看后面）：

    fusionscript.dll
      1. 环境变量 RESOLVE_SCRIPT_LIB
      2. config.json 的 resolve_script_lib / resolve_install_dir
      3. .runtime/resolve_paths.json 里上次成功找到的
      4. 正在运行的 Resolve.exe 所在目录（最权威：进程在哪，安装目录就在哪）
      5. Windows 安装器（MSI）登记的安装目录 —— 达芬奇是 MSI 装的，Windows 把
         每个安装目录都记在 Installer\\Folders 里，装在哪个盘、叫什么名字都查得到
      6. 开始菜单 / 桌面 / 任务栏快捷方式里解析出来的路径
      7. 「盘符 × 常见软件目录 × 达芬奇目录名」组合
      8. 限定深度全盘搜索（兜底，先浅后深）
      9. 导入成功后从 dvr.__file__ 反查真实 dll 路径，顺手记进缓存

    磁盘上有多份达芬奇（升级后旧目录没删干净）时，用注册表记录的版本号给候选
    打分，优先挑「正在用的那一份」—— 挑错版本会直接连不上/连上就崩。

    DaVinciResolveScript.py
      1. 环境变量 RESOLVE_SCRIPT_API
      2. config.json 的 resolve_script_path
      3. 缓存
      4. <ProgramData>（含各盘符）…\\Support\\Developer\\Scripting\\Modules
      5. 达芬奇安装目录下的 Support\\Developer\\Scripting\\Modules
      6. 限定深度全盘搜索

找不到时不再只丢一句「未找到」，而是把「我找过哪些地方」一并报出来
（search_report()），对方截图发过来就能一眼看出问题。
"""

import json
import os
import re
import sys
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_DIR = os.path.join(BASE_DIR, ".runtime")
_CACHE_FILE = os.path.join(_RUNTIME_DIR, "resolve_paths.json")
_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DLL_NAME = "fusionscript.dll"
MODULE_NAME = "DaVinciResolveScript.py"
EXE_NAME = "Resolve.exe"
# fusionscript.dll 也可能在 Fusion 子目录里（Fusion Studio / 旧版布局）
_DLL_SUBDIRS = ("", "Fusion")

# ---------------------------------------------------------------- 路径发现

# 「装软件的父目录」——配合盘符枚举，覆盖装在非默认盘/自定义目录的情况。
# 前导空串表示「盘符根目录本身」（例：E:\Davinci）。
_PARENT_DIR_NAMES = (
    "",
    "Program Files", "Program Files (x86)", "ProgramData",
    "Blackmagic Design",
    "Program Files\\Blackmagic Design",
    "Program Files (x86)\\Blackmagic Design",
    "ProgramData\\Blackmagic Design",
    "Software", "software", "Programs", "Apps", "App", "Applications",
    "Tools", "Green", "Portable", "DaVinci", "达芬奇", "软件",
)

# 达芬奇安装目录可能叫什么（大小写无关）。列这些只是为了「先试最可能的」；
# 真正保证兼容的是后面的限定深度搜索，不依赖这张表。
_LEAF_DIR_NAMES = (
    "", "DaVinci Resolve", "DaVinciResolve", "Davinci", "DaVinci", "Resolve",
)

# 全盘搜索（兜底）的预算：总耗时上限与最大深度
_SCAN_BUDGET = 6.0
_SCAN_MAX_DEPTH = 3

# 搜索时直接跳过的目录名（系统目录 / 缓存 / 包管理器，进去也是白跑，还很慢）
_SKIP_DIRS = frozenset((
    "windows", "$recycle.bin", "system volume information", "winsxs",
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".cache",
    "temp", "tmp", "installer", "servicing", "assembly", "windowsapps",
    "driverstore", "softwaredistribution", "recovery", "perflogs", "msocache",
    "config.msi", "$windows.~bt", "$windows.~ws", "appdata", "onedrivetemp",
    "packages", "microsoft", "common files", "windows defender", "intel",
    "nvidia", "amd", "$sysreset",
))

# 进程内缓存：{"key": (写入时间, 结果)}。找不到的结论只留 _NEG_TTL 秒，
# 免得用户刚装好达芬奇、插件却一直说「找不到」。
_MEMO = {}
_NEG_TTL = 60.0

# config.json 的读取结果说明（由 _load_config_uncached 填充）
_CONFIG_PROBLEM = ""
_CONFIG_NOTES = []

# 本进程刚启动时环境里有没有 RESOLVE_SCRIPT_LIB（用过一次就会被我们自己赋值，
# 所以必须在模块导入的那一刻抓下来，否则诊断信息会「自己说自己设过」）。
_ENV_LIB_AT_IMPORT = os.environ.get("RESOLVE_SCRIPT_LIB") or ""

_DRIVES_MEMO = None
_FIXED_DRIVES_MEMO = None


def _memo(key, producer, ttl_neg=_NEG_TTL):
    """进程内缓存一层。空结果（None/[]/""）只缓存一小会儿。"""
    now = time.time()
    if key in _MEMO:
        ts, val = _MEMO[key]
        if val:
            return val
        if now - ts < ttl_neg:
            return val
    val = producer()
    _MEMO[key] = (now, val)
    return val


def forget_discovery():
    """丢掉「路径发现」的进程内记忆，下次重新找一遍。

    连接失败时调用：达芬奇常常是在插件**之后**才启动的，那一刻「正在运行的
    进程」这条线索才会出现，前面记下来的「找不到」就该作废。
    只清便宜的部分；全盘搜索的结果（很贵）另有缓存，不会因此重扫。
    """
    for k in ("lib", "module_dir", "install_candidates"):
        _MEMO.pop(k, None)


def _read_config():
    """读 config.json（带容错 + 记忆化）。

    走 config_io 而不是自己 json.load：用户手写的 config.json 经常是
    带 BOM 的 UTF-8 / ANSI 编码 / 带尾逗号 / 路径只写了单反斜杠，
    老写法一律静默返回 {}，用户就会以为「我明明指定了达芬奇目录」而实际
    整份配置都没生效。容错之后这些都能读出来，读不出来也有一句人话说明。
    """
    return _memo("config", _load_config_uncached)


def _load_config_uncached():
    global _CONFIG_PROBLEM, _CONFIG_NOTES
    try:
        import config_io
    except Exception:
        # 极端情况（有人只拷了半个项目）：退回老行为，绝不因此抛异常
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    data, problem, notes = config_io.load_config_file(_CONFIG_FILE)
    _CONFIG_PROBLEM, _CONFIG_NOTES = problem, notes
    return data


def config_problem():
    """config.json 有没有「导致配置没生效」的问题（空串 = 没问题）。"""
    _read_config()          # 确保已经读过一次
    return _CONFIG_PROBLEM


def config_notes():
    """config.json 已经自动处理掉的小提醒（BOM / ANSI / 自动修复语法）。"""
    _read_config()
    return list(_CONFIG_NOTES)


def _load_cache():
    """读「上次成功找到的路径」缓存。"""
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(**kw):
    """把找到的路径记下来，下次就不用再找（含全盘搜索的结果，最值钱）。"""
    d = _load_cache()
    for k, v in kw.items():
        if v:
            d[k] = v
    if not d:
        return
    d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _list_drives():
    """枚举系统中真实存在的盘符（Windows）。失败时回退到 A~Z 全量。"""
    global _DRIVES_MEMO
    if _DRIVES_MEMO is not None:
        return _DRIVES_MEMO
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        bitmask = 0
    drives = []
    if bitmask:
        for i in range(26):
            if bitmask & (1 << i):
                drives.append(chr(ord("A") + i))
    if not drives:
        drives = [chr(ord("A") + i) for i in range(26)]
    _DRIVES_MEMO = drives
    return drives


_DRIVE_TYPE_FIXED = 3


def _fixed_drives():
    """只保留**本地固定磁盘**。

    为什么要过滤：对网络盘（Z:）、空光驱、拔掉的读卡器做 isdir/isfile 可能
    卡住好几秒，扫描一遍能把体检拖到没法用。而达芬奇装在网络盘/光驱上的情况
    基本不存在；真装在移动盘上的话，「正在运行的进程」「快捷方式」这两条
    线索照样能把它揪出来。
    """
    global _FIXED_DRIVES_MEMO
    if _FIXED_DRIVES_MEMO is not None:
        return _FIXED_DRIVES_MEMO
    drives = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        for dr in _list_drives():
            try:
                if k32.GetDriveTypeW(dr + ":\\") == _DRIVE_TYPE_FIXED:
                    drives.append(dr)
            except Exception:
                continue
    except Exception:
        pass
    if not drives:                      # 调不到 Win32 时的兜底
        drives = _list_drives()
    _FIXED_DRIVES_MEMO = drives
    return drives


def _system_dirs():
    """常见「装软件」的目录：优先读环境变量，读不到才退回系统盘默认路径。

    不要写死 C:\\ ：用户的 Program Files 或 ProgramData 完全可能在别的盘。
    """
    out = []

    def add(p):
        if not p:
            return
        try:
            p = os.path.expandvars(p)
        except Exception:
            return
        if p and os.path.isdir(p):
            p = os.path.normpath(p)
            if p not in out:
                out.append(p)

    for key in ("PROGRAMFILES", "ProgramW6432", "PROGRAMFILES(X86)",
                "ProgramFiles(x86)", "PROGRAMDATA", "ALLUSERSPROFILE"):
        add(os.environ.get(key))
    # 环境变量被清空时（服务/沙箱里启动）的兜底
    for d in (r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData"):
        add(d)
    return out


def _process_exe_paths(exe_names=(EXE_NAME,)):
    """枚举正在运行的进程，取指定进程名的可执行文件完整路径。

    纯 ctypes（Toolhelp 快照 + QueryFullProcessImageNameW），不依赖第三方库、
    不弹窗、不开子进程。失败一律返回 []，不影响其它发现渠道。

    这是最权威的一条线索：达芬奇进程在哪，它的安装目录就在哪。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    try:
        k32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(PROCESSENTRY32W)]

        want = {n.lower() for n in exe_names}
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        found = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if (entry.szExeFile or "").lower() in want:
                    h = k32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False,
                        entry.th32ProcessID)
                    if h:
                        try:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = wintypes.DWORD(1024)
                            if k32.QueryFullProcessImageNameW(
                                    h, 0, buf, ctypes.byref(size)):
                                p = buf.value
                                if p and p not in found:
                                    found.append(p)
                        finally:
                            k32.CloseHandle(h)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return found
    except Exception:
        return []


# .lnk 里「像 Resolve.exe 完整路径」的字符串
_LOOKS_LIKE_EXE = re.compile(
    r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\x00]{1,80}\\){0,8}[^\\/:*?\"<>|\x00]{0,80}"
    + re.escape(EXE_NAME), re.I)


def _shortcut_locations():
    """返回「可能放达芬奇快捷方式」的目录（开始菜单 / 桌面 / 任务栏固定）。"""
    out = []

    def add(p):
        if p and os.path.isdir(p) and p not in out:
            out.append(p)

    menus = r"Microsoft\Windows\Start Menu\Programs"
    for key in ("ProgramData", "ALLUSERSPROFILE", "APPDATA"):
        base = os.environ.get(key)
        if base:
            add(os.path.join(base, menus))
    for key, sub in (("USERPROFILE", "Desktop"), ("PUBLIC", "Desktop")):
        base = os.environ.get(key)
        if base:
            add(os.path.join(base, sub))
    appdata = os.environ.get("APPDATA")
    if appdata:
        add(os.path.join(appdata,
                         r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"))
    return out


def _discover_shortcut_dirs():
    """从开始菜单/桌面/任务栏的 .lnk 里抠出达芬奇的安装目录。

    .lnk 是二进制格式，这里不追求完整解析 —— 只把它里面出现的「含 Resolve.exe
    的盘符路径」捞出来当线索（验证不过就丢掉）。快捷方式一定指向真实安装位置，
    所以这是「装在非默认目录」时最有用的一条线索。
    """
    dirs = []

    def add_dir(p):
        if not p:
            return
        p = os.path.normpath(p)
        if os.path.isfile(os.path.join(p, EXE_NAME)) and p not in dirs:
            dirs.append(p)

    for root in _shortcut_locations():
        try:
            entries = []
            with os.scandir(root) as it:
                for e in it:
                    entries.append(e)
            # 开始菜单是 Menus\<厂商>\<快捷方式>.lnk，所以再看一层子目录
            for e in list(entries):
                try:
                    if e.is_dir(follow_symlinks=False):
                        with os.scandir(e.path) as it2:
                            entries.extend(list(it2))
                except OSError:
                    continue
        except OSError:
            continue

        for e in entries:
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                if not e.name.lower().endswith(".lnk"):
                    continue
                if e.name.lower().startswith(("~$", "$")):
                    continue
                if "resolve" not in e.name.lower() and "davinci" not in e.name.lower():
                    continue
                if e.stat().st_size > 262144:
                    continue
                with open(e.path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            for text in _strings_in_binary(raw):
                if EXE_NAME.lower() not in text.lower():
                    continue
                for m in _LOOKS_LIKE_EXE.finditer(text):
                    add_dir(os.path.dirname(m.group(0)))
    return dirs


def _install_dirs_from_msi():
    """从 Windows 安装器（MSI）登记的目录里找出达芬奇的安装目录。

    为什么值得专门开一条渠道：达芬奇是用 MSI 装的，Windows 会把**每个**安装
    目录登记到
        HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Installer\\Folders
    的「值名」里（值是 0/1）。枚举 1 万多个值只要 60 毫秒，比全盘搜索快 100 倍，
    而且拿到的就是当初装到哪儿了 —— 装在 D:\\软件\\达芬奇 这种完全没规律的位置
    也能直接命中。

    注意它**包含旧版残留**（升级后旧路径往往还留着），所以这里只当候选，
    到底用哪份由 _pick_best_install 按版本挑。
    """
    dirs = []
    try:
        import winreg
    except ImportError:
        return dirs
    keys = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\Folders",)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    seen = set()
    for hive in hives:
        for view in views:
            for key in keys:
                try:
                    h = winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        name = winreg.EnumValue(h, i)[0]
                    except OSError:
                        break
                    i += 1
                    if not isinstance(name, str):
                        continue
                    low = name.lower()
                    if not any(k in low for k in ("blackmagic", "davinci", "resolve")):
                        continue
                    d = name.rstrip("\\/")
                    if d and d.lower() not in seen:
                        seen.add(d.lower())
                        dirs.append(d)
    return dirs


def _strings_in_binary(raw):
    """从二进制里捞出所有「可读字符串」（同时按 UTF-16LE 和单字节两种编法）。"""
    out = []
    try:
        for m in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", raw):
            try:
                out.append(m.group(0).decode("utf-16-le"))
            except Exception:
                pass
    except Exception:
        pass
    try:
        for m in re.finditer(rb"[\x20-\x7e]{4,}", raw):
            out.append(m.group(0).decode("latin-1"))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 版本线索
#
# 为什么需要这一节：机器上**可能同时存在好几份达芬奇**（升级后旧目录没删干净、
# 卸载没卸全）。磁盘上多份 fusionscript.dll 时，挑错版本会直接连不上/连上就崩。
# 注册表里记着「现在装的是哪个版本」，拿它给候选打分，就能把旧残留筛掉。

_REG_VERSION_MEMO = {}


def registry_version():
    """读注册表记录的达芬奇版本（如 "21.0.00047"）。读不到返回 ""。"""
    if "v" in _REG_VERSION_MEMO:
        return _REG_VERSION_MEMO["v"]
    ver = ""
    try:
        import winreg
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for path in (r"SOFTWARE\Blackmagic Design\DaVinci Resolve",
                             r"SOFTWARE\Blackmagic Design\DaVinci Resolve Renderer"):
                    try:
                        h = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
                    except OSError:
                        continue
                    try:
                        v = winreg.QueryValueEx(h, "Version")[0]
                        if v and not ver:
                            ver = str(v)
                    except OSError:
                        pass
    except Exception:
        pass
    _REG_VERSION_MEMO["v"] = ver
    return ver


def _file_version(path):
    """读 exe 的版本号（Windows 版本资源）。读不到返回 ""。"""
    try:
        import ctypes
        import ctypes.wintypes as w
        fn = ctypes.windll.version
        size = fn.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not fn.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        for name in ("ProductVersion", "FileVersion"):
            val = ctypes.c_wchar_p()
            ln = w.UINT()
            sub = "\\StringFileInfo\\040904b0\\" + name
            if fn.VerQueryValueW(buf, sub, ctypes.byref(val), ctypes.byref(ln)) and ln.value:
                return str(val.value)
    except Exception:
        pass
    return ""


def _major_minor(v):
    """把 "21.0.00047" / "21.0.0.47" 都规约成 "21.0"（只比大小版本，够用）。"""
    nums = re.findall(r"\d+", str(v or ""))
    return ".".join(nums[:2]) if len(nums) >= 2 else ""


def _install_score(dll_path):
    """给候选 fusionscript.dll 打分：越像「正在用的那一份」分越高。"""
    d = os.path.dirname(dll_path)
    exe = os.path.join(d, EXE_NAME)
    if not os.path.isfile(exe):
        return 0
    score = 4                      # 旁边有 Resolve.exe = 是个完整安装目录
    v = _file_version(exe)
    reg = registry_version()
    if v and reg and _major_minor(v) == _major_minor(reg):
        score += 8                 # 版本和注册表记录一致 = 就是现在在用的那份
    elif v:
        score += 1
    return score


def _pick_best_install(hits):
    """磁盘上有好几份 fusionscript.dll 时，挑最可能是「当前在用」的那份。"""
    if not hits:
        return ""
    if len(hits) == 1:
        return hits[0]
    return max(hits, key=_install_score)


def _install_dir_candidates():
    """按「最可能 -> 最不可能」返回候选安装目录（可能不存在，交给调用方验证）。

    这一层只会做字符串拼接 + 少量 os.path.isfile/isdir，很快；真正慢的全盘
    搜索放在后面，只有这里全军覆没才会触发。
    """
    return _memo("install_candidates", _build_install_dir_candidates)


def _build_install_dir_candidates():
    out, seen = [], set()

    def add(p):
        if not p:
            return
        try:
            p = os.path.normpath(p)
        except Exception:
            return
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)

    # 1) 显式指定：环境变量 > config.json
    cfg = _read_config()
    for v in (os.environ.get("RESOLVE_INSTALL_DIR"),
              os.environ.get("RESOLVE_SCRIPT_LIB"),
              cfg.get("resolve_install_dir"),
              cfg.get("resolve_script_lib")):
        if not isinstance(v, str):
            continue
        if v.lower().endswith(".dll"):
            add(os.path.dirname(v))
        else:
            add(v)
            add(os.path.dirname(v))

    # 2) 上次成功找到的
    cached = _load_cache()
    for key in ("install_dir", "lib"):
        v = cached.get(key)
        if isinstance(v, str) and v:
            add(os.path.dirname(v) if key == "lib" else v)

    # 3) 正在运行的达芬奇（最权威）
    for exe in _process_exe_paths():
        add(os.path.dirname(exe))

    # 4) Windows 安装器登记的安装目录（第二权威，且装在哪儿都能查到）
    for d in _install_dirs_from_msi():
        add(d)

    # 5) 快捷方式里解析出来的
    for d in _shortcut_install_dirs():
        add(d)

    # 6) 盘符 × 常见软件父目录 × 达芬奇目录名
    #    只和「盘符根」组合：_system_dirs() 里的 Program Files 之类本来就已经
    #    是 _PARENT_DIR_NAMES 的一员，再拿它们当根会拼出
    #    「Program Files\Program Files」这种没意义的组合。
    for dr in _fixed_drives():
        root = dr + ":\\"
        for parent in _PARENT_DIR_NAMES:
            base = os.path.join(root, parent) if parent else root
            for leaf in _LEAF_DIR_NAMES:
                add(os.path.join(base, leaf) if leaf else base)

    # 7) 系统盘之外，ProgramData 也可能被挪走（极少见，但顺手加上）
    for sd in _system_dirs():
        for leaf in _LEAF_DIR_NAMES:
            add(os.path.join(sd, leaf) if leaf else sd)
    return out


def _shortcut_install_dirs():
    return _memo("shortcut_install_dirs", _discover_shortcut_dirs)


def _dir_has_dll(d):
    for sub in _DLL_SUBDIRS:
        p = os.path.join(d, sub) if sub else d
        if os.path.isfile(os.path.join(p, DLL_NAME)):
            return os.path.join(p, DLL_NAME)
    return ""


def _scan_roots():
    """全盘搜索的起点：常见软件目录在前（大概率第一下就中），盘符根在后。"""
    out, seen = [], set()
    for r in list(_system_dirs()) + [dr + ":\\" for dr in _fixed_drives()]:
        try:
            k = os.path.normcase(os.path.normpath(r))
        except Exception:
            continue
        if k not in seen and os.path.isdir(r):
            seen.add(k)
            out.append(r)
    return out


def _scan_for_all(filename, want=1, budget=_SCAN_BUDGET, max_depth=_SCAN_MAX_DEPTH,
                  good_enough=None):
    """限定深度 + 限定时间地找文件，返回命中的完整路径列表（最多 want 个）。

    达芬奇的目录名/位置千奇百怪，与其穷举不如给个几秒预算扫一遍磁盘。
    结果会写进 .runtime 缓存，所以这个代价一台机器一辈子只付一次。

    want > 1 是为了「磁盘上有好几份达芬奇」的情况：先都收着，回头挑版本对的
    那份（见 _pick_best_install）。good_enough(hit) 返回 True 时立刻收工，
    避免明明一次就找对了还要把预算跑满。

    分两轮扫（这个顺序很关键，踩过坑）：
        第 1 轮：每个盘符只看浅层。几乎不花时间，专门用来抓「直接装在盘根」的
                 情况（E:\\Davinci、D:\\软件\\达芬奇）—— 这类最容易被第 2 轮的
                 预算耗尽漏掉：实测某台机器上，第 2 轮在 C:\\Program Files 里
                 耗光 6 秒，明明 E:\\Davinci 就在盘根却压根没扫到。
        第 2 轮：从常见软件目录往下挖，覆盖
                 C:\\Program Files\\Blackmagic Design\\DaVinci Resolve 这类深一层的位置。
    """
    deadline = time.time() + budget
    target = filename.lower()
    hits = []
    rounds = (
        ([(dr + ":\\", 0) for dr in _fixed_drives()], 2),
        ([(r, 0) for r in _scan_roots()], max_depth),
    )
    for starts, depth_limit in rounds:
        for start, depth0 in starts:
            queue = [(start, depth0)]
            head = 0
            while head < len(queue):
                if time.time() > deadline:
                    return hits
                d, depth = queue[head]
                head += 1
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    continue
                for e in entries:
                    try:
                        if e.is_file(follow_symlinks=False):
                            if e.name.lower() == target and e.path not in hits:
                                hits.append(e.path)
                                if good_enough and good_enough(e.path):
                                    return hits
                                if len(hits) >= want:
                                    return hits
                        elif depth < depth_limit and e.is_dir(follow_symlinks=False):
                            n = e.name.lower()
                            if n in _SKIP_DIRS or n.startswith("$"):
                                continue
                            queue.append((e.path, depth + 1))
                    except OSError:
                        continue
    return hits


def _scan_for(filename, budget=_SCAN_BUDGET, max_depth=_SCAN_MAX_DEPTH):
    """只要第一个命中就行（用于 DaVinciResolveScript.py 这种唯一性强的文件）。"""
    hits = _scan_for_all(filename, want=1, budget=budget, max_depth=max_depth)
    return hits[0] if hits else ""


def _deep_scan_lib():
    """兜底用的全盘搜索（结果带缓存，别每次重扫）。"""
    reg = registry_version()

    def good(path):
        """旁边有 Resolve.exe 且版本和注册表一致 → 就是它，不用再扫了。"""
        exe = os.path.join(os.path.dirname(path), EXE_NAME)
        if not os.path.isfile(exe):
            return False
        if not reg:
            return True
        return _major_minor(_file_version(exe)) == _major_minor(reg)

    return _memo("deep_scan_lib",
                 lambda: _scan_for_all(DLL_NAME, want=6, good_enough=good),
                 ttl_neg=300.0)


def _discover_lib():
    """按优先级找出 fusionscript.dll 的完整路径；找不到返回 ""。"""
    # 1) 环境变量（安装器/用户显式指定的就是它）
    v = os.environ.get("RESOLVE_SCRIPT_LIB")
    if v and os.path.isfile(os.path.expandvars(v)):
        return os.path.normpath(os.path.expandvars(v))

    # 2) config.json
    v = _read_config().get("resolve_script_lib")
    if isinstance(v, str) and v and os.path.isfile(v):
        return os.path.normpath(v)

    # 3) 上次找到的
    v = _load_cache().get("lib")
    if isinstance(v, str) and v and os.path.isfile(v):
        return os.path.normpath(v)

    # 4) 正在运行的达芬奇 —— 进程在哪，安装目录就在哪，不用挑也不用猜。
    #    这一步不缓存结果，因为达芬奇可能在插件之后才启动（用户就是这么用的）。
    for exe in _process_exe_paths():
        p = _dir_has_dll(os.path.dirname(exe))
        if p:
            _save_cache(lib=p, install_dir=os.path.dirname(p))
            return p

    # 5) 命名候选（快捷方式 / 盘符组合）。收集所有命中的，再挑「最像当前在用」的那份
    #    —— 机器上留着旧版达芬奇目录是常事，撞上就白连了。
    hits = []
    for d in _install_dir_candidates():
        p = _dir_has_dll(d)
        if p and p not in hits:
            hits.append(p)
    if hits:
        p = _pick_best_install(hits)
        _save_cache(lib=p, install_dir=os.path.dirname(p))
        return p

    # 6) 兜底：限定深度全盘搜索
    hits = _deep_scan_lib()
    if hits:
        p = _pick_best_install(hits)
        _save_cache(lib=p, install_dir=os.path.dirname(p))
        return p
    return ""


def _find_fusionscript_lib():
    """定位 fusionscript.dll 的完整路径（用于设置 RESOLVE_SCRIPT_LIB）。"""
    return _memo("lib", _discover_lib)


def _module_dir_candidates():
    """DaVinciResolveScript.py 所在目录的候选列表。"""
    out, seen = [], set()

    def add(p):
        if not p:
            return
        try:
            p = os.path.normpath(p)
        except Exception:
            return
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)

    # 1) 环境变量 / config.json / 缓存
    for v in (os.environ.get("RESOLVE_SCRIPT_API"),
              _read_config().get("resolve_script_path"),
              _load_cache().get("module_dir")):
        if isinstance(v, str) and v:
            add(v)

    # 2) ProgramData 下的官方布局（环境变量给的 ProgramData 优先，再兜系统盘）
    tails = (r"Support\Developer\Scripting\Modules", "Modules")
    programdata = [os.environ.get("PROGRAMDATA"), os.environ.get("ALLUSERSPROFILE"),
                   r"C:\ProgramData"]
    for dr in _fixed_drives():
        programdata.append(dr + ":\\ProgramData")
    for pd in programdata:
        if not pd:
            continue
        for tail in tails:
            add(os.path.join(pd, "Blackmagic Design", "DaVinci Resolve", tail))

    # 3) 达芬奇安装目录自己带的 Support\Developer\Scripting\Modules
    install = find_resolve_install_dir()
    if install:
        for base in (install, os.path.dirname(install)):
            if base:
                add(os.path.join(base, "Support", "Developer", "Scripting", "Modules"))
    return out


def _find_module_dir():
    """定位 DaVinciResolveScript.py 所在目录（用于 sys.path）。"""
    return _memo("module_dir", _discover_module_dir)


def _discover_module_dir():
    for d in _module_dir_candidates():
        if os.path.isfile(os.path.join(d, MODULE_NAME)):
            _save_cache(module_dir=d)
            return d
    p = _scan_for(MODULE_NAME)
    if p:
        d = os.path.dirname(p)
        _save_cache(module_dir=d)
        return d
    return ""


def find_resolve_install_dir():
    """达芬奇安装目录（fusionscript.dll 所在目录）；找不到返回 ""。"""
    lib = _find_fusionscript_lib()
    return os.path.dirname(lib) if lib else ""


def find_resolve_exe():
    """达芬奇主程序 Resolve.exe 的完整路径；找不到返回 ""。"""
    # 1) 正在运行的进程（最权威）
    for p in _process_exe_paths():
        if os.path.isfile(p):
            return p
    # 2) 安装目录 / 快捷方式
    for d in [find_resolve_install_dir()] + list(_shortcut_install_dirs()):
        if d:
            p = os.path.join(d, EXE_NAME)
            if os.path.isfile(p):
                return p
    return ""


def search_report(limit=14):
    """把「找过哪些地方」写成给用户看的中文清单（找不到时用）。"""
    cands = _install_dir_candidates()
    lines = []
    for d in cands[:limit]:
        mark = "有" if _dir_has_dll(d) else "无"
        lines.append("  [%s] %s" % (mark, d))
    if len(cands) > limit:
        lines.append("  …（命名候选共 %d 个；若都不命中，还会做一次限定深度的全盘搜索）"
                     % len(cands))
    return lines


def find_all_installs():
    """磁盘上所有「像达芬奇安装目录」的目录（含旧版残留），返回 [].

    用来提醒「机器上留着旧版达芬奇」：插件只会用其中一份，如果用的是旧的
    那份（升级后没删干净），连不上或者连上就崩都不奇怪。
    """
    found = []
    for d in _install_dir_candidates():
        p = _dir_has_dll(d)
        if p and p not in found:
            found.append(p)
    return found


def diagnose():
    """把「达芬奇路径是怎么找到的」全过程摊开，给体检报告 / 探针用。"""
    lib = _find_fusionscript_lib()
    mod = _find_module_dir()
    exe = find_resolve_exe()
    return {
        "lib": lib,
        "module_dir": mod,
        "install_dir": os.path.dirname(lib) if lib else "",
        "exe": exe,
        "exe_version": _file_version(exe) if exe else "",
        "registered_version": registry_version(),
        # 注意：我们自己连接时也会写 RESOLVE_SCRIPT_LIB，所以这里看的是
        # 「本进程刚启动时环境里有没有」——那才代表用户/安装器显式指定过。
        "lib_from_env": bool(_ENV_LIB_AT_IMPORT),
        "lib_from_config": bool(_read_config().get("resolve_script_lib")),
        "candidates": _install_dir_candidates()[:30],
        "candidate_total": len(_install_dir_candidates()),
        "module_candidates": _module_dir_candidates()[:12],
        "installs": find_all_installs(),
        "cached": _load_cache(),
        "cache_file": _CACHE_FILE,
        "search_report": search_report(),
        # 用户在 config.json 里到底填了什么（排查「我明明指定了目录」必备）
        "config": _read_config(),
        "config_problem": config_problem(),
        "config_notes": config_notes(),
        "configured_install_dir": _read_config().get("resolve_install_dir") or "",
        "configured_script_lib": _read_config().get("resolve_script_lib") or "",
        "configured_script_path": _read_config().get("resolve_script_path") or "",
    }


def check_configured_paths():
    """检查用户在 config.json 里手填的路径到底有没有用。

    返回 (结果列表, 是否有问题)。每条结果是一句中文 + 布尔（True=有这个问题）。

    「我明明在 config.json 里指定了达芬奇目录，怎么还是连不上」——这个问题
    太常出现了，值得单列一项：填的目录里到底有没有 fusionscript.dll。
    常见错法：填成了 Modules 目录、填成了上级目录、路径打错一个字。
    """
    cfg = _read_config()
    items = []

    raw_dir = cfg.get("resolve_install_dir")
    if isinstance(raw_dir, str) and raw_dir.strip():
        d = raw_dir.strip()
        if not os.path.isdir(d):
            items.append(("resolve_install_dir 填的目录不存在：%s" % d, True))
        elif _dir_has_dll(d):
            items.append(("resolve_install_dir 有效：%s" % d, False))
        else:
            items.append(("resolve_install_dir 里没有 fusionscript.dll：%s"
                          "（是不是填成了 …\\Support\\Developer\\Scripting\\Modules？"
                          "要填达芬奇自己的安装目录）" % d, True))

    for key in ("resolve_script_lib", "resolve_script_path"):
        raw = cfg.get(key)
        if not (isinstance(raw, str) and raw.strip()):
            continue
        v = raw.strip()
        if key == "resolve_script_lib":
            items.append(("resolve_script_lib %s：%s"
                          % ("有效" if os.path.isfile(v) else "文件不存在", v),
                          not os.path.isfile(v)))
        else:
            ok = os.path.isfile(os.path.join(v, MODULE_NAME))
            items.append(("resolve_script_path %s：%s"
                          % ("有效" if ok else "目录里没有 %s" % MODULE_NAME, v), not ok))

    return items, any(bad for _msg, bad in items)


# ---------------------------------------------------------------- 连接

class ResolveConnection:
    """封装与达芬奇的连接与时间码读取。"""

    def __init__(self, module_path: str = None, lib_path: str = None):
        self.resolve = None
        self.module_path = module_path or _find_module_dir()     # DaVinciResolveScript.py 目录
        self.lib_path = lib_path or _find_fusionscript_lib()     # fusionscript.dll 完整路径
        self._loaded = False
        self._init_key = None
        self.last_error = ""

    def refresh_paths(self):
        """重新找一遍两个关键文件（只补空的那部分）。

        为什么每次连接前都要做：本插件支持「先开插件、后开达芬奇」，而达芬奇
        没运行时能用的线索会少很多（比如「正在运行的进程」这条就没有）。
        如果开机那一刻恰好没找到 fusionscript.dll，用户再启动达芬奇时，
        这里必须能补上，否则会永远卡在「等待达芬奇」。
        """
        if not self.module_path:
            self.module_path = _find_module_dir()
        if not self.lib_path:
            self.lib_path = _find_fusionscript_lib()

    def _ensure_module(self):
        # 路径变了就重新初始化（比如 lib_path 是刚刚才补上的）
        key = (self.module_path or "", self.lib_path or "")
        if self._loaded and self._init_key == key:
            return
        self._init_key = key

        # ---- Windows 下加载 fusionscript 的关键初始化（顺序不能乱） ----
        py_home = os.path.dirname(sys.executable)   # 当前解释器目录（含 python3xx.dll）

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

        # 3) 注册 DLL 搜索目录：fusionscript.dll 还要靠安装目录里其它达芬奇 DLL 才行。
        #    （只加目录不加文件，找不到 dll 时这里也帮不上忙，所以下面还会加一批
        #      「看着像安装目录」的候选目录，万一官方默认路径那套能救回来呢。）
        dll_dirs = [py_home]
        if self.lib_path:
            dll_dirs.insert(0, os.path.dirname(self.lib_path))
        else:
            for d in _install_dir_candidates()[:6]:
                if os.path.isfile(os.path.join(d, EXE_NAME)):
                    dll_dirs.append(d)
        for d in dll_dirs:
            if d and os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass

        # 4) Blackmagic 官方 DaVinciResolveScript.py 查找的环境变量。
        #    ★ 这个变量必须在 import DaVinciResolveScript **之前** 设好：
        #      官方那个 .py 是在 import 的那一刻读它的。
        if self.lib_path:
            os.environ["RESOLVE_SCRIPT_LIB"] = self.lib_path
        if self.module_path:
            os.environ["RESOLVE_SCRIPT_API"] = self.module_path

        # 5) Modules 目录加入 sys.path，才能 import DaVinciResolveScript
        if self.module_path and self.module_path not in sys.path:
            sys.path.append(self.module_path)

        self._loaded = True

    def _not_found_error(self):
        """两个关键文件都找不到时，给一份「我找过哪些地方」的清单。"""
        parts = []
        if not self.module_path:
            parts.append("没有找到 DaVinciResolveScript.py（达芬奇的脚本接口文件）。")
        if not self.lib_path:
            parts.append("没有找到 fusionscript.dll（达芬奇的原生脚本库）。")
        parts.append("已经找过这些地方（[有] = 该目录里确实有这个文件）：")
        parts.extend(search_report())
        parts.append("如果是装在很特别的位置，可以在 config.json 里手动指定：")
        parts.append('  "resolve_install_dir": "达芬奇安装目录（fusionscript.dll 所在的那个）"')
        parts.append('  "resolve_script_path": "…\\Support\\Developer\\Scripting\\Modules"')
        return "\n".join(parts)

    def connect(self):
        """连接达芬奇，返回 True/False。失败时把原因写入 self.last_error。"""
        try:
            self.refresh_paths()
            self._ensure_module()
            import DaVinciResolveScript as dvr_script
            self.resolve = dvr_script.scriptapp("Resolve")
        except Exception as e:
            self.resolve = None
            # 这次没成，把路径记忆清掉：达芬奇如果刚好被打开了，
            # 下一次重试就能靠「运行中的进程」找到它。
            forget_discovery()
            msg = "%s: %s" % (type(e).__name__, e)
            # 「两个文件没找齐」比具体的异常类型更能指导用户，所以只要缺文件，
            # 就把「我找过哪些地方」的清单附上（ModuleNotFoundError 也算，
            # 以前漏了这一类，用户只能看到一个干巴巴的 No module named ...）。
            if (not self.module_path) or (not self.lib_path) or \
                    "Could not locate module dependencies" in str(e) or \
                    "DLL load failed" in str(e) or \
                    "ImportError" in type(e).__name__ or \
                    "ModuleNotFoundError" in type(e).__name__:
                msg += "\n" + self._not_found_error()
            self.last_error = msg
            return False

        # 导入成功 = 两个文件都对了。顺手把「真实路径」记下来（尤其是官方默认
        # 路径帮忙找到的那种情况），下次就不用再猜了。
        self._learn_real_paths()

        if not self.resolve:
            self.last_error = (
                "scriptapp(\"Resolve\") 返回 None。达芬奇可能没启动，或 'External scripting' 没设为 Local，"
                "或本进程运行的 Python 版本与达芬奇 fusionscript 模块不兼容（需用达芬奇匹配的 Python 版本），"
                "或达芬奇是免费版（19.1 起外部进程调用脚本仅 Studio 付费版可用）。"
            )
            return False
        return True

    def _learn_real_paths(self):
        """导入成功后，从模块对象反查 fusionscript.dll 的真实路径并记进缓存。

        为什么能拿到：官方 DaVinciResolveScript.py 最后把 sys.modules[__name__]
        换成了它加载出来的 fusionscript 扩展模块，而扩展模块带 __file__。
        所以哪怕前面一路没找到 dll（靠官方默认路径蒙对的），这里也能学到。
        """
        try:
            m = sys.modules.get("DaVinciResolveScript") or sys.modules.get("fusionscript")
            real = getattr(m, "__file__", "") or ""
            if real.lower().endswith(".dll") and os.path.isfile(real):
                real = os.path.normpath(real)
                if os.path.normcase(real) != os.path.normcase(self.lib_path or ""):
                    self.lib_path = real
                _save_cache(lib=real, install_dir=os.path.dirname(real),
                            module_dir=self.module_path)
        except Exception:
            pass

    def is_connected(self):
        return self.resolve is not None

    def disconnect(self):
        """主动断开：丢掉可能已失效的 app 句柄。

        达芬奇被关闭后，self.resolve 指向的对象已经无效，但 API 不一定立刻报错
        （可能只是持续返回空值）。这里显式清空，让下一次 connect() 重新
        scriptapp("Resolve") 建立连接。
        """
        self.resolve = None

    def is_alive(self) -> bool:
        """轻量探活：达芬奇进程还在吗？

        与「有没有时间线」无关——只判断连接本身是否还有效：
            - 达芬奇还开着（哪怕没打开工程）→ GetProjectManager() 返回非 None
            - 达芬奇已关闭 / 句柄失效        → 抛异常或返回 None

        用 GetProjectManager() 当探针是因为它不依赖任何工程/时间线状态，
        开销也足够小，可以每 2 秒调一次。
        """
        if not self.resolve:
            return False
        try:
            return self.resolve.GetProjectManager() is not None
        except Exception:
            return False

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
