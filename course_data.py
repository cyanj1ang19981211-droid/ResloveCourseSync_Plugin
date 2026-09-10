# -*- coding: utf-8 -*-
"""
课件数据模型 —— 加载/解析课程数据文件，并提供按时间查询当前强度信息的接口。

数据文件格式（JSON）：

    {
      "course_name": "跑步机-进阶跑姿训练",     # 课程名（用于和达芬奇时间线名匹配）
      "equipment": "treadmill",                # 器械类型 key（见 equipment_config.py）
      "duration": 1050,                        # 总时长（秒）
      "segments": [                            # 课程环节列表（按时间升序）
        {"name": "热身激活", "start": 60,  "end": 170},
        {"name": "快速跑",   "start": 680, "end": 800}
      ],
      "points": [                              # 各环节起点处的强度数据（分段恒定）
        {"time": 60,  "speed": 2, "pace": "30:00", "action": "机上热身"},
        {"time": 170, "speed": 8, "pace": "7:30",  "action": "慢跑", "keyword": "有氧输出"}
      ]
    }

说明：
    - 健身课件的强度（速度/坡度/阻力等）在「一个环节内恒定」，所以 points 只需在
      每个环节起点记录一次，查询时取「最后一个 time <= t 的点」（step 阶梯查询）。
    - speed / incline / distance 等数值字段，值可能为 null（表示该环节无此指标，
      例如冷身/拉伸环节没有跑步机速度）。前端对 null 显示为 "—"。
    - pace 配速用 "mm:ss" 字符串（如 "7:30" = 7 分 30 秒/公里），由速度换算而来。
    - action（动作名称）、keyword（关键词/强度标签）为可选文本字段。
"""

import json
import os

from equipment_config import (
    get_equipment,
    get_field_order,
    SEGMENT_KEY,
    ACTION_KEY,
    KEYWORD_KEY,
    TIME_KEY,
)


