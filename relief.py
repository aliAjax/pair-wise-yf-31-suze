#!/usr/bin/env python3
"""后备机组接替判定。

判定一名后备机组能否接替某航段，需要同时满足：

1. 身份是后备机组（crew.status == 'reserve'），且不是当前正在执飞该段的机组；
2. 基地对得上：机组 base 与航段起飞机场一致；
3. 时段不冲突：与该方案内其它航段、已锁定方案航段、方案外实际排班均不重叠；
4. 接上去继续累计后不超限：把该段并入候选机组在方案内的航段后，任一段
   累计执勤都不超过本人 max_duty_minutes。

接替补班后，原机组当场释放该段（方案 assignment 的 crew_id 改写），
并在 crew_reliefs 表留下换班记录。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from duty import cumulative_for_crew, over_limit_legs, summarize


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _busy_windows(conn: sqlite3.Connection, plan_id: int, crew_id: str, skip_assignment_id: int) -> list[tuple[datetime, datetime, str]]:
    """候选机组在该方案之外（以及方案内除被接替航段外）的占用时段。"""
    windows: list[tuple[datetime, datetime, str]] = []
    rows = conn.execute("""SELECT a.new_std s,a.new_sta e,p.id pid FROM assignments a
        JOIN recovery_plans p ON p.id=a.plan_id
        WHERE a.crew_id=? AND a.status!='canceled'
        AND (p.id=? AND a.id!=? OR p.id!=? AND p.status='locked')""",
        (crew_id, plan_id, skip_assignment_id, plan_id)).fetchall()
    for row in rows:
        windows.append((parse_time(row["s"]), parse_time(row["e"]), f"方案{row['pid']}"))
    plan_flight_ids = [r[0] for r in conn.execute("SELECT flight_id FROM assignments WHERE plan_id=?", (plan_id,)).fetchall()]
    if plan_flight_ids:
        placeholders = ",".join("?" for _ in plan_flight_ids)
        rows = conn.execute(f"""SELECT std s,sta e,flight_no FROM flights
            WHERE crew_id=? AND status!='canceled' AND id NOT IN ({placeholders})""",
                            (crew_id, *plan_flight_ids)).fetchall()
        for row in rows:
            windows.append((parse_time(row["s"]), parse_time(row["e"]), row["flight_no"]))
    return windows


def plan_payload(conn: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
    """汇总一个方案所需的机组字典与航段（按机组分组），供 duty 模块累计。"""
    plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        return None
    rows = conn.execute("""SELECT a.id assignment_id,a.flight_id,a.crew_id,a.new_std,a.new_sta,a.status,
                                  f.flight_no,f.origin,f.destination
                           FROM assignments a JOIN flights f ON f.id=a.flight_id
                           WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,)).fetchall()
    crews: dict[str, dict[str, Any]] = {}
    legs_by_crew: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "canceled":
            continue
        crew_id = row["crew_id"]
        if crew_id not in crews:
            crew = conn.execute("SELECT * FROM crew WHERE id=?", (crew_id,)).fetchone()
            if crew:
                crews[crew_id] = {"id": crew["id"], "name": crew["name"], "base": crew["base"],
                                  "status": crew["status"], "duty_start": parse_time(crew["duty_start"]),
                                  "max_duty_minutes": crew["max_duty_minutes"]}
        legs_by_crew.setdefault(crew_id, []).append({
            "assignment_id": row["assignment_id"], "flight_id": row["flight_id"], "flight_no": row["flight_no"],
            "origin": row["origin"], "destination": row["destination"],
            "std": parse_time(row["new_std"]), "sta": parse_time(row["new_sta"]),
        })
    return {"plan": dict(plan), "crews": crews, "legs_by_crew": legs_by_crew}


