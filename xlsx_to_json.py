# -*- coding: utf-8 -*-
"""
xlsx_to_json.py —— 把「冠军课程课件.xlsx」里的课件表转换成插件可读的课程 JSON。

用法：
    python xlsx_to_json.py <xlsx路径> [工作表名]
      - 指定工作表名：只转换该表，输出到 data/<课程名>.json
      - 省略工作表名：批量转换所有表

依赖：标准库（zipfile + xml.etree），无需 openpyxl。
（openpyxl 在解析该 xlsx 的样式时存在兼容问题，故直接用底层 XML 解析，更稳健。）

字段映射（xlsx 列 -> 插件 JSON）：
    A 环节          -> segment（环节名，空则继承上一个非空环节）
    B 动作名称      -> action
    C 关键词        -> keyword（强度标签）
    D 建议速度/RPM/SPM -> 主指标（跑步机 speed / 单车 rpm / 划船机 spm）
    E 建议阻力/坡度 -> 副指标（跑步机 incline / 单车·划船机 resistance）
    F 持续时间(原始) -> 时长（Excel 天序列，×24 = 分钟）
    G 持续时间(计算) -> 时长（分钟，优先用这列）
    H 预计距离      -> distance（米 -> 公里）

器械主/副指标映射：
    跑步机 D->speed(km/h)  E->incline(%)  + 自动换算 pace(min/km)
    单车   D->rpm(rpm)     E->resistance(级)
    划船机 D->spm(spm)     E->resistance(级)
    椭圆机 D->rpm(rpm)     E->resistance(级)
"""

import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

# 确保脚本所在目录在 sys.path（兼容 embedded Python 的 ._pth 机制不自动加脚本目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# 器械类型 -> (主指标 key, 副指标 key)
EQUIP_METRICS = {
    "treadmill":   ("speed",     "incline"),
    "bike":        ("rpm",       "resistance"),
    "rower":       ("spm",       "resistance"),
    "elliptical":  ("rpm",       "resistance"),
    "bodyweight":  (None,        None),
}


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


def _parse_bodyweight_sheet(sheet_name, rows, header_idx):
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
        "course_name": sheet_name.replace("\n", "").strip(),
        "title": meta_title or sheet_name.strip(),
        "equipment": "bodyweight",
        "duration": int(round(cur_time)),
        "segments": segments,
        "points": points,
    }
    return course


