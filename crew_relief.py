#!/usr/bin/env python3
"""机组接替补班判定与执行。

接替规则（缺一不可）：
1. 后备机组存在且未停用，基地必须与超限航段出发机场一致（从属地起飞接班）；
2. 接替时段与该机组在本方案其它航段及已锁定方案的航段不重叠；
3. 把航段接进该机组自己的执勤序列（从其 duty_start 起算）后不超 max_duty_minutes。

执行时原机组当场释放该航段（assignment 的 crew_id 改为接班机组），
接班机组状态置为 active 并在后续航段继续累计；接后仍超限或冲突直接拦截。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from crew_duty import load_legs


class ReliefError(Exception):
    """接替判定失败。app 层负责翻译成 ApiError，避免与 __main__ 模块身份耦合。"""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _leg_for(conn: Any, plan_id: int, assignment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT a.id assignment_id,a.plan_id,a.status assignment_status,a.crew_id current_crew_id,
                  a.new_std,a.new_sta,f.id flight_id,f.flight_no,f.origin,f.destination
             FROM assignments a JOIN flights f ON f.id=a.flight_id
            WHERE a.plan_id=? AND a.id=?""",
        (plan_id, assignment_id),
    ).fetchone()
    return dict(row) if row else None


def _busy_intervals(conn: Any, plan_id: int, crew_id: str, assignment_id: int) -> list[tuple[datetime, datetime, int]]:
    """该机组的占用时段：本方案其它未取消航段 + 已锁定方案的执行中航段。"""
    intervals: list[tuple[datetime, datetime, int]] = []
    rows = conn.execute(
        """SELECT id,new_std,new_sta FROM assignments
            WHERE plan_id=? AND crew_id=? AND status!='canceled' AND id!=?""",
        (plan_id, crew_id, assignment_id),
    ).fetchall()
    rows += conn.execute(
        """SELECT a.id,a.new_std,a.new_sta FROM assignments a
            JOIN recovery_plans p ON p.id=a.plan_id
           WHERE p.status='locked' AND a.status!='canceled' AND a.crew_id=?""",
        (crew_id,),
    ).fetchall()
    for row in rows:
        intervals.append((_parse(row["new_std"]), _parse(row["new_sta"]), row["id"]))
    return intervals


