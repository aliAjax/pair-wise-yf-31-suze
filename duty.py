#!/usr/bin/env python3
"""机组执勤累计：把方案航段按机组归并，逐段计算累计执勤时间并标出超限航段。

口径与 app 校验保持一致：执勤钟从机组签到时间 duty_start 起算，到该航段
结束 new_sta 的经过时间即该段的累计执勤分钟；任一段累计超过
max_duty_minutes，该航段即为超限航段。本模块是纯函数，不读写数据库。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def minutes(delta: Any) -> int:
    return int(delta.total_seconds() // 60)


def cumulative_for_crew(crew: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    """计算单个机组在其一组航段上的逐段累计执勤情况。

    crew 需含 duty_start(datetime) 与 max_duty_minutes；
    leg 需含 std/sta(datetime) 以及航班展示字段。
    """
    duty_start: datetime = crew["duty_start"]
    maximum = int(crew["max_duty_minutes"])
    leg_rows: list[dict[str, Any]] = []
    for leg in sorted(legs, key=lambda x: (x["std"], x["sta"])):
        cumulative = minutes(leg["sta"] - duty_start)
        leg_rows.append({
            "assignment_id": leg["assignment_id"],
            "flight_id": leg["flight_id"],
            "flight_no": leg.get("flight_no"),
            "origin": leg.get("origin"),
            "destination": leg.get("destination"),
            "std": leg["std"],
            "sta": leg["sta"],
            "block_minutes": minutes(leg["sta"] - leg["std"]),
            "cumulative_minutes": cumulative,
            "limit_minutes": maximum,
            "excess_minutes": max(0, cumulative - maximum),
            "over_limit": cumulative > maximum,
        })
    duty_minutes = leg_rows[-1]["cumulative_minutes"] if leg_rows else 0
    return {
        "crew_id": crew["id"],
        "name": crew.get("name", crew["id"]),
        "base": crew.get("base"),
        "duty_start": duty_start,
        "max_duty_minutes": maximum,
        "legs": leg_rows,
        "leg_count": len(leg_rows),
        "flying_minutes": sum(row["block_minutes"] for row in leg_rows),
        "duty_minutes": duty_minutes,
        "remaining_minutes": maximum - duty_minutes,
        "over_limit": any(row["over_limit"] for row in leg_rows),
    }


def summarize(crews: dict[str, dict[str, Any]], legs_by_crew: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """按机组生成累计执勤汇总，按当前累计压力（剩余分钟升序）排列。"""
    result = []
    for crew_id, legs in legs_by_crew.items():
        crew = crews.get(crew_id)
        if crew is None:
            continue
        result.append(cumulative_for_crew(crew, legs))
    result.sort(key=lambda s: (s["remaining_minutes"], s["crew_id"]))
    return result


def over_limit_legs(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从汇总中取出全部超限航段，按起飞时间排序。"""
    legs = [leg for summary in summaries for leg in summary["legs"] if leg["over_limit"]]
    return sorted(legs, key=lambda leg: (leg["std"], leg["sta"]))