def parse_sheet(sheet_name, rows):
    """把单个 sheet 的原始行解析成课程 JSON dict。"""
    from equipment_config import detect_equipment

    equipment = detect_equipment(sheet_name)
    main_key, sub_key = EQUIP_METRICS.get(equipment, (None, None))

    # 表头在第 3 行（第 1、2 行是课程元信息）。数据从第 4 行开始。
    # 但行号不一定是连续的 1..N，这里按「有内容的行」解析。
    # 找表头行：A 列 = "环节"（器械类）或 "动作名称"（徒手类）。
    header_idx = None
    for i, row in enumerate(rows):
        a = (row.get("A") or "").strip()
        if a in ("环节", "动作名称"):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"工作表 {sheet_name} 无「环节」或「动作名称」表头，无法解析")

    # 徒手课走独立解析分支（无器械指标列，强度靠动作类型判断）
    if equipment == "bodyweight":
        return _parse_bodyweight_sheet(sheet_name, rows, header_idx)

    # 课程元信息（第 1 行表头 + 第 2 行值）
    meta = {}
    if header_idx >= 1:
        # 第 1 行是元信息表头，第 2 行是值
        header_row = rows[0]
        value_row = rows[1] if len(rows) > 1 else {}
        for col, label in header_row.items():
            label = (label or "").strip()
            if label == "课程主题":
                meta["course_name"] = (value_row.get(col) or "").strip()
            elif label == "课程时长（min）":
                meta["duration_min"] = (value_row.get(col) or "").strip()

    # 数据行：header_idx 之后
    data_rows = rows[header_idx + 1:]

    # 解析每个数据行
    segments = []   # {name, start, end}
    points = []     # {time, speed?, pace?, incline?, distance?, action?, keyword?}
    cur_time = 0.0
    cur_segment = None  # 当前环节名（A 列空则继承）

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

    for row in data_rows:
        g_val = fcol(row, "G")   # 时长（分钟）
        f_val = fcol(row, "F")   # 时长（原始天序列）

        # 跳过空行/图例行：F、G 都无值的行不是课程环节数据（是图例或空行）
        if g_val is None and f_val is None:
            continue

        seg_name = fcol(row, "A")
        action = fcol(row, "B")
        keyword = fcol(row, "C")
        d_val = fcol(row, "D")   # 主指标（速度/RPM/SPM）
        e_val = fcol(row, "E")   # 副指标（坡度/阻力）
        h_val = fcol(row, "H")   # 距离（米）

        # 时长（分钟）：优先 G，其次 F×24
        dur_min = to_float(g_val)
        if dur_min is None and f_val is not None:
            dur_min = to_float(f_val) * 24 if to_float(f_val) is not None else None
        if dur_min is None:
            dur_min = 0.0
        # 时长秒：四舍五入到整数（健身课件时长均为 10 秒整数倍）
        dur_sec = round(dur_min * 60.0)

        # 环节：A 非空则新环节
        if seg_name:
            if cur_segment:
                # 结束上一个环节
                cur_segment["end"] = int(round(cur_time))
            cur_segment = {"name": seg_name, "start": int(round(cur_time)), "end": int(round(cur_time + dur_sec))}
            segments.append(cur_segment)
        elif cur_segment:
            # 继承上一个环节，延长其结束时间
            cur_segment["end"] = int(round(cur_time + dur_sec))

        # 生成 point
        point = {"time": int(round(cur_time))}

        # 主指标（速度/RPM/SPM）
        d_num = to_float(d_val)
        if main_key and d_num is not None:
            point[main_key] = d_num
            if equipment == "treadmill":
                p = pace_from_speed(d_num)
                if p:
                    point["pace"] = p

        # 副指标（坡度/阻力）
        e_num = to_float(e_val)
        if sub_key and e_num is not None:
            point[sub_key] = e_num

        # 距离（米 -> 公里）
        h_num = to_float(h_val)
        if h_num is not None and h_num > 0:
            point["distance"] = round(h_num / 1000.0, 3)

        # 动作 / 关键词
        if action:
            point["action"] = action
        if keyword:
            point["keyword"] = keyword

        points.append(point)
        cur_time += dur_sec

    # 结束最后一个环节
    if cur_segment:
        cur_segment["end"] = int(round(cur_time))

    course = {
        "course_name": sheet_name.replace("\n", "").strip(),  # 用工作表全名（含器械前缀），与达芬奇时间线名匹配
        "title": meta.get("course_name") or sheet_name.strip(),  # 课程主题（友好显示名）
        "equipment": equipment,
        "duration": int(round(cur_time)),
        "segments": segments,
        "points": points,
    }
    return course


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

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)

    all_sheets = parse_xlsx(xlsx_path)

    targets = {}
    if only_sheet:
        # 支持精确名或模糊匹配
        for name in all_sheets:
            if name == only_sheet or only_sheet in name:
                targets[name] = all_sheets[name]
        if not targets:
            print(f"未找到工作表: {only_sheet}")
            print("可用工作表:", list(all_sheets.keys()))
            sys.exit(1)
    else:
        targets = all_sheets

    for sheet_name, rows in targets.items():
        try:
            course = parse_sheet(sheet_name, rows)
        except ValueError as e:
            print(f"⚠ 跳过 {sheet_name}：{e}")
            continue
        # 文件名用课程名（去掉可能存在的换行/空格）
        fname = course["course_name"].replace("\n", "").strip()
        out_path = os.path.join(out_dir, fname + ".json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(course, f, ensure_ascii=False, indent=2)
        n_seg = len(course["segments"])
        n_pt = len(course["points"])
        print(f"✓ {sheet_name}  ->  {os.path.basename(out_path)}  "
              f"({course['equipment']}, {course['duration']}s, {n_seg}环节, {n_pt}点)")


if __name__ == "__main__":
    main()
