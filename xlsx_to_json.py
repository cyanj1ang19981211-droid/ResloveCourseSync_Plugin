# -*- coding: utf-8 -*-
"""
xlsx_to_json.py —— 把「冠军课程课件.xlsx」里的课件表转换成插件可读的课程 JSON。

用法：
    python xlsx_to_json.py <xlsx路径> [工作表名]
      - 指定工作表名：只转换该表，输出到 data/<课程名>.json
      - 省略工作表名：批量转换所有表

依赖：标准库（zipfile + xml.etree），无需 openpyxl。
（openpyxl 在解析该 xlsx 的样式时存在兼容问题，故直接用底层 XML 解析，更稳健。）

字段映射（xlsx 列 -> 插件 JSON）——**按表头文字识别，不再写死列号**：
    表头含「环节」/「阶段」         -> segment（环节名，空则继承上一个非空环节）
    表头含「动作」                 -> action
    表头含「关键词」/「标签」      -> keyword（强度标签）
    表头含「速度/踏频/桨频/转速/频率/步频」 -> 主指标（跑步机/爬楼机 speed、单车/椭圆机 rpm、划船机 spm）
    表头含「阻力/坡度/档位」       -> 副指标（跑步机 incline、其余 resistance）
    表头含「持续时间」/「时长」    -> 时长（分钟；若只有一个「持续时间」列且是
                                      Excel 天分数，会自动 ×24 换算成分钟）
    表头含「距离」/「里程」        -> distance（米 -> 公里）

器械主/副指标映射：
    跑步机 D->speed(km/h)  E->incline(%)  + 自动换算 pace(min/km)
    爬楼机 D->speed(级)    E->resistance(级)
    单车   D->rpm(rpm)     E->resistance(级)
    划船机 D->spm(spm)     E->resistance(级)
    椭圆机 D->rpm(rpm)     E->resistance(级)

为什么改成「按表头识别」：
    团队现在有两种课件表格——
      (1) 老的总表：一个 xlsx 里很多工作表，表头在第 3 行，
          列序是 A环节 B动作名称 C关键词 D速度 E阻力/坡度 F持续时间(天) G持续时间(分钟) H距离；
      (2) 新的单课表：一节课一个 xlsx（工作表常叫 Sheet1），表头在第 1 行，
          列序变成 A环节 B动作名称 C关键词 D速度 E阻力/坡度 F持续时间 G距离 H消耗。
    按列号写死会把 (2) 的「距离」当成「时长」（300 米 -> 300 分钟），所以改成看表头。
"""

import difflib
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

# 确保脚本所在目录在 sys.path（兼容 embedded Python 的 ._pth 机制不自动加脚本目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# 器械类型 -> (主指标 key, 副指标 key, 距离列的换算除数)
#
# 距离列的原始值都是「米」（拿跑步机老总表核过：一节 20 分钟的爬坡课合计 1208 米，
# 对应速度 2~7 km/h，量级对得上）。
#   跑步机 / 单车 / 划船机 / 椭圆机：换算成公里显示（÷1000）
#   爬楼机：原地蹬踏，一节课也就几百米，按「米」显示更直观（÷1）
EQUIP_METRICS = {
    "treadmill":    ("speed",  "incline",    1000.0),
    "stairclimber": ("speed",  "resistance",    1.0),
    "bike":         ("rpm",    "resistance", 1000.0),
    "rower":        ("spm",    "resistance", 1000.0),
    "elliptical":   ("rpm",    "resistance", 1000.0),
    "bodyweight":   (None,     None,         1000.0),
}

# 表头文字 -> 语义列。按顺序匹配，先命中的角色生效（每个表头只归一个角色）。
# 注意「动作名称」要在「速度」之前判断不到冲突，但「建议速度(km/h)/踏频/桨频」
# 这种复合表头必须能命中 main，所以关键词要写全。
HEADER_ROLE_KEYWORDS = (
    ("segment",  ("环节", "阶段")),
    ("action",   ("动作",)),
    ("keyword",  ("关键词", "标签")),
    ("main",     ("速度", "踏频", "桨频", "转速", "频率", "步频", "spm", "rpm")),
    ("sub",      ("阻力", "坡度", "档位")),
    ("duration", ("持续时间", "时长")),
    ("distance", ("距离", "里程")),
    ("calories", ("消耗", "卡路里", "kcal")),
)


