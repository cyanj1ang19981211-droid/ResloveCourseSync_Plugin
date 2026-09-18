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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

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


def check_davinci():
    """达芬奇本体：脚本模块、fusionscript.dll、进程是否在跑。"""
    lines = []
    level = OK
    advice = ""

    try:
        import resolve_connection as rc
        mod_dir = rc._find_module_dir()
        lib = rc._find_fusionscript_lib()
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
        advice = ("没有找到达芬奇的脚本接口文件，通常是没装达芬奇造成的。\n"
                  "装好达芬奇后，还要在达芬奇里开启脚本功能：\n"
                  "偏好设置 -> 系统 -> 常规 -> External scripting using 设为 Local。")

    if lib:
        lines.append("运行库　：已找到 fusionscript.dll")
    else:
        if level != BAD:
            level = BAD
        lines.append("运行库　：未找到 fusionscript.dll")

    # 进程是否在跑（没开达芬奇也能用插件，只是没数据）
    out = _run_quiet(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"])
    running = "Resolve.exe" in out
    lines.append("运行状态：" + ("达芬奇正在运行" if running else "达芬奇当前没开（插件会等它启动）"))
    if not running and level == OK:
        level = WARN
        advice = ("达芬奇现在没开 —— 这不影响启动插件，但悬浮窗会先显示等待状态。\n"
                  "打开达芬奇并载入时间线后会自动接上。")

    return dict(level=level, title="达芬奇（DaVinci Resolve）", lines=lines, advice=advice)


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


def check_port():
    """后端监听端口是否被别的程序占着。"""
    port = 8765
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            port = int(json.load(f).get("port") or port)
    except Exception:
        pass

    busy = False
    try:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            busy = True
        except OSError:
            busy = False
        finally:
            s.close()
    except Exception:
        pass

    if busy:
        return dict(
            level=WARN, title="后端端口 %d" % port,
            lines=["端口已被占用"],
            advice=("如果插件此刻正在运行，这是正常的（说明旧实例还开着）。\n"
                    "如果确认没开插件却占着端口，关掉占用它的程序，或改 config.json 里的端口。"))
    return dict(level=OK, title="后端端口 %d" % port, lines=["端口空闲"], advice="")


def check_files():
    """必需项目文件是否齐全（防止只拷贝了一部分文件）。"""
    need = ["server.py", "launcher.py", "overlay.py", "course_data.py",
            "equipment_config.py", "resolve_connection.py", "xlsx_to_json.py",
            "convert_course.py", "config.json",
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


CHECKS = (check_python, check_davinci, check_edge, check_courses,
          check_port, check_files, check_portable_python)


# ---------------------------------------------------------------- 报告渲染

def run_checks():
    """跑全部检查，返回结果列表。"""
    results = []
    for fn in CHECKS:
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
