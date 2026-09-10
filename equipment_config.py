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
        "fields": [
            {"key": "reps",      "label": "个数",   "unit": "个",   "required": False, "priority": 1},
        ],
    },
}


# ---------------------------------------------------------------------------
# 徒手动作强度规则表（打分制）
#
# 徒手课没有速度/坡度/阻力等数值指标，无法像器械课那样直接读一个数字画强度曲线。
# 旧方案用「是否复合动作」做二值判断（命中 compound_actions → 高强度，否则低强度），
# 导致核心训练、臀腿塑形这类"抗阻/稳定类"课程整节都是低强度，曲线没有起伏、失去意义。
#
# 新方案：把徒手动作按运动科学常见的分类维度拆成若干「动作类型」，每类配一个
# 相对强度分（0~1）。判据是「动作名里的关键词」而非手写清单，因此新课件的动作
# 能自动落到对应分数段，无需逐条补全。
#
# 分类强度分参考逻辑（非医学精确值，仅用于让曲线呈现相对起伏）：
#   爆发跳跃类（波比/开合跳/跳跃箭步蹲等）        -> 0.90 ~ 1.00  全身+心肺，最高
#   全身复合抗阻类（深蹲推举/土耳其起立/熊爬等）  -> 0.80 ~ 0.90  多关节大肌群
#   多关节抗阻类（深蹲/弓步/俯卧撑/引体等）       -> 0.60 ~ 0.80  下肢/推拉复合
#   核心等长稳定类（平板/臀桥/死虫/鸟狗/侧平板）  -> 0.40 ~ 0.60  抗阻稳定，负荷不低
#   孤立局部类（卷腹/抬腿/二头弯举/侧平举等）     -> 0.20 ~ 0.40  单关节/小肌群
#   拉伸放松类（拉伸/鸽式/蝴蝶式/猫牛式等）       -> 0.10 ~ 0.20  柔韧，几乎无代谢负荷
#   休息/介绍/总结                               -> 0.00          无运动负荷
#
# 每条规则：(关键词列表, 强度分, 优先级)。匹配时按优先级从高到低，命中即返回；
# 同一条规则内命中任一关键词即可。关键词做「子串匹配」，能兼容动作变体
# （"简易波比跳"/"慢速登山跑"/"弹力带深蹲"）。
# ---------------------------------------------------------------------------

BODYWEIGHT_ACTION_RULES = [
    # (关键词列表, 强度分, 优先级) —— 优先级数字越小越先匹配
    # —— 休息/介绍/总结（必须最先，避免"总结""介绍"里的字被误判）——
    (["休息", "恢复", "介绍", "总结", "踏步", "冷身", "放松结束", "讲解"], 0.00, 0),

    # —— 拉伸放松类（柔韧，几乎无代谢负荷；优先级提到核心稳定之前，
    #    因为"婴儿式/前伸/穿针式/抱肩"等肩背拉伸动作名里常带"跪姿/四足"字，
    #    若让核心稳定先命中会误判成中强度）——
    (["拉伸", "鸽式", "蝴蝶式", "劈叉", "猫牛", "穿针式", "放松", "泡沫轴",
      "伸展", "前屈", "前倾", "前伸", "扭转", "呼吸", "婴儿式", "小狗式",
      "抱肩", "开肩", "十指", "后撑", "侧屈", "下犬", "上犬", "眼镜蛇"], 0.15, 1),

    # —— 爆发跳跃类（心肺+全身，最高强度）——
    (["波比跳", "立卧撑", "开合跳", "星星跳", "海星跳", "深蹲跳", "抱膝跳",
      "屈膝跳", "箭步蹲跳", "跳跃箭步蹲", "弓步跳", "青蛙跳", "滑冰跳",
      "侧向跳", "登山跑", "高抬腿", "跳绳", "冲刺"], 0.95, 2),

    # —— 全身复合抗阻类（多关节大肌群协调）——
    (["火箭推", "深蹲推举", "土耳其起立", "熊爬", "蜘蛛俯卧撑", "平板支撑开合跳",
      "俯卧撑波比", "立卧撑跳"], 0.85, 3),

    # —— 多关节抗阻类（下肢推拉/俯卧撑/引体）——
    (["深蹲", "弓步", "箭步", "蹲", "俯卧撑", "引体", "划船", "硬拉",
      "臀桥", "臀推", "跨步", "螃蟹"], 0.70, 4),

    # —— 核心等长/稳定类（抗阻稳定，负荷中等）——
    (["平板", "支撑", "死虫", "鸟狗", "侧桥", "四足", "跪姿", "侧平板",
      "蚌式", "熊式", "提膝", "转体"], 0.50, 5),

    # —— 孤立局部类（单关节/小肌群）——
    (["卷腹", "仰卧", "抬腿", "举腿", "弯举", "臂屈伸", "侧平举", "飞鸟",
      "腿举", "后抬腿", "髋外展", "内收", "提踵"], 0.30, 6),

    # —— 兜底：无法识别 → 中低强度 0.40（宁可比"低"略高，避免未知动作全塌成 0）——
    ([], 0.40, 99),
]

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


