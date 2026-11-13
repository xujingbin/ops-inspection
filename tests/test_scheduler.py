import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import scheduler
import store


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = store.Store(os.path.join(self.tmp.name, "test.db"))
        self.host_id = self.st.add_host("linux-sh-app-021", "10.20.3.21", "生产", "交易中心")
        self.plan_id = self.st.add_plan("每日主机巡检", self.host_id, core.CYCLE_DAILY, "08:00")
        with self.st._connect() as conn:  # 人为置为到期
            conn.execute(
                "UPDATE plans SET next_run_at = ? WHERE id = ?",
                ("2020-01-01 00:00:00", self.plan_id),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def _collectors(self):
        return {"cpu": lambda: 10.0, "memory": lambda: 20.0, "disk": lambda: 30.0}

    def test_due_plan_executes_and_advances_schedule(self):
        moment = datetime(2026, 11, 13, 8, 0, 0)
        ids = scheduler.run_due_plans(self.st, now=moment, collectors=self._collectors())
        self.assertEqual(len(ids), 1)

        run = self.st.get_run(ids[0])
        self.assertEqual(run["source"], "PLAN")
        self.assertEqual(run["plan_id"], self.plan_id)
        self.assertEqual(run["normal"], 3)

        plan = self.st.get_plan(self.plan_id)
        self.assertEqual(plan["last_run_at"], "2026-11-13 08:00:00")
        self.assertEqual(plan["next_run_at"], "2026-11-14 08:00:00")

    def test_second_scan_does_not_duplicate(self):
        moment = datetime(2026, 11, 13, 8, 0, 0)
        scheduler.run_due_plans(self.st, now=moment, collectors=self._collectors())
        self.assertEqual(scheduler.run_due_plans(self.st, now=moment, collectors=self._collectors()), [])

    def test_disabled_plan_is_skipped(self):
        self.st.set_plan_enabled(self.plan_id, False)
        with self.st._connect() as conn:
            conn.execute(
                "UPDATE plans SET next_run_at = ? WHERE id = ?",
                ("2020-01-01 00:00:00", self.plan_id),
            )
        ids = scheduler.run_due_plans(
            self.st, now=datetime(2026, 11, 13, 8, 0), collectors=self._collectors()
        )
        self.assertEqual(ids, [])

    def test_plan_with_missing_host_is_skipped_but_advanced(self):
        with self.st._connect() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE plans SET host_id = 9999 WHERE id = ?", (self.plan_id,))
        ids = scheduler.run_due_plans(
            self.st, now=datetime(2026, 11, 13, 8, 0), collectors=self._collectors()
        )
        self.assertEqual(ids, [])
        self.assertEqual(self.st.get_plan(self.plan_id)["next_run_at"], "2026-11-14 08:00:00")

    def test_manual_run_source_default(self):
        run = core.execute_run(self.host_id, "linux-sh-app-021", collectors=self._collectors())
        saved = self.st.get_run(self.st.save_run(run))
        self.assertEqual(saved["source"], "MANUAL")
        self.assertIsNone(saved["plan_id"])


if __name__ == "__main__":
    unittest.main()
