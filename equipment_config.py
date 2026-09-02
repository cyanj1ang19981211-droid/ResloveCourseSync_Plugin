# -*- coding: utf-8 -*-
"""
器械配置模块 —— 定义不同运动器械需要显示/读取哪些指标字段。

设计目标：
    - 支持跑步机 / 单车 / 划船机 / 椭圆机 / 徒手等多种器械，可持续扩展。
    - 每种器械定义一组「指标字段」，每个字段有：key、显示名称、单位、可选性。
    - 课程数据文件里如果某个可选字段缺失（例如跑步机课程不带坡度），
      显示端会自动隐藏该字段，不会报错。

真实课件字段映射（来自「冠军课程课件.xlsx」）：
    - 跑步机  D列「建议速度(km/h)」-> speed   E列「建议阻力/坡度」-> incline
    - 单车    D列「RPM(踏频)」       -> rpm     E列「阻力」         -> resistance
    - 划船机  D列「SPM(桨频)」       -> spm     E列「阻力」         -> resistance

扩展方式：新增一个器械，只需在 EQUIPMENTS 里加一个条目即可。
"""

# 每种器械的指标字段定义
# 字段属性：
#   key        : 数据文件里的字段名（必须与 JSON 数据里的 key 一致）
#   label      : 悬浮窗上显示的中文名称
#   unit       : 单位（无单位则为 None）
#   required   : True 表示该器械课程必须有此字段；False 表示可选（缺失则隐藏）
#   priority   : 显示顺序权重（数字越小越靠前）
EQUIPMENTS = {
    # ---------- 跑步机 ----------
    "treadmill": {
        "name": "跑步机",
        "fields": [
            {"key": "speed",     "label": "速度",   "unit": "km/h",   "required": True,  "priority": 1},
            {"key": "pace",      "label": "配速",   "unit": "min/km", "required": False, "priority": 2},
            {"key": "incline",   "label": "坡度",   "unit": "%",      "required": False, "priority": 3},
            {"key": "distance",  "label": "距离",   "unit": "km",     "required": False, "priority": 4},
        ],
    },

    # ---------- 动感单车 ----------
    "bike": {
        "name": "动感单车",
        "fields": [
            {"key": "rpm",        "label": "踏频",   "unit": "rpm",   "required": True,  "priority": 1},
            {"key": "resistance", "label": "阻力",   "unit": "级",    "required": False, "priority": 2},
            {"key": "power",      "label": "功率",   "unit": "W",     "required": False, "priority": 3},
        ],
    },

    # ---------- 划船机 ----------
    "rower": {
        "name": "划船机",
        "fields": [
            {"key": "spm",        "label": "桨频",   "unit": "spm",   "required": True,  "priority": 1},
            {"key": "resistance", "label": "阻力",   "unit": "级",    "required": False, "priority": 2},
            {"key": "split",      "label": "配速",   "unit": "/500m", "required": False, "priority": 3},
        ],
    },

    # ---------- 椭圆机 ----------
    "elliptical": {
        "name": "椭圆机",
        "fields": [
            {"key": "rpm",        "label": "转速",   "unit": "rpm",   "required": True,  "priority": 1},
            {"key": "resistance", "label": "阻力",   "unit": "级",    "required": False, "priority": 2},
        ],
    },

    # ---------- 徒手（无器械，只有动作/环节） ----------
    "bodyweight": {
        "name": "徒手",
        "fields": [],
    },
}

# 所有器械共享的「课程环节」字段（不放进 fields 里，统一处理）
SEGMENT_KEY = "segment"   # 数据文件里表示「课程环节名称」的 key
ACTION_KEY = "action"     # 数据文件里表示「动作名称」的 key
KEYWORD_KEY = "keyword"   # 数据文件里表示「关键词/强度标签」的 key
TIME_KEY = "time"         # 数据文件里表示「时间戳（秒）」的 key


def get_equipment(eq_type: str):
    """根据器械类型 key 返回配置；找不到返回 None。"""
    return EQUIPMENTS.get(eq_type)


def get_field_order(eq_type: str):
    """返回按 priority 排序后的字段列表（用于显示顺序）。"""
    eq = get_equipment(eq_type)
    if not eq:
        return []
    return sorted(eq["fields"], key=lambda f: f["priority"])


def detect_equipment(sheet_name: str) -> str:
    """根据工作表名前缀判断器械类型 key。"""
    for prefix, eq in (("跑步机", "treadmill"), ("单车", "bike"),
                       ("划船机", "rower"), ("椭圆机", "elliptical"),
                       ("徒手", "bodyweight")):
        if sheet_name.strip().startswith(prefix):
            return eq
    return "bodyweight"
