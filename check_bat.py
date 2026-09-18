# -*- coding: utf-8 -*-
"""
check_bat.py —— bat 文件体检（防回归守卫）。

背景（踩过的坑）
----------------
cmd.exe 是**按控制台代码页**去解码 .bat 文件的。只要文件里混进了 GBK 字节，
在代码页不是 936 的机器上（典型情况：系统开了「Beta: 使用 Unicode UTF-8 提供
全球语言支持」→ 代码页 65001）就会被逐字节拆错：一个汉字被劈成两半、
后面的 ASCII 字符被当成汉字的第二字节吞掉，于是 `rem` 注释漏出来当命令执行、
`if (...)` 块的括号配不上。现象就是满屏

    'xxx' 不是内部或外部命令，也不是可运行的程序或批处理文件。

而且更麻烦的是：**同一份文件在中文 Windows 上是好的**，只有在别人机器上才炸。

约定
----
所有 .bat 只允许出现 ASCII 字符，中文一律交给 Python 打印
（Python 3.6+ 在 Windows 上写控制台走 WriteConsoleW，和代码页无关，不会乱码）。
换行统一 CRLF（LF-only 的 bat 在 goto 标签等场景下会有诡异行为）。

改完 bat 请跑一下：

    python check_bat.py

退出码 0 = 全部合格；1 = 有问题，具体哪一行会打印出来。
"""

import glob
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 这几个标签是 cmd 自带的，不需要在本文件里定义
BUILTIN_LABELS = {"eof"}

_LABEL_RE = re.compile(r"^\s*:([A-Za-z0-9_.\-]+)\s*$")
_GOTO_RE = re.compile(r"\bgoto\s+:?([A-Za-z0-9_.\-]+)", re.IGNORECASE)
_CALL_LABEL_RE = re.compile(r"\bcall\s+:([A-Za-z0-9_.\-]+)", re.IGNORECASE)
_CALL_FILE_RE = re.compile(r'\bcall\s+"?([^"\s>|&]+\.bat)"?', re.IGNORECASE)


def _line_of(text, pos):
    """字节偏移 -> 行号（从 1 开始）。"""
    return text.count("\n", 0, pos) + 1


def check_file(path):
    """检查单个 bat，返回问题列表（每条是一句人话）。"""
    name = os.path.basename(path)
    problems = []
    raw = open(path, "rb").read()

    # ---- 1) 纯 ASCII ----
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("带 UTF-8 BOM 开头 —— cmd 会把 BOM 当命令的一部分，必须去掉")
        raw = raw[3:]

    bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    if bad:
        # 尽量还原出犯规的内容，方便定位
        try:
            text = raw.decode("gbk", errors="replace")
            enc_hint = "GBK"
        except Exception:
            text = raw.decode("latin-1")
            enc_hint = "非 ASCII"
        pos = bad[0][0]
        snippet = text[max(0, pos - 25):pos + 25].replace("\r", "").replace("\n", "\\n")
        problems.append(
            "第 %d 行有非 ASCII 字节（共 %d 处，疑似 %s 编码）：...%s..."
            % (_line_of(text, pos), len(bad), enc_hint, snippet))
        problems.append("   -> 中文一律别写在 .bat 里，交给 Python 打印（见文件头注释）")

    # ---- 2) 换行必须是 CRLF ----
    lf_total = raw.count(b"\n")
    crlf = raw.count(b"\r\n")
    lone_lf = lf_total - crlf
    if lf_total and lone_lf:
        problems.append("有 %d 行是裸 LF 换行 —— bat 必须用 CRLF" % lone_lf)

    # ---- 3) 控制流完整性（只在 ASCII 前提成立时才有意义）----
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")

    labels = set()
    for line in text.splitlines():
        m = _LABEL_RE.match(line)
        if m:
            labels.add(m.group(1).lower())

    for m in _GOTO_RE.finditer(text):
        tgt = m.group(1).lower()
        if tgt in BUILTIN_LABELS or tgt in labels:
            continue
        problems.append("第 %d 行 goto :%s —— 没有这个标签"
                        % (_line_of(text, m.start()), m.group(1)))

    for m in _CALL_LABEL_RE.finditer(text):
        tgt = m.group(1).lower()
        if tgt in labels:
            continue
        problems.append("第 %d 行 call :%s —— 没有这个标签"
                        % (_line_of(text, m.start()), m.group(1)))

    for m in _CALL_FILE_RE.finditer(text):
        ref = m.group(1)
        # %~dp0 是「本 bat 所在目录」，在这里就是项目目录，先展开再判断。
        # 注意用 lambda 做替换：Windows 路径里的 \U \1 之类会被 re 当成转义。
        ref_expanded = re.sub(r"%~dp0", lambda _m: BASE_DIR + os.sep,
                              ref, flags=re.IGNORECASE)
        if not os.path.isfile(ref_expanded):
            problems.append("第 %d 行 call %s —— 文件不存在"
                            % (_line_of(text, m.start()), ref))

    # ---- 4) 基本卫生 ----
    first = text.splitlines()[0].strip().lower() if text.splitlines() else ""
    if not first.startswith("@echo off"):
        problems.append("第一行应该是 @echo off（否则命令会被逐条回显）")

    if not problems:
        print("  %-18s [通过]  %d 字节，%d 行，%d 个标签"
              % (name, len(raw), crlf, len(labels)))
    else:
        print("  %-18s [失败]" % name)
        for p in problems:
            print("      - " + p)
    return problems


def main():
    if sys.stdout is not None and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    files = sorted(glob.glob(os.path.join(BASE_DIR, "*.bat")))
    print("=" * 62)
    print("  bat 文件体检（只允许 ASCII + CRLF，详见文件头注释）")
    print("=" * 62)
    if not files:
        print("  没找到任何 .bat 文件")
        return 1

    all_problems = []
    for f in files:
        all_problems += check_file(f)

    print("-" * 62)
    if all_problems:
        print("结论：发现 %d 个问题，请修好再提交。" % len(all_problems))
        return 1
    print("结论：%d 个 bat 文件全部合格。" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
