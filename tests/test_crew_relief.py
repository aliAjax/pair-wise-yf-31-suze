import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, create_server, iso, utcnow
from urllib import request


class CrewReliefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        base = utcnow() + timedelta(days=1)
        self.base = base
        for code in ("AAA", "BBB", "CCC"):
            self.svc.seed_airport("ops", "ops_manager", {"code": code, "country": "CN", "curfew_start": "00:00", "curfew_end": "00:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        # 原机组：已签到 4 小时，上限 12 小时（720 分）
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "原机组", "base": "AAA", "duty_start": iso(base - timedelta(hours=4)), "max_duty_minutes": 720})
        # 合格后备：签到时间晚，接 base+10 结束的航段累计 600 分
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "后备甲", "base": "AAA", "duty_start": iso(base), "max_duty_minutes": 720, "status": "standby"})
        # 基地不符
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR3", "name": "外基地", "base": "BBB", "duty_start": iso(base), "max_duty_minutes": 720, "status": "standby"})
        # 接后超限（与原机组一样已签到 4 小时）
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR4", "name": "额度不足", "base": "AAA", "duty_start": iso(base - timedelta(hours=4)), "max_duty_minutes": 720, "status": "standby"})
        # 本方案已有同时段航段（时段冲突）
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR5", "name": "时段占用", "base": "AAA", "duty_start": iso(base), "max_duty_minutes": 720, "status": "standby"})
        # 已停用
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR7", "name": "停用组", "base": "AAA", "duty_start": iso(base), "max_duty_minutes": 720, "status": "inactive"})
        # 第二个超限航段的合格后备（base+6 才签到，接 base+13 结束航段累计 420 分）
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR6", "name": "后备乙", "base": "AAA", "duty_start": iso(base + timedelta(hours=6)), "max_duty_minutes": 720, "status": "standby"})
        for o, d in (("AAA", "BBB"), ("BBB", "AAA")):
            self.svc.create_permit("ops", "ops_manager", {"origin": o, "destination": d, "valid_from": iso(base - timedelta(days=1)), "valid_to": iso(base + timedelta(days=2))})
        self.flights = {}
        for number, off, hours in (("AB101", 0, 2), ("AB102", 2, 4), ("AB103", 8, 10), ("AB104", 8, 10), ("AB105", 11, 13)):
            origin, dest = ("BBB", "AAA") if number == "AB102" else ("AAA", "BBB")
            aircraft = "AC2" if number == "AB104" else "AC1"
            self.flights[number] = self.svc.create_flight("sched", "scheduler", {
                "flight_no": number, "origin": origin, "destination": dest,
                "std": iso(base + timedelta(hours=off)), "sta": iso(base + timedelta(hours=hours)),
                "aircraft_id": aircraft, "crew_id": "CR1", "passenger_count": 120})

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1",
                                                                       "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=3))})
        assignments = [
            {"flight_id": self.flights["AB101"]["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))},
            {"flight_id": self.flights["AB102"]["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))},
            {"flight_id": self.flights["AB103"]["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": iso(self.base + timedelta(hours=8)), "new_sta": iso(self.base + timedelta(hours=10))},
            # AB104 分给后备 CR5，与 AB103 同时段，制造候选时段冲突
            {"flight_id": self.flights["AB104"]["id"], "aircraft_id": "AC2", "crew_id": "CR5", "new_std": iso(self.base + timedelta(hours=8)), "new_sta": iso(self.base + timedelta(hours=10))},
            {"flight_id": self.flights["AB105"]["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": iso(self.base + timedelta(hours=11)), "new_sta": iso(self.base + timedelta(hours=13))},
        ]
        return self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "延误拖堂", "assignments": assignments})

    def test_accumulation_flags_overruns_and_candidates(self):
        plan = self._plan()
        board = self.svc.crew_board(plan["id"])
        # CR1 签到 4h：AB101 结束累计 6h（360 分）、AB102 8h（480）；AB103 结束 14h（840）、AB105 结束 17h（1020），后两段超限
        over = {v["flight_no"]: v for v in board["overruns"]}
        self.assertEqual(set(over), {"AB103", "AB105"})
        self.assertEqual(over["AB103"]["cumulative_minutes"], 840)
        self.assertEqual(over["AB103"]["overtime_minutes"], 120)
        # 前两段不超限
        cr1 = next(c for c in board["crews"] if c["crew_id"] == "CR1")
        self.assertEqual([leg["cumulative_minutes"] for leg in cr1["legs"] if leg["flight_no"] in ("AB101", "AB102")], [360, 480])

        cand = {c["crew_id"]: c for c in over["AB103"]["candidates"]}
        # 基地对得上才入选；外基地 CR3 根本不在候选池
        self.assertNotIn("CR3", cand)
        self.assertTrue(cand["CR2"]["eligible"])
        self.assertEqual(cand["CR2"]["projected_cumulative_minutes"], 600)
        self.assertEqual(cand["CR4"]["reject_reasons"], ["duty_limit"])
        self.assertEqual(cand["CR5"]["reject_reasons"], ["time_conflict"])
        self.assertEqual(cand["CR7"]["reject_reasons"], ["inactive"])
        self.assertTrue(cand["CR5"]["conflict_assignment_ids"])

        # 校验器与页面同源：锁定前 duty_limit 指向同一批航段
        check = self.svc.validate_plan(plan["id"], "auditor", "auditor")
        self.assertFalse(check["valid"])
        flagged = {p["assignment_id"] for p in check["problems"] if p["code"] == "duty_limit"}
        self.assertEqual(flagged, {over["AB103"]["assignment_id"], over["AB105"]["assignment_id"]})

    def test_relief_releases_original_crew_and_blocks_further_overrun(self):
        plan = self._plan()
        board = self.svc.crew_board(plan["id"])
        l3 = next(v for v in board["overruns"] if v["flight_no"] == "AB103")

        # 缺少 expected_revision
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "sched", "scheduler", {"assignment_id": l3["assignment_id"], "relief_crew_id": "CR2"})
        self.assertEqual(ctx.exception.code, "revision_required")
        # viewer 无权换班
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "v", "viewer", {"expected_revision": 1, "assignment_id": l3["assignment_id"], "relief_crew_id": "CR2"})
        self.assertEqual(ctx.exception.status, 403)

        out = self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                    {"expected_revision": 1, "assignment_id": l3["assignment_id"], "relief_crew_id": "CR2"})
        # 原机组当场释放，接班后备转 active 并继续累计
        self.assertEqual(out["relief"]["released_crew_id"], "CR1")
        self.assertEqual(out["relief"]["relief_crew_id"], "CR2")
        self.assertEqual(out["board"]["plan"]["revision"], 2)
        cr2 = next(c for c in out["board"]["crews"] if c["crew_id"] == "CR2")
        self.assertEqual(cr2["crew_status"], "active")
        self.assertFalse(cr2["overrun"])
        self.assertEqual(cr2["used_minutes"], 600)
        # CR1 只剩 AB105 一段超限
        self.assertEqual([v["flight_no"] for v in out["board"]["overruns"]], ["AB105"])
        self.assertEqual(len(out["board"]["relief_records"]), 1)
        record = out["board"]["relief_records"][0]
        self.assertEqual((record["released_crew_id"], record["relief_crew_id"], record["relief_base"]), ("CR1", "CR2", "AAA"))

        board = out["board"]
        l5 = next(v for v in board["overruns"] if v["flight_no"] == "AB105")
        # 接班机组继续累计：CR2 再接 AB105 累计将达 780 分，超限直接拦截
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                  {"expected_revision": 2, "assignment_id": l5["assignment_id"], "relief_crew_id": "CR2"})
        self.assertEqual(ctx.exception.code, "relief_duty_limit")
        self.assertEqual(ctx.exception.details["cumulative_minutes"], 780)
        # 外基地拦截
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                  {"expected_revision": 2, "assignment_id": l5["assignment_id"], "relief_crew_id": "CR3"})
        self.assertEqual(ctx.exception.code, "base_mismatch")
        # 停用机组拦截
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                  {"expected_revision": 2, "assignment_id": l5["assignment_id"], "relief_crew_id": "CR7"})
        self.assertEqual(ctx.exception.code, "crew_unavailable")
        # 版本冲突
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                  {"expected_revision": 1, "assignment_id": l5["assignment_id"], "relief_crew_id": "CR6"})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        # 合格后备 CR6 接替成功，方案执勤超限清零（CR5 备用资源状态问题不在本测试范围）
        out = self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                    {"expected_revision": 2, "assignment_id": l5["assignment_id"], "relief_crew_id": "CR6"})
        self.assertEqual([v["flight_no"] for v in out["board"]["overruns"] if v["crew_id"] in ("CR1", "CR6")], [])
        self.assertEqual(len(out["board"]["relief_records"]), 2)

    def test_locked_plan_cannot_relieve(self):
        # 一个可以顺利锁定的小方案，锁定后换班被拦
        f = self.svc.create_flight("sched", "scheduler", {"flight_no": "AB200", "origin": "AAA", "destination": "BBB",
                                                          "std": iso(self.base + timedelta(hours=5)), "sta": iso(self.base + timedelta(hours=7)),
                                                          "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 10})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR8", "name": "现役乙", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        d = self.svc.create_disruption("sched", "scheduler", {"kind": "crew_timeout", "resource_id": "CR1",
                                                              "starts_at": iso(self.base + timedelta(hours=5)), "ends_at": iso(self.base + timedelta(hours=7))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": d["id"], "name": "锁定方案",
                                                           "assignments": [{"flight_id": f["id"], "aircraft_id": "AC2", "crew_id": "CR8",
                                                                            "new_std": iso(self.base + timedelta(hours=5)), "new_sta": iso(self.base + timedelta(hours=7))}]})
        self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        board = self.svc.crew_board(plan["id"])
        self.assertFalse(board["overruns"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_crew(plan["id"], "sched", "scheduler",
                                  {"expected_revision": 1, "assignment_id": plan["assignments"][0]["id"], "relief_crew_id": "CR2"})
        self.assertEqual(ctx.exception.code, "plan_locked")

    def test_http_routes(self):
        plan = self._plan()
        server = create_server(Path(self.tmp.name) / "test.db", "127.0.0.1", 0)
        port = server.server_port
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            page = request.urlopen(f"http://127.0.0.1:{port}/crew", timeout=5).read().decode()
            self.assertIn("机组接替补班", page)
            req = request.Request(f"http://127.0.0.1:{port}/api/plans/{plan['id']}/crew-board",
                                  headers={"X-User-Id": "v", "X-Role": "viewer"})
            payload = request.urlopen(req, timeout=5).read().decode()
            self.assertIn("AB103", payload)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
