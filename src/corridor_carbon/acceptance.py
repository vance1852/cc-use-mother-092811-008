"""走廊碳核算与核验的离线端到端验收。

演示完整链路：主数据版本登记 → 双侧绿证凭证登记 → 批次冻结（缺失/冲突
证据进入补证）→ 独立核验与发布 → 每吨减排来源解释 → 迟到数据重述 →
凭证撤回吊销 → 更正重述再发布 → 历史报告按原输入复算 → 走廊目标口径。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.clock import FixedClock
from transport_coordination.errors import PermissionDenied
from transport_coordination.service import DomainService

from .service import CarbonService
from .storage import CarbonDatabase


def _trips_v1():
    """初版：A 证据完整；B 载货量迟到缺失；C 车队与站点申报量冲突。"""
    return [
        {
            "trip_id": "trip-A", "vehicle_id": "ev-truck-01", "energy_type_id": "et-ev",
            "started_at": "2026-09-10T01:00:00Z", "completed_at": "2026-09-10T05:00:00Z",
            "logged_at": "2026-09-10T05:10:00Z",
            "segment_legs": [{"segment_id": "seg-north", "distance_km": 120.0}],
            "payload": {"load_t": 30.0, "recorded_at": "2026-09-10T08:00:00Z"},
            "energy_records": [
                {"record_id": "e-A-1", "station_id": "st-01", "energy_kwh": 150.0,
                 "occurred_at": "2026-09-10T02:30:00Z", "certificate_id": "cert-1"}
            ],
            "declarations": [
                {"certificate_id": "cert-1", "fleet_id": "fleet-01", "energy_kwh": 150.0,
                 "declared_at": "2026-09-10T06:00:00Z"}
            ],
        },
        {
            "trip_id": "trip-B", "vehicle_id": "ev-truck-02", "energy_type_id": "et-ev",
            "started_at": "2026-09-12T00:00:00Z", "completed_at": "2026-09-12T03:00:00Z",
            "logged_at": "2026-09-12T03:05:00Z",
            "segment_legs": [{"segment_id": "seg-north", "distance_km": 100.0}],
            # 载货量行程结束后补传，初版冻结时尚未到达
            "energy_records": [
                {"record_id": "e-B-1", "station_id": "st-01", "energy_kwh": 130.0,
                 "occurred_at": "2026-09-12T01:30:00Z", "certificate_id": "cert-1"}
            ],
            "declarations": [
                {"certificate_id": "cert-1", "fleet_id": "fleet-01", "energy_kwh": 130.0,
                 "declared_at": "2026-09-12T05:00:00Z"}
            ],
        },
        {
            "trip_id": "trip-C", "vehicle_id": "ev-truck-03", "energy_type_id": "et-ev",
            "started_at": "2026-09-15T01:00:00Z", "completed_at": "2026-09-15T03:00:00Z",
            "logged_at": "2026-09-15T03:02:00Z",
            "segment_legs": [{"segment_id": "seg-south", "distance_km": 80.0}],
            "payload": {"load_t": 20.0, "recorded_at": "2026-09-15T04:00:00Z"},
            "energy_records": [
                {"record_id": "e-C-1", "station_id": "st-02", "energy_kwh": 100.0,
                 "occurred_at": "2026-09-15T02:00:00Z", "certificate_id": "cert-1"}
            ],
            # 车队只申报 60 kWh，与站点 100 kWh 冲突
            "declarations": [
                {"certificate_id": "cert-1", "fleet_id": "fleet-01", "energy_kwh": 60.0,
                 "declared_at": "2026-09-15T04:30:00Z"}
            ],
        },
    ]


def _trips_v2():
    """迟到载货量到达；C 的车队申报更正为 100 kWh。"""
    trips = _trips_v1()
    trips[1]["payload"] = {"load_t": 40.0, "recorded_at": "2026-09-20T09:00:00Z"}
    trips[2]["declarations"][0]["energy_kwh"] = 100.0
    return trips


def _trips_v3():
    """cert-1 被撤回后，全部改用 cert-2。"""
    trips = _trips_v2()
    for trip in trips:
        for record in trip["energy_records"]:
            record["certificate_id"] = "cert-2"
        for declaration in trip["declarations"]:
            declaration["certificate_id"] = "cert-2"
    return trips


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = CarbonDatabase(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        service = CarbonService(database, clock)

        # ---- 组织与角色 ----
        base.register_organization(request_id="req-org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范走廊运营机构")
        base.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="op-001",
                            display_name="核算员", role="operator", organization_id="org-001")
        base.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="rv-001",
                            display_name="独立核验人", role="reviewer", organization_id="org-001")

        # ---- 版本化主数据 ----
        service.register_segment_version(request_id="req-seg-n1", actor_id="op-001",
                                         segment_id="seg-north", corridor_id="corr-01",
                                         name="北段边界", length_km=120.0,
                                         boundary=[{"lat": 30.1, "lng": 120.1}])
        service.register_segment_version(request_id="req-seg-s1", actor_id="op-001",
                                         segment_id="seg-south", corridor_id="corr-01",
                                         name="南段边界", length_km=80.0,
                                         boundary=[{"lat": 30.0, "lng": 120.2}])
        # 路段边界修订（v2 只追加，v1 保留）
        service.register_segment_version(request_id="req-seg-n2", actor_id="op-001",
                                         segment_id="seg-north", corridor_id="corr-01",
                                         name="北段边界-勘界", length_km=121.2,
                                         boundary=[{"lat": 30.12, "lng": 120.1}])
        service.register_energy_type_version(request_id="req-et", actor_id="op-001",
                                             energy_type_id="et-ev", code="EV-HEAVY",
                                             name="纯电动重卡", energy_carrier="electricity",
                                             grid_ef_kg_per_kwh=0.5)
        # 因子修订演示：电网因子新版本
        service.register_factor_version(request_id="req-ef-1", actor_id="op-001", factor_id="ef-grid",
                                        factor_key="grid_region_east", name="华东电网因子",
                                        unit="kgCO2/kWh", value=0.55)
        service.register_factor_version(request_id="req-ef-2", actor_id="op-001", factor_id="ef-grid",
                                        factor_key="grid_region_east", name="华东电网因子(修订)",
                                        unit="kgCO2/kWh", value=0.52)
        service.register_baseline_version(
            request_id="req-bl", actor_id="op-001", baseline_id="bl-01", corridor_id="corr-01",
            name="柴油重卡基准", baseline_ef_kg_per_km=1.2,
            load_correction={"method": "payload_ratio", "reference_load_t": 40.0, "min_factor": 0.5})
        service.register_policy_version(request_id="req-pol", actor_id="op-001",
                                        policy_id="pol-01", name="标准证据政策")
        service.register_target_version(request_id="req-tgt", actor_id="op-001", target_id="tgt-01",
                                        corridor_id="corr-01", period_start="2026-09-01",
                                        period_end="2026-09-30", target_tco2=0.3, metric="published")

        # ---- 绿电凭证（车队与站点将共同引用同一批） ----
        service.register_certificate(request_id="req-cert1", actor_id="op-001",
                                     certificate_id="cert-1", batch_no="GEC-2026-09",
                                     energy_kwh=1000.0, valid_from="2026-09-01",
                                     valid_to="2026-09-30")
        service.register_certificate(request_id="req-cert2", actor_id="op-001",
                                     certificate_id="cert-2", batch_no="GEC-2026-10",
                                     energy_kwh=1000.0, valid_from="2026-09-01",
                                     valid_to="2026-10-31")

        # ---- 初版冻结：B 缺载货量、C 双侧冲突 ----
        frozen = service.freeze_batch(request_id="req-freeze1", actor_id="op-001",
                                      corridor_id="corr-01", period_start="2026-09-01",
                                      period_end="2026-09-30", trips=_trips_v1())
        batch1 = frozen["batch_id"]
        deficits = service.evidence_deficits(batch1)
        recompute1 = service.recompute_batch(batch1)

        # 只有 A 计入：120km * 1.2 * (30/40) = 108 kg
        v1_reductions = frozen["result"]["reductions_tco2"]

        # 冻结人不能核验自己；核验人必须独立
        try:
            service.verify_batch(request_id="req-self-verify", actor_id="op-001", batch_id=batch1)
            self_verify_blocked = False
        except PermissionDenied:
            self_verify_blocked = True

        service.verify_batch(request_id="req-verify1", actor_id="rv-001", batch_id=batch1,
                             notes="补证行程不计入，A 行程复算一致")
        published1 = service.publish_batch(request_id="req-publish1", actor_id="rv-001",
                                          batch_id=batch1)
        explain1 = service.explain_reductions(batch1)

        # ---- 迟到数据与冲突更正：生成重述版本，不覆盖初版 ----
        restated = service.restate_batch(request_id="req-restate2", actor_id="op-001",
                                         parent_batch_id=batch1, trips=_trips_v2(),
                                         reason="trip-B 载货量补传；trip-C 车队申报更正")
        batch2 = restated["batch_id"]
        service.verify_batch(request_id="req-verify2", actor_id="rv-001", batch_id=batch2)
        service.publish_batch(request_id="req-publish2", actor_id="rv-001", batch_id=batch2)
        v2_reductions = restated["result"]["reductions_tco2"]

        # 初版历史报告仍以原输入可复算（0.108 t），hash 不变
        history1 = service.recompute_batch(batch1)

        # ---- 凭证撤回：已发布报告不静默改写，先吊销 ----
        withdrawal = service.withdraw_certificate(request_id="req-withdraw", actor_id="rv-001",
                                                  certificate_id="cert-1", reason="凭证复核发现重复签发")
        progress_warned = service.corridor_progress("corr-01", "2026-09-01", "2026-09-30")
        service.revoke_published_batch(request_id="req-revoke2", actor_id="rv-001",
                                       batch_id=batch2, reason="绿电凭证 cert-1 撤回")

        # ---- 更正重述：改用 cert-2，核验后再发布 ----
        restated3 = service.restate_batch(request_id="req-restate3", actor_id="op-001",
                                          parent_batch_id=batch2, trips=_trips_v3(),
                                          reason="以有效凭证 cert-2 替换被撤回的 cert-1")
        batch3 = restated3["batch_id"]
        service.verify_batch(request_id="req-verify3", actor_id="rv-001", batch_id=batch3)
        published3 = service.publish_batch(request_id="req-publish3", actor_id="rv-001",
                                          batch_id=batch3)
        v3_reductions = restated3["result"]["reductions_tco2"]

        progress = service.corridor_progress("corr-01", "2026-09-01", "2026-09-30")
        reports = service.list_published_reports("corr-01")
        segment_versions = len(service.list_master_versions("segment", "seg-north"))
        audit_valid, event_count = service.verify_audit()

        result = {
            "status": "ok",
            "v1_pending_trips": frozen["result"]["pending_trip_count"],
            "v1_deficit_reasons": sorted({reason for item in deficits["items"]
                                          for reason in item["reasons"]}),
            "v1_recompute_matches": recompute1["hash_matches"],
            "self_verify_blocked": self_verify_blocked,
            "v1_reductions_tco2": v1_reductions,
            "v1_explained_trips": len(explain1["valid_trips"]),
            "v2_reductions_tco2": v2_reductions,
            "v3_reductions_tco2": v3_reductions,
            "history_v1_still_recomputes": history1["hash_matches"],
            "history_v1_reductions_tco2": history1["result"]["reductions_tco2"],
            "withdrawal_impact_batch_ids": withdrawal["impact"]["published_batch_ids"],
            "published_report_kinds": [row["kind"] for row in reports],
            "current_published_tco2": progress["achieved_reductions_tco2"],
            "target_tco2": progress["target_tco2"],
            "completion_rate": progress["completion_rate"],
            "integrity_warnings": progress["integrity_warnings"],
            "segment_versions_kept": segment_versions,
            "audit_valid": audit_valid,
            "audit_events": event_count,
            "final_kind": published3["kind"],
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (
        result["status"] == "ok"
        and result["v1_pending_trips"] == 2
        and result["v1_recompute_matches"]
        and result["self_verify_blocked"]
        and abs(result["v1_reductions_tco2"] - 0.108) < 1e-6
        and abs(result["v2_reductions_tco2"] - 0.276) < 1e-6
        and abs(result["v3_reductions_tco2"] - 0.276) < 1e-6
        and result["history_v1_still_recomputes"]
        and abs(result["history_v1_reductions_tco2"] - 0.108) < 1e-6
        and result["published_report_kinds"] == ["initial", "restatement", "revocation", "restatement"]
        and abs(result["current_published_tco2"] - 0.276) < 1e-6
        and result["audit_valid"]
        and result["segment_versions_kept"] == 2
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
