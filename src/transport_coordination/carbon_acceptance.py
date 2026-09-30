"""运行走廊碳核算与核验的离线端到端验收。

场景：沪杭走廊首批减排公布前，两条新能源重卡行程——
一条证据齐备且持有车队绿电凭证；另一条充电来源和载荷行程结束后才补传，
冻结时先进入补证；补证补齐重新冻结后，经独立核验与发布；随后因子修订产生
重述版本、凭证撤回产生吊销版本；最后验证历史版本可按原输入复算、走廊目标
只汇总每个批次族最新发布版本。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .carbon_service import CarbonService
from .clock import FixedClock
from .storage import Database


def run() -> dict[str, object]:
    """执行完整核算-核验-重述链并返回可核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "carbon_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc))
        svc = CarbonService(database, clock)

        def advance(days: float) -> None:
            nonlocal clock
            clock = FixedClock(clock.now() + timedelta(days=days))
            svc.clock = clock

        # 主体与角色 -------------------------------------------------------
        svc.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="沪杭走廊运营机构")
        svc.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                           display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in [
            ("op", "op-1", "运营员", "operator"),
            ("rv", "rv-1", "核验员", "reviewer"),
            ("au", "au-1", "发布审计员", "auditor"),
        ]:
            svc.register_actor(request_id=rid, actor_id="admin-1", new_actor_id=aid,
                               display_name=name, role=role, organization_id="o1")

        # 配置版本 ---------------------------------------------------------
        svc.register_corridor(request_id="corridor", actor_id="admin-1",
                              corridor_id="c1", name="沪杭零碳运输走廊")
        svc.publish_boundary(request_id="boundary1", actor_id="admin-1", corridor_id="c1",
                             segments=[{"segment_id": "seg-a", "name": "沪苏段",
                                        "origin": "上海", "destination": "苏州",
                                        "distance_km": 100}], change_note="首版边界")
        svc.publish_target(request_id="target1", actor_id="admin-1", corridor_id="c1",
                           period_start="2026-01-01T00:00:00Z",
                           period_end="2026-03-31T23:59:59Z", target_reduction_t=1.0,
                           methodology="柴油基准排放减电动实际排放")
        svc.publish_vehicle_type(request_id="vt1", actor_id="admin-1",
                                 vehicle_type_id="evt", energy_type="electric",
                                 consumption_rate=1.5, rated_payload_t=30.0,
                                 change_note="新能源重卡v1")
        svc.publish_factor(request_id="fd1", actor_id="admin-1",
                           factor_key="diesel_lcv_per_liter", value=2.68, unit="kgCO2e/L",
                           source="省级温室气体指南",
                           valid_from="2025-01-01T00:00:00Z",
                           valid_to="2026-12-31T23:59:59Z", change_note="柴油v1")
        svc.publish_factor(request_id="fg1", actor_id="admin-1",
                           factor_key="electricity_grid", value=0.58, unit="kgCO2e/kWh",
                           source="区域电网公告",
                           valid_from="2025-01-01T00:00:00Z",
                           valid_to="2026-12-31T23:59:59Z", change_note="电网v1")
        svc.publish_factor(request_id="fr1", actor_id="admin-1",
                           factor_key="electricity_renewable", value=0.01,
                           unit="kgCO2e/kWh", source="绿电核算口径",
                           valid_from="2025-01-01T00:00:00Z",
                           valid_to="2026-12-31T23:59:59Z", change_note="可再生v1")
        svc.publish_baseline(request_id="bl1", actor_id="admin-1", baseline_id="bl1",
                             corridor_id="c1", vehicle_class="重型柴油货车",
                             fuel_intensity_l_per_km=0.32,
                             factor_key="diesel_lcv_per_liter", change_note="基准v1")
        svc.publish_evidence_policy(request_id="ep1", actor_id="admin-1",
                                    evidence_type="payload_report", validity_days=30,
                                    required=True, change_note="载荷30日")
        svc.publish_evidence_policy(request_id="ep2", actor_id="admin-1",
                                    evidence_type="energy_source", validity_days=30,
                                    required=True, change_note="来源30日")

        # 行程 t1：证据齐备 + 车队绿电凭证 ----------------------------------
        svc.record_trip(actor_id="op-1", trip_id="t1", corridor_id="c1", segment_id="seg-a",
                        vehicle_id="truck-1", vehicle_type_id="evt", distance_km=100,
                        occurred_at="2026-01-10T08:00:00Z")
        svc.report_trip_payload(actor_id="op-1", trip_id="t1", payload_t=30)
        svc.record_energy_event(actor_id="op-1", event_id="e1", trip_id="t1", amount=150,
                                station_id="st-1", source_status="reported",
                                occurred_at="2026-01-10T08:30:00Z")
        svc.register_certificate(request_id="g1", actor_id="op-1", certificate_id="g1",
                                 claimant_type="fleet", claimant_id="fleet-1",
                                 kwh_total=100000,
                                 generation_start="2026-01-01T00:00:00Z",
                                 generation_end="2026-01-31T23:59:59Z",
                                 valid_from="2025-01-01T00:00:00Z",
                                 valid_to="2027-12-31T23:59:59Z")
        svc.file_claim(actor_id="op-1", claim_id="cl1", certificate_id="g1",
                       energy_event_id="e1", claimant_type="fleet", claimant_id="fleet-1",
                       kwh=150)

        # 站点对同一事件重复申报 → 拒绝冲突，不重复计算 ---------------------
        svc.register_certificate(request_id="g2", actor_id="op-1", certificate_id="g2",
                                 claimant_type="station", claimant_id="st-1",
                                 kwh_total=100000,
                                 generation_start="2026-01-01T00:00:00Z",
                                 generation_end="2026-01-31T23:59:59Z",
                                 valid_from="2025-01-01T00:00:00Z",
                                 valid_to="2027-12-31T23:59:59Z")
        conflict = svc.file_claim(actor_id="op-1", claim_id="cl2", certificate_id="g2",
                                  energy_event_id="e1", claimant_type="station",
                                  claimant_id="st-1", kwh=150)

        # 行程 t2：来源/载荷行程结束后补传，首次冻结必须进补证 ---------------
        svc.record_trip(actor_id="op-1", trip_id="t2", corridor_id="c1", segment_id="seg-a",
                        vehicle_id="truck-2", vehicle_type_id="evt", distance_km=100,
                        occurred_at="2026-01-11T08:00:00Z")

        svc.create_batch(request_id="batch1", actor_id="op-1", batch_id="b1",
                         corridor_id="c1", period_start="2026-01-01T00:00:00Z",
                         period_end="2026-01-31T23:59:59Z", baseline_id="bl1")
        frozen1 = svc.freeze_batch(actor_id="op-1", batch_id="b1")
        evidence_codes_first = {item["code"] for item in
                                svc.list_evidence(batch_id="b1", version=1, status_filter="open")}
        svc.review_batch(actor_id="rv-1", batch_id="b1", decision="reject",
                         note="t2 载荷与来源缺失，进入补证")

        # 补传后重新冻结 ---------------------------------------------------
        svc.report_trip_payload(actor_id="op-1", trip_id="t2", payload_t=28)
        svc.record_energy_event(actor_id="op-1", event_id="e2", trip_id="t2", amount=150,
                                station_id="st-1", source_status="reported",
                                occurred_at="2026-01-11T08:30:00Z")
        frozen2 = svc.freeze_batch(actor_id="op-1", batch_id="b1")
        # 重复申报产生的非阻断补证单由核验员裁定：错误申报不产生减排，电量已按电网处理。
        for item in svc.list_evidence(batch_id="b1", version=1, status_filter="open"):
            svc.adjudicate_evidence(actor_id="rv-1", request_id=item["request_id"],
                                    decision="waived", note="站点重复申报不成立")
        svc.review_batch(actor_id="rv-1", batch_id="b1", decision="approve",
                         note="补证齐备，独立核验通过")
        published1 = svc.publish_batch(actor_id="au-1", batch_id="b1")

        # 因子修订 → 重述 v2（t2 无绿证，走电网因子）------------------------
        advance(30)
        svc.publish_factor(request_id="fg2", actor_id="admin-1",
                           factor_key="electricity_grid", value=0.50, unit="kgCO2e/kWh",
                           source="区域电网公告",
                           valid_from="2026-01-01T00:00:00Z",
                           valid_to="2026-12-31T23:59:59Z", change_note="电网v2修订")
        svc.restate_batch(actor_id="op-1", batch_id="b1", revision_kind="factor_revision",
                          change_note="采用修订后电网因子")
        for item in svc.list_evidence(batch_id="b1", version=2, status_filter="open"):
            svc.adjudicate_evidence(actor_id="rv-1", request_id=item["request_id"],
                                    decision="waived", note="重复申报不影响电网口径")
        svc.review_batch(actor_id="rv-1", batch_id="b1", decision="approve",
                         note="重述版本核验通过")
        svc.publish_batch(actor_id="au-1", batch_id="b1")

        # 凭证撤回 → 吊销重述 v3，绿电收益回退电网 --------------------------
        advance(5)
        svc.withdraw_certificate(actor_id="au-1", certificate_id="g1",
                                 reason="发现重复签发")
        svc.restate_batch(actor_id="op-1", batch_id="b1",
                          revision_kind="certificate_revocation",
                          change_note="g1 撤回，t1 电量回退电网口径")
        pending = svc.list_evidence(batch_id="b1", version=3, status_filter="open")
        for item in pending:
            svc.adjudicate_evidence(actor_id="rv-1", request_id=item["request_id"],
                                    decision="waived", note="撤回事实成立，已回退电网")
        svc.review_batch(actor_id="rv-1", batch_id="b1", decision="approve",
                         note="吊销版本核验通过")
        svc.publish_batch(actor_id="au-1", batch_id="b1")

        # 历史版本全部按原输入复算 -----------------------------------------
        recomputed = {v: svc.recompute_batch("b1", v) for v in (1, 2, 3)}
        versions = svc.list_batch_versions("b1")
        progress = svc.corridor_progress("c1")
        audit_valid, audit_events = svc.verify_audit()
        explanation = svc.explain_batch("b1", 3)

        result = {
            "status": "ok",
            "first_freeze_accepted": frozen1["trips_accepted"],
            "first_freeze_excluded": frozen1["trips_excluded"],
            "first_freeze_evidence_codes": sorted(evidence_codes_first),
            "station_double_claim_status": conflict["status"],
            "second_freeze_accepted": frozen2["trips_accepted"],
            "published_v1_avoided_tco2": published1["total_avoided_tco2"],
            "version_statuses": [{"version": item["version"], "status": item["status"]}
                                 for item in versions],
            "v3_avoided_tco2": explanation["totals"]["avoided_tco2"],
            "v3_green_kwh": explanation["trip_contributions"][0]["matched_green_kwh"],
            "all_versions_recompute": all(item["result_matches"] and item["snapshot_matches"]
                                          for item in recomputed.values()),
            "v1_hashes_stable": recomputed[1]["stored_result_hash"]
                               == recomputed[1]["recomputed_result_hash"],
            "progress_latest_version": progress.included_batches[0]["version"],
            "progress_family_count": len(progress.included_batches),
            "progress_achieved_t": progress.achieved_reduction_t,
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["first_freeze_excluded"] == 1
          and result["first_freeze_accepted"] == 1
          and result["second_freeze_accepted"] == 2
          and result["station_double_claim_status"] == "rejected_conflict"
          and result["all_versions_recompute"]
          and result["v1_hashes_stable"]
          and result["progress_latest_version"] == 3
          and result["progress_family_count"] == 1
          and result["v3_green_kwh"] == 0.0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