def _to_float(v):
    """字符串 -> float；转不了返回 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cell_text(row, col):
    """取某行某列的文字值；空/空白返回 None。"""
    if col is None:
        return None
    v = row.get(col)
    if v is None:
        return None
    v = str(v).strip()
    return v if v != "" else None


def find_header_row(rows):
    """定位表头行：A~其他列里出现「环节」或「动作名称」的那一行。

    老总表表头在第 3 行、新单课表在第 1 行，所以这里不写死行号。
    返回下标；找不到返回 None。
    """
    for i, row in enumerate(rows):
        for v in row.values():
            s = str(v or "").strip()
            if s in ("环节", "动作名称"):
                return i
    return None


def build_colmap(header_row):
    """把表头行解析成 {语义角色: [列字母, ...]}（列按 A/B/C... 顺序）。

    同一个角色可能出现多列（例如老总表有两列都叫「持续时间」）。
    """
    colmap = {}
    for col in sorted(c for c in header_row.keys() if c):
        text = str(header_row.get(col) or "").strip().lower()
        if not text:
            continue
        for role, kws in HEADER_ROLE_KEYWORDS:
            if any(kw.lower() in text for kw in kws):
                colmap.setdefault(role, []).append(col)
                break
    return colmap


# 无意义的工作表名（新单课表往往只有一张表，默认名就是 Sheet1）
_GENERIC_SHEET_RE = re.compile(r"^(sheet|工作表|工作簿)\s*\d*$", re.IGNORECASE)


def effective_course_name(sheet_name, source_name="", single_sheet=False):
    """确定课程名（也就是生成的 JSON 文件名、匹配达芬奇时间线用的名字）。

    工作表名有意义时直接用它（老总表是「跑步机-爬坡模拟训练」这种）；
    工作表名是 Sheet1 / 工作表1 这类默认名时，改用**文件名**（去掉扩展名）——
    新单课表就是一节课一个 xlsx，文件名才是真正的课程名。
    否则所有单课表都会叫 Sheet1.json 互相覆盖。

    参数 single_sheet：整个 xlsx 只有一张工作表（新单课表就是这样）。
    此时再补一条规则——**取「文件名」和「工作表名」里更长的那个**：
    新单课表里两边通常都写着课程名（一样长，随便取）；
    但如果工作表名是个短的通用词（「课程」「课件」「Sheet1」），
    用文件名才不会被多个文件撞成同一个 JSON 互相覆盖。
    多表工作簿（老总表）不启用这条，照旧以工作表名为准。
    """
    name = str(sheet_name or "").replace("\n", "").strip()
    stem = ""
    if source_name:
        stem = os.path.splitext(os.path.basename(str(source_name)))[0].strip()
    if not stem:
        return name
    if not name or _GENERIC_SHEET_RE.match(name):
        return stem
    # 单表工作簿：名字明显更短的一方多半是通用词，取更具体的那个
    if single_sheet and len(stem) > len(name) and name not in stem:
        return stem
    return name


def target_course_minutes(sheet_name, meta=None):
    """猜这节课的总时长（分钟）：优先从工作表名里读（如「20min综合能力提升」）。"""
    m = re.search(r"(\d+)\s*(?:min|分钟)", str(sheet_name or ""), re.IGNORECASE)
    if m:
        return float(m.group(1))
    raw = str((meta or {}).get("duration_min") or "")
    m = re.search(r"\d+(?:\.\d+)?", raw)
    if m:
        return float(m.group(0))
    return None


def choose_duration_col(rows, cols, sheet_name, meta=None, log=None):
    """在候选「持续时间」列里挑一列，并判断它的单位。

    两种单位都见过（同一张表里两列都叫「持续时间」，只是格式不同）：
        - 分钟原值（老总表的 G 列、新单课表的「持续时间」）  -> 直接 ×60 得秒
        - Excel 天分数（老总表的 F 列、新单课表单独的「持续时间」列）
          -> ×24 才是分钟（例如 0.0416667 × 24 = 1 分钟）

    判别办法：**看这一列的数值量级**。
        天分数永远是 <1 的小数（0.02~0.17 之间，因为一节课的单个环节不会超过 24 小时）；
        分钟原值只要有一个环节长度 ≥ 1 分钟就会出现 ≥1 的数。
      所以「列里出现过 ≥1 的值」就按分钟读，全是 <1 的小数就按天分数读。
      两列同时存在时，优先用能按分钟读的那列（就是 G 列）。

    如课程名里写了总时长（「20min综合能力提升」），再用它复核一遍：
    哪个单位算出来的总时长更接近目标，就用哪个。

    返回 (列字母, 单位, {列字母: 单位})：
        第 1 个是首选列，第 2 个是它的单位（"min" / "day"）。
        第 3 个是**每个候选列各自的单位**——必须有，因为老总表里有些行
        只有 F 列（天分数）、没有 G 列，回退用 F 时若沿用 G 的分钟单位，
        0.104 天会被当成 0.104 分钟（少算 144 秒）。
    找不到可用列返回 (cols[0] 或 None, "min", {})。
    """
    target = target_course_minutes(sheet_name, meta)
    candidates = []   # (unit, col, total_minutes)
    for c in cols:
        vals = [v for v in (_to_float(_cell_text(r, c)) for r in rows)
                if v is not None and v > 0]
        if not vals:
            continue
        unit = "min" if max(vals) >= 1.0 else "day"
        total = sum(vals) * (24.0 if unit == "day" else 1.0)
        # 课程名里给了总时长时，按目标复核；差太多就换另一种读法
        if target:
            other_unit = "day" if unit == "min" else "min"
            other_total = sum(vals) * (24.0 if other_unit == "day" else 1.0)
            if (abs(total - target) > abs(other_total - target)
                    and abs(other_total - target) <= target * 0.5):
                unit, total = other_unit, other_total
        candidates.append((unit, c, total))

    if not candidates:
        return (cols[0] if cols else None), "min", {}

    # 优先「按分钟读」的列；没有的话就用天分数列
    mins = [x for x in candidates if x[0] == "min"]
    pool = mins or candidates
    unit, col, total = max(pool, key=lambda x: x[2])
    units = {c: u for u, c, _ in candidates}
    if log:
        log(f"    时长列取 {col}（单位：{'分钟' if unit == 'min' else '天分数×24'}，"
            f"合计约 {total:.1f} 分钟）")
    return col, unit, units



def pace_from_speed(speed_kmh):
    """速度(km/h) -> 配速字符串 "mm:ss"。"""
    if speed_kmh is None or float(speed_kmh) <= 0:
        return None
    sec_per_km = 3600.0 / float(speed_kmh)  # 每公里秒数
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def parse_xlsx(path):
    """解析 xlsx，返回 {sheet_name: [rows]}，每行是 {col_letter: value_str}。"""
    z = zipfile.ZipFile(path)

    # sharedStrings
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss_root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in ss_root.findall(f"{{{NS}}}si"):
            txt = "".join(t.text or "" for t in si.iter(f"{{{NS}}}t"))
            shared.append(txt)

    # workbook: sheet name -> rId
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid2target = {r.get("Id"): r.get("Target") for r in rels}
    sheets_el = wb.find(f"{{{NS}}}sheets")
    name2file = {}
    for s in sheets_el:
        name = s.get("name")
        rid = s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rid2target.get(rid, "")
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        name2file[name] = target

    result = {}
    for name, sheetfile in name2file.items():
        root = ET.fromstring(z.read(sheetfile))
        sheet_data = root.find(f"{{{NS}}}sheetData")
        rows = []
        for r in sheet_data.findall(f"{{{NS}}}row"):
            cells = {}
            for c in r.findall(f"{{{NS}}}c"):
                ref = c.get("r", "")
                col = "".join(ch for ch in ref if ch.isalpha())
                t = c.get("t")
                v = c.find(f"{{{NS}}}v")
                if v is None:
                    val = None
                elif t == "s":
                    val = shared[int(v.text)]
                else:
                    val = v.text
                cells[col] = val
            rows.append(cells)
        result[name] = rows
    return result


def _estimate_reps_duration(reps):
    """根据动作「个数」估算时长（秒）。

    徒手课的个数型动作（B 列有个数、C 列无时长）没有固定时长。
    采用经验估算：每个重复约 2.5 秒 + 10 秒讲解/转场，向上取整到 10 的倍数，
    保证时间轴连续且符合健身课件节奏（8~16 个约 30~50 秒）。
    """
    if reps is None:
        return 40  # 无个数也无时长时，默认 40 秒（一个动作的常见耗时）
    sec = reps * 2.5 + 10.0
    # 向上取整到 10 的整数倍，保持时间轴整洁
    return int((sec + 9) // 10) * 10


def _parse_bodyweight_sheet(course_name, rows, header_idx):
    """解析徒手课（bodyweight）工作表。

    徒手课表结构（与器械课不同）：
        第 3 行（header_idx）是表头：A=动作名称  B=个数  C=持续时间  D=话术 ...
        数据从 header_idx+1 开始，每行一个动作：
            A 列 = 动作名称（每个动作就是一个 point/环节，无「环节」分组概念）
            B 列 = 个数（数字，或 "-"/空）
            C 列 = 持续时间（三种格式：纯数字秒 30/35/60、"30S"/"20S" 带 S、"-"/空格/空）

    强度判断（徒手课无速度/坡度/阻力数值指标）：
        用 score_action() 按动作类型规则表给每个动作打 0~1 连续强度分，
        同时写 intensity 三档标签（high/mid/low）供前端标签显示。

    时间轴：C 列有明确秒数则用它累加；否则（个数型动作）用 _estimate_reps_duration 估算。

    参数 course_name 已由调用方算好（工作表名无意义时会换成文件名）。
    """
    from equipment_config import score_action, intensity_level

    # 课程主题（第 1 行 A 列的值，例如「核心训练（瑜伽垫）」）
    meta_title = ""
    if header_idx >= 1:
        meta_title = (rows[1].get("A") or "").strip() if len(rows) > 1 else ""

    def fcol(row, col):
        v = row.get(col)
        if v is None:
            return None
        v = str(v).strip()
        return v if v != "" else None

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # 解析持续时间（秒）：支持 "30"、"30S"/"20S"、"-"/" "/空
    def parse_duration(v):
        if v is None:
            return None
        s = str(v).strip().upper().replace("S", "")
        if s in ("", "-"):
            return None
        n = to_float(s)
        if n is None:
            return None
        return int(round(n))

    segments = []
    points = []
    cur_time = 0.0

    for row in rows[header_idx + 1:]:
        action = fcol(row, "A")
        reps_raw = fcol(row, "B")
        dur_raw = fcol(row, "C")

        # 跳过完全空行（A/B/C 都无值）
        if action is None and reps_raw is None and dur_raw is None:
            continue

        # 个数：B 列数字有效，"-"/空 视为 None
        reps = None
        if reps_raw is not None and reps_raw.strip() != "-":
            reps = to_float(reps_raw)
            if reps is not None:
                reps = int(reps)

        # 时长：优先 C 列明确秒数，否则用个数估算
        dur_sec = parse_duration(dur_raw)
        if dur_sec is None:
            dur_sec = _estimate_reps_duration(reps)
        # 时长下限：至少 10 秒，避免估算异常
        if dur_sec < 10:
            dur_sec = 10

        # 动作名：空则用「休息」兜底（如 HIIT 里的「休息 」行）
        name = action if action else "休息"

        # 强度判断：连续强度分 + 三档标签
        score = score_action("bodyweight", name)
        intensity = intensity_level(score)

        # point：记录动作名、个数、强度分、强度标签
        point = {"time": int(round(cur_time))}
        if action:
            point["action"] = action
        if reps is not None:
            point["reps"] = reps
        point["intensity_score"] = round(score, 2)
        point["intensity"] = intensity

        points.append(point)

        # segment：每个动作一个环节（动作名即环节名）
        segments.append({
            "name": name,
            "start": int(round(cur_time)),
            "end": int(round(cur_time + dur_sec)),
        })

        cur_time += dur_sec

    course = {
        "course_name": course_name,
        "title": meta_title or course_name,
        "equipment": "bodyweight",
        "duration": int(round(cur_time)),
        "segments": segments,
        "points": points,
    }
    return course


def parse_sheet(sheet_name, rows, source_name="", log=None, single_sheet=False):
    """把单个 sheet 的原始行解析成课程 JSON dict。

    参数：
        sheet_name   : 工作表名（会成为 course_name，用于和达芬奇时间线名匹配）
        rows         : parse_xlsx 出来的原始行
        source_name  : 课件文件名/路径，仅用于兜底识别器械（新单课表的工作表常叫
                       Sheet1，看不出器械，但文件名或所在文件夹名里有「爬楼机」之类）
        log          : 可选日志函数
        single_sheet : 这个 xlsx 是否只有一张工作表（见 effective_course_name）
    """
    from equipment_config import detect_equipment

    # 课程名：工作表名没意义（Sheet1）时退回用文件名
    course_name = effective_course_name(sheet_name, source_name, single_sheet)

    # 器械识别：工作表名前缀 -> 工作表名关键词 -> 文件名/路径关键词
    equipment = detect_equipment(sheet_name, source_name)
    main_key, sub_key, dist_div = EQUIP_METRICS.get(equipment, (None, None, 1000.0))

    # 表头行不写死行号：老总表在第 3 行，新单课表在第 1 行
    header_idx = find_header_row(rows)
    if header_idx is None:
        raise ValueError(f"工作表 {sheet_name} 无「环节」或「动作名称」表头，无法解析")

    if log:
        log(f"    器械={equipment} 表头在第 {header_idx + 1} 行")

    # 徒手课走独立解析分支（无器械指标列，强度靠动作类型判断）
    if equipment == "bodyweight":
        return _parse_bodyweight_sheet(course_name, rows, header_idx)

    colmap = build_colmap(rows[header_idx])
    seg_col = (colmap.get("segment") or [None])[0]
    act_col = (colmap.get("action") or [None])[0]
    kw_col = (colmap.get("keyword") or [None])[0]
    main_col = (colmap.get("main") or [None])[0]
    sub_col = (colmap.get("sub") or [None])[0]
    dist_col = (colmap.get("distance") or [None])[0]
    # 所有「持续时间」列（老总表有 F/G 两列，格式不同），首选列排最前
    dur_cols = colmap.get("duration") or []
    dur_col, dur_unit, dur_units = choose_duration_col(
        rows[header_idx + 1:], dur_cols, sheet_name, log=log)
    # 去重且保持「首选列优先」的顺序
    dur_cols_ordered = []
    for c in [dur_col] + list(dur_cols):
        if c and c not in dur_cols_ordered:
            dur_cols_ordered.append(c)

    # 课程元信息：老总表在表头前有两行（「课程主题」表头 + 值）
    meta = {}
    if header_idx >= 2:
        meta_header = rows[header_idx - 2]
        meta_value = rows[header_idx - 1]
        for col, label in meta_header.items():
            label = str(label or "").strip()
            if "课程主题" in label:
                meta["course_name"] = str(meta_value.get(col) or "").strip()
            elif "课程时长" in label:
                meta["duration_min"] = str(meta_value.get(col) or "").strip()

    data_rows = rows[header_idx + 1:]

    segments = []   # {name, start, end}
    points = []     # {time, speed?, pace?, incline?, distance?, action?, keyword?}
    cur_time = 0.0
    cur_segment = None  # 当前环节名（环节列空则继承）

    for row in data_rows:
        # 跳过空行/图例行：所有「持续时间」列都没值的行不是课程数据。
        # 逐列找值，并**跟着该列自己的单位**换算——老总表里有的行只填了 F 列
        # （天分数）、G 列空着，沿用首选列的单位会把 0.104 天读成 0.104 分钟。
        dur_raw = None
        row_dur_unit = dur_unit
        for c in dur_cols_ordered:
            v = _cell_text(row, c)
            if v is not None:
                dur_raw = v
                row_dur_unit = dur_units.get(c, dur_unit)
                break
        if dur_raw is None:
            continue

        seg_name = _cell_text(row, seg_col)
        action = _cell_text(row, act_col)
        keyword = _cell_text(row, kw_col)

        # 时长：按选中的列和单位换算成秒
        dur_min = _to_float(dur_raw)
        if dur_min is None:
            dur_min = 0.0
        if row_dur_unit == "day":
            dur_min *= 24.0
        dur_sec = round(dur_min * 60.0)

        # 环节：环节列非空则新开一个环节，否则继承上一个
        if seg_name:
            if cur_segment:
                cur_segment["end"] = int(round(cur_time))
            cur_segment = {"name": seg_name, "start": int(round(cur_time)),
                           "end": int(round(cur_time + dur_sec))}
            segments.append(cur_segment)
        elif cur_segment:
            cur_segment["end"] = int(round(cur_time + dur_sec))

        point = {"time": int(round(cur_time))}

        # 主指标（速度/RPM/SPM）
        d_num = _to_float(_cell_text(row, main_col))
        if main_key and d_num is not None:
            point[main_key] = d_num
            if equipment == "treadmill":
                p = pace_from_speed(d_num)
                if p:
                    point["pace"] = p

        # 副指标（坡度/阻力）
        e_num = _to_float(_cell_text(row, sub_col))
        if sub_key and e_num is not None:
            point[sub_key] = e_num

        # 距离：原始值是米，按器械决定显示单位（跑步机->公里 / 爬楼机->米）
        h_num = _to_float(_cell_text(row, dist_col))
        if h_num is not None and h_num > 0:
            point["distance"] = round(h_num / dist_div, 3)

        if action:
            point["action"] = action
        if keyword:
            point["keyword"] = keyword

        points.append(point)
        cur_time += dur_sec

    # 结束最后一个环节
    if cur_segment:
        cur_segment["end"] = int(round(cur_time))

    # 课程主题（悬浮窗显示用的友好名）。
    # 老总表里元信息的「课程主题」是准的（如工作表「跑步机-爬坡模拟训练」对应主题
    # 「爬坡模拟训练」）；但新的单课表常常是**拿别的课复制一份改出来的**，元信息里
    # 那一格往往还留着旧课名（实测见过文件叫「20min心肺间歇突破攀登」、里面写着
    # 「35min稳态匀速耐力攀登」）。所以两者明显对不上时以 course_name 为准，
    # 否则悬浮窗会赫然显示成另一节课。
    #
    # 用相似度而不是「互相包含」来判断，因为老总表里本来就有措辞微差（真实数据里
    # 见过「跑步机 -护膝专项跑训练」配主题「护膝专项训练」、「轻松开跑训练」配
    # 主题「轻松开炮训练」，后者是课件里的错别字）——那些相似度都在 0.6 以上，
    # 而复制改名的两节课只有 0.38，用 0.5 作阈值正好分得开。
    title = meta.get("course_name") or course_name
    meta_title = meta.get("course_name")
    if meta_title and difflib.SequenceMatcher(None, meta_title, course_name).ratio() < 0.5:
        title = course_name

    course = {
        # course_name 用工作表全名（含器械前缀），与达芬奇时间线名匹配；
        # 工作表名是 Sheet1 时已由 effective_course_name 换成文件名
        "course_name": course_name,
        "title": title,
        "equipment": equipment,
        "duration": int(round(cur_time)),
        "segments": segments,
        "points": points,
    }
    return course


def convert_file(xlsx_path, only_sheet=None, out_dir=None, log=print):
    """把课件 xlsx 转成 data/*.json，返回转换统计。

    与命令行入口解耦，方便 convert_course.py（图形化选择）直接复用。

    参数：
        xlsx_path  : 课件表格路径
        only_sheet : 只转某个工作表（支持模糊匹配）；None 表示全部转
        out_dir    : 输出目录，默认 <脚本目录>/data
        log        : 日志输出函数（默认 print）

    返回：
        {"ok": 成功数, "skipped": [(表名, 原因), ...], "outputs": [json 文件名, ...]}

    异常：
        文件不存在 / 读不出工作表 → 抛 ValueError，由调用方决定怎么提示。
    """
    if not xlsx_path or not os.path.exists(xlsx_path):
        raise ValueError(f"找不到课件文件：{xlsx_path}")

    # Excel 打开文件时会生成 ~$ 开头的临时锁文件，误选到它只会报错，直接提示
    if os.path.basename(xlsx_path).startswith("~$"):
        raise ValueError(
            f"这是 Excel 打开文件时产生的临时文件，不是课件本身：\n{xlsx_path}\n"
            "请关闭 Excel 后选择不带 ~$ 前缀的那个文件。"
        )

    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)

    all_sheets = parse_xlsx(xlsx_path)
    if not all_sheets:
        raise ValueError("表格里没有读到任何工作表，请确认这是课程课件文件。")

    targets = {}
    if only_sheet:
        # 支持精确名或模糊匹配
        for name in all_sheets:
            if name == only_sheet or only_sheet in name:
                targets[name] = all_sheets[name]
        if not targets:
            raise ValueError(f"未找到工作表「{only_sheet}」，可用工作表：{list(all_sheets.keys())}")
    else:
        targets = all_sheets

    single_sheet = (len(all_sheets) == 1)   # 新单课表：一节课一个 xlsx
    result = {"ok": 0, "skipped": [], "outputs": []}
    for sheet_name, rows in targets.items():
        try:
            # source_name 传完整路径：新单课表的工作表可能叫 Sheet1，
            # 靠路径里的「爬楼机」「跑步机」等目录名兜底识别器械
            course = parse_sheet(sheet_name, rows, source_name=xlsx_path, log=log,
                                 single_sheet=single_sheet)
        except ValueError as e:
            log(f"跳过 {sheet_name}：{e}")
            result["skipped"].append((sheet_name, str(e)))
            continue
        # 文件名用课程名（去掉可能存在的换行/空格）
        fname = course["course_name"].replace("\n", "").strip()
        out_path = os.path.join(out_dir, fname + ".json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(course, f, ensure_ascii=False, indent=2)
        n_seg = len(course["segments"])
        n_pt = len(course["points"])
        mm, ss = divmod(course["duration"], 60)
        log(f"  {sheet_name}  ->  {os.path.basename(out_path)}  "
            f"({course['equipment']}, {mm}分{ss:02d}秒, {n_seg}环节, {n_pt}点)")
        result["ok"] += 1
        result["outputs"].append(os.path.basename(out_path))

    return result


def main():
    # 无参数时，默认找桌面上的「冠军课程课件.xlsx」（方便双击 convert.bat 直接转换）
    if len(sys.argv) < 2:
        default_xlsx = os.path.join(os.path.expanduser("~"), "Desktop", "冠军课程课件.xlsx")
        if os.path.exists(default_xlsx):
            xlsx_path = default_xlsx
            print(f"未指定文件，使用桌面默认课件: {xlsx_path}")
        else:
            print("用法: python xlsx_to_json.py <xlsx路径> [工作表名]")
            print(f"默认也未找到: {default_xlsx}")
            sys.exit(1)
    else:
        xlsx_path = sys.argv[1]
    only_sheet = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        convert_file(xlsx_path, only_sheet=only_sheet)
    except ValueError as e:
        print(f"[错误] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
