# -*- coding: utf-8 -*-
"""
env_check.py —— 环境体检。

回答一个事：**这台电脑能不能跑插件？缺什么？怎么补？**

被两个地方用到：
    1. 「检查环境.bat」双击 → 在控制台里打印一份完整的中文体检报告；
    2. launcher.py 启动前静默跑一遍 → 发现致命问题就用弹窗直接告诉用户，
       而不是双击完什么都不发生。

几个约定：
    - 每项检查都独立 try/except，单项失败不影响其它项，报告永远能打完；
    - 只依赖标准库（本项目全项目都不用第三方包），所以便携版 Python 也能跑；
    - 输出编码按「控制台实际代码页」走：中文 Windows 是 936、UTF-8 代码页是
      65001，两边都不会乱码（写死 utf-8 会在 936 控制台里变成乱码）；
    - 报告里只用 ASCII 的 [] 和 = - 做装饰，不用 emoji / 花哨符号
      （GBK 里没有的字符会显示成问号）。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 连接实测探针的启动标记（probe_resolve.py 里定义的同一串）
_PROBE_MARK = "<<<PROBE>>>"

# 连接实测最多等多久。达芬奇刚启动时第一次脚本连接可能要几秒，
# 但再久就不正常了（真卡住的话宁可报「超时」也别让用户干等）。
PROBE_TIMEOUT = 30

# 检查结论的三个档位
OK, WARN, BAD = "ok", "warn", "bad"

_LEVEL_TAG = {OK: "[通过]", WARN: "[注意]", BAD: "[失败]"}

# 达芬奇脚本接口只认这两个 Python 版本（fusionscript.pyd 是按版本编译的）
GOOD_PY = ((3, 10), (3, 11))


# ---------------------------------------------------------------- 基础设施

def _setup_stdout():
    """只处理「输出被重定向」的情况，写控制台时什么都不做。

    为什么不在控制台里自己按代码页重编码：Python 3.6 起在 Windows 上写控制台
走的是 WriteConsoleW（PEP 528），中文本来就不会乱码，而且和代码页无关。
我们要是自作主张按 cp936 重新编码，反而会把好端端的中文写坏。

