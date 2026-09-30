"""碳核算纯领域规则：证据有效期、双侧凭证匹配、排放与减排计算。

本模块不访问数据库，所有函数都接受已冻结的快照数据，便于独立复算与测试。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# 证据种类与默认有效期政策（可被版本化 evidence_policies 覆盖）
EVIDENCE_KINDS = ("trip_log", "payload_record", "energy_record",
                  "electricity_declaration", "certificate")

DEFAULT_POLICY = {
    "name": "默认证据政策",
    "rules": {
        "trip_log": {"validity_days": 7, "required": True},
        "payload_record": {"validity_days": 30, "required": True},
        "energy_record": {"validity_days": 30, "required": True},
        "electricity_declaration": {"validity_days": 30, "required": True},
        "certificate": {"validity_days": None, "required": True},
    },
    # 证据允许早于行程开始的宽限天数
    "before_grace_days": 2,
}

ENERGY_TOLERANCE_KWH = 1e-6


def parse_time(value: str) -> datetime:
    """解析日期或日期时间字符串。"""

    text = str(value).strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evidence_in_window(*, recorded_at: str | None, trip_start: datetime,
                       trip_end: datetime, validity_days: int | None,
                       grace_days: int) -> bool:
    """判断证据时间是否落在政策允许窗口内。"""

    if not recorded_at:
        return False
    recorded = parse_time(recorded_at)
    earliest = trip_start - timedelta(days=grace_days)
    latest = trip_end + timedelta(days=float("inf") if validity_days is None else validity_days)
    return earliest <= recorded <= latest


def _approx_equal(left: float, right: float) -> bool:
    return abs(left - right) <= ENERGY_TOLERANCE_KWH + 1e-9 * max(abs(left), abs(right))


def evaluate_trip(trip: dict[str, Any], snapshot: dict[str, Any],
                  certificates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """对单条行程执行证据闸门与排放计算，返回可冻结的结果明细。

    ``certificates`` 为批次内引用到的凭证当前登记内容映射。

    关键原则：缺失或冲突证据只会产生补证原因，该行程不计入减排，
    绝不会被当作零排放行程。
    """

    policy = snapshot["policy"]["rules"]
    grace_days = snapshot["policy"].get("before_grace_days", DEFAULT_POLICY["before_grace_days"])
    reasons: list[str] = []

    trip_id = str(trip["trip_id"])
    trip_start = parse_time(trip["started_at"])
    trip_end = parse_time(trip["completed_at"])
    if trip_end < trip_start:
        raise ValueError(f"行程 {trip_id} 的结束时间早于开始时间")

    evidence_valid: dict[str, bool] = {}

    # ---- 行程日志 ----
    evidence_valid["trip_log"] = evidence_in_window(
        recorded_at=trip.get("logged_at", trip["started_at"]),
        trip_start=trip_start, trip_end=trip_end,
        validity_days=policy["trip_log"]["validity_days"], grace_days=grace_days,
    )
    if not evidence_valid["trip_log"]:
        reasons.append("trip_log_expired")

    # ---- 路段边界：行程经过的每个路段必须存在于冻结边界版本中 ----
    segments = snapshot["segments"]
    legs = trip.get("segment_legs") or []
    if not legs:
        reasons.append("missing_segment_legs")
    distance_km = 0.0
    for leg in legs:
        distance_km += float(leg["distance_km"])
        if leg["segment_id"] not in segments:
            reasons.append(f"segment_not_in_boundary:{leg['segment_id']}")
    if distance_km <= 0:
        reasons.append("non_positive_distance")

    # ---- 车辆与能源类型 ----
    energy_type = snapshot["energy_types"].get(trip["energy_type_id"])
    if energy_type is None:
        reasons.append(f"energy_type_not_in_version:{trip['energy_type_id']}")

    # ---- 载货量（行程结束后可能补传，缺失即 pending） ----
    payload = trip.get("payload")
    payload_status = "complete" if isinstance(payload, dict) and payload.get("load_t") is not None else "pending"
    if payload_status == "pending":
        reasons.append("missing_payload")
    else:
        evidence_valid["payload_record"] = evidence_in_window(
            recorded_at=payload.get("recorded_at"),
            trip_start=trip_start, trip_end=trip_end,
            validity_days=policy["payload_record"]["validity_days"], grace_days=grace_days,
        )
        if not evidence_valid["payload_record"]:
            reasons.append("payload_record_expired")
        if float(payload["load_t"]) < 0:
            reasons.append("invalid_payload")

    # ---- 补能记录 ----
    energy_records = trip.get("energy_records") or []
    evidence_valid["energy_record"] = True
    duplicate: dict[str, dict[str, Any]] = {}
    conflict = False
    for record in energy_records:
        rid = record["record_id"]
        compact = {k: record.get(k) for k in ("energy_kwh", "station_id", "occurred_at", "certificate_id")}
        if rid in duplicate and duplicate[rid] != compact:
            conflict = True
            reasons.append(f"energy_record_conflict:{rid}")
        duplicate[rid] = compact
        if not evidence_in_window(
                recorded_at=record.get("occurred_at"),
                trip_start=trip_start, trip_end=trip_end,
                validity_days=policy["energy_record"]["validity_days"], grace_days=grace_days):
            evidence_valid["energy_record"] = False
    if not evidence_valid["energy_record"]:
        reasons.append("energy_record_expired")

    carrier = energy_type["energy_carrier"] if energy_type else None
    if carrier in ("electricity", "hydrogen") and not energy_records:
        energy_status = "pending"
        reasons.append("missing_energy_records")
    elif conflict:
        energy_status = "conflict"
    else:
        energy_status = "complete"

    # ---- 双侧电力声明匹配（车队 vs 站点） ----
    matched_kwh_by_cert: dict[str, float] = {}
    declarations = trip.get("declarations") or []
    claim_status = "matched"
    has_cert_claims = False
    evidence_valid["electricity_declaration"] = True
    evidence_valid["certificate"] = True

    if carrier == "electricity":
        station_by_cert: dict[str, float] = {}
        for record in energy_records:
            cert_id = record.get("certificate_id")
            if cert_id:
                station_by_cert[cert_id] = station_by_cert.get(cert_id, 0.0) + float(record["energy_kwh"])
        fleet_by_cert: dict[str, float] = {}
        for declaration in declarations:
            cert_id = declaration["certificate_id"]
            fleet_by_cert[cert_id] = fleet_by_cert.get(cert_id, 0.0) + float(declaration["energy_kwh"])
            if not evidence_in_window(
                    recorded_at=declaration.get("declared_at"),
                    trip_start=trip_start, trip_end=trip_end,
                    validity_days=policy["electricity_declaration"]["validity_days"], grace_days=grace_days):
                evidence_valid["electricity_declaration"] = False
        all_certs = set(station_by_cert) | set(fleet_by_cert)
        has_cert_claims = bool(all_certs)
        # 没有任何绿证引用时不构成绿电主张，按普通并网电量走电网因子；
        # 一旦引用凭证，车队与站点双侧必须匹配，单侧即进入补证。
        for cert_id in sorted(all_certs):
            station_kwh = station_by_cert.get(cert_id)
            fleet_kwh = fleet_by_cert.get(cert_id)
            cert = certificates.get(cert_id)
            cert_problem = None
            if cert is None:
                cert_problem = "certificate_not_registered"
            elif cert["status"] == "withdrawn":
                cert_problem = "certificate_withdrawn"
            else:
                covers = all(
                    cert["valid_from"] <= parse_time(r["occurred_at"]).date().isoformat() <= cert["valid_to"]
                    for r in energy_records if r.get("certificate_id") == cert_id
                )
                if not covers:
                    cert_problem = "certificate_out_of_validity"
            if cert_problem:
                claim_status = "conflict"
                reasons.append(f"{cert_problem}:{cert_id}")
                evidence_valid["certificate"] = False
                continue
            if station_kwh is None or fleet_kwh is None:
                # 单侧申报：进入补证，按普通并网电量处理
                claim_status = "missing" if station_kwh is not None else "conflict"
                reasons.append(
                    f"fleet_declaration_missing:{cert_id}" if fleet_kwh is None
                    else f"station_record_missing:{cert_id}"
                )
                continue
            if not _approx_equal(station_kwh, fleet_kwh):
                claim_status = "conflict"
                reasons.append(f"declaration_quantity_conflict:{cert_id}")
                continue
            matched_kwh_by_cert[cert_id] = station_kwh
        if not evidence_valid["electricity_declaration"]:
            reasons.append("declaration_expired")

    # ---- 排放与减排计算 ----
    baseline = snapshot["baseline"]
    baseline_ef = float(baseline["baseline_ef_kg_per_km"])
    correction = baseline.get("load_correction") or {}
    load_factor = 1.0
    if correction.get("method") == "payload_ratio":
        reference = float(correction["reference_load_t"])
        floor = float(correction.get("min_factor", 0.0))
        load_t = float(payload["load_t"]) if payload_status == "complete" else 0.0
        load_factor = max(floor, min(1.0, load_t / reference)) if reference > 0 else 0.0
        if payload_status == "pending":
            load_factor = 0.0

    baseline_emissions_kg = distance_km * baseline_ef * load_factor
    actual_emissions_kg: float | None = None
    calc_detail: dict[str, Any] = {
        "distance_km": distance_km,
        "load_factor": load_factor,
        "baseline_ef_kg_per_km": baseline_ef,
        "carrier": carrier,
    }

    countable = (
        energy_type is not None
        and payload_status == "complete"
        and energy_status == "complete"
        and evidence_valid.get("payload_record", False)
        and evidence_valid.get("energy_record", False)
        and evidence_valid.get("trip_log", False)
        and not any(r.startswith("segment_not_in_boundary") for r in reasons)
        and not any(r in ("non_positive_distance", "missing_segment_legs", "invalid_payload") for r in reasons)
        and (not has_cert_claims or (claim_status == "matched"
                                     and evidence_valid.get("certificate", False)
                                     and evidence_valid.get("electricity_declaration", False)))
    )

    if countable and carrier == "electricity":
        total_kwh = sum(float(r["energy_kwh"]) for r in energy_records)
        matched_kwh = sum(matched_kwh_by_cert.values())
        grid_kwh = max(0.0, total_kwh - matched_kwh)
        grid_ef = energy_type.get("grid_ef_kg_per_kwh")
        if grid_ef is None:
            factor = snapshot["factors"].get("grid_default")
            grid_ef = factor["value"] if factor else None
        if grid_kwh > 0 and grid_ef is None:
            countable = False
            reasons.append("missing_grid_emission_factor")
        else:
            actual_emissions_kg = grid_kwh * float(grid_ef or 0.0)
            calc_detail.update(total_kwh=total_kwh, matched_green_kwh=matched_kwh,
                               grid_kwh=grid_kwh, grid_ef_kg_per_kwh=grid_ef,
                               matched_certificates=matched_kwh_by_cert)
    elif countable and carrier == "diesel":
        fuel_intensity = energy_type.get("fuel_intensity_l_per_km")
        fuel_ef = energy_type.get("fuel_ef_kg_per_l")
        if fuel_ef is None:
            factor = snapshot["factors"].get("diesel_default")
            fuel_ef = factor["value"] if factor else None
        if fuel_intensity is None or fuel_ef is None:
            countable = False
            reasons.append("missing_fuel_emission_factor")
        else:
            fuel_l = distance_km * float(fuel_intensity)
            actual_emissions_kg = fuel_l * float(fuel_ef)
            calc_detail.update(fuel_l=fuel_l, fuel_ef_kg_per_l=fuel_ef)
    elif countable:
        countable = False
        reasons.append(f"unsupported_carrier:{carrier}")

    if not countable:
        # 关键闸门：证据不足的行程不产出减排，实际排放标记为不可确认而非 0
        reductions_kg = 0.0
        actual_emissions_kg = None
        verification_status = "pending_evidence"
    else:
        reductions_kg = max(0.0, baseline_emissions_kg - float(actual_emissions_kg))
        verification_status = "valid"

    return {
        "trip_id": trip_id,
        "payload_status": payload_status,
        "energy_status": energy_status,
        "claim_status": claim_status,
        "evidence_valid": evidence_valid,
        "verification_status": verification_status,
        "pending_reasons": reasons,
        "distance_km": round(distance_km, 6),
        "baseline_emissions_kg": round(baseline_emissions_kg, 6),
        "actual_emissions_kg": None if actual_emissions_kg is None else round(actual_emissions_kg, 6),
        "reductions_kg": round(reductions_kg, 6),
        "matched_kwh_by_cert": matched_kwh_by_cert,
        "calc_detail": calc_detail,
    }


def summarize(trip_results: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐行程结果汇总成批次结果。"""

    valid = [item for item in trip_results if item["verification_status"] == "valid"]
    deficits: list[dict[str, Any]] = []
    for item in trip_results:
        if item["verification_status"] == "pending_evidence":
            deficits.append({"trip_id": item["trip_id"], "reasons": item["pending_reasons"]})
    baseline_kg = sum(item["baseline_emissions_kg"] for item in valid)
    actual_kg = sum(item["actual_emissions_kg"] for item in valid if item["actual_emissions_kg"] is not None)
    reductions_kg = sum(item["reductions_kg"] for item in valid)
    return {
        "trip_count": len(trip_results),
        "valid_trip_count": len(valid),
        "pending_trip_count": len(deficits),
        "baseline_emissions_tco2": round(baseline_kg / 1000.0, 6),
        "actual_emissions_tco2": round(actual_kg / 1000.0, 6),
        "reductions_tco2": round(reductions_kg / 1000.0, 6),
        "evidence_deficits": deficits,
        "valid_trip_ids": [item["trip_id"] for item in valid],
    }
