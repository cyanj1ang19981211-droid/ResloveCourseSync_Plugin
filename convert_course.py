# -*- coding: utf-8 -*-
"""
convert_course.py —— 课件转换的「图形化」入口（双击 convert.bat 时调用）。

流程：
    1. 弹出 Windows 原生的「打开文件」对话框，让用户挑课件表格（**支持多选**：
       按住 Ctrl 点选多个文件，或框选一批；也可以直接框选整个文件夹里的表格）；
    2. 逐个调用 xlsx_to_json.convert_file() 转成 data/*.json；
    3. 控制台打印结果；出错时弹系统提示框，避免新手不知道哪里错了。

为什么要支持多选：
    现在课件是「一节课一个 xlsx」（例如 20min舒缓解压轻氧攀登.xlsx、
    35min变速循环燃脂攀登.xlsx …），一次要导好几节。老版本一次只能选一个，
    选完还要重新双击 convert.bat，很烦。

为什么路径要绕一圈临时文件：
    cmd 控制台默认是 GBK 代码页，中文路径经命令行传递容易乱码。
    这里让 PowerShell 把选中的路径按 UTF-8 写进临时文件（一行一个），
    再由 Python（UTF-8）读取，全程不经过 cmd 的编码转换。

也支持把 xlsx 直接拖到 convert.bat 上（可以一次拖多个）：此时参数会透传进来，
跳过文件选择框。
"""

import base64
import ctypes
import os
import subprocess
import sys
import tempfile

# 确保脚本所在目录在 sys.path（兼容 embedded Python 的 ._pth 机制不自动加脚本目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xlsx_to_json import convert_file  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RUNTIME_DIR = os.path.join(BASE_DIR, ".runtime")

# 记住上次选文件的目录：课件常在网络盘（\\xxx\课程课件）里，每次重新点一遍很烦
LAST_DIR_FILE = os.path.join(RUNTIME_DIR, "last_convert_dir.txt")

# 没有图形界面时（极端环境下）退回转换的默认课件
DEFAULT_XLSX = os.path.join(os.path.expanduser("~"), "Desktop", "冠军课程课件.xlsx")


# ---------- 弹窗提示 ----------

def message_box(text, title="课件转换", warn=False):
    """弹一个系统提示框（出错时用，普通用户不用会去看控制台）。

    用 MessageBoxW（宽字符版），中文不会乱码。
    """
    try:
        MB_ICONWARNING = 0x30
        MB_ICONINFORMATION = 0x40
        ctypes.windll.user32.MessageBoxW(
            None, str(text), str(title),
            (MB_ICONWARNING if warn else MB_ICONINFORMATION) | 0x40000  # MB_TOPMOST
        )
    except Exception:
        # 连弹窗都不行就算了，控制台里已经有输出
        pass


# ---------- 「上次用过的目录」记忆 ----------

def _read_last_dir():
    """读上次选文件的目录；没有/已失效返回空串。"""
    try:
        with open(LAST_DIR_FILE, "r", encoding="utf-8") as f:
            d = f.read().strip()
        if d and os.path.isdir(d):
            return d
    except OSError:
        pass
    return ""


def _write_last_dir(path):
    """记住这次选的文件所在目录（下次对话框直接开在这里）。"""
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(LAST_DIR_FILE, "w", encoding="utf-8") as f:
            f.write(os.path.dirname(os.path.abspath(path)))
    except OSError:
        pass


# ---------- 文件选择框 ----------

# 用 -EncodedCommand（base64 UTF-16LE）传脚本，彻底绕开 PowerShell 对中文脚本的
# 编码猜测问题 —— 直接写 .ps1 文件在非 UTF-8 BOM 时中文会乱码。
_PS_PICK = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'

$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = '选择课程课件表格（可按住 Ctrl / Shift 一次选多个）'
$dlg.Filter = 'Excel 课件 (*.xlsx;*.xlsm)|*.xlsx;*.xlsm|所有文件 (*.*)|*.*'
$dlg.CheckFileExists = $true
$dlg.Multiselect = $true
$dlg.RestoreDirectory = $true

$init = $env:RESOLVE_PICK_DIR
if ([string]::IsNullOrWhiteSpace($init) -or -not (Test-Path -LiteralPath $init)) {
    $init = [Environment]::GetFolderPath('Desktop')
}
if (Test-Path -LiteralPath $init) { $dlg.InitialDirectory = $init }

$result = $dlg.ShowDialog($owner)
$owner.Dispose()

