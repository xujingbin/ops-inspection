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
