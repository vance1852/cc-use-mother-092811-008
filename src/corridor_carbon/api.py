"""走廊碳核算的 HTTP/JSON 边界（仅依赖标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from transport_coordination.api import route as base_route
from transport_coordination.errors import DomainError
from transport_coordination.service import DomainService

from .service import CarbonService
from .storage import CarbonDatabase


def route(service: CarbonService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, base_service: DomainService | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 语义请求分派到碳核算服务，未命中的基础服务路由同库回退。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    p = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        # ---------- 版本化主数据 ----------
        if method == "POST" and p == ["segments"]:
            receipt = service.register_segment_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and p == ["energy-types"]:
            receipt = service.register_energy_type_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and p == ["emission-factors"]:
            receipt = service.register_factor_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and p == ["baselines"]:
            receipt = service.register_baseline_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and p == ["evidence-policies"]:
            receipt = service.register_policy_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and p == ["targets"]:
            receipt = service.register_target_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "GET" and len(p) == 3 and p[0] == "master-versions":
            items = service.list_master_versions(p[1], p[2])
            return 200, {"items": items}

        # ---------- 凭证 ----------
        if method == "POST" and p == ["certificates"]:
            receipt = service.register_certificate(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and len(p) == 3 and p[0] == "certificates" and p[2] == "withdraw":
            receipt = service.withdraw_certificate(actor_id=actor_id, certificate_id=p[1],
                                                   reason=body.get("reason", ""))
            return 200, receipt

        # ---------- 批次 ----------
        if method == "POST" and p == ["batches", "freeze"]:
            receipt = service.freeze_batch(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and p == ["batches", "restate"]:
            receipt = service.restate_batch(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "GET" and p[:1] == ["batches"] and len(p) == 1:
            return 200, {"items": service.list_batches(q("corridor_id"), q("status"))}
        if method == "GET" and len(p) == 3 and p[0] == "batches":
            batch_id, sub = p[1], p[2]
            if sub == "explain":
                return 200, service.explain_reductions(batch_id)
            if sub == "deficits":
                return 200, service.evidence_deficits(batch_id)
            if sub == "recompute":
                return 200, service.recompute_batch(batch_id)
            if sub == "findings":
                return 200, {"items": service.findings(batch_id)}
        if method == "GET" and len(p) == 2 and p[0] == "batches":
            return 200, service.get_batch(p[1])
        if method == "POST" and len(p) == 3 and p[0] == "batches" and p[2] == "findings":
            receipt = service.add_finding(actor_id=actor_id, batch_id=p[1], **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and len(p) == 4 and p[0] == "batches" and p[2] == "findings":
            receipt = service.resolve_finding(actor_id=actor_id, finding_id=p[3], **body)
            return 200, receipt
        if method == "POST" and len(p) == 3 and p[0] == "batches" and p[2] == "verify":
            body.pop("batch_id", None)
            receipt = service.verify_batch(actor_id=actor_id, batch_id=p[1], **body)
            return 200, receipt
        if method == "POST" and len(p) == 3 and p[0] == "batches" and p[2] == "publish":
            body.pop("batch_id", None)
            receipt = service.publish_batch(actor_id=actor_id, batch_id=p[1], **body)
            return 200, receipt
        if method == "POST" and len(p) == 3 and p[0] == "batches" and p[2] == "revoke":
            body.pop("batch_id", None)
            receipt = service.revoke_published_batch(actor_id=actor_id, batch_id=p[1], **body)
            return 200, receipt

        # ---------- 走廊口径 ----------
        if method == "GET" and p[:1] == ["corridors"] and len(p) == 3 and p[2] == "progress":
            return 200, service.corridor_progress(p[1], q("period_start", ""), q("period_end", ""),
                                                  q("metric"))
        if method == "GET" and p[:1] == ["corridors"] and len(p) == 3 and p[2] == "reports":
            return 200, {"items": service.list_published_reports(p[1])}
        if method == "GET" and p == ["reports"]:
            return 200, {"items": service.list_published_reports(q("corridor_id"))}
        if method == "GET" and parsed.path == "/audit-events":
            return 200, {"items": service.audit_events(int(q("after_sequence", "0")))}

        # 未命中碳核算路由时，回退到同库的基础服务（组织、操作者、场所、资料登记）
        if base_service is not None:
            status, payload = base_route(base_service, method, path, body, headers)
            if status != 404:
                return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: CarbonService
    base_service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                base_service=self.base_service)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动碳核算 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动零碳走廊碳核算与核验服务")
    parser.add_argument("--database", default="corridor_carbon.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    database = CarbonDatabase(args.database)
    Handler.service = CarbonService(database)
    Handler.base_service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