def crew_duty_summaries(conn: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
    """方案内各机组累计执勤 + 超限航段清单。"""
    payload = plan_payload(conn, plan_id)
    if payload is None:
        return None
    summaries = summarize(payload["crews"], payload["legs_by_crew"])
    return {"summaries": summaries, "over_limit_legs": over_limit_legs(summaries)}


def eligible_backups(conn: sqlite3.Connection, plan_id: int, leg: dict[str, Any]) -> list[dict[str, Any]]:
    """为指定超限航段找出全部后备候选，逐项给出能否接替及淘汰原因。"""
    payload = plan_payload(conn, plan_id)
    if payload is None:
        return []
    start, finish = leg["std"], leg["sta"]
    backups = conn.execute("SELECT * FROM crew WHERE status='reserve' ORDER BY id").fetchall()
    candidates: list[dict[str, Any]] = []
    for crew in backups:
        reasons: list[str] = []
        if crew["id"] == leg["current_crew_id"]:
            reasons.append("current_crew")
        if crew["base"] != leg["origin"]:
            reasons.append("base_mismatch")
        busy = [(s, e, where) for s, e, where in _busy_windows(conn, plan_id, crew["id"], leg["assignment_id"])
                if overlaps(start, finish, s, e)]
        if busy:
            reasons.append("time_conflict")
        crew_dict = {"id": crew["id"], "name": crew["name"], "base": crew["base"],
                     "duty_start": parse_time(crew["duty_start"]), "max_duty_minutes": crew["max_duty_minutes"]}
        proposed_legs = list(payload["legs_by_crew"].get(crew["id"], []))
        proposed_legs.append({"assignment_id": leg["assignment_id"], "flight_id": leg["flight_id"],
                              "flight_no": leg.get("flight_no"), "origin": leg["origin"],
                              "destination": leg["destination"], "std": start, "sta": finish})
        projection = cumulative_for_crew(crew_dict, proposed_legs)
        if projection["over_limit"]:
            reasons.append("would_exceed_duty")
        candidates.append({
            "crew_id": crew["id"], "name": crew["name"], "base": crew["base"],
            "duty_start": crew_dict["duty_start"], "max_duty_minutes": crew["max_duty_minutes"],
            "projected_duty_minutes": projection["duty_minutes"],
            "projected_remaining_minutes": projection["remaining_minutes"],
            "eligible": not reasons, "reasons": reasons,
            "conflicts": [{"start": s, "end": e, "source": where} for s, e, where in busy],
        })
    candidates.sort(key=lambda c: (not c["eligible"], c["projected_remaining_minutes"], c["crew_id"]))
    return candidates


def apply_relief(conn: sqlite3.Connection, plan_id: int, assignment_id: int, backup_crew_id: str,
                 actor: str, role: str) -> dict[str, Any] | None:
    """在已有事务内执行一次接替补班：改写 assignment.crew_id 并写换班记录。

    返回记录 dict；航段不存在、已取消或后备机组不存在时返回 None。
    """
    assignment = conn.execute("SELECT * FROM assignments WHERE plan_id=? AND id=?", (plan_id, assignment_id)).fetchone()
    if not assignment or assignment["status"] == "canceled":
        return None
    backup = conn.execute("SELECT * FROM crew WHERE id=? AND status='reserve'", (backup_crew_id,)).fetchone()
    if not backup:
        return None
    from_crew = assignment["crew_id"]
    flight = conn.execute("SELECT * FROM flights WHERE id=?", (assignment["flight_id"],)).fetchone()
    conn.execute("UPDATE assignments SET crew_id=? WHERE id=?", (backup_crew_id, assignment_id))
    detail = {"flight_no": flight["flight_no"] if flight else None,
              "new_std": assignment["new_std"], "new_sta": assignment["new_sta"]}
    conn.execute("""INSERT INTO crew_reliefs(plan_id,assignment_id,flight_id,from_crew_id,to_crew_id,
                     reason,detail_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                 (plan_id, assignment_id, assignment["flight_id"], from_crew, backup_crew_id,
                  "duty_limit", json.dumps(detail, ensure_ascii=False), actor, _now()))
    conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                 (plan_id, actor, role, "crew_relief",
                  json.dumps({"assignment_id": assignment_id, "flight_id": assignment["flight_id"],
                              "from_crew": from_crew, "to_crew": backup_crew_id}, ensure_ascii=False), _now()))
    return {"assignment_id": assignment_id, "flight_id": assignment["flight_id"],
            "from_crew_id": from_crew, "to_crew_id": backup_crew_id,
            "flight_no": flight["flight_no"] if flight else None,
            "new_std": assignment["new_std"], "new_sta": assignment["new_sta"],
            "created_by": actor, "created_at": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_reliefs(conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM crew_reliefs WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
    return [dict(r) for r in rows]
