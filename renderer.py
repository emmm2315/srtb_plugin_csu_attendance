"""把 WEDS 到课 / 打卡接口的返回整理成文本。

ShiritaiBot 的插件目前只支持文本回复，所以这里不再有图片模板：

    {
      "Msg": null, "Code": "600",
      "Data": {
        "ClassNum": "10", "WeekNow": "3", "WeekNum": "21",
        "Week": [{"WeekName": "星期日", "Rq": "2026-09-20"}, ...],  # 下标 0..6 = 周日..周六
        "Rank": [
          {"CourseName": "大学物理B（二）", "TeachName": "周良玉",
           "ClassLxBz": "未到",          # 到勤情况：正常 / 未到 / 调课 / null
           "Class": 3, "ClassEnd": 4,     # 起止节次
           "ClassRoom": "A座421", "Rq": "2026-09-22"}
        ]
      }
    }

时间换算与学校作息一致：第 n 节上课时间取 TIMES[n-1]，结束时间 = 上课时间 + 45 分钟。
"""

from __future__ import annotations

import json
import re as _re
from datetime import date as _date
from typing import Any

# 节次上课时间，按学校作息调整
TIMES = [
    "08:00", "08:55", "10:00", "10:55", "14:00", "14:55",
    "16:00", "16:55", "19:00", "19:55", "20:30", "21:25",
]
CLASS_MINUTES = 45
# Week 数组按 周日 起始，下标 +1 即星期几
DAY_NAMES = ["", "周日", "周一", "周二", "周三", "周四", "周五", "周六"]
# 文字输出里用的完整写法
WEEKDAY_FULL = [
    "", "星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六",
]

# 用户输入 -> 接口的星期编号（1=周日 … 7=周六）
WEEKDAY_ALIASES = {
    "日": 1, "天": 1, "7": 1, "周日": 1, "周天": 1, "星期日": 1, "星期天": 1,
    "一": 2, "1": 2, "周一": 2, "星期一": 2,
    "二": 3, "2": 3, "周二": 3, "星期二": 3,
    "三": 4, "3": 4, "周三": 4, "星期三": 4,
    "四": 5, "4": 5, "周四": 5, "星期四": 5,
    "五": 6, "5": 6, "周五": 6, "星期五": 6,
    "六": 7, "6": 7, "周六": 7, "星期六": 7,
}

# 到勤状态分类
OK_STATES = ("正常", "已到", "出勤", "已打卡", "签到")
BAD_STATES = ("未到", "缺勤", "旷课", "迟到", "早退")
NEUTRAL_STATES = ("调课", "请假", "补课", "停课")


def weekday_from_text(text: str) -> int:
    """把「周三 / 星期3 / 3」解析成接口口径的星期编号（1=周日 … 7=周六）。"""
    raw = (text or "").strip().lower()
    value = WEEKDAY_ALIASES.get(raw)
    if value:
        return value
    match = _re.fullmatch(r"(?:星期|周|礼拜)([一二三四五六日天1-7])", raw)
    if match:
        return WEEKDAY_ALIASES.get(match.group(1), 0)
    return 0


def to_minutes(text: str) -> int:
    try:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return -1


