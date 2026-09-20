# -*- coding: utf-8 -*-
"""config.json 的容错读取。

为什么值得单独一个模块
------------------------------------------------------------
config.json 是**用户唯一需要手改的文件**，而 Windows 上「用记事本改 JSON」
有一堆踩坑方式。实测（每一种都真的会让插件歇菜）：

1. 记事本「另存为 UTF-8」→ 文件开头带 BOM（`EF BB BF`）→ json 直接报
   ``Unexpected UTF-8 BOM``。旧版 server.py 的 load_config 没有 try/except，
   **后端在 import 阶段就崩**，用户只看到「后端启动失败」。
2. 记事本「另存为 ANSI」→ 文件其实是 GBK → 按 UTF-8 解码
   ``UnicodeDecodeError``。同上，后端崩。
3. 路径按 Windows 习惯写成**单反斜杠** ``"D:\\软件\\达芬奇"`` → json 报
   ``Invalid \\escape``（实测：``\\D`` ``\\u`` ``\\n`` 一律报错，不存在宽容放过）。
   server.py 崩；而 resolve_connection 那份是 try/except 静默吞掉 →
   **用户以为自己指定了达芬奇目录，其实整份配置都被丢弃了**，连端口都回默认值。
4. 多写一个尾逗号、写 ``//`` 注释 → json 报错，同上。

更阴险的是「看起来能跑但其实错了」：路径里如果出现 ``\\t`` ``\\n`` ``\\b``
这类**合法**转义（``D:\\tools``、``D:\\new``、``D:\\bin``），json 不会报错，
而是悄悄把它换成一个制表符/换行符，路径就永远对不上了。这一条连「解析成功」
都骗得过，所以下面专门做了一次逆变换。

策略
------------------------------------------------------------
**先严格解析；失败就逐条修复后重试；修复成功也明确告诉用户「我替你修了什么」**，
而不是默默吞掉。全都修不好时，返回一条人话错误（带行号），调用方降级成
默认配置继续跑 —— 绝不因为一个配置文件把整个插件搞死。

对外只有一个入口：``load_config_file()`` → ``(data, problem)``。
``problem`` 为空串表示一切正常。
"""

import json
import os
import re

# 会被当成「路径」处理的配置键：这些值里的控制字符要还原成字面反斜杠，
# 而且大小写/斜杠写法要宽容。
PATH_KEYS = ("resolve_install_dir", "resolve_script_path", "resolve_script_lib",
             "data_dir")

_HEX = set("0123456789abcdefABCDEF")

# 合法 JSON 转义里的「字母」
_LEGAL_ESC_LETTERS = set('"\\/bfnrt')

# 控制字符 → 用户在 JSON 里本来想写的两个字符（逆变换，见上面第 4 点）
_CTRL_BACK = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


# ---------------------------------------------------------------- 解码

def read_text(path):
    """把文件读成文本。返回 (文本, 编码说明, 问题说明)。

    编码按「UTF-8(带BOM) → UTF-8 → GB18030(覆盖 GBK/ANSI 中文)」逐级退让。
    """
    with open(path, "rb") as f:
        raw = f.read()

    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "UTF-8（带 BOM）", \
                "config.json 是「带 BOM 的 UTF-8」——Windows 记事本默认就这么存。" \
                "已经自动兼容，不影响使用。"
        except UnicodeDecodeError:
            pass

    try:
        return raw.decode("utf-8"), "UTF-8", ""
    except UnicodeDecodeError:
        pass

    try:
        text = raw.decode("gb18030")
    except UnicodeDecodeError:
        # 连 GB18030 都读不了：按 UTF-8 强解，坏字符替换掉，至少别把程序搞崩
        return raw.decode("utf-8", "replace"), "未知编码", \
            "config.json 的编码无法识别（既不是 UTF-8 也不是 ANSI）。" \
            "已勉强按 UTF-8 读取，里面若有中文可能已经损坏，建议重新填写。"

    return text, "ANSI/GBK", \
        "config.json 是 ANSI(GBK) 编码——记事本「另存为」时选了 ANSI 就会这样。" \
        "已经自动按 GBK 读出来了，不影响使用。"


# ---------------------------------------------------------------- 修复