def _duty_after_relief(conn: Any, plan_id: int, crew_id: str, assignment_id: int) -> dict[str, Any]:
    """假设 assignment_id 交给 crew_id 后，重算该机组的累计执勤（不写库）。"""
    legs = [leg for leg in load_legs(conn, plan_id) if leg["assignment_status"] != "canceled"]
    picked = next((leg for leg in legs if leg["assignment_id"] == assignment_id), None)
    own = [leg for leg in legs if leg["crew_id"] == crew_id and leg["assignment_id"] != assignment_id]
    crew = conn.execute("SELECT * FROM crew WHERE id=?", (crew_id,)).fetchone()
    if picked is None or crew is None:
        return {"eligible": False, "reason": "missing_crew_or_leg"}
    duty_start = _parse(crew["duty_start"])
    rows = sorted(own + [{
        "assignment_id": picked["assignment_id"],
        "new_std": picked["new_std"],
        "new_sta": picked["new_sta"],
        "crew_id": crew_id,
    }], key=lambda item: (item["new_std"], item["assignment_id"]))
    maximum = crew["max_duty_minutes"]
    cumulative = 0
    first_overrun = None
    for item in rows:
        cumulative = int((_parse(item["new_sta"]) - duty_start).total_seconds() // 60)
        if cumulative > maximum and first_overrun is None:
            first_overrun = {"assignment_id": item["assignment_id"], "cumulative_minutes": cumulative,
                             "max_duty_minutes": maximum, "overtime_minutes": cumulative - maximum}
    return {
        "eligible": first_overrun is None,
        "cumulative_minutes": cumulative,
        "max_duty_minutes": maximum,
        "remaining_minutes": maximum - cumulative,
        "overrun": first_overrun,
    }


def list_candidates(conn: Any, plan_id: int, assignment_id: int) -> dict[str, Any]:
    """为指定超限航段列出可接替人选及每人是否合格/拦截原因。"""
    leg = _leg_for(conn, plan_id, assignment_id)
    if leg is None:
        return {"assignment_id": assignment_id, "origin": None, "candidates": []}
    if leg["assignment_status"] == "canceled":
        return {"assignment_id": assignment_id, "origin": leg["origin"], "candidates": []}
    start, finish = _parse(leg["new_std"]), _parse(leg["new_sta"])
    pool = conn.execute(
        """SELECT * FROM crew WHERE base=? AND id!=? ORDER BY
             CASE status WHEN 'standby' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, id""",
        (leg["origin"], leg["current_crew_id"]),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for crew in pool:
        reasons: list[str] = []
        if crew["status"] == "inactive":
            reasons.append("inactive")
        clashes = [other_id for other_start, other_end, other_id in _busy_intervals(conn, plan_id, crew["id"], assignment_id)
                   if _overlaps(start, finish, other_start, other_end)]
        if clashes:
            reasons.append("time_conflict")
        duty = _duty_after_relief(conn, plan_id, crew["id"], assignment_id)
        if not duty["eligible"]:
            reasons.append("duty_limit")
        candidates.append({
            "crew_id": crew["id"],
            "crew_name": crew["name"],
            "base": crew["base"],
            "status": crew["status"],
            "duty_start": crew["duty_start"],
            "max_duty_minutes": crew["max_duty_minutes"],
            "eligible": not reasons,
            "reject_reasons": reasons,
            "projected_cumulative_minutes": duty["cumulative_minutes"],
            "projected_remaining_minutes": duty["remaining_minutes"],
            "conflict_assignment_ids": clashes,
        })
    return {"assignment_id": assignment_id, "flight_no": leg["flight_no"], "origin": leg["origin"],
            "destination": leg["destination"], "new_std": leg["new_std"], "new_sta": leg["new_sta"],
            "current_crew_id": leg["current_crew_id"], "candidates": candidates}


def relief_records(conn: Any, plan_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM crew_relief WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]


def apply_relief(conn: Any, plan_id: int, actor: str, role: str, body: dict[str, Any],
                 audit: Callable[..., None]) -> dict[str, Any]:
    """在已开启事务的连接上执行换班，返回接班记录所需字段；不合格直接抛 ReliefError。

    audit(conn, plan_id, actor, role, action, detail) 由 app 层注入。
    """
    assignment_id, reliever_id = body.get("assignment_id"), str(body.get("relief_crew_id", "")).strip()
    if not isinstance(assignment_id, int) or not reliever_id:
        raise ReliefError(400, "invalid_relief", "assignment_id 和 relief_crew_id 必填")

    leg = _leg_for(conn, plan_id, assignment_id)
    if leg is None:
        raise ReliefError(404, "assignment_not_found", "待接替的航段调整不存在")
    if leg["assignment_status"] == "canceled":
        raise ReliefError(409, "assignment_canceled", "已取消航段不能接替")
    reliever = conn.execute("SELECT * FROM crew WHERE id=?", (reliever_id,)).fetchone()
    if not reliever:
        raise ReliefError(404, "crew_not_found", f"后备机组 {reliever_id} 不存在")
    if reliever["id"] == leg["current_crew_id"]:
        raise ReliefError(409, "relief_same_crew", "接班机组与原机组相同，无需换班")
    if reliever["base"] != leg["origin"]:
        raise ReliefError(409, "base_mismatch",
                          f"基地不匹配: 航段从 {leg['origin']} 起飞，机组基地为 {reliever['base']}")
    if reliever["status"] == "inactive":
        raise ReliefError(409, "crew_unavailable", f"机组 {reliever_id} 已停用")

    start, finish = _parse(leg["new_std"]), _parse(leg["new_sta"])
    clashes = [other_id for other_start, other_end, other_id
               in _busy_intervals(conn, plan_id, reliever_id, assignment_id)
               if _overlaps(start, finish, other_start, other_end)]
    if clashes:
        raise ReliefError(409, "time_conflict", f"机组 {reliever_id} 在该时段已有航段", {"conflict_assignment_ids": clashes})

    duty = _duty_after_relief(conn, plan_id, reliever_id, assignment_id)
    if not duty["eligible"]:
        overrun = duty["overrun"] or {}
        raise ReliefError(409, "relief_duty_limit",
                          f"接替后机组 {reliever_id} 累计执勤 {overrun.get('cumulative_minutes')} 分钟，"
                          f"超过上限 {overrun.get('max_duty_minutes')} 分钟", overrun)

    # 原机组当场释放航段，接班机组继续累计
    conn.execute("UPDATE assignments SET crew_id=? WHERE id=? AND plan_id=?",
                 (reliever_id, assignment_id, plan_id))
    if reliever["status"] != "active":
        conn.execute("UPDATE crew SET status='active' WHERE id=?", (reliever_id,))
    cur = conn.execute(
        """INSERT INTO crew_relief(plan_id,assignment_id,flight_id,flight_no,origin,destination,
                                   released_crew_id,relief_crew_id,relief_base,new_std,new_sta,
                                   projected_cumulative_minutes,max_duty_minutes,created_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (plan_id, assignment_id, leg["flight_id"], leg["flight_no"], leg["origin"], leg["destination"],
         leg["current_crew_id"], reliever_id, reliever["base"], leg["new_std"], leg["new_sta"],
         duty["cumulative_minutes"], duty["max_duty_minutes"], actor,
         _iso(datetime.now(timezone.utc))),
    )
    conn.execute("UPDATE recovery_plans SET revision=revision+1 WHERE id=?", (plan_id,))
    audit(conn, plan_id, actor, role, "crew_relieved",
          {"assignment_id": assignment_id, "flight_no": leg["flight_no"],
           "released_crew_id": leg["current_crew_id"], "relief_crew_id": reliever_id})
    return {"relief_id": cur.lastrowid, "released_crew_id": leg["current_crew_id"],
            "relief_crew_id": reliever_id, "projected_cumulative_minutes": duty["cumulative_minutes"]}
