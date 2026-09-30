"""走廊碳核算的纯函数计算引擎。

引擎只接收冻结快照字典并输出 :class:`BatchResult`，不访问数据库、时钟或
网络，因此任意历史批次都可以用当初冻结的输入逐字节复算。

核算口径（formula_version = corridor-carbon-v1）：

1. 范围：行程发生时间落在批次周期内，且行程路段属于冻结的边界版本。
2. 证据门槛：载荷、补能电量、充电来源等必需证据缺失、冲突或超过有效期时，
   整条行程进入补证名单并排除，绝不按零排放处理。
3. 载荷系数：``0.5 + 0.5 * min(实际载荷/额定载荷, 1)``，空驶行程只能得到
   一半口径的基准排放承认，避免空载行程套取减排量。
4. 基准排放（柴油重卡）：
   ``基准油耗 L/公里 * 里程 * 柴油因子(kgCO2e/L) / 1000 * 载荷系数``。
5. 实际排放（电动车）：
   ``总电耗 = 车型电耗 kWh/公里 * 里程``；
   被有效凭证匹配的电量使用可再生电力因子，其余电量使用区域电网因子；
   没有凭证不等于零排放。
6. 减排量 = 基准排放 - 实际排放，并在 0 处截断。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .carbon_models import BatchResult, TripContribution

FORMULA_VERSION = "corridor-carbon-v1"

# 阻断性证据问题：命中后整条行程排除，进入补证。
BLOCKING_CODES = frozenset({
    "EVIDENCE_PAYLOAD_MISSING",
    "EVIDENCE_PAYLOAD_CONFLICT",
    "EVIDENCE_PAYLOAD_EXPIRED",
    "EVIDENCE_ENERGY_RECORD_MISSING",
    "EVIDENCE_ENERGY_SOURCE_MISSING",
    "EVIDENCE_ENERGY_SOURCE_CONFLICT",
    "EVIDENCE_ENERGY_EXPIRED",
})
# 非阻断性问题：行程仍可核算，但问题电量按电网处理，且必须补证/裁定。
NON_BLOCKING_CODES = frozenset({
    "CERTIFICATE_CLAIM_CONFLICT",
    "CERTIFICATE_WITHDRAWN",
    "CERTIFICATE_EXPIRED",
    "CERTIFICATE_WINDOW_MISMATCH",
})
OUT_OF_BOUNDARY = "SEGMENT_OUT_OF_BOUNDARY"

DIESEL_FACTOR_KEY = "diesel_lcv_per_liter"
GRID_FACTOR_KEY = "electricity_grid"
RENEWABLE_FACTOR_KEY = "electricity_renewable"

_POLICY_PAYLOAD = "payload_report"
_POLICY_ENERGY = "energy_source"


def _round6(value: float) -> float:
    return round(float(value) + 0.0, 6)


def parse_ts(value: str) -> datetime:
    """解析引擎快照内的规范时间字符串。"""

    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(later: str, earlier: str) -> float:
    return (parse_ts(later) - parse_ts(earlier)) / timedelta(days=1)


def _evidence(code: str, trip_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"code": code, "trip_id": trip_id, "detail": detail}


def certificate_demand(snapshot: dict[str, Any]) -> dict[str, float]:
    """计算批次内各凭证被有效申报需求的电量（不按凭证总量截断）。

    与 :func:`compute_batch` 使用完全相同的有效性门槛：行程通过阻断性证据
    检查、申报 accepted、凭证有效且发电窗口覆盖补能时刻。需求再按事件电量
    和车型行程总电耗封顶。用于服务层在冻结时硬性阻止凭证超订/重复计算。
    """

    frozen_at = snapshot["frozen_at"]
    vehicle_types = snapshot["vehicle_types"]
    payload_policy = snapshot["policies"].get("payload_report", {"validity_days": 10**9})
    energy_policy = snapshot["policies"].get("energy_source", {"validity_days": 10**9})
    segment_ids = {item["segment_id"] for item in snapshot["boundary"]["segments"]}
    demand: dict[str, float] = {}
    for trip in snapshot["trips"]:
        if trip["segment_id"] not in segment_ids:
            continue
        flags = set(trip.get("conflict_flags", []))
        if trip["payload_status"] != "reported" or trip.get("payload_t") is None or "payload" in flags:
            continue
        if _age_days(trip["updated_at"], trip["occurred_at"]) > float(payload_policy["validity_days"]):
            continue
        events = trip.get("energy_events", [])
        if not events or "energy_source" in flags:
            continue
        if any(event["source_status"] == "unknown" for event in events):
            continue
        if any(_age_days(event["created_at"], trip["occurred_at"])
               > float(energy_policy["validity_days"]) for event in events):
            continue
        trip_cap = float(vehicle_types[trip["vehicle_type_id"]]["consumption_rate"]) \
            * float(trip["distance_km"])
        remaining_trip = trip_cap
        for event in events:
            event_available = float(event["amount"])
            for claim in event.get("claims", []):
                if claim["status"] != "accepted":
                    continue
                cert = claim["certificate"]
                if cert["status"] != "active":
                    continue
                if parse_ts(frozen_at) > parse_ts(cert["valid_to"]):
                    continue
                if not (parse_ts(cert["generation_start"]) <= parse_ts(event["occurred_at"])
                        <= parse_ts(cert["generation_end"])):
                    continue
                take = min(float(claim["kwh"]), event_available, remaining_trip)
                if take <= 0:
                    continue
                event_available -= take
                remaining_trip -= take
                key = cert["certificate_id"]
                demand[key] = demand.get(key, 0.0) + take
    return {key: round(value, 6) for key, value in sorted(demand.items())}


def compute_batch(snapshot: dict[str, Any]) -> BatchResult:
    """对冻结快照执行完整核算，返回结构化结果。"""

    if snapshot.get("formula_version") != FORMULA_VERSION:
        raise ValueError("快照 formula_version 不受支持")

    boundary_segments = {item["segment_id"]: item for item in snapshot["boundary"]["segments"]}
    vehicle_types = snapshot["vehicle_types"]
    factors = snapshot["factors"]
    baseline = snapshot["baseline"]
    policies = snapshot["policies"]
    frozen_at = snapshot["frozen_at"]

    diesel_factor = factors[baseline["factor_key"]]["value"]
    grid_factor = factors[GRID_FACTOR_KEY]["value"]
    renewable_factor = factors[RENEWABLE_FACTOR_KEY]["value"]

    payload_policy = policies.get(_POLICY_PAYLOAD, {"validity_days": 10**9, "required": True})
    energy_policy = policies.get(_POLICY_ENERGY, {"validity_days": 10**9, "required": True})

    contributions: list[TripContribution] = []
    exclusions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    certificate_usage: dict[str, dict[str, Any]] = {}
    certificate_used_kwh: dict[str, float] = {}

    total_baseline = 0.0
    total_actual = 0.0
    total_avoided = 0.0
    trips_accepted = 0

    for trip in snapshot["trips"]:
        trip_id = trip["trip_id"]
        codes: list[str] = []

        if trip["segment_id"] not in boundary_segments:
            exclusions.append({"trip_id": trip_id, "code": OUT_OF_BOUNDARY,
                               "detail": {"segment_id": trip["segment_id"]}})
            continue

        flags = set(trip.get("conflict_flags", []))
        if "payload" in flags:
            codes.append("EVIDENCE_PAYLOAD_CONFLICT")
        elif trip["payload_status"] == "absent" or trip.get("payload_t") is None:
            codes.append("EVIDENCE_PAYLOAD_MISSING")
        elif payload_policy.get("required", True):
            age = _age_days(trip["updated_at"], trip["occurred_at"])
            if age > float(payload_policy["validity_days"]):
                codes.append("EVIDENCE_PAYLOAD_EXPIRED")

        events = trip.get("energy_events", [])
        if not events:
            codes.append("EVIDENCE_ENERGY_RECORD_MISSING")
        else:
            if "energy_source" in flags:
                codes.append("EVIDENCE_ENERGY_SOURCE_CONFLICT")
            stale_events = []
            unknown_events = []
            for event in events:
                if event["source_status"] == "unknown":
                    unknown_events.append(event["event_id"])
                elif energy_policy.get("required", True):
                    if _age_days(event["created_at"], trip["occurred_at"]) > float(energy_policy["validity_days"]):
                        stale_events.append(event["event_id"])
            if unknown_events:
                codes.append("EVIDENCE_ENERGY_SOURCE_MISSING")
            if stale_events:
                codes.append("EVIDENCE_ENERGY_EXPIRED")

        for event in events:
            for claim in event.get("claims", []):
                if claim["status"] == "rejected_conflict":
                    codes.append("CERTIFICATE_CLAIM_CONFLICT")
                    evidence.append(_evidence("CERTIFICATE_CLAIM_CONFLICT", trip_id, {
                        "event_id": event["event_id"], "certificate_id": claim["certificate_id"],
                        "conflict_code": claim.get("conflict_code"),
                        "claimant_type": claim["claimant_type"], "claimant_id": claim["claimant_id"],
                    }))

        blocking = [code for code in codes if code in BLOCKING_CODES]
        if blocking:
            for code in sorted(set(blocking)):
                detail: dict[str, Any] = {}
                if code == "EVIDENCE_PAYLOAD_EXPIRED":
                    detail = {"validity_days": payload_policy["validity_days"]}
                if code == "EVIDENCE_ENERGY_EXPIRED":
                    detail = {"validity_days": energy_policy["validity_days"]}
                evidence.append(_evidence(code, trip_id, detail))
            exclusions.append({"trip_id": trip_id, "codes": sorted(set(blocking)),
                               "detail": {"segment_id": trip["segment_id"]}})
            continue

        vehicle = vehicle_types[trip["vehicle_type_id"]]
        rated_payload = float(vehicle["rated_payload_t"])
        payload_t = float(trip["payload_t"])
        load_ratio = min(payload_t / rated_payload, 1.0) if rated_payload > 0 else 1.0
        payload_factor = 0.5 + 0.5 * load_ratio
        distance = float(trip["distance_km"])

        baseline_tco2 = (float(baseline["fuel_intensity_l_per_km"]) * distance
                         * diesel_factor / 1000.0 * payload_factor)

        total_kwh = float(vehicle["consumption_rate"]) * distance
        green_kwh = 0.0
        grid_kwh = total_kwh
        matched_certificates: list[str] = []
        certificate_ids = set()

        for event in events:
            event_available = float(event["amount"])
            for claim in event.get("claims", []):
                if claim["status"] != "accepted":
                    continue
                certificate = claim["certificate"]
                cert_id = certificate["certificate_id"]
                reason = None
                if certificate["status"] != "active":
                    reason = "CERTIFICATE_WITHDRAWN"
                elif parse_ts(frozen_at) > parse_ts(certificate["valid_to"]):
                    reason = "CERTIFICATE_EXPIRED"
                elif not (parse_ts(certificate["generation_start"]) <= parse_ts(event["occurred_at"])
                          <= parse_ts(certificate["generation_end"])):
                    reason = "CERTIFICATE_WINDOW_MISMATCH"
                if reason is not None:
                    codes.append(reason)
                    evidence.append(_evidence(reason, trip_id, {
                        "event_id": event["event_id"], "certificate_id": cert_id}))
                    continue
                remaining_total = (float(certificate["kwh_total"])
                                   - certificate_used_kwh.get(cert_id, 0.0))
                take = min(float(claim["kwh"]), event_available, total_kwh - green_kwh,
                           remaining_total)
                if take <= 0:
                    continue
                green_kwh += take
                event_available -= take
                certificate_used_kwh[cert_id] = certificate_used_kwh.get(cert_id, 0.0) + take
                certificate_ids.add(cert_id)
                usage = certificate_usage.setdefault(cert_id, {
                    "certificate_id": cert_id, "kwh_total": float(certificate["kwh_total"]),
                    "kwh_used": 0.0, "claimant_type": certificate["claimant_type"],
                    "claimant_id": certificate["claimant_id"], "allocations": [],
                })
                usage["kwh_used"] += take
                usage["allocations"].append({"event_id": event["event_id"], "kwh": take})
                if cert_id not in matched_certificates:
                    matched_certificates.append(cert_id)

        grid_kwh = total_kwh - green_kwh
        actual_tco2 = ((green_kwh * renewable_factor + grid_kwh * grid_factor)
                       / 1000.0 * payload_factor)
        avoided_tco2 = max(baseline_tco2 - actual_tco2, 0.0)

        total_baseline += baseline_tco2
        total_actual += actual_tco2
        total_avoided += avoided_tco2
        trips_accepted += 1
        contributions.append(TripContribution(
            trip_id=trip_id, segment_id=trip["segment_id"], vehicle_id=trip["vehicle_id"],
            vehicle_type_id=trip["vehicle_type_id"], energy_type=vehicle["energy_type"],
            distance_km=_round6(distance), payload_t=_round6(payload_t),
            payload_factor=_round6(payload_factor),
            baseline_tco2=_round6(baseline_tco2), actual_tco2=_round6(actual_tco2),
            avoided_tco2=_round6(avoided_tco2), energy_kwh=_round6(total_kwh),
            matched_green_kwh=_round6(green_kwh), grid_kwh=_round6(grid_kwh),
            applied_factors={
                baseline["factor_key"]: diesel_factor,
                GRID_FACTOR_KEY: grid_factor,
                RENEWABLE_FACTOR_KEY: renewable_factor,
            },
            certificate_ids=tuple(sorted(certificate_ids)),
        ))

    all_codes = sorted({item["code"] for item in evidence})
    result = BatchResult(
        formula_version=FORMULA_VERSION,
        total_baseline_tco2=_round6(total_baseline),
        total_actual_tco2=_round6(total_actual),
        total_avoided_tco2=_round6(total_avoided),
        trips_in_scope=len(snapshot["trips"]),
        trips_accepted=trips_accepted,
        trips_excluded=len(exclusions),
        contributions=tuple(contributions),
        exclusions=tuple(exclusions),
        evidence_items=tuple(evidence),
        evidence_codes=tuple(all_codes),
        evidence_open_at_freeze=tuple(all_codes),
        certificate_usage=tuple(
            {**usage, "kwh_used": _round6(usage["kwh_used"]),
             "allocations": tuple(
                 {"event_id": alloc["event_id"], "kwh": _round6(alloc["kwh"])}
                 for alloc in usage["allocations"])}
            for usage in (certificate_usage.get(key) for key in sorted(certificate_usage))
        ),
    )
    return result


def result_to_dict(result: BatchResult) -> dict[str, Any]:
    """把结果转为可哈希、可持久化的普通字典。"""

    return asdict(result)