def repair_json_text(text):
    """尽最大努力把「不像标准 JSON 但人看着对」的文本修成标准 JSON。

    返回 (新文本, 修复说明列表)。只处理三类最常见的手写错误：
      · 字符串里的非法反斜杠（Windows 路径）→ 补成字面反斜杠
      · 注释 // 和 /* */  → 删掉
      · 多余尾逗号 → 删掉
    """
    out = []
    fixes = []
    i = 0
    n = len(text)
    in_str = False
    escaped_bs = 0

    while i < n:
        c = text[i]

        if not in_str:
            # --- 注释 ---
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                j = text.find("\n", i)
                i = n if j < 0 else j
                if "行内注释" not in fixes:
                    fixes.append("行内注释（//）")
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                if "块注释（/* */）" not in fixes:
                    fixes.append("块注释（/* */）")
                continue

            if c == '"':
                in_str = True
                out.append(c)
                i += 1
                continue

            # --- 尾逗号：跳过空白后紧跟 } 或 ] ---
            if c == ",":
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] in "}]":
                    i += 1                      # 丢掉这个逗号
                    if "多余的尾逗号" not in fixes:
                        fixes.append("多余的尾逗号")
                    continue

            out.append(c)
            i += 1
            continue

        # ---------------- 字符串内部 ----------------
        if c == "\\":
            nxt = text[i + 1] if i + 1 < n else ""

            if nxt == "u":
                hx = text[i + 2:i + 6]
                if len(hx) == 4 and all(h in _HEX for h in hx):
                    out.append(text[i:i + 6])
                    i += 6
                    continue
                # \u 后面不是 4 位十六进制：几乎一定是路径里的 \users 之类
                out.append("\\\\")
                escaped_bs += 1
                i += 1
                continue

            if nxt in _LEGAL_ESC_LETTERS:
                out.append(text[i:i + 2])
                i += 2
                continue

            # \D \软件 这类非法转义 → 补一个反斜杠，保持字面意思
            out.append("\\\\")
            escaped_bs += 1
            i += 1
            continue

        if c == '"':
            in_str = False
            out.append(c)
            i += 1
            continue

        out.append(c)
        i += 1

    if escaped_bs:
        fixes.append("路径里没写够的反斜杠（Windows 路径要用双反斜杠 \\\\ 或正斜杠 /）")

    return "".join(out), fixes


def _line_of(text, pos):
    return text.count("\n", 0, max(0, pos)) + 1


def repair_path_values(data):
    """把路径值里被 JSON 悄悄转义掉的字符还原。

    ``"D:\\tools\\x"``（单反斜杠）在 JSON 里是**合法**的（``\\t`` 是制表符），
    所以解析不会报错，但值是 ``D:<TAB>ools\\x`` —— 拿去 isfile 必然找不到。
    这里做逆变换：制表符 → 字面 ``\\t``。
    """
    fixed = []
    for k in PATH_KEYS:
        v = data.get(k)
        if not isinstance(v, str):
            continue
        if not any(ch in v for ch in _CTRL_BACK):
            continue
        new = v
        for ch, back in _CTRL_BACK.items():
            new = new.replace(ch, back)
        data[k] = new
        fixed.append(k)
    return fixed


def normalize_path_value(v):
    """把用户在配置里写的路径整理成可直接用的形式。

    允许的写法：正斜杠、双反斜杠、带引号、带前后空格、末尾带反斜杠。
    """
    if not isinstance(v, str):
        return v
    s = v.strip().strip('"').strip("'").strip()
    if not s:
        return ""
    # 剩下的裸反斜杠（非转义场景，比如从资源管理器复制来的）保持原样
    return s


# ---------------------------------------------------------------- 对外入口

def load_config_file(path, default=None):
    """读配置文件。返回 (data, problem, notes)。

    problem : 空串 = 配置正常生效；非空 = **配置被丢弃了**，一句话说明原因。
              这类问题会顶到悬浮窗状态栏（因为它会让用户「改了配置却毫无反应」）。
    notes   : 已经自动处理好、不影响使用的小提醒（BOM / ANSI 编码 / 自动修复语法 /
              还原被转义的路径）。只进体检报告与控制台，不打扰用户。
    """
    notes = []

    if not path or not os.path.isfile(path):
        return dict(default or {}), "", notes

    try:
        text, _enc, enc_note = read_text(path)
    except OSError as e:
        return dict(default or {}), "config.json 读不出来（%s），已改用默认设置。" % e, notes

    if enc_note:
        notes.append(enc_note)

    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        # 严格解析失败 → 修一遍再试
        repaired, fixes = repair_json_text(text)
        try:
            parsed = json.loads(repaired)
        except Exception as e2:
            where = ""
            m = re.search(r"line (\d+)", str(e2))
            if m:
                where = "（第 %s 行）" % m.group(1)
            return dict(default or {}), (
                "config.json 有语法错误%s，已改用默认设置"
                "（所以你在里面写的达芬奇路径这次没生效）。"
                % where
            ), notes
        notes.append(
            "config.json 不是标准 JSON，已自动修好再读：%s。"
            "建议按标准写法改回去（路径用正斜杠 / 或双反斜杠 \\\\）。"
            % "、".join(fixes)
        )

    if not isinstance(parsed, dict):
        return dict(default or {}), (
            "config.json 的内容不是一个配置对象（应该用 { } 包起来），"
            "已改用默认设置。"
        ), notes

    data = dict(parsed)

    # 路径值：还原被 JSON 吃掉的转义 + 整理写法
    ctrl_fixed = repair_path_values(data)
    if ctrl_fixed:
        notes.append(
            "config.json 里的路径含有被 JSON 误转义的字符（%s），已自动还原。"
            "路径中的反斜杠请写两个（\\\\）或直接用正斜杠（/）。"
            % "、".join(ctrl_fixed)
        )
    for k in PATH_KEYS:
        if isinstance(data.get(k), str):
            data[k] = normalize_path_value(data[k])

    return data, "", notes
