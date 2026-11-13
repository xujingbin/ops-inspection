import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd, cls.store = app.create_server(
            os.path.join(cls.tmp.name, "test.db"), host="127.0.0.1", port=0
        )
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    # ---------- helpers ----------
    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers

    def post(self, path, data, follow=False):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.url(path), data=body, method="POST")
        opener = urllib.request.build_opener(
            urllib.request.HTTPRedirectHandler() if follow else _NoRedirect()
        )
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers

    def add_host(self, name="linux-sh-app-021"):
        status, _, headers = self.post(
            "/hosts", {"name": name, "hostname": "10.20.3.21", "env": "生产", "team": "交易中心"}
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/")
        return self.store.list_hosts()[0]["id"]

    # ---------- cases ----------
    def test_index_page(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("主机巡检 · 最小闭环", body)
        self.assertIn("主机列表", body)
        self.assertIn("历史执行记录", body)

    def test_create_host_and_list(self):
        name = "linux-test-http-01"
        self.add_host(name)
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(name, body)
        self.assertIn("10.20.3.21", body)

    def test_create_host_validation(self):
        status, body, _ = self.post("/hosts", {"name": "", "hostname": "", "env": "生产", "team": ""})
        self.assertEqual(status, 400)
        self.assertIn("均为必填", body)

    def test_run_and_detail(self):
        host_id = self.add_host("linux-test-run-01")
        status, _, headers = self.post("/runs", {"host_id": str(host_id)})
        self.assertEqual(status, 303)
        location = headers.get("Location")
        self.assertTrue(location.startswith("/runs/"))

        status, body, _ = self.get(location)
        self.assertEqual(status, 200)
        self.assertIn("巡检结果", body)
        self.assertIn("逐项结果", body)
        for text in ("CPU 使用率", "内存使用率", "磁盘使用率"):
            self.assertIn(text, body)
        self.assertIn("不计入分子也不计入分母", body)

    def test_run_without_host_is_rejected(self):
        status, body, _ = self.post("/runs", {"host_id": ""})
        self.assertEqual(status, 400)
        self.assertIn("请先选择要巡检的主机", body)

    def test_run_unknown_host(self):
        status, _, _ = self.post("/runs", {"host_id": "999999"})
        self.assertEqual(status, 404)

    def test_health(self):
        status, body, headers = self.get("/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type"))
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertGreaterEqual(payload["hosts"], 0)

    def test_missing_run_detail(self):
        status, body, _ = self.get("/runs/999999")
        self.assertEqual(status, 404)
        self.assertIn("执行记录不存在", body)

    def test_unknown_path(self):
        status, body, _ = self.get("/nope")
        self.assertEqual(status, 404)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    unittest.main()