def fmt_minutes(total: int) -> str:
    return "%02d:%02d" % ((total // 60) % 24, total % 60)


def time_range(start: int, end: int) -> tuple[str, str]:
    """第 start 节 ~ 第 end 节的 (上课时间, 下课时间)。"""
    if start < 1 or start > len(TIMES):
        return "", ""
    begin = TIMES[start - 1]
    begin_min = to_minutes(begin)
    if end < 1 or end > len(TIMES) or begin_min < 0:
        return begin, ""
    return begin, fmt_minutes(to_minutes(TIMES[end - 1]) + CLASS_MINUTES)


def state_kind(status: Any) -> str:
    """把到勤状态归成 ok / bad / neutral / none 四类。"""
    if status is None:
        return "none"
    text = str(status).strip()
    if not text:
        return "none"
    if any(k in text for k in OK_STATES):
        return "ok"
    if any(k in text for k in BAD_STATES):
        return "bad"
    if any(k in text for k in NEUTRAL_STATES):
        return "neutral"
    return "neutral"


def weekday_of(record: dict[str, Any], week_map: dict[str, int]) -> int:
    """确定这条记录是星期几（1=周日 … 7=周六）。无法判定返回 0。"""
    raw = record.get("Week")
    if raw not in (None, "", 0, "0"):
        try:
            value = int(raw)
            if 1 <= value <= 7:
                return value
        except (TypeError, ValueError):
            pass

    rq = record.get("Rq")
    if rq and rq in week_map:
        return week_map[rq]
    if rq:
        try:
            return _date.fromisoformat(str(rq)[:10]).isoweekday() % 7 + 1
        except ValueError:
            return 0
    return 0


def build_week_map(data: dict[str, Any]) -> dict[str, int]:
    """Rq -> 星期几。Week 数组下标 +1 即星期几。"""
    result: dict[str, int] = {}
    week = data.get("Week")
    if isinstance(week, list):
        for idx, item in enumerate(week):
            if isinstance(item, dict) and item.get("Rq"):
                result[str(item["Rq"])] = idx + 1
    return result


def date_of_week(data: dict[str, Any], weekday: int) -> str:
    """取某星期对应的日期（MM-DD）。"""
    week = data.get("Week")
    if isinstance(week, list) and 1 <= weekday <= len(week):
        item = week[weekday - 1]
        if isinstance(item, dict) and item.get("Rq"):
            return str(item["Rq"])[5:10]
    return ""


def _empty_result() -> dict[str, Any]:
    return {
        "courses": [],
        "stats": {},
        "days": [],
        "week_now": "",
        "week_num": "",
        "class_num": "",
    }


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """把接口 Data 整理成便于渲染 / 同步日程的结构。"""
    if not isinstance(data, dict):
        return _empty_result()

    week_map = build_week_map(data)
    rank = data.get("Rank")
    rank = rank if isinstance(rank, list) else []

    courses: list[dict[str, Any]] = []
    for item in rank:
        if not isinstance(item, dict):
            continue
        weekday = weekday_of(item, week_map)
        start = int(item.get("Class") or item.get("ClassStart") or 1)
        end = int(item.get("ClassEnd") or start)
        if end < start:
            end = start
        begin, finish = time_range(start, end)
        status = item.get("ClassLxBz")
        raw_date = str(item.get("Rq") or "").strip()[:10]
        courses.append(
            {
                "name": item.get("CourseName") or "未知课程",
                "room": item.get("ClassRoom") or "",
                "weekday": weekday,
                "day_name": DAY_NAMES[weekday] if weekday else "未知",
                "date": raw_date[5:10] if raw_date else date_of_week(data, weekday),
                "date_full": raw_date,
                "class_start": start,
                "class_end": end,
                "period": "第 %d%s 节" % (start, "-%d" % end if end > start else ""),
                "time_begin": begin,
                "time_end": finish,
                # ClassSj 是实际打卡时刻，例如 "2026-09-20 07:59:33"（08:00 的课）
                "check_in": str(item.get("ClassSj") or "").strip(),
                "status": status,
                "kind": state_kind(status),
            }
        )

    courses.sort(
        key=lambda c: (
            c["weekday"] if c["weekday"] else 99,
            c["class_start"],
            c["name"],
        )
    )

    stats = {"total": len(courses)}
    for kind, label in (("ok", "正常"), ("bad", "未到"), ("neutral", "其他"), ("none", "无标记")):
        count = sum(1 for c in courses if c["kind"] == kind)
        if count:
            stats[label] = count

    # 有课的星期列表（用于头部摘要）
    days = sorted({c["weekday"] for c in courses if c["weekday"]})

    return {
        "courses": courses,
        "stats": stats,
        "days": days,
        "week_now": data.get("WeekNow") or "",
        "week_num": data.get("WeekNum") or "",
        "class_num": data.get("ClassNum") or "",
    }


def render_day_group(courses: list[dict[str, Any]]) -> str:
    """整周文字输出里的一天（无 emoji）。"""
    head = courses[0]
    date_text = "（%s）" % head["date"] if head["date"] else ""
    lines = ["  %s%s" % (head["day_name"], date_text)]
    for course in courses:
        lines.append(_course_line(course))
    return "\n".join(lines)


def render_week_rank(
    data: dict[str, Any],
    user_serial: str = "",
    title: str = "本周",
    raw: bool = False,
) -> str:
    """渲染到课 / 打卡卡片。raw=True 时附带整理前的原始 JSON。"""
    info = normalize(data)
    courses = info["courses"]

    label = (user_serial or "").strip() or "你"
    week_word = "第%s周" % info["week_now"] if info["week_now"] else "本周"
    lines = ["%s的%s打卡数据:" % (label, week_word)]

    if not courses:
        lines.append("  本周没有课")
    else:
        current_day = None
        for course in courses:
            if course["weekday"] != current_day:
                current_day = course["weekday"]
                group = [c for c in courses if c["weekday"] == current_day]
                if len(lines) > 1:
                    lines.append("")
                lines.append(render_day_group(group))

    counted = {}
    for c in courses:
        if c["kind"] != "none":
            counted[c["kind"]] = counted.get(c["kind"], 0) + 1
    tail = ["共 %d 节" % len(courses)]
    if counted.get("ok"):
        tail.append("正常 %d" % counted["ok"])
    if counted.get("bad"):
        tail.append("未到 %d" % counted["bad"])
    lines.append("")
    lines.append("  " + "，".join(tail))

    body = "\n".join(lines)

    if raw:
        body += "\n\n原始返回:\n%s" % json.dumps(data, ensure_ascii=False, indent=2)[:3000]
    return body


def render_error(payload: dict[str, Any]) -> str:
    """接口返回业务错误时的提示。"""
    code = payload.get("Code", payload.get("code"))
    msg = payload.get("Msg") or payload.get("msg") or "未知错误"
    return "⚠️ 接口返回错误 (Code=%s): %s" % (code, msg)


def check_in_text(raw: str, with_seconds: bool = False) -> str:
    """把 ClassSj 的完整时间戳压成 HH:MM 或 HH:MM:SS。取不到返回空串。"""
    text = (raw or "").strip()
    if not text:
        return ""
    # 形如 "2026-09-20 07:59:33"
    if " " in text:
        text = text.split(" ", 1)[1]
    parts = text.split(":")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return ""
    if with_seconds and len(parts) >= 3 and parts[2].isdigit():
        return "%02d:%02d:%02d" % (int(parts[0]), int(parts[1]), int(parts[2]))
    return "%02d:%02d" % (int(parts[0]), int(parts[1]))


# 文字输出里的状态标签（不使用 emoji）
STATE_LABEL = {
    "ok": "正常",
    "bad": "未到",
    "neutral": "调课/请假",
    "none": "",
}


def state_label(status: Any, kind: str) -> str:
    """状态文字（无 emoji）。有原始状态用原始的，否则按类别给词。"""
    text = str(status).strip() if status not in (None, "") else ""
    if text:
        return text
    return STATE_LABEL.get(kind, "")


def _course_line(course: dict[str, Any], with_day: bool = False) -> str:
    """一条课程记录：课程名 教室 打卡时间HH:MM:SS（无打卡则写"未打卡"）。

    格式与使用者确认的一致：空格分隔、不用 emoji、不打节次。
    """
    bits: list[str] = []
    if with_day and course.get("day_name"):
        bits.append(course["day_name"])
    bits.append(course["name"])
    if course.get("room"):
        bits.append(course["room"])
    checked = check_in_text(course.get("check_in", ""), with_seconds=True)
    bits.append("打卡时间%s" % checked if checked else "未打卡")
    return "  " + " ".join(bits)


def render_day_text(
    data: dict[str, Any],
    weekday: int,
    title: str = "",
    user_label: str = "",
    user_fallback: str = "",
) -> str:
    """单天课表的文字输出。

    格式（与使用者确认）::

        小明的星期三打卡数据:
          高等数学A A座101 打卡时间08:02:02
          大学英语（三） B座214 打卡时间09:44:56

    - 不使用 emoji
    - 条目缩进两格
    - 抬头为「昵称（或学号）的星期X打卡数据:」
    - title 参数保留仅为兼容调用方，不参与输出（星期X 已表明是哪天）
    """
    info = normalize(data)
    courses = [c for c in info["courses"] if c["weekday"] == weekday]

    label = (user_label or "").strip() or (user_fallback or "").strip() or "你"
    day_word = (
        WEEKDAY_FULL[weekday] if 1 <= weekday < len(WEEKDAY_FULL) else "未知日"
    )

    lines = ["%s的%s打卡数据:" % (label, day_word)]
    if not courses:
        lines.append("  这一天没有课")
        return "\n".join(lines)

    for c in courses:
        lines.append(_course_line(c))
    return "\n".join(lines)
