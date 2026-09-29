import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow


class CrewReliefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "relief.db")
        # 固定在正午前后，不受宵禁窗口影响
        self.base = utcnow().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=2)
        b = self.base
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "CCC", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        for ac in ("AC1", "AC2", "AC3"):
            self.svc.seed_aircraft("ops", "ops_manager", {"id": ac, "model": "A320", "maintenance_due": iso(b + timedelta(days=5))})
        # CR1 上限 10 小时：签到 11:00，22:00 结束累计 11 小时 -> 超限
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "原班", "base": "AAA", "duty_start": iso(b - timedelta(hours=1)), "max_duty_minutes": 600})
        self.svc.seed_crew("ops", "ops_manager", {"id": "RV1", "name": "合格后备", "base": "AAA", "duty_start": iso(b - timedelta(hours=1)), "max_duty_minutes": 900, "status": "reserve"})
        self.svc.seed_crew("ops", "ops_manager", {"id": "RV2", "name": "基地不符", "base": "CCC", "duty_start": iso(b - timedelta(hours=1)), "max_duty_minutes": 900, "status": "reserve"})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(b - timedelta(days=1)), "valid_to": iso(b + timedelta(days=2))})

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        b = self.base
        f = self.svc.create_flight("s", "scheduler", {"flight_no": "AB1", "origin": "AAA", "destination": "BBB",
            "std": iso(b + timedelta(hours=1)), "sta": iso(b + timedelta(hours=3)), "aircraft_id": "AC1", "crew_id": "CR1"})
        d = self.svc.create_disruption("s", "scheduler", {"kind": "airport_closure", "resource_id": "AAA",
            "starts_at": iso(b), "ends_at": iso(b + timedelta(hours=2))})
        return self.svc.create_plan("s", "scheduler", {"disruption_id": d["id"], "name": "延误", "assignments": [
            {"flight_id": f["id"], "aircraft_id": "AC1", "crew_id": "CR1",
             "new_std": iso(b + timedelta(hours=8)), "new_sta": iso(b + timedelta(hours=10))}]}), f

    def test_duty_accumulation_flags_over_limit_leg(self):
        plan, _ = self._plan()
        data = self.svc.crew_duty(plan["id"])
        self.assertEqual(len(data["over_limit_legs"]), 1)
        leg = data["over_limit_legs"][0]
        self.assertEqual(leg["flight_no"], "AB1")
        self.assertEqual((leg["cumulative_minutes"], leg["limit_minutes"], leg["excess_minutes"]), (660, 600, 60))
        cands = {c["crew_id"]: c for c in leg["candidates"]}
        self.assertTrue(cands["RV1"]["eligible"])
        self.assertEqual(cands["RV2"]["reasons"], ["base_mismatch"])
        # 调整后、锁定前即可发现超限，validate 也拦住
        check = self.svc.validate_plan(plan["id"], "auditor", "auditor")
        self.assertFalse(check["valid"])
        self.assertIn("duty_limit", {p["code"] for p in check["problems"]})

    def test_relief_releases_leg_and_continues_accumulation(self):
        plan, flight = self._plan()
        leg = self.svc.crew_duty(plan["id"])["over_limit_legs"][0]
        result = self.svc.relieve_plan(plan["id"], "s", "scheduler",
                                      {"expected_revision": 1, "assignment_id": leg["assignment_id"], "crew_id": "RV1"})
        self.assertEqual(result["relieved"][0]["from_crew_id"], "CR1")
        self.assertEqual(result["relieved"][0]["to_crew_id"], "RV1")
        after = self.svc.crew_duty(plan["id"])
        self.assertEqual(after["over_limit_legs"], [])
        legs_by_crew = {s["crew_id"]: s for s in after["crew_duty"]}
        # 原机组当场释放：CR1 不再执飞该段；RV1 继续累计到 660 分钟
        self.assertNotIn("CR1", legs_by_crew)
        self.assertEqual([l["flight_no"] for l in legs_by_crew["RV1"]["legs"]], ["AB1"])
        self.assertEqual(legs_by_crew["RV1"]["duty_minutes"], 660)
        # 换班记录
        logs = after["reliefs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0]["from_crew_id"], logs[0]["to_crew_id"], logs[0]["reason"]), ("CR1", "RV1", "duty_limit"))
        # 接替后校验通过、可锁定，航班实际改派
        self.assertTrue(self.svc.validate_plan(plan["id"], "auditor", "auditor")["valid"])
        locked = self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 2})
        self.assertEqual(locked["status"], "locked")
        stored = self.svc.repo.conn.execute("SELECT crew_id FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(stored["crew_id"], "RV1")

    def test_ineligible_backup_and_cascade_are_blocked(self):
        b = self.base
        plan, _ = self._plan()
        leg = self.svc.crew_duty(plan["id"])["over_limit_legs"][0]
        # 基地不符的后备不能接
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_plan(plan["id"], "s", "scheduler",
                                  {"expected_revision": 1, "assignment_id": leg["assignment_id"], "crew_id": "RV2"})
        self.assertEqual(ctx.exception.code, "backup_not_eligible")
        # 事务回滚：无记录、版本未变
        self.assertEqual(self.svc.crew_duty(plan["id"])["reliefs"], [])
        # 再加一段给唯一后备，制造“接上去继续累计后仍超限”的级联：
        # RV1 上限改 9 小时；两段（18:00-20:00、20:00-22:00）都压给 RV1 时末段累计 660 > 540
        self.svc.seed_crew("ops", "ops_manager", {"id": "RV1", "name": "合格后备", "base": "AAA",
                                                  "duty_start": iso(b - timedelta(hours=1)), "max_duty_minutes": 540, "status": "reserve"})
        f2 = self.svc.create_flight("s", "scheduler", {"flight_no": "AB2", "origin": "AAA", "destination": "BBB",
            "std": iso(b + timedelta(hours=5)), "sta": iso(b + timedelta(hours=7)), "aircraft_id": "AC2", "crew_id": "CR1"})
        plan2 = self.svc.create_plan("s", "scheduler", {"disruption_id": plan["disruption_id"], "name": "两段延误", "assignments": [
            {"flight_id": plan["assignments"][0]["flight_id"], "aircraft_id": "AC1", "crew_id": "CR1",
             "new_std": iso(b + timedelta(hours=8)), "new_sta": iso(b + timedelta(hours=10))},
            {"flight_id": f2["id"], "aircraft_id": "AC2", "crew_id": "CR1",
             "new_std": iso(b + timedelta(hours=6)), "new_sta": iso(b + timedelta(hours=8))},
        ]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_plan(plan2["id"], "s", "scheduler", {"expected_revision": 1, "crew_id": "__auto__"})
        self.assertEqual(ctx.exception.code, "still_over_limit")
        # 整批回滚：没有留下半套换班
        self.assertEqual(self.svc.crew_duty(plan2["id"])["reliefs"], [])

    def test_permissions_and_revision(self):
        plan, _ = self._plan()
        for role in ("viewer", "auditor"):
            with self.assertRaises(ApiError) as ctx:
                self.svc.relieve_plan(plan["id"], "u", role, {"expected_revision": 1, "crew_id": "RV1"})
            self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.relieve_plan(plan["id"], "s", "scheduler", {"expected_revision": 99, "crew_id": "RV1"})
        self.assertEqual(ctx.exception.code, "revision_conflict")


if __name__ == "__main__":
    unittest.main()
