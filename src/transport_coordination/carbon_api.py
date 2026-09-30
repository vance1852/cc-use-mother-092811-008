"""把碳核算服务方法映射为 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .carbon_service import CarbonService


def route_carbon(service: CarbonService, method: str, path: str, body: dict[str, Any]
                 ) -> tuple[int, dict[str, Any]] | None:
    """返回 (状态码, 响应体)；不认识的路径返回 None。"""

    parsed = urlparse(path)
    p = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        # -- 配置版本化 ----------------------------------------------------
        if method == "POST" and p == ["carbon", "corridors"]:
            receipt = service.register_corridor(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "corridors" \
                and p[3] == "boundary-versions":
            receipt = service.publish_boundary(corridor_id=p[2], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "corridors" \
                and p[3] == "target-versions":
            receipt = service.publish_target(corridor_id=p[2], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == ["carbon", "vehicle-types"]:
            receipt = service.publish_vehicle_type(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == ["carbon", "factors"]:
            receipt = service.publish_factor(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == ["carbon", "baselines"]:
            receipt = service.publish_baseline(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == ["carbon", "evidence-policies"]:
            receipt = service.publish_evidence_policy(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # -- 运营输入 ------------------------------------------------------
        if method == "POST" and p == ["carbon", "trips"]:
            return 201, service.record_trip(**body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "trips" \
                and p[3] == "payload":
            return 200, service.report_trip_payload(trip_id=p[2], **body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "trips" \
                and p[3] == "conflict-flags":
            return 200, service.flag_trip_conflict(trip_id=p[2], **body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "trips" \
                and p[3] == "conflict-clears":
            return 200, service.clear_trip_conflict(trip_id=p[2], **body)
        if method == "POST" and p == ["carbon", "energy-events"]:
            return 201, service.record_energy_event(**body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "energy-events" \
                and p[3] == "source":
            return 200, service.report_energy_source(event_id=p[2], **body)

        # -- 凭证与申报 ----------------------------------------------------
        if method == "POST" and p == ["carbon", "certificates"]:
            receipt = service.register_certificate(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "certificates" \
                and p[3] == "withdrawal":
            return 200, service.withdraw_certificate(certificate_id=p[2], **body)
        if method == "POST" and p == ["carbon", "claims"]:
            return 201, service.file_claim(**body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "claims" \
                and p[3] == "void":
            return 200, service.void_claim(claim_id=p[2], **body)

        # -- 批次、核验、发布、重述 ----------------------------------------
        if method == "POST" and p == ["carbon", "batches"]:
            receipt = service.create_batch(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "freeze":
            return 200, service.freeze_batch(batch_id=p[2], **body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "review":
            return 200, service.review_batch(batch_id=p[2], **body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "publish":
            return 200, service.publish_batch(batch_id=p[2], **body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "restate":
            return 201, service.restate_batch(batch_id=p[2], **body)
        if method == "POST" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "recompute":
            version = q("version")
            return 200, service.recompute_batch(p[2], int(version) if version else None)

        # -- 查询 ----------------------------------------------------------
        if method == "GET" and len(p) == 3 and p[0] == "carbon" and p[1] == "batches":
            version = q("version")
            return 200, service.get_batch(p[2], int(version) if version else None).__dict__
        if method == "GET" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "versions":
            return 200, {"items": service.list_batch_versions(p[2])}
        if method == "GET" and len(p) == 4 and p[0] == "carbon" and p[1] == "batches" \
                and p[3] == "explanation":
            version = q("version")
            return 200, service.explain_batch(p[2], int(version) if version else None)
        if method == "GET" and len(p) == 4 and p[0] == "carbon" and p[1] == "corridors" \
                and p[3] == "progress":
            return 200, service.corridor_progress(p[2]).__dict__
        if method == "GET" and len(p) == 4 and p[0] == "carbon" and p[1] == "corridors" \
                and p[3] == "boundary-versions":
            return 200, {"items": service.list_boundary_versions(p[2])}
        if method == "GET" and len(p) == 4 and p[0] == "carbon" and p[1] == "factors" \
                and p[3] == "versions":
            return 200, {"items": service.list_factor_versions(p[2])}
        if method == "GET" and p == ["carbon", "evidence"]:
            version = q("version")
            return 200, {"items": service.list_evidence(
                batch_id=q("batch_id"), version=int(version) if version else None,
                status_filter=q("status"))}
        if method == "POST" and p == ["carbon", "evidence", "resolve"]:
            return 200, service.resolve_evidence(**body)
        if method == "POST" and p == ["carbon", "evidence", "adjudicate"]:
            return 200, service.adjudicate_evidence(**body)
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
