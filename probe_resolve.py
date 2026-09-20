# -*- coding: utf-8 -*-
"""probe_resolve.py —— 真连一次达芬奇，把结果用一行 JSON 吐出来。

为什么单独一个文件、而且要跑在**子进程**里
------------------------------------------------------------
fusionscript 是原生库（.dll）。Python 版本不匹配、达芬奇刚被关掉这类情况下，
它可能直接把进程打崩（access violation）——这种崩溃在 Python 层 try/except 是
拦不住的。所以：

    * 探针自己是一个独立脚本（env_check.py 用 subprocess 起它）；
    * 它就算被打崩，父进程也只是读不到结果，体检报告照样能打完。

它回答的是体检之前答不了的那个问题：**「文件都在、设置也对，那到底连上了没有？」**
连不上的常见原因（免费版限制、改完设置没重启达芬奇、没打开工程…）只有真连一次
才看得出来。

输出协议
------------------------------------------------------------
最后一行固定是：

    <<<PROBE>>>{"ok": true, ...}

JSON 用 ensure_ascii=True 序列化，保证输出**全是 ASCII**：这样不管控制台代码页
是 936 还是 65001、不管 stdout 是控制台还是管道，都不会因为编码把 JSON 弄坏。
父进程 env_check.py 负责解析并渲染成中文报告。

单独调试（直接看人话版）：
    python probe_resolve.py --human
"""

import json
import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

MARK = "<<<PROBE>>>"


def _safe(fn, default=""):
    """调达芬奇 API 时一律套一层：任何异常都退化成 default。"""
    try:
        v = fn()
    except Exception:
        return default
    return default if v is None else v


def probe():
    """跑完所有检查，返回结果字典（不负责输出）。"""
    out = {"ok": False, "connected": False, "error": "", "stage": "start"}
    try:
        import resolve_connection as rc

        # ---- 0) 先把「达芬奇的路径是怎么找到的」摊开 ----
        # 这一步不改任何东西，纯记录：对方机器上两个文件分别在哪儿、找过哪些
        # 目录、有没有用上缓存。连不上时这几行就是最关键的线索 ——
        # 官方那份 DaVinciResolveScript.py 只认
        # "C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
        # 这个写死的路径，装在别的盘就必然连不上。
        out["stage"] = "locate"
        try:
            d = rc.diagnose()
            out["python"] = sys.executable
            out["module_dir"] = d.get("module_dir") or ""
            out["lib"] = d.get("lib") or ""
            out["install_dir"] = d.get("install_dir") or ""
            out["exe"] = d.get("exe") or ""
            out["exe_version"] = d.get("exe_version") or ""
            out["registered_version"] = d.get("registered_version") or ""
            out["candidate_total"] = d.get("candidate_total") or 0
            out["search_report"] = (d.get("search_report") or [])[:12]
            out["cached"] = bool(d.get("cached"))
            out["cache_file"] = d.get("cache_file") or ""
            # config.json 里用户手填了什么、这份文件有没有被正常读进去。
            # 「我明明指定了达芬奇目录」这类抱怨，答案基本都在这几行里。
            out["config_problem"] = d.get("config_problem") or ""
            out["config_notes"] = d.get("config_notes") or []
            out["configured_install_dir"] = d.get("configured_install_dir") or ""
            out["configured_script_lib"] = d.get("configured_script_lib") or ""
            out["configured_script_path"] = d.get("configured_script_path") or ""
        except Exception as e:
            out["locate_error"] = "%s: %s" % (type(e).__name__, e)

        conn = rc.ResolveConnection()
        out["module_dir"] = conn.module_path or out.get("module_dir", "")
        out["lib"] = conn.lib_path or out.get("lib", "")

        # ---- 1) 真连一次（这一步失败就是「悬浮窗一直不同步」最常见的原因）----
        out["stage"] = "connect"
        ok = conn.connect()
        out["connected"] = bool(ok)
        if not ok:
            out["error"] = conn.last_error
            out["stage"] = "connect"
            return out

        # ---- 2) 连上了：把「后端到底看到了什么」全查一遍 ----
        out["stage"] = "query"
        r = conn.resolve
        out["version"] = str(_safe(lambda: r.GetVersionString(), ""))
        out["product"] = str(_safe(lambda: r.GetProductName(), ""))
        out["page"] = str(_safe(lambda: r.GetCurrentPage(), ""))

        pm = _safe(lambda: r.GetProjectManager(), None)
        out["project_manager"] = pm is not None
        proj = _safe(lambda: pm.GetCurrentProject(), None) if pm else None
        out["project"] = str(_safe(lambda: proj.GetName(), "")) if proj else ""
        tl = _safe(lambda: proj.GetCurrentTimeline(), None) if proj else None
        out["timeline"] = str(_safe(lambda: tl.GetName(), "")) if tl else ""
        out["timecode"] = str(_safe(lambda: tl.GetCurrentTimecode(), "")) if tl else ""
        out["timeline_count"] = int(_safe(lambda: len(tl.GetTrackCount("video") or []), 0)) if tl else 0

        # ---- 3) 自检：这条时间线能不能匹配到课件 ----
        # 直接复用 server.py 里的 CourseManager，保证「体检说的匹配结果」和
        # 「插件运行时的匹配结果」是同一套逻辑，不会两边说法不一样。
        out["course_total"] = 0
        out["courses"] = []
        out["match"] = ""
        try:
            import server
            mgr = server.COURSES
            mgr.reload_now()
            names = sorted(mgr._cache.keys())
            out["course_total"] = len(names)
            out["courses"] = names[:8]
            out["data_dir"] = os.path.abspath(mgr.data_dir)
            hit = mgr.find(out["timeline"]) if out["timeline"] else None
            out["match"] = hit.course_name if hit else ""
        except Exception as e:
            out["match_error"] = "%s: %s" % (type(e).__name__, e)

        out["ok"] = True
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        out["trace"] = traceback.format_exc()[-800:]
    return out


