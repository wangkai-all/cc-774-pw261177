"""标准库实现的 HTTP 服务：把 AnalysisService 暴露为 REST API，并托管静态网页。

零第三方依赖（http.server + json）。所有写操作都在服务内部的锁内完成，
可安全处理并发请求。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..models import Ledger, cents_to_yuan
from ..service import AnalysisService, ConflictError, ServiceError


class _Handler(BaseHTTPRequestHandler):
    server_version = "FundFlow/1.0"

    # 静默默认日志，改用统一格式
    def log_message(self, fmt, *args):
        pass

    # ---- 工具 ----
    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _service(self) -> AnalysisService:
        return self.server.fundflow_service  # type: ignore[attr-defined]

    # ---- 路由 ----
    def do_GET(self):
        svc = self._service()
        path = urlparse(self.path).path
        try:
            if path == "/api/state":
                return self._send_json(_state_payload(svc))
            if path.startswith("/api/chains/"):
                chain_id = path.rsplit("/", 1)[-1]
                return self._send_json(svc.chain_detail(chain_id))
            if path == "/api/health":
                return self._send_json({"status": "ok"})
            if path in ("/", "/index.html"):
                return self._serve_static("index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._serve_static("app.js", "application/javascript; charset=utf-8")
            if path == "/styles.css":
                return self._serve_static("styles.css", "text/css; charset=utf-8")
            self._send_json({"error": "not found"}, 404)
        except ServiceError as exc:
            self._send_json({"error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

    def do_POST(self):
        svc = self._service()
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/objective":
                svc.set_objective(data["objective"])
            elif path == "/api/exclude":
                svc.exclude_edge(data["txn_id"])
            elif path == "/api/include":
                svc.include_edge(data["txn_id"])
            elif path == "/api/lock":
                svc.lock_chain(data["chain_id"], int(data["amount_cents"]))
            elif path == "/api/unlock":
                svc.unlock_chain(data["chain_id"])
            elif path == "/api/confirm":
                sol = svc.confirm_solution(data["solution_id"], data.get("note", ""))
                self._send_json({"confirmed": sol.to_dict()})
                return
            elif path == "/api/recompute":
                svc.recompute()
            else:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json(_state_payload(svc))
        except ConflictError as exc:
            self._send_json({"error": str(exc), "conflicts": exc.conflicts}, 409)
        except (ServiceError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

    def _serve_static(self, filename: str, content_type: str):
        import os
        path = os.path.join(os.path.dirname(__file__), "static", filename)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _state_payload(svc: AnalysisService) -> dict:
    s = svc.snapshot()
    ledger = s.ledger
    return {
        "version": s.version,
        "objective": s.objective,
        "config": ledger.config.to_dict(),
        "accounts": sorted(ledger.accounts()),
        "transactions": [_txn_payload(t, s) for t in ledger.txns_sorted()],
        "refunds": [r.to_dict() for r in sorted(ledger.refunds.values(),
                                                key=lambda r: r.refund_id)],
        "refund_errors": s.refund_errors,
        "chains": [c.to_dict() for c in s.chains],
        "drafts": [d.to_dict() for d in s.drafts],
        "confirmed": [d.to_dict() for d in s.confirmed],
        "locks": [{"chain_id": cid, "amount_cents": amt,
                   "amount_yuan": cents_to_yuan(amt)}
                  for cid, amt in sorted(s.locks.items())],
        "excluded_txn_ids": s.excluded_txn_ids,
        "conflicts": s.conflicts,
        "disclaimer": (
            "本系统基于虚构收支明细进行数学分解，链条仅为资金关系的一种解释，"
            "不构成任何违法事实认定。"
        ),
    }


def _txn_payload(t, s) -> dict:
    d = t.to_dict()
    d["account_allowed"] = (
        t.from_account not in s.ledger.config.blocked_accounts
        and t.to_account not in s.ledger.config.blocked_accounts
    )
    return d


def build_server(ledger: Ledger, host: str = "127.0.0.1",
                 port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.fundflow_service = AnalysisService(ledger)  # type: ignore[attr-defined]
    return server


def serve(ledger: Ledger, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = False) -> None:
    server = build_server(ledger, host, port)
    print(f"资金往来闭合分析系统已启动: http://{host}:{port}/")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务……")
    finally:
        server.shutdown()
        server.server_close()
