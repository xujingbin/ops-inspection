import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core


class ThresholdTest(unittest.TestCase):
    """三条内置规则的三档阈值边界。"""

    def setUp(self):
        self.rules = {r.code: r for r in core.BUILTIN_RULES}

    def assert_bands(self, code, normal_edge, critical_edge):
        rule = self.rules[code]
        self.assertEqual(rule.evaluate(normal_edge - 0.1), core.NORMAL)
        self.assertEqual(rule.evaluate(normal_edge), core.WARNING)
        self.assertEqual(rule.evaluate(critical_edge - 0.1), core.WARNING)
        self.assertEqual(rule.evaluate(critical_edge), core.CRITICAL)
        self.assertEqual(rule.evaluate(0.0), core.NORMAL)
        self.assertEqual(rule.evaluate(100.0), core.CRITICAL)

    def test_cpu_boundaries(self):
        self.assert_bands("R-INFRA-001", 75.0, 90.0)

    def test_memory_boundaries(self):
        self.assert_bands("R-INFRA-002", 80.0, 90.0)

    def test_disk_boundaries(self):
        self.assert_bands("R-INFRA-003", 80.0, 90.0)

    def test_three_rules_are_builtin(self):
        self.assertEqual(
            [r.metric for r in core.BUILTIN_RULES], ["cpu", "memory", "disk"]
        )


class ScheduleTimeTest(unittest.TestCase):
    def test_daily_today_when_time_not_reached(self):
        now = datetime(2026, 11, 13, 7, 30)  # 周五
        self.assertEqual(core.next_run_time("DAILY", "08:00", now), datetime(2026, 11, 13, 8, 0))

    def test_daily_rolls_to_tomorrow(self):
        now = datetime(2026, 11, 13, 9, 0)
        self.assertEqual(core.next_run_time("DAILY", "08:00", now), datetime(2026, 11, 14, 8, 0))

    def test_daily_exact_time_rolls_forward(self):
        now = datetime(2026, 11, 13, 8, 0)
        self.assertEqual(core.next_run_time("DAILY", "08:00", now), datetime(2026, 11, 14, 8, 0))

    def test_weekly_goes_to_next_monday(self):
        now = datetime(2026, 11, 13, 14, 0)  # 周五
        self.assertEqual(core.next_run_time("WEEKLY", "09:00", now), datetime(2026, 11, 16, 9, 0))

    def test_weekly_on_monday_before_time_uses_today(self):
        now = datetime(2026, 11, 16, 8, 0)  # 周一
        self.assertEqual(core.next_run_time("WEEKLY", "09:00", now), datetime(2026, 11, 16, 9, 0))

    def test_weekly_on_monday_after_time_uses_next_week(self):
        now = datetime(2026, 11, 16, 10, 0)
        self.assertEqual(core.next_run_time("WEEKLY", "09:00", now), datetime(2026, 11, 23, 9, 0))

    def test_invalid_cycle_and_time(self):
        with self.assertRaises(ValueError):
            core.next_run_time("MONTHLY", "08:00")
        for bad in ("8", "25:00", "08:70", "abc"):
            with self.assertRaises(ValueError):
                core.parse_hhmm(bad)


class ExecuteRunTest(unittest.TestCase):
    def test_all_normal(self):
        run = core.execute_run(
            1, "host-a", collectors={"cpu": lambda: 10.0, "memory": lambda: 20.0, "disk": lambda: 30.0}
        )
        self.assertEqual(run.total, 3)
        self.assertEqual(run.counts[core.NORMAL], 3)
        self.assertEqual(run.pass_rate, 100.0)

    def test_mixed_statuses(self):
        run = core.execute_run(
            1, "host-a", collectors={"cpu": lambda: 75.0, "memory": lambda: 92.0, "disk": lambda: 10.0}
        )
        self.assertEqual(run.counts[core.WARNING], 1)
        self.assertEqual(run.counts[core.CRITICAL], 1)
        self.assertEqual(run.counts[core.NORMAL], 1)
        self.assertEqual(run.pass_rate, 33.3)

    def test_collector_exception_becomes_unknown(self):
        def boom():
            raise RuntimeError("vm query timeout")

        run = core.execute_run(
            1,
            "host-a",
            collectors={"cpu": boom, "memory": lambda: 10.0, "disk": lambda: 10.0},
        )
        cpu_item = run.items[0]
        self.assertEqual(cpu_item.status, core.UNKNOWN)
        self.assertIn("RuntimeError", cpu_item.reason)
        self.assertIn("vm query timeout", cpu_item.reason)
        # 单项失败不影响其它项，任务仍然完成
        self.assertEqual(run.finished_at >= run.started_at, True)
        self.assertEqual(run.counts[core.NORMAL], 2)

    def test_collector_returns_none_becomes_unknown(self):
        run = core.execute_run(1, "host-a", collectors={"memory": lambda: None})
        item = run.items[1]
        self.assertEqual(item.status, core.UNKNOWN)
        self.assertIn("空值", item.reason)
        self.assertEqual(item.value_text, "—")

    def test_non_numeric_value_becomes_unknown(self):
        run = core.execute_run(1, "host-a", collectors={"disk": lambda: "n/a"})
        item = run.items[2]
        self.assertEqual(item.status, core.UNKNOWN)
        self.assertIn("不是数值", item.reason)

    def test_unknown_excluded_from_pass_rate(self):
        def boom():
            raise RuntimeError("collect failed")

        run = core.execute_run(
            1, "host-a", collectors={"cpu": boom, "memory": lambda: 50.0, "disk": lambda: 95.0}
        )
        self.assertEqual(run.counts[core.UNKNOWN], 1)
        self.assertEqual(run.counts[core.NORMAL], 1)
        self.assertEqual(run.counts[core.CRITICAL], 1)
        # 1 正常 / (1 正常 + 1 异常) = 50%，UNKNOWN 不进分母
        self.assertEqual(run.pass_rate, 50.0)

    def test_all_unknown_has_no_pass_rate(self):
        def boom():
            raise RuntimeError("down")

        run = core.execute_run(
            1, "host-a", collectors={"cpu": boom, "memory": boom, "disk": boom}
        )
        self.assertEqual(run.counts[core.UNKNOWN], 3)
        self.assertIsNone(run.pass_rate)
        self.assertEqual(run.pass_rate_text, "—")

    def test_missing_collector_becomes_unknown(self):
        run = core.execute_run(
            1,
            "host-a",
            collectors={"cpu": lambda: 10.0, "memory": lambda: 10.0, "disk": lambda: 10.0},
            rules=core.BUILTIN_RULES + [core.Rule("R-X", "自定义", "unknown_metric", 1, 2)],
        )
        self.assertEqual(run.items[-1].status, core.UNKNOWN)
        self.assertIn("未注册采集器", run.items[-1].reason)


class RealCollectorTest(unittest.TestCase):
    """真实读取本机 /proc，确认采集器返回 0~100 的数值。"""

    def test_cpu(self):
        value = core.collect_cpu(interval=0.05)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_memory(self):
        value = core.collect_memory()
        self.assertGreater(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_disk(self):
        value = core.collect_disk()
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)


if __name__ == "__main__":
    unittest.main()