def score_action(eq_type: str, action_name: str) -> float:
    """给徒手动作打一个 0~1 的连续强度分（基于动作类型规则表）。

    徒手课没有速度/阻力等数值指标，强度靠动作类型判断。与旧 is_compound_action
    的二值不同，这里按动作名关键词命中 BODYWEIGHT_ACTION_RULES 里的分类，
    返回一个连续分数，让前端能画出「有起伏」的强度曲线：
        - 爆发跳跃（波比/开合跳）≈ 0.95
        - 全身复合抗阻 ≈ 0.85
        - 多关节抗阻（深蹲/俯卧撑）≈ 0.70
        - 核心稳定（平板/死虫/臀桥）≈ 0.50
        - 孤立局部（卷腹/弯举）≈ 0.30
        - 拉伸放松 ≈ 0.15
        - 休息/介绍/总结 = 0.00

    匹配规则：
        - 按优先级从高到低遍历规则，命中任一关键词即返回该规则分数。
        - 关键词做子串匹配（兼容变体，如"弹力带深蹲"命中"深蹲"）。
        - 休息/介绍/总结优先级最高，避免"总结"里的字被其他规则误判。

    参数：
        eq_type     : 器械类型 key（如 "bodyweight"）
        action_name : 动作名称（来自课程数据 point 的 action 字段）
    返回：
        0~1 的浮点数。非徒手课或参数无效返回 0.0。
    """
    eq = get_equipment(eq_type)
    if not eq or eq_type != "bodyweight":
        return 0.0
    if not action_name:
        return 0.0
    name = str(action_name).strip()
    # 规则已按优先级排序（priority 越小越靠前），直接顺序匹配即可
    for keywords, score, _priority in BODYWEIGHT_ACTION_RULES:
        for kw in keywords:
            if kw in name:
                return float(score)
    # 空关键词兜底规则（priority 99）的 score 是 0.40，上面会命中，理论上走不到这里
    return 0.40


def intensity_level(score: float) -> str:
    """把 0~1 强度分映射为三档标签："high" / "mid" / "low"。

    用于前端强度标签文字（高强度/中强度/低强度）和旧逻辑的兼容。
    阈值：>=0.8 high，>=0.35 mid，其余 low（含休息 0 分）。
    """
    if score >= 0.8:
        return "high"
    if score >= 0.35:
        return "mid"
    return "low"


def is_compound_action(eq_type: str, action_name: str) -> bool:
    """判断某动作是否为「复合动作」（涉及 3 个及以上关节、全身调度大）。

    兼容旧调用：以 score_action 的分数是否达到「全身复合/爆发」阈值(>=0.8)来判定。
    保留此函数是为了不破坏已有调用点；新代码请优先用 score_action 拿连续分数。

    参数：
        eq_type     : 器械类型 key（如 "bodyweight"）
        action_name : 动作名称（来自课程数据 point 的 action 字段）
    返回：
        True 表示复合动作；False 表示非复合动作或参数无效。
    """
    return score_action(eq_type, action_name) >= 0.8


def detect_equipment(sheet_name: str) -> str:
    """根据工作表名前缀判断器械类型 key。"""
    for prefix, eq in (("跑步机", "treadmill"), ("单车", "bike"),
                       ("划船机", "rower"), ("椭圆机", "elliptical"),
                       ("徒手", "bodyweight")):
        if sheet_name.strip().startswith(prefix):
            return eq
    return "bodyweight"
