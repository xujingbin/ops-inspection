import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import store


class HostStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = store.Store(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_list_host(self):
        host_id = self.st.add_host("linux-sh-app-021", "10.20.3.21", "生产", "交易中心")
        self.assertIsInstance(host_id, int)
        hosts = self.st.list_hosts()
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["name"], "linux-sh-app-021")
        self.assertEqual(hosts[0]["env"], "生产")
        self.assertEqual(hosts[0]["team"], "交易中心")
        self.assertEqual(self.st.count_hosts(), 1)

    def test_required_fields(self):
        with self.assertRaises(ValueError):
            self.st.add_host("", "10.0.0.1", "生产", "交易中心")
        with self.assertRaises(ValueError):
            self.st.add_host("h1", "", "生产", "交易中心")

    def test_get_host(self):
        host_id = self.st.add_host("h1", "10.0.0.1", "测试", "支付平台")
        self.assertEqual(self.st.get_host(host_id)["hostname"], "10.0.0.1")
        self.assertIsNone(self.st.get_host(9999))


class RuleStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = store.Store(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_seeded_builtin_rules(self):
        rules = self.st.list_rules()
        self.assertEqual([r["code"] for r in rules], ["R-INFRA-001", "R-INFRA-002", "R-INFRA-003"])
        cpu = self.st.get_rule("R-INFRA-001")
        self.assertEqual((cpu["normal_below"], cpu["critical_from"]), (75.0, 90.0))
        self.assertTrue(cpu["enabled"])

    def test_update_thresholds(self):
        updated = self.st.update_rule("R-INFRA-001", 60, 85)
        self.assertEqual((updated["normal_below"], updated["critical_from"]), (60.0, 85.0))
        self.assertEqual(self.st.get_rule("R-INFRA-001")["normal_below"], 60.0)

    def test_disable_rule(self):
        self.st.update_rule("R-INFRA-003", 80, 90, enabled=False)
        codes = [r["code"] for r in self.st.list_rules(enabled_only=True)]
        self.assertNotIn("R-INFRA-003", codes)
        self.assertEqual(len(codes), 2)

    def test_invalid_thresholds(self):
        with self.assertRaises(ValueError):
            self.st.update_rule("R-INFRA-001", 90, 80)   # 正常上限 >= 异常下界
        with self.assertRaises(ValueError):
            self.st.update_rule("R-INFRA-001", 75, 120)  # 超出 0~100
        with self.assertRaises(ValueError):
            self.st.update_rule("R-INFRA-001", "abc", 90)
        with self.assertRaises(ValueError):
            self.st.update_rule("R-NOT-EXIST", 50, 80)

    def test_seed_is_idempotent(self):
        again = store.Store(self.st.path)
        self.assertEqual(len(again.list_rules()), 3)


class PlanStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = store.Store(os.path.join(self.tmp.name, "test.db"))
        self.host_id = self.st.add_host("h1", "10.0.0.1", "生产", "交易中心")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_plan(self):
        plan_id = self.st.add_plan("每日主机巡检", self.host_id, core.CYCLE_DAILY, "08:00")
        plan = self.st.get_plan(plan_id)
        self.assertEqual(plan["name"], "每日主机巡检")
        self.assertTrue(plan["enabled"])
        self.assertIsNone(plan["last_run_at"])
        listed = self.st.list_plans()
        self.assertEqual(listed[0]["host_name"], "h1")

    def test_add_plan_validation(self):
        with self.assertRaises(ValueError):
            self.st.add_plan("", self.host_id, core.CYCLE_DAILY, "08:00")
        with self.assertRaises(ValueError):
            self.st.add_plan("p", 9999, core.CYCLE_DAILY, "08:00")
        with self.assertRaises(ValueError):
            self.st.add_plan("p", self.host_id, "HOURLY", "08:00")
        with self.assertRaises(ValueError):
            self.st.add_plan("p", self.host_id, core.CYCLE_DAILY, "8点")

    def test_due_plans_only_returns_due_and_enabled(self):
        plan_id = self.st.add_plan("p", self.host_id, core.CYCLE_DAILY, "08:00")
        self.assertEqual(self.st.due_plans(), [])  # 刚建的计划在将来
        with self.st._connect() as conn:
            conn.execute(
                "UPDATE plans SET next_run_at = ? WHERE id = ?", ("2020-01-01 00:00:00", plan_id)
            )
        self.assertEqual([p["id"] for p in self.st.due_plans()], [plan_id])
        self.st.set_plan_enabled(plan_id, False)
        self.assertEqual(self.st.due_plans(), [])

    def test_toggle_plan_recomputes_next_run(self):
        plan_id = self.st.add_plan("p", self.host_id, core.CYCLE_DAILY, "08:00")
        self.st.set_plan_enabled(plan_id, False)
        with self.st._connect() as conn:
            conn.execute("UPDATE plans SET next_run_at = ? WHERE id = ?", ("2020-01-01 00:00:00", plan_id))
        again = self.st.set_plan_enabled(plan_id, True)
        self.assertTrue(again["enabled"])
        self.assertGreater(again["next_run_at"], "2020-01-01 00:00:00")

    def test_unknown_plan(self):
        with self.assertRaises(ValueError):
            self.st.set_plan_enabled(9999, True)
        self.assertIsNone(self.st.get_plan(9999))


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = store.Store(os.path.join(self.tmp.name, "test.db"))
        self.host_id = self.st.add_host("h1", "10.0.0.1", "生产", "交易中心")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        def boom():
            raise RuntimeError("collect failed")

        return core.execute_run(
            self.host_id,
            "h1",
            collectors={"cpu": lambda: 95.0, "memory": lambda: 60.0, "disk": boom},
        )

    def test_save_and_get_run(self):
        run = self._run()
        run_id = self.st.save_run(run)
        saved = self.st.get_run(run_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["total"], 3)
        self.assertEqual(saved["critical"], 1)
        self.assertEqual(saved["normal"], 1)
        self.assertEqual(saved["unknown"], 1)
        self.assertEqual(saved["pass_rate"], 50.0)
        self.assertEqual(len(saved["items"]), 3)
        statuses = [i["status"] for i in saved["items"]]
        self.assertIn(core.UNKNOWN, statuses)
        unknown = [i for i in saved["items"] if i["status"] == core.UNKNOWN][0]
        self.assertIn("collect failed", unknown["reason"])
        self.assertEqual(unknown["value_text"], "—")

    def test_list_runs_newest_first(self):
        first = self.st.save_run(self._run())
        second = self.st.save_run(self._run())
        runs = self.st.list_runs()
        self.assertEqual([r["id"] for r in runs], [second, first])

    def test_list_runs_by_host(self):
        other = self.st.add_host("h2", "10.0.0.2", "测试", "支付平台")
        self.st.save_run(self._run())
        run = core.execute_run(other, "h2", collectors={"cpu": lambda: 1, "memory": lambda: 1, "disk": lambda: 1})
        other_run = self.st.save_run(run)
        self.assertEqual([r["id"] for r in self.st.list_runs(host_id=other)], [other_run])

    def test_all_unknown_pass_rate_is_null(self):
        def boom():
            raise RuntimeError("down")

        run = core.execute_run(
            self.host_id, "h1", collectors={"cpu": boom, "memory": boom, "disk": boom}
        )
        saved = self.st.get_run(self.st.save_run(run))
        self.assertIsNone(saved["pass_rate"])
        self.assertEqual(saved["unknown"], 3)

    def test_missing_run(self):
        self.assertIsNone(self.st.get_run(123456))


if __name__ == "__main__":
    unittest.main()
