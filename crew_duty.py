#!/usr/bin/env python3
"""机组执勤累计。

恢复方案每次调整（新增/改派航段）后，按机组把其名下未取消航段按起飞时间排序，
以机组签到时间 duty_start 为起点逐段累计执勤分钟，累计值超过 max_duty_minutes
的航段即标记为超限航段。校验器锁定前与接替补班页都以本模块为唯一判定来源。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

LEG_SQL = """
SELECT a.id assignment_id, a.status assignment_status, a.new_std, a.new_sta,
       c.id crew_id, c.name crew_name, c.base crew_base, c.status crew_status,
       c.duty_start, c.max_duty_minutes,
       f.id flight_id, f.flight_no, f.origin, f.destination
  FROM assignments a
  JOIN flights f ON f.id = a.flight_id
  JOIN crew c ON c.id = a.crew_id
 WHERE a.plan_id = ?
 ORDER BY a.new_std, a.id
"""


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def load_legs(conn: Any, plan_id: int) -> list[dict[str, Any]]:
    """方案内全部航段（含取消），按新起飞时间排序。"""
    return [dict(row) for row in conn.execute(LEG_SQL, (plan_id,)).fetchall()]


def accumulate(conn: Any, plan_id: int) -> dict[str, Any]:
    """按机组累计执勤。

    返回 {"plan_id", "crews", "overruns"}：
    - crews: 每位机组的有序航段、逐段累计分钟、剩余额度、首个超限航段；
    - overruns: 超限航段的扁平列表（机组字段 + 累计字段），供页面与校验直接使用。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for leg in load_legs(conn, plan_id):
        if leg["assignment_status"] == "canceled":
            continue
        grouped.setdefault(leg["crew_id"], []).append(leg)

    crews: list[dict[str, Any]] = []
    overruns: list[dict[str, Any]] = []
    for crew_id, items in grouped.items():
        items.sort(key=lambda row: (row["new_std"], row["assignment_id"]))
        first = items[0]
        duty_start = _parse(first["duty_start"])
        maximum = first["max_duty_minutes"]
        legs_out: list[dict[str, Any]] = []
        for sequence, leg in enumerate(items, start=1):
            std, sta = _parse(leg["new_std"]), _parse(leg["new_sta"])
            cumulative = _minutes(duty_start, sta)
            overrun = cumulative > maximum
            entry = {
                "sequence": sequence,
                "assignment_id": leg["assignment_id"],
                "flight_id": leg["flight_id"],
                "flight_no": leg["flight_no"],
                "origin": leg["origin"],
                "destination": leg["destination"],
                "new_std": leg["new_std"],
                "new_sta": leg["new_sta"],
                "block_minutes": _minutes(std, sta),
                "cumulative_minutes": cumulative,
                "max_duty_minutes": maximum,
                "overrun": overrun,
                "overtime_minutes": max(0, cumulative - maximum),
            }
            legs_out.append(entry)
            if overrun:
                overruns.append({
                    "crew_id": crew_id,
                    "crew_name": first["crew_name"],
                    "crew_base": first["crew_base"],
                    "duty_start": first["duty_start"],
                    **entry,
                })
        used = legs_out[-1]["cumulative_minutes"]
        crews.append({
            "crew_id": crew_id,
            "crew_name": first["crew_name"],
            "crew_base": first["crew_base"],
            "crew_status": first["crew_status"],
            "duty_start": first["duty_start"],
            "max_duty_minutes": maximum,
            "used_minutes": used,
            "remaining_minutes": maximum - used,
            "overrun": any(leg["overrun"] for leg in legs_out),
            "first_overrun_assignment_id": next((leg["assignment_id"] for leg in legs_out if leg["overrun"]), None),
            "legs": legs_out,
        })

    # 超限机组排前面，调度员一眼看到谁超了
    crews.sort(key=lambda crew: (not crew["overrun"], crew["crew_id"]))
    return {"plan_id": plan_id, "crews": crews, "overruns": overruns}


def overrun_assignment_ids(conn: Any, plan_id: int) -> set[int]:
    """超限航段的 assignment_id 集合，供 _validate_plan 标注 duty_limit。"""
    return {item["assignment_id"] for item in accumulate(conn, plan_id)["overruns"]}