def emit(data):
    """把结果按「一行 JSON」写出去 —— 父进程只认这一行。"""
    try:
        line = MARK + json.dumps(data, ensure_ascii=True)
    except Exception:
        line = MARK + '{"ok": false, "connected": false, "error": "json encode failed"}'
    out = sys.stdout
    if out is None:                     # pythonw 下没有 stdout（理论上不会走到）
        return 0
    try:
        out.write(line + "\n")
        out.flush()
    except Exception:
        return 0
    return 0


def human(data):
    """--human：直接给人看的版本（排查时手动跑用）。"""
    print("=" * 60)
    print("达芬奇连接实测")
    print("=" * 60)
    print("Python      :", data.get("python", ""))
    print("脚本模块目录:", data.get("module_dir", "") or "(没找到)")
    print("fusionscript:", data.get("lib", "") or "(没找到)")
    if data.get("install_dir"):
        print("安装目录    :", data["install_dir"])
    if data.get("exe"):
        print("达芬奇主程序:", data["exe"],
              ("v" + str(data["exe_version"])) if data.get("exe_version") else "")
    if data.get("registered_version"):
        print("注册表版本  :", data["registered_version"])
    for key, label in (("configured_install_dir", "配置指定安装目录"),
                       ("configured_script_path", "配置指定模块目录"),
                       ("configured_script_lib", "配置指定 dll")):
        if data.get(key):
            print("%s: %s" % (label, data[key]))
    if data.get("config_problem"):
        print("配置有问题  :", str(data["config_problem"]).replace("\n", " "))
    for _n in (data.get("config_notes") or []):
        print("配置提醒    :", str(_n).replace("\n", " "))
    if data.get("search_report"):
        print("找过这些地方（[有] = 该目录里确实有这个文件）:")
        for l in data["search_report"]:
            print("   ", l.strip())
    print("-" * 60)
    if not data.get("connected"):
        print("连接结果: 失败")
        print("原因    :", data.get("error", ""))
        return
    print("连接结果: 成功")
    print("达芬奇    :", data.get("product", "") or "(未知)", data.get("version", ""))
    print("当前工程  :", data.get("project", "") or "(没有打开的工程)")
    print("当前页面  :", data.get("page", ""))
    print("当前时间线:", data.get("timeline", "") or "(没有打开/选中的时间线)")
    print("播放头    :", data.get("timecode", ""))
    print("课件数    :", data.get("course_total", 0))
    print("匹配课件  :", data.get("match", "") or "(没匹配上)")
    if data.get("courses"):
        print("课件名示例:", "、".join(data["courses"]))
    if data.get("match_error"):
        print("匹配自检出错:", data["match_error"])


if __name__ == "__main__":
    data = probe()
    if "--human" in sys.argv[1:]:
        human(data)          # 手动排查时看人话
    else:
        emit(data)           # 给 env_check.py 解析：固定一行 <<<PROBE>>>{json}
    sys.exit(0)
