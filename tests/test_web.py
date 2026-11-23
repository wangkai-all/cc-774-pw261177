"""HTTP 端到端测试：在随机端口启动标准库服务，覆盖静态资源与全部 REST 操作。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fundflow.web.server import _Handler, build_server
from tests.factories import build_ledger


def _request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestWebAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server: ThreadingHTTPServer = build_server(
            build_ledger(), host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_health_and_static(self):
        status, body = _request(self.base, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        for path, ctype in (("/", "text/html"), ("/app.js", "javascript"),
                            ("/styles.css", "css")):
            req = urllib.request.Request(self.base + path)
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(ctype, resp.headers["Content-Type"])

    def test_state_contains_disclaimer(self):
        status, body = _request(self.base, "GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("不构成", body["disclaimer"])
        self.assertTrue(body["chains"])
        self.assertTrue(body["drafts"])

    def test_objective_switch(self):
        status, body = _request(self.base, "POST", "/api/objective",
                                {"objective": "min_chains"})
        self.assertEqual(status, 200)
        self.assertEqual(body["objective"], "min_chains")

    def test_lock_conflict_409_and_unlock(self):
        _, state = _request(self.base, "GET", "/api/state")
        cid = state["chains"][0]["chain_id"]
        status, body = _request(self.base, "POST", "/api/lock",
                                {"chain_id": cid, "amount_cents": 1})
        self.assertEqual(status, 200)
        self.assertTrue(any(l["chain_id"] == cid for l in body["locks"]))
        # 超额锁定
        status, body = _request(self.base, "POST", "/api/lock",
                                {"chain_id": cid, "amount_cents": 999_999})
        self.assertEqual(status, 409)
        self.assertTrue(body["conflicts"])
        # 解锁
        status, body = _request(self.base, "POST", "/api/unlock",
                                {"chain_id": cid})
        self.assertEqual(status, 200)
        self.assertFalse(any(l["chain_id"] == cid for l in body["locks"]))

    def test_exclude_include_confirm_recompute_flow(self):
        _, state = _request(self.base, "GET", "/api/state")
        txn = state["transactions"][0]["txn_id"]
        status, body = _request(self.base, "POST", "/api/exclude",
                                {"txn_id": txn})
        self.assertEqual(status, 200)
        self.assertIn(txn, body["excluded_txn_ids"])
        status, body = _request(self.base, "POST", "/api/include",
                                {"txn_id": txn})
        self.assertEqual(status, 200)
        self.assertNotIn(txn, body["excluded_txn_ids"])

        sid = body["drafts"][0]["solution_id"]
        status, confirmed = _request(self.base, "POST", "/api/confirm",
                                     {"solution_id": sid, "note": "http 确认"})
        self.assertEqual(status, 200)
        self.assertTrue(confirmed["confirmed"]["confirmed"])

        status, body = _request(self.base, "POST", "/api/recompute")
        self.assertEqual(status, 200)
        self.assertTrue(any(c["solution_id"] == sid for c in body["confirmed"]))

    def test_chain_detail(self):
        _, state = _request(self.base, "GET", "/api/state")
        cid = state["drafts"][0]["allocations"][0]["chain_id"]
        status, body = _request(self.base, "GET", f"/api/chains/{cid}")
        self.assertEqual(status, 200)
        self.assertIn("steps", body)
        self.assertTrue(body["steps"])

    def test_bad_path_404(self):
        status, _ = _request(self.base, "GET", "/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