检查项一览（[3] 是唯一一项「真动手试一次」的）：
    [1] Python 版本（达芬奇只认 3.10/3.11）
    [2] 达芬奇本体：脚本模块、fusionscript.dll、进程、版本号
    [3] 达芬奇连接实测：真跑一次 scriptapp("Resolve")，看当前工程/时间线/能否匹配课件
    [4] 悬浮窗浏览器（Edge）
    [5] 课件数据（data/ 下的 JSON 数量）
    [6] 后端端口：被占时顺便把「上面那个后端看到了什么」问出来
    [7] 项目文件完整性
    [8] 便携版 Python

    被重定向到文件/管道时 Python 会退回按 locale 编码（中文 Windows 是 GBK），
    这里统一改成 utf-8，日志文件跨机器看不会乱码。

    注意：launcher 是用 pythonw 跑的，sys.stdout 可能是 None，必须容忍。
    """
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            return                      # 真控制台 -> 交给 Python 自己，别插手
    except Exception:
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _out(text=""):
    """安全打印：pythonw 下没有 stdout，静默跳过即可。"""
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass


def _run_quiet(cmd, timeout=10):
    """跑一条命令取 stdout（不弹窗、不抛异常）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def _http_json(url, timeout=2.5):
    """GET 一个本地 HTTP 接口并解析 JSON；失败返回 None（不抛异常）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _resolve_running():
    """达芬奇进程在不在跑。"""
    return "Resolve.exe" in _run_quiet(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"])


def _resolve_exe():
    """找出达芬奇主程序的完整路径。

    交给 resolve_connection 统一处理（进程 → 快捷方式 → 安装目录 → 常见位置），
    这里不再写死 C:/D:/E: 那几个目录 —— 每个人的安装位置都不一样。
    """
    try:
        import resolve_connection as rc
        return rc.find_resolve_exe() or None
    except Exception:
        return None


def _file_version(path):
    """读 exe 的版本号（Windows 的版本资源）。读不到返回 ""。"""
    try:
        import resolve_connection as rc
        return rc._file_version(path)
    except Exception:
        return ""


# ---------------------------------------------------------------- 各项检查

def check_python():
    """Python 解释器版本 —— 达芬奇 fusionscript 只支持 3.10 / 3.11。"""
    v = sys.version_info
    ver = "%d.%d.%d" % (v.major, v.minor, v.micro)
    lines = ["版本：" + ver, "路径：" + sys.executable]

    if (v.major, v.minor) in GOOD_PY:
        return dict(level=OK, title="Python 解释器",
                    lines=lines,
                    advice="")

    if (v.major, v.minor) > (3, 11):
        return dict(
            level=BAD, title="Python 解释器", lines=lines,
            advice=("达芬奇的脚本接口（fusionscript）是按 Python 3.10 / 3.11 编译的，"
                    "用 %s 连不上达芬奇。\n"
                    "请另装一个 Python 3.11（可以和现在这个共存），"
                    "装的时候记得勾选 \"Add python.exe to PATH\"。" % ver))

    return dict(level=BAD, title="Python 解释器", lines=lines,
                advice="Python 版本过低（需要 3.10 或 3.11），请升级。")


# 找不到 fusionscript.dll 时统一给的说明（这个文件的位置每个人都不一样）
_LIB_NOT_FOUND_ADVICE = (
    "插件要用达芬奇安装目录里的 fusionscript.dll（原生脚本库），但在这台电脑上\n"
    "没找到。这个文件的位置「每个人都不一样」——\n"
    "有人是 C:\\Program Files\\Blackmagic Design\\DaVinci Resolve，\n"
    "有人是 D:\\软件\\达芬奇\\DaVinci Resolve，有人干脆是 E:\\Davinci。\n"
    "插件已经按这个顺序找过一遍了：正在运行的达芬奇 → 开始菜单/桌面快捷方式 →\n"
    "各磁盘的常见软件目录 → 限定深度的全盘搜索。\n"
    "\n"
    "怎么办（从上往下试）：\n"
    "1) 先把达芬奇打开（进程在跑的时候最好找），再双击一次 检查环境.bat。\n"
    "2) 还找不到就手动指定。找法：开始菜单里右键「DaVinci Resolve」→ 更多 →\n"
    "   打开文件位置，看看它落在哪个目录；然后在 config.json 里加一行：\n"
    '       "resolve_install_dir": "那个目录的完整路径"\n'
    "   （该目录里应该同时有 Resolve.exe 和 fusionscript.dll）\n"
    "3) 如果达芬奇根本没装，先装达芬奇。"
)


def check_davinci():
    """达芬奇本体：两个关键文件在不在、在哪、进程跑没跑、版本号。

    注意：这一项**只看「东西在不在」**，不验「能不能连上」。连接实测是下一项，
    两件事分开，一份报告才能看出到底是「没装」还是「装了但连不上」。
    """
    lines = []
    level = OK
    advice = ""

    try:
        import resolve_connection as rc
        d = rc.diagnose()
        mod_dir = d.get("module_dir") or ""
        lib = d.get("lib") or ""
    except Exception as e:
        return dict(level=WARN, title="达芬奇（DaVinci Resolve）",
                    lines=["检查时出错：%s: %s" % (type(e).__name__, e)],
                    advice="")

    if mod_dir:
        lines.append("脚本模块：已找到")
        lines.append("          " + mod_dir)
    else:
        level = BAD
        lines.append("脚本模块：未找到 DaVinciResolveScript.py")
        lines.append("          找过这些目录（[有] = 该目录里确实有这个文件）：")
        for c in (d.get("module_candidates") or [])[:8]:
            mark = "有" if os.path.isfile(os.path.join(c, "DaVinciResolveScript.py")) else "无"
            lines.append("          [%s] %s" % (mark, c))
        advice = ("没有找到达芬奇的脚本接口文件（DaVinciResolveScript.py）。\n"
                  "它通常在这里：<系统盘>\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\\n"
                  "Support\\Developer\\Scripting\\Modules\n"
                  "如果那里确实没有，多半是达芬奇没装好，或者装完被清理软件删过。\n"
                  "重装一次达芬奇即可；装好之后别忘了在达芬奇里开启脚本功能：\n"
                  "偏好设置 -> 系统 -> 常规 -> External scripting using 设为 Local。\n"
                  "也可以手动指定：config.json 里加一行\n"
                  '    "resolve_script_path": "…\\Support\\Developer\\Scripting\\Modules"')

    if lib:
        lines.append("运行库　：已找到 fusionscript.dll")
        lines.append("          " + lib)
    else:
        if level != BAD:
            level = BAD
        lines.append("运行库　：未找到 fusionscript.dll")
        lines.append("          找过这些地方（[有] = 该目录里确实有这个文件）：")
        for l in (d.get("search_report") or [])[:10]:
            lines.append("          " + l.strip())
        advice = _LIB_NOT_FOUND_ADVICE

    # 磁盘上可能有不止一份达芬奇（升级后旧目录没删干净是常事）。
    # 旧版残留会让插件挑错版本，连不上或者连上就崩，所以明确报出来。
    installs = d.get("installs") or []
    if len(installs) > 1:
        lines.append("")
        lines.append("磁盘上发现有 %d 份达芬奇安装（插件用标了 (*) 的那份）：" % len(installs))
        used = os.path.normcase(os.path.dirname(lib)) if lib else ""
        for p in installs[:6]:
            d = os.path.dirname(p)
            mark = "(*)" if used and os.path.normcase(d) == used else "   "
            v = _file_version(os.path.join(d, "Resolve.exe"))
            lines.append("          %s %s%s" % (mark, d, ("  v" + v) if v else ""))
        if level == OK:
            level = WARN
            advice = ("这台电脑上有不止一份达芬奇。插件已经按「正在运行的那份 →\n"
                      "注册表记录的版本 → 目录里有没有 Resolve.exe」的顺序挑了标 (*) 的\n"
                      "那一份。如果确认用错了，把不用的旧目录删掉/改名，或者干脆把达芬奇\n"
                      "重装一次，问题即可消失。")

    # 达芬奇主程序在哪、什么版本（版本号很关键：19.1 之后外部脚本只给 Studio 用）
    exe = _resolve_exe()
    if exe:
        ver = _file_version(exe)
        lines.append("主程序　：" + exe)
        if ver:
            lines.append("版本　　：" + ver)

    # 进程是否在跑（没开达芬奇也能用插件，只是没数据）
    running = _resolve_running()
    lines.append("运行状态：" + ("达芬奇正在运行" if running else "达芬奇当前没开（插件会等它启动）"))
    if not running and level == OK:
        level = WARN
        advice = ("达芬奇现在没开 —— 这不影响启动插件，但悬浮窗会先显示等待状态。\n"
                  "打开达芬奇并载入时间线后会自动接上。")

    return dict(level=level, title="达芬奇（DaVinci Resolve）", lines=lines, advice=advice)


# 连不上达芬奇时统一给这份排查清单（按可能性从高到低）
_LINK_ADVICE = (
    "1) 装的是免费版吗？—— 这是最常见的原因，先确认这一条。\n"
    "   达芬奇 19.1 之后，「外部进程」调用脚本被限制为 Studio（付费版）专属：\n"
    "   免费版只能在「工作区 -> 脚本」菜单里跑脚本，外部程序一律连不上，\n"
    "   改任何设置都没用。确认办法：打开达芬奇 -> 菜单「帮助 / Help」->「关于」，\n"
    "   或者直接看窗口标题栏，写着 Studio 才是付费版。\n"
    "   如果是免费版：这个插件（以及所有外部脚本工具）都用不了，\n"
    "   要升级到 Studio 才可以。\n"
    "\n"
    "2) 改完设置要重启达芬奇。\n"
    "   偏好设置 -> 系统 -> 常规 -> 「外部脚本使用 / External scripting using」\n"
    "   设为「本地 / Local」，然后「完全退出达芬奇再重新打开」（不重启常常不生效）。\n"
    "\n"
    "3) 达芬奇里要先打开一个工程，并且停在「剪辑 / Edit」页（有时间线）。\n"
    "\n"
    "4) 安全软件拦截。把插件所在文件夹加进杀毒软件白名单，或临时关掉\n"
    "   「脚本防护 / 勒索防护」再试一次。\n"
    "\n"
    "5) 确认插件用的是本机的 Python 3.10/3.11（本报告里「Python 解释器」\n"
    "   那一项通过就说明没问题）。\n"
)


def check_resolve_link():
    """真连一次达芬奇 —— 体检里唯一能回答「到底连没连上」的一项。

    为什么必须有这一项：[2] 只看文件在不在、进程跑没跑。文件都在、设置也对，
    却始终连不上（免费版限制、改完设置没重启达芬奇、没打开工程）时，以前
    整份报告全是「通过」，用户根本不知道问题在哪 —— 表现就是
    「悬浮窗一直显示等待/未同步」。

    实现在子进程里跑 probe_resolve.py：fusionscript 是原生库，连不上时可能
    直接把进程打崩（access violation），Python 层拦不住，隔一层子进程才安全。
    """
    title = "达芬奇连接实测"
    probe = os.path.join(BASE_DIR, "probe_resolve.py")
    if not os.path.isfile(probe):
        return dict(level=WARN, title=title,
                    lines=["找不到 probe_resolve.py，跳过实测",
                           "（这份文件是检查用的探针，缺失说明解压包不完整）"],
                    advice="请重新完整解压一遍下载的压缩包。")

    running = _resolve_running()
    try:
        p = subprocess.run([sys.executable, probe], capture_output=True, text=True,
                           errors="replace", timeout=PROBE_TIMEOUT,
                           creationflags=0x08000000)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return dict(level=WARN, title=title,
                    lines=["实测超时：%d 秒都没有返回" % PROBE_TIMEOUT,
                           "达芬奇可能正忙（正在导出/渲染），或脚本接口卡住了。"],
                    advice="等达芬奇空下来再跑一次体检。如果每次都超时，按下面的清单排查。")
    except Exception as e:
        return dict(level=WARN, title=title,
                    lines=["没能启动探针：%s: %s" % (type(e).__name__, e)], advice="")

    data = None
    for line in out.splitlines():
        if line.startswith(_PROBE_MARK):
            try:
                data = json.loads(line[len(_PROBE_MARK):])
            except Exception:
                data = None
    if not isinstance(data, dict):
        # 探针被 fusionscript 原生崩溃带走了 —— 这时候 stdout 里什么都没有
        lines = ["探针进程异常退出，没有返回任何结果",
                 "（达芬奇没开、或 Python 版本与 fusionscript 不匹配时会出现这种情况）"]
        lines += ["          " + l.strip()[-100:]
                  for l in [x for x in out.splitlines() if x.strip()][-3:]]
        return dict(level=(BAD if running else WARN), title=title, lines=lines,
                    advice=_LINK_ADVICE)

    # ---- 连不上 ----
    if not data.get("connected"):
        lines = ["连接结果：失败"]
        err = str(data.get("error") or "").strip()
        if err:
            lines.append("原因　　：" + err.splitlines()[0][:150])
        lines.append("脚本模块：" + (str(data.get("module_dir") or "") or "（没找到）"))
        lines.append("运行库　：" + (str(data.get("lib") or "") or "（没找到）"))
        lines.append("达芬奇　：" + ("正在运行（所以不是「没开软件」的问题）"
                                    if running else "没有在运行"))

        # 两个关键文件没找到 —— 这跟「免费版/没设外部脚本」是完全不同的病，
        # 药方也不一样，所以分开给建议（以前只会让人去查版别，白折腾）。
        if not data.get("lib") or not data.get("module_dir"):
            return dict(level=BAD, title=title, lines=lines,
                        advice=_LIB_NOT_FOUND_ADVICE)

        if not running:
            return dict(level=WARN, title=title, lines=lines,
                        advice=("达芬奇现在没开，所以连不上 —— 这不影响启动插件。\n"
                                "要测连接的话：先打开达芬奇、新建或载入一个工程，再跑一次体检。"))
        return dict(level=BAD, title=title, lines=lines, advice=_LINK_ADVICE)

    # ---- 连上了 ----
    lines = ["连接结果：成功"]
    product = str(data.get("product") or "")
    version = str(data.get("version") or "")
    if product or version:
        lines.append("达芬奇　：" + (product or "DaVinci Resolve") + (" " + version if version else ""))
    lines.append("当前工程：" + (str(data.get("project") or "") or "（没有打开的工程）"))
    tl = str(data.get("timeline") or "")
    lines.append("当前时间线：" + (tl or "（没有打开/选中的时间线）"))
    if data.get("timecode"):
        lines.append("播放头　：" + str(data["timecode"]))

    total = data.get("course_total")
    if isinstance(total, int):
        lines.append("已导入课件：%d 份" % total)
    match = str(data.get("match") or "")
    lines.append("课程匹配：" + ("成功 -> " + match if match else "没匹配上"))

    if not tl:
        return dict(
            level=WARN, title=title, lines=lines,
            advice=("插件能连上达芬奇，但达芬奇现在没有「当前时间线」。\n"
                    "请在达芬奇的「剪辑 / Edit」页里打开一条时间线，再跑一次体检。"))

    if not match:
        names = [str(n) for n in (data.get("courses") or [])]
        advice = ("插件是按「时间线名字」去找课件的 —— 名字对不上就没有数据。\n"
                  "空格、连字符、大小写会自动忽略，但字必须对得上。\n\n"
                  "当前时间线叫：%s\n" % tl)
        if names:
            advice += ("data/ 里现有的课件名（前几个）：\n    " + "\n    ".join(names) + "\n")
            if isinstance(total, int) and total > len(names):
                advice += "    …（共 %d 份）\n" % total
        advice += ("\n办法：在达芬奇的时间线管理器里双击改名，改成和课件名一致即可。\n"
                   "带器械前缀更保险，例如「爬楼机-20min心肺间歇突破攀登」。")
        return dict(level=WARN, title=title, lines=lines, advice=advice)

    return dict(level=OK, title=title, lines=lines, advice="")


def check_edge():
    """悬浮窗靠 Edge 的 --app 模式开，没 Edge 会退回普通浏览器标签页。"""
    try:
        import overlay
        edge = overlay.find_edge()
    except Exception as e:
        return dict(level=WARN, title="悬浮窗浏览器（Edge）",
                    lines=["检查时出错：%s: %s" % (type(e).__name__, e)], advice="")
    if edge:
        return dict(level=OK, title="悬浮窗浏览器（Edge）",
                    lines=["已找到 Microsoft Edge", "          " + edge], advice="")
    return dict(
        level=WARN, title="悬浮窗浏览器（Edge）",
        lines=["未找到 Microsoft Edge"],
        advice=("没有 Edge 时会退回用默认浏览器打开，但没有「独立小窗 + 置顶」效果。\n"
                "Windows 10/11 一般都自带 Edge，如果确实没有，装一个即可。"))


def check_courses():
    """data/ 目录里有没有转换好的课件 JSON。"""
    data_dir = os.path.join(BASE_DIR, "data")
    try:
        files = [f for f in os.listdir(data_dir) if f.lower().endswith(".json")] \
            if os.path.isdir(data_dir) else []
    except OSError:
        files = []

    lines = ["课件目录：" + data_dir,
             "已导入课件：%d 份" % len(files)]
    if files:
        sample = sorted(files)[:3]
        lines.append("例如：" + "、".join(os.path.splitext(f)[0] for f in sample)
                     + ("…" if len(files) > 3 else ""))
        return dict(level=OK, title="课程数据", lines=lines, advice="")

    return dict(
        level=WARN, title="课程数据", lines=lines,
        advice=("还没有导入任何课件。双击 convert.bat 选择课件表格（.xlsx）转换即可，\n"
                "转完不用重启插件，它会自己重新读取。"))


def _read_port():
    port = 8765
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            port = int(json.load(f).get("port") or port)
    except Exception:
        pass
    return port


def _port_busy(port):
    try:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()
    except Exception:
        return False


def check_port():
    """后端端口：被占时顺便把「上面那个后端到底看到了什么」问出来。

    这是排查「悬浮窗一直不同步」最快的一步：后端自己在 /diag 里就把
    「连没连上达芬奇、当前时间线是哪条、有没有匹配到课件、读的是哪个 data 目录」
    全都说了。用户只要双击一次体检，就能分清到底是插件的锅还是时间线名的锅。

    用 /diag 而不是 /state：/state 会被后端当成「前端心跳」，体检去问一下
    就可能让一个本该自动退出的旧后端继续赖着不走。
    """
    port = _read_port()
    if not _port_busy(port):
        return dict(level=OK, title="后端端口 %d" % port, lines=["端口空闲"], advice="")

    info = _http_json("http://127.0.0.1:%d/diag" % port)
    if info is None:
        info = _http_json("http://127.0.0.1:%d/state" % port)   # 兼容旧版后端

    if not isinstance(info, dict):
        return dict(
            level=WARN, title="后端端口 %d" % port,
            lines=["端口被占用，但上面的程序不回应本插件的诊断接口",
                   "（多半是别的软件占了这个端口，插件后端没跑起来）"],
            advice=("把占用这个端口的程序关掉，或者把 config.json 里的 port\n"
                    "改成别的（比如 8766），再双击 start.bat。"))

    lines = ["端口上正在运行插件后端（正常，说明插件正开着）", "",
             "它自己报告的状态：",
             "  连接达芬奇：" + ("已连接" if info.get("connected") else "未连接"),
             "  当前时间线：" + (str(info.get("timeline_name") or "") or "（没读到）"),
             "  匹配到课件：" + (("是 -> " + str(info.get("course_name") or ""))
                                if info.get("course_loaded") else "否"),
             "  课件数量　：" + str(info.get("course_count")),
             "  读取目录　：" + str(info.get("server_data_dir") or "（未上报）")]
    if info.get("message"):
        lines.append("  悬浮窗那行小字：" + str(info["message"]))

    level, advice = OK, ""

    # 端口上的后端竟然是别的文件夹里的 —— 这种情况最坑：改代码/换版本都像没生效
    code_dir = str(info.get("server_code_dir") or "")
    if code_dir and os.path.normcase(os.path.normpath(code_dir)) != \
            os.path.normcase(os.path.normpath(BASE_DIR)):
        level = WARN
        advice = ("端口上的后端来自另一个文件夹：\n    %s\n"
                  "而你现在这份是：\n    %s\n"
                  "先关掉悬浮窗（或结束对应的 python 进程），再双击本目录的 start.bat。\n"
                  "否则你看到的永远是那个旧实例的画面。" % (code_dir, BASE_DIR))

    if not info.get("connected"):
        if level == OK:
            level = WARN
            advice = ("这个后端还没连上达芬奇 —— 请看上面【达芬奇连接实测】那一项，\n"
                      "按里面的排查清单处理。")
    elif not info.get("course_loaded"):
        if level == OK:
            level = WARN
            tl = str(info.get("timeline_name") or "")
            advice = ("后端连上达芬奇了，但当前时间线没有匹配到课件。\n"
                      "当前时间线：%s\n"
                      "把时间线名改成和课件名一致（带器械前缀更稳，例如\n"
                      "「爬楼机-20min心肺间歇突破攀登」），插件会自动接上。" % (tl or "（空）"))
    elif level == OK:
        lines.append("")
        lines.append("=> 同步链路是通的，悬浮窗应该正常显示。")

    return dict(level=level, title="后端端口 %d" % port, lines=lines, advice=advice)


def check_files():
    """必需项目文件是否齐全（防止只拷贝了一部分文件）。"""
    need = ["server.py", "launcher.py", "overlay.py", "course_data.py",
            "equipment_config.py", "resolve_connection.py", "xlsx_to_json.py",
            "convert_course.py", "probe_resolve.py", "config.json",
            os.path.join("frontend", "overlay.html")]
    missing = [f for f in need if not os.path.exists(os.path.join(BASE_DIR, f))]
    if missing:
        return dict(
            level=BAD, title="项目文件完整性",
            lines=["缺少 %d 个文件：" % len(missing)] + ["          " + m for m in missing],
            advice="文件不完整，请重新完整解压一遍下载的压缩包。")
    return dict(level=OK, title="项目文件完整性",
                lines=["%d 个必需文件都在" % len(need)], advice="")


def check_portable_python():
    """看看项目目录下有没有放便携版 Python（没权限装 Python 时的兜底）。"""
    for name in ("pythonw.exe", "python.exe"):
        p = os.path.join(BASE_DIR, "runtime", "python", name)
        if os.path.isfile(p):
            return dict(level=OK, title="便携版 Python",
                        lines=["已发现：" + p], advice="")
    return dict(
        level=OK, title="便携版 Python",
        lines=["未使用（runtime\\python\\ 下没有 Python）",
               "提示：如果这台电脑装不了 Python（比如没有管理员权限），可以在一台",
               "      装好的电脑上把 Python 整个文件夹拷成 runtime\\python\\，插件优先用它。"],
        advice="")


# ---------------------------------------------------------------- 报告渲染

def run_checks(include_link=True):
    """跑全部检查，返回结果列表。

    include_link=False 用来跳过「达芬奇连接实测」：那一项要起子进程真连一次，
    慢的时候要几秒（极端情况等满 30 秒）。launcher.py 启动前的自检只想知道
    「有没有致命问题」，没必要每次都实测一遍，所以它会传 False。
    """
    checks = [check_python, check_davinci]
    if include_link:
        checks.append(check_resolve_link)
    checks += [check_edge, check_courses, check_port, check_files, check_portable_python]

    results = []
    for fn in checks:
        try:
            results.append(fn())
        except Exception as e:      # 单项炸了也要继续
            results.append(dict(level=WARN, title=fn.__name__,
                                lines=["检查异常：%s: %s" % (type(e).__name__, e)],
                                advice=""))
    return results


def render(results, with_advice=True):
    """把检查结果渲染成给人看的中文报告文本。"""
    line = "=" * 62
    out = [line, "  课程强度同步插件 · 环境体检", line,
           "  项目目录：" + BASE_DIR,
           "  体检时间：" + time.strftime("%Y-%m-%d %H:%M:%S"),
           "  Python  ：" + sys.version.replace("\n", " "),
           ""]

    for i, r in enumerate(results, 1):
        out.append("【%d】%s ... %s" % (i, r["title"], _LEVEL_TAG[r["level"]]))
        for l in r.get("lines") or []:
            out.append("      " + l)
        # 只有真出问题（注意/失败）才附「怎么办」，通过的不啰嗦
        if with_advice and r["level"] != OK and r.get("advice"):
            out.append("      怎么办：")
            for l in r["advice"].split("\n"):
                out.append("        " + l)
        out.append("")

    bad = [r for r in results if r["level"] == BAD]
    warn = [r for r in results if r["level"] == WARN]

    out.append("-" * 62)
    if bad:
        out.append("结论：还不能正常运行，请先解决上面标记 [失败] 的 %d 项。" % len(bad))
        for r in bad:
            out.append("      · " + r["title"])
    elif warn:
        out.append("结论：可以运行。有 %d 项需要注意（不影响启动，但影响效果）。" % len(warn))
        for r in warn:
            out.append("      · " + r["title"])
    else:
        out.append("结论：全部通过，双击 start.bat 就能用了。")
    out.append("-" * 62)
    return "\n".join(out)


def fatal_problems(results):
    """返回致命问题列表（launcher 用它决定要不要拦住启动）。"""
    return [r for r in results if r["level"] == BAD]


def save_report(text):
    """把报告存到 .runtime/ 下，方便用户直接把文件发给别人排查。"""
    try:
        d = os.path.join(BASE_DIR, ".runtime")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "环境体检报告.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p
    except OSError:
        return ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _setup_stdout()

    results = run_checks()

    # 静默模式：只回答「有没有致命问题」，不打印（给脚本判断用）
    if "--quiet" in argv:
        return 1 if fatal_problems(results) else 0

    text = render(results)
    _out(text)
    saved = save_report(text)
    if saved:
        _out("报告已存到：" + saved)
    _out("")
    return 1 if fatal_problems(results) else 0


if __name__ == "__main__":
    sys.exit(main())
