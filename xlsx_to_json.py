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


def parse_sheet(sheet_name, rows):
    """把单个 sheet 的原始行解析成课程 JSON dict。"""
    from equipment_config import detect_equipment

    equipment = detect_equipment(sheet_name)
    main_key, sub_key = EQUIP_METRICS.get(equipment, (None, None))

    # 表头在第 3 行（第 1、2 行是课程元信息）。数据从第 4 行开始。
    # 但行号不一定是连续的 1..N，这里按「有内容的行」解析。
    # 找表头行：A 列 = "环节" 的行（徒手类课件是「动作名称」，需单独处理）
    header_idx = None
    for i, row in enumerate(rows):
        a = (row.get("A") or "").strip()
        if a == "环节":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"工作表 {sheet_name} 无「环节」表头（可能是徒手类，暂不支持自动转换）")

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