if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [System.IO.File]::WriteAllLines(
        $env:RESOLVE_PICK_OUT,
        [string[]]$dlg.FileNames,
        (New-Object System.Text.UTF8Encoding($false))
    )
}
"""


def _pick_with_powershell():
    """用 PowerShell 的 OpenFileDialog 多选文件。

    返回：文件列表（list）；用户取消 -> None；PowerShell 不可用 -> ""。
    """
    out_file = os.path.join(tempfile.gettempdir(), "resolve_course_pick.txt")
    try:
        if os.path.exists(out_file):
            os.remove(out_file)
    except OSError:
        pass

    encoded = base64.b64encode(_PS_PICK.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ)
    env["RESOLVE_PICK_OUT"] = out_file
    env["RESOLVE_PICK_DIR"] = _read_last_dir()

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
            env=env,
            timeout=300,
            creationflags=0x08000000,  # CREATE_NO_WINDOW：不弹多余的 PowerShell 黑框
        )
    except FileNotFoundError:
        return ""          # 没有 powershell，让调用方走别的方案
    except Exception:
        return ""

    if os.path.exists(out_file):
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                paths = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return ""
        paths = [p for p in paths if os.path.exists(p)]
        return paths or None
    return None            # PowerShell 跑通了，但用户点了取消


def _pick_with_tkinter():
    """PowerShell 不可用时的备选：用 tkinter 的原生文件对话框（同样支持多选）。

    返回：文件列表；用户取消 -> None；tkinter 不可用 -> ""。
    """
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return ""

    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial = _read_last_dir() or os.path.expanduser("~/Desktop")
        picked = filedialog.askopenfilenames(
            title="选择课程课件表格（可一次选多个）",
            initialdir=initial if os.path.isdir(initial) else os.path.expanduser("~"),
            filetypes=[("Excel 课件", "*.xlsx *.xlsm"), ("所有文件", "*.*")],
        )
        root.destroy()
    except Exception:
        return ""

    return list(picked) or None


def pick_course_files():
    """弹出文件选择框（可多选），返回文件路径列表；用户取消返回 None。"""
    for picker in (_pick_with_powershell, _pick_with_tkinter):
        result = picker()
        if result != "":
            return result
    return ""  # 所有图形方案都不可用


# ---------- 主流程 ----------

def main():
    args = [a for a in sys.argv[1:] if a and a.strip()]

    print("=" * 52)
    print("  课件数据转换（xlsx -> JSON）")
    print("=" * 52)
    print()

    if args:
        # 拖拽进来的文件：直接用，不弹框（支持一次拖多个）
        xlsx_paths = args
        print(f"待转换文件（{len(xlsx_paths)} 个）：")
        for p in xlsx_paths:
            print(f"  {p}")
    else:
        print("请在弹出的窗口里选择课件表格（可按住 Ctrl / Shift 多选）…")
        picked = pick_course_files()

        if picked is None:
            print()
            print("已取消：没有选择文件，未做任何修改。")
            return 0

        if picked == "":
            # 图形界面不可用 → 退回桌面默认课件，保持老习惯能用
            if os.path.exists(DEFAULT_XLSX):
                xlsx_paths = [DEFAULT_XLSX]
                print(f"（图形界面不可用）改用桌面默认课件：{DEFAULT_XLSX}")
            else:
                print()
                print("[错误] 无法弹出文件选择框，也没找到桌面上的「冠军课程课件.xlsx」。")
                message_box(
                    "无法弹出文件选择框，也没有找到桌面上的「冠军课程课件.xlsx」。\n\n"
                    "请把课件表格放到桌面并改名为「冠军课程课件.xlsx」，\n"
                    "或者把表格直接拖到 convert.bat 上。",
                    warn=True,
                )
                return 1
        else:
            xlsx_paths = picked
            print(f"已选择 {len(xlsx_paths)} 个文件：")
            for p in xlsx_paths:
                print(f"  {p}")

    # 记住这次选的目录，下次打开对话框直接定位过去
    _write_last_dir(xlsx_paths[0])

    print()
    print(f"输出目录：{DATA_DIR}")
    print("-" * 52)

    total_ok = 0
    total_outputs = []
    failed = []      # [(文件, 原因)]
    skipped = []     # [(文件/工作表, 原因)]
    for idx, xlsx_path in enumerate(xlsx_paths, 1):
        print()
        print(f"[{idx}/{len(xlsx_paths)}] {os.path.basename(xlsx_path)}")
        try:
            result = convert_file(xlsx_path, out_dir=DATA_DIR)
        except ValueError as e:
            print(f"  [错误] {e}")
            failed.append((xlsx_path, str(e)))
            continue
        total_ok += result["ok"]
        total_outputs.extend(result["outputs"])
        for name, reason in result["skipped"]:
            skipped.append((f"{os.path.basename(xlsx_path)} / {name}", reason))

    print()
    print("-" * 52)
    print()
    print(f"全部完成：{len(xlsx_paths)} 个文件，共成功转换 {total_ok} 个课件。")
    if total_outputs:
        print(f"输出到：{DATA_DIR}")
        for name in total_outputs:
            print(f"  - {name}")
    if skipped:
        print(f"跳过 {len(skipped)} 个工作表：")
        for name, reason in skipped:
            print(f"  - {name}：{reason}")
    if failed:
        print(f"失败 {len(failed)} 个文件：")
        for path, reason in failed:
            print(f"  - {os.path.basename(path)}：{reason}")

    # 全部失败 / 一个课件都没转出来才弹框报警；部分成功只在控制台说明
    if total_ok == 0:
        message_box(
            "没有转换出任何课程数据。\n\n"
            + "\n".join(f"- {os.path.basename(p)}：{r}" for p, r in failed[:5])
            + ("\n…" if len(failed) > 5 else "")
            + "\n\n请确认选择的表格里含有课件工作表"
              "（表头应包含「环节」或「动作名称」）。",
            title="课件转换失败",
            warn=True,
        )
        return 1

    if failed:
        message_box(
            f"成功转换 {total_ok} 个课件，但有 {len(failed)} 个文件转换失败：\n\n"
            + "\n".join(f"- {os.path.basename(p)}" for p, _ in failed[:5])
            + ("\n…" if len(failed) > 5 else "")
            + "\n\n详细原因见转换窗口里的提示。",
            title="部分文件转换失败",
            warn=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