class CourseData:
    def __init__(self, path: str):
        self.path = path
        with open(path, "r", encoding="utf-8") as f:
            self.raw = json.load(f)

        self.course_name = self.raw.get("course_name", "")
        self.title = self.raw.get("title", self.course_name)  # 课程主题（友好显示名）
        self.equipment = self.raw.get("equipment", "treadmill")
        self.duration = float(self.raw.get("duration", 0))

        # 环节列表（按 start 排序）
        self.segments = sorted(self.raw.get("segments", []), key=lambda s: s.get("start", 0))

        # 采样点（按 time 排序）
        self.points = sorted(self.raw.get("points", []), key=lambda p: p.get(TIME_KEY, 0))

        # 器械字段配置
        self.eq_cfg = get_equipment(self.equipment)
        self.field_order = get_field_order(self.equipment)

    # ---------- 查询接口 ----------

    def _point_at(self, t: float):
        """返回时刻 t 所在的上一个采样点（time <= t 的最后一个点）。"""
        last = None
        for p in self.points:
            pt = float(p.get(TIME_KEY, 0))
            if pt <= t:
                last = p
            else:
                break
        return last

    def segment_at(self, t: float) -> str:
        """返回时刻 t 所在的课程环节名称；超出范围返回空字符串。"""
        for seg in self.segments:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
            if start <= t < end:
                return seg.get("name", "")
        return ""

    def segment_info_at(self, t: float) -> dict:
        """返回时刻 t 所在环节的完整信息 dict；不在任何环节内返回 None。

        返回：{"name", "start", "end", "remaining"}，其中 remaining = end - t（秒）。
        """
        for seg in self.segments:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
            if start <= t < end:
                return {
                    "name": seg.get("name", ""),
                    "start": start,
                    "end": end,
                    "remaining": end - t,
                }
        return None

    def next_segment_at(self, t: float) -> dict:
        """返回时刻 t 之后的下一个环节信息 dict；没有则 None。

        返回：{"name", "start", "end"}，start 为下一环节开始时间（秒）。
        用于「下一环节预告」。
        """
        for seg in self.segments:
            start = float(seg.get("start", 0))
            if start > t:
                return {
                    "name": seg.get("name", ""),
                    "start": start,
                    "end": float(seg.get("end", start)),
                }
        return None

    def next_point_at(self, t: float) -> dict:
        """返回时刻 t 之后第一个含 action 或 keyword 的 point 信息；没有则 None。

        返回：{"name" (action), "keyword", "start" (time 秒)}。

        与 next_segment_at 的区别：segment 是粗粒度大环节（例如「间歇组」可能
        持续 10 分钟包含 5 组冲刺+恢复），而 point 是细粒度采样点（每 30~60s
        一个动作/强度变化）。间歇训练中需要在「下一小环节」粒度上做预告，
        才能看到「冲刺 → 轻松跑 → 节奏跑 ...」的真实小节切换。
        """
        for p in self.points:
            pt = float(p.get(TIME_KEY, 0))
            if pt > t and (ACTION_KEY in p or KEYWORD_KEY in p):
                return {
                    "name": p.get(ACTION_KEY, "") or "",
                    "keyword": p.get(KEYWORD_KEY, "") or "",
                    "start": pt,
                }
        return None

    def action_at(self, t: float) -> str:
        """返回时刻 t 的动作名称；无则空字符串。"""
        p = self._point_at(t)
        return (p or {}).get(ACTION_KEY, "") or ""

    def keyword_at(self, t: float) -> str:
        """返回时刻 t 的关键词/强度标签；无则空字符串。"""
        p = self._point_at(t)
        return (p or {}).get(KEYWORD_KEY, "") or ""

    def intensity_at(self, t: float) -> str:
        """返回时刻 t 的强度标签（仅徒手课有意义）："high" / "low" / ""。"""
        p = self._point_at(t)
        return (p or {}).get("intensity", "") or ""

    def reps_at(self, t: float):
        """返回时刻 t 的动作个数（仅徒手课有意义）；无则 None。"""
        p = self._point_at(t)
        v = (p or {}).get("reps")
        return v if v is not None else None

    def intensity_segments(self) -> list:
        """把 points 序列转成「徒手课强度阶梯段」列表（每段一个 constant 强度）。

        返回：[{"start", "end", "intensity", "reps", "action"}, ...]
        - 强度来自每个 point 的 intensity 字段（"high"/"low"/""）。
        - end 默认为「下一个有 action 的 point 的 time」；最后一个段 end = duration。
          这样阶梯图能自动延伸到课程末尾，不会出现"最后一段没尾巴"。
        - action/reps 直接透传 point 字段（绘图/悬浮提示用）。

        注意：与 points 不同，强度段是「连续覆盖整节课时长」的，不会有间隙。
        """
        if not self.points:
            return []
        # 过滤掉"没有 action 也没有 intensity"的纯时间码点（理论上 xlsx_to_json 不会产出）
        pts = [p for p in self.points if ACTION_KEY in p or p.get("intensity")]
        if not pts:
            return []
        result = []
        for i, p in enumerate(pts):
            start = float(p.get(TIME_KEY, 0))
            end = float(pts[i + 1].get(TIME_KEY, start)) if i + 1 < len(pts) else self.duration
            if end < start:
                end = start
            result.append({
                "start": start,
                "end": end,
                "intensity": p.get("intensity") or "low",
                "reps": p.get("reps"),
                "action": p.get(ACTION_KEY, "") or "",
            })
        return result

    def values_at(self, t: float) -> dict:
        """返回时刻 t 的各个指标值（仅返回当前采样点里实际存在且非 null 的字段）。

        返回值 dict 的 key 是字段 key（如 "speed"、"incline"），value 是显示值。
        null 值会被跳过（前端对该字段显示 "—"）。
        """
        p = self._point_at(t)
        if not p:
            return {}
        result = {}
        for field in self.field_order:
            key = field["key"]
            if key in p and p[key] is not None:
                result[key] = p[key]
        return result


def load_course(path: str) -> CourseData:
    """加载课程数据文件，返回 CourseData 实例。"""
    return CourseData(path)


def list_course_files(data_dir: str):
    """列出数据目录下所有 .json 课程文件路径。"""
    if not os.path.isdir(data_dir):
        return []
    files = [f for f in os.listdir(data_dir) if f.lower().endswith(".json")]
    return [os.path.join(data_dir, f) for f in sorted(files)]
