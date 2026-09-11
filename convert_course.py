# -*- coding: utf-8 -*-
"""
convert_course.py —— 课件转换的「图形化」入口（双击 convert.bat 时调用）。

流程：
    1. 弹出 Windows 原生的「打开文件」对话框，让用户挑课件表格（默认定位到桌面）；
    2. 调用 xlsx_to_json.convert_file() 把课件里所有工作表转成 data/*.json；
    3. 控制台打印结果；出错时弹系统提示框，避免新手不知道哪里错了。

为什么路径要绕一圈临时文件：
    cmd 控制台默认是 GBK 代码页，中文路径经命令行传递容易乱码。
    这里让 PowerShell 把选中的路径按 UTF-8 写进临时文件，再由 Python（UTF-8）
    读取，全程不经过 cmd 的编码转换。

也支持把 xlsx 直接拖到 convert.bat 上：此时参数会透传进来，跳过文件选择框。
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
$dlg.Title = '选择课程课件表格'
$dlg.Filter = 'Excel 课件 (*.xlsx;*.xlsm)|*.xlsx;*.xlsm|所有文件 (*.*)|*.*'
$dlg.CheckFileExists = $true
$dlg.Multiselect = $false
$dlg.RestoreDirectory = $true
$desktop = [Environment]::GetFolderPath('Desktop')
if (Test-Path $desktop) { $dlg.InitialDirectory = $desktop }

$result = $dlg.ShowDialog($owner)
$owner.Dispose()

if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [System.IO.File]::WriteAllText(
        $env:RESOLVE_PICK_OUT,
        $dlg.FileName,
        (New-Object System.Text.UTF8Encoding($false))
    )
}
"""


def _pick_with_powershell():
    """用 PowerShell 的 OpenFileDialog 选文件。返回路径；取消返回 None；不可用返回 ""。"""
    out_file = os.path.join(tempfile.gettempdir(), "resolve_course_pick.txt")
    try:
        if os.path.exists(out_file):
            os.remove(out_file)
    except OSError:
        pass

    encoded = base64.b64encode(_PS_PICK.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ)
    env["RESOLVE_PICK_OUT"] = out_file

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
                path = f.read().strip()
        except OSError:
            return ""
        if path and os.path.exists(path):
            return path
    return None            # PowerShell 跑通了，但用户点了取消


def _pick_with_tkinter():
    """PowerShell 不可用时的备选：用 tkinter 的原生文件对话框。不可用返回 ""。"""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return ""

    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial = os.path.expanduser("~/Desktop")
        path = filedialog.askopenfilename(
            title="选择课程课件表格",
            initialdir=initial if os.path.isdir(initial) else os.path.expanduser("~"),
            filetypes=[("Excel 课件", "*.xlsx *.xlsm"), ("所有文件", "*.*")],
        )
        root.destroy()
    except Exception:
        return ""

    return path or None


def pick_course_file():
    """弹出文件选择框，返回选中路径；用户取消返回 None。"""
    for picker in (_pick_with_powershell, _pick_with_tkinter):
        result = picker()
        if result != "":
            return result
    return ""  # 所有图形方案都不可用


# ---------- 主流程 ----------

def main():
    args = [a for a in sys.argv[1:] if a and a.strip()]

    print("=" * 46)
    print("  课件数据转换（xlsx -> JSON）")
    print("=" * 46)
    print()

    if args:
        # 拖拽进来的文件：直接用，不弹框
        xlsx_path = args[0]
        print(f"待转换文件：{xlsx_path}")
    else:
        print("请在弹出的窗口里选择课件表格…")
        xlsx_path = pick_course_file()

        if xlsx_path is None:
            print()
            print("已取消：没有选择文件，未做任何修改。")
            return 0

        if xlsx_path == "":
            # 图形界面不可用 → 退回桌面默认课件，保持老习惯能用
            if os.path.exists(DEFAULT_XLSX):
                xlsx_path = DEFAULT_XLSX
                print(f"（图形界面不可用）改用桌面默认课件：{xlsx_path}")
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

        print(f"已选择：{xlsx_path}")

    print()
    print(f"输出目录：{DATA_DIR}")
    print("-" * 46)

    try:
        result = convert_file(xlsx_path, out_dir=DATA_DIR)
    except ValueError as e:
        print()
        print(f"[错误] {e}")
        message_box(f"转换失败：\n\n{e}", title="课件转换失败", warn=True)
        return 1

    print("-" * 46)
    print()
    print(f"转换完成：成功 {result['ok']} 个课件，输出到 data 目录。")
    if result["skipped"]:
        print(f"跳过 {len(result['skipped'])} 个：")
        for name, reason in result["skipped"]:
            print(f"  - {name}：{reason}")

    if result["ok"] == 0:
        message_box(
            "没有转换出任何课程数据。\n\n"
            "请确认选择的表格里含有课件工作表（表头应包含「环节」或「动作名称」）。",
            title="课件转换失败",
            warn=True,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
