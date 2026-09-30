"""测试碳核算服务：版本化、冻结、防重复计算、独立核验与重述吊销。"""

import json
import unittest
from datetime import datetime, timezone

from transport_coordination.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

from tests._carbon_world import World, iso


class CarbonFlowTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.s = self.world.service

    def tearDown(self):
        self.world.close()

    def freeze_review_publish(self, batch_id="b1"):
        frozen = self.s.freeze_batch(actor_id="op-1", batch_id=batch_id)
        self.s.review_batch(actor_id="rv-1", batch_id=batch_id, decision="approve",
                            note="证据齐备")
        published = self.s.publish_batch(actor_id="au-1", batch_id=batch_id)
        return frozen, published

    def test_config_versions_are_immutable_and_numbered(self):
        self.s.publish_factor(request_id="f-grid-v2", actor_id="admin-1",
                              factor_key="electricity_grid", value=0.52,
                              unit="kgCO2e/kWh", source="区域电网公告",
                              valid_from="2026-02-01T00:00:00Z",
                              valid_to="2026-12-31T23:59:59Z", change_note="电网因子v2")
        rows = self.database_query(
            "SELECT version,value FROM emission_factor_versions WHERE factor_key='electricity_grid'"
            " ORDER BY version")
        self.assertEqual([(1, 0.58), (2, 0.52)], [(r[0], r[1]) for r in rows])

    def database_query(self, sql):
        return self.s.database.connection.execute(sql).fetchall()

    def test_published_batch_explains_each_ton_by_trip(self):
        self.world.add_green_trip("t1")
        self.world.add_green_trip("t2")
        self.world.make_batch()
        _, published = self.freeze_review_publish()
        self.assertGreater(published["total_avoided_tco2"], 0)
        explanation = self.s.explain_batch("b1")
        self.assertEqual(len(explanation["trip_contributions"]), 2)
        # 逐行程减排量之和等于批次总量。
        total_by_trips = round(sum(c["avoided_tco2"] for c in
                                   explanation["trip_contributions"]), 6)
        self.assertEqual(total_by_trips, explanation["totals"]["avoided_tco2"])
        self.assertEqual(explanation["config_versions"]["factors"]["electricity_grid"], 1)

    def test_missing_payload_becomes_evidence_request_and_zero_accepted(self):
        self.world.add_trip("t1")
        self.world.add_energy("e1", "t1")
        self.world.make_batch()
        frozen = self.s.freeze_batch(actor_id="op-1", batch_id="b1")
        self.assertEqual(frozen["trips_accepted"], 0)
        self.assertEqual(frozen["trips_excluded"], 1)
        self.assertEqual(frozen["total_avoided_tco2"], 0.0)
        evidence = self.s.list_evidence(batch_id="b1")
        codes = {item["code"] for item in evidence}
        self.assertIn("EVIDENCE_PAYLOAD_MISSING", codes)
        # 有补证未处理时不能发布（核验环节阻断）。
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="reject",
                            note="缺载荷，补证后重新冻结")
        with self.assertRaises(ConflictError):
            self.s.publish_batch(actor_id="au-1", batch_id="b1")

    def test_energy_source_reported_late_then_recalculate(self):
        # 登记行程时充电来源未知 → 冻结被排除；补报后重新冻结可承认。
        self.world.add_trip("t1")
        self.world.add_payload("t1")
        self.world.add_energy("e1", "t1", source_status="unknown")
        self.world.add_certificate("g1")
        self.world.make_batch()
        first = self.s.freeze_batch(actor_id="op-1", batch_id="b1")
        self.assertEqual(first["trips_excluded"], 1)
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="reject", note="补来源")
        # 补证补齐后重新冻结。
        self.s.report_energy_source(actor_id="op-1", event_id="e1")
        self.world.add_claim("cl1", cert_id="g1", event_id="e1")
        second = self.s.freeze_batch(actor_id="op-1", batch_id="b1")
        self.assertEqual(second["trips_accepted"], 1)
        self.assertGreater(second["total_avoided_tco2"], 0)

    def test_certificate_double_claim_by_fleet_and_station_is_conflict(self):
        self.world.add_trip("t1")
        self.world.add_payload("t1")
        self.world.add_energy("e1", "t1")
        self.world.add_certificate("g1", claimant_type="fleet", claimant_id="fleet-1")
        accepted = self.world.add_claim("cl-fleet", cert_id="g1", event_id="e1",
                                        claimant_type="fleet", claimant_id="fleet-1")
        self.assertEqual(accepted["status"], "accepted")
        # 站点拿另一张凭证对同一事件申报 → 拒绝并记录冲突原因。
        self.world.add_certificate("g2", claimant_type="station",
                                   claimant_id="station-7", request_id="cert-g2")
        conflict = self.s.file_claim(
            actor_id="op-1", claim_id="cl-station", certificate_id="g2",
            energy_event_id="e1", claimant_type="station", claimant_id="station-7", kwh=150)
        self.assertEqual(conflict["status"], "rejected_conflict")
        self.assertEqual(conflict["conflict_code"],
                         "event_already_claimed_other_certificate")

    def test_certificate_capacity_blocks_overuse_across_batches(self):
        self.world.add_certificate("g-small", kwh_total=200.0, request_id="cert-small",
                                   generation_start="2026-01-01T00:00:00Z",
                                   generation_end="2026-03-31T23:59:59Z")
        self.world.add_green_trip("t1", cert_id="g-small")
        self.world.make_batch("b1")
        self.freeze_review_publish("b1")
        self.world.add_green_trip(
            "t2", cert_id="g-small",
            when=datetime(2026, 2, 10, 8, tzinfo=timezone.utc),
            energy_at="2026-02-10T08:30:00Z")
        self.s.create_batch(request_id="batch-b2", actor_id="op-1", batch_id="b2",
                                  corridor_id="c1",
                                  period_start="2026-02-01T00:00:00Z",
                                  period_end="2026-02-28T23:59:59Z", baseline_id="bl1")
        with self.assertRaises(ConflictError) as ctx:
            self.s.freeze_batch(actor_id="op-1", batch_id="b2")
        self.assertIn("g-small", str(ctx.exception))

    def test_certificate_overuse_within_single_batch_is_rejected(self):
        # 同一批次内两条行程对一张 200kWh 凭证各申报 150kWh → 冻结即拒绝。
        self.world.add_certificate("g-small", kwh_total=200.0, request_id="cert-small",
                                   generation_start="2026-01-01T00:00:00Z",
                                   generation_end="2026-03-31T23:59:59Z")
        self.world.add_green_trip("t1", cert_id="g-small")
        self.world.add_green_trip(
            "t2", cert_id="g-small",
            when=datetime(2026, 1, 12, 8, tzinfo=timezone.utc),
            energy_at="2026-01-12T08:30:00Z")
        self.world.make_batch("b1")
        with self.assertRaises(ConflictError) as ctx:
            self.s.freeze_batch(actor_id="op-1", batch_id="b1")
        self.assertIn("g-small", str(ctx.exception))
        # 冻结失败不留占用、不留批次锁。
        held = self.database_query(
            "SELECT COUNT(*) AS c FROM carbon_allocations WHERE status='held'")[0]["c"]
        self.assertEqual(held, 0)
        locks = self.database_query(
            "SELECT COUNT(*) AS c FROM carbon_trip_locks")[0]["c"]
        self.assertEqual(locks, 0)

    def test_trip_cannot_join_two_batch_families(self):
        self.world.add_green_trip("t1")
        self.world.make_batch("b1")
        self.freeze_review_publish("b1")
        self.world.make_batch("b2")
        with self.assertRaises(ConflictError):
            self.s.freeze_batch(actor_id="op-1", batch_id="b2")

    def test_independent_review_and_publish_segregation_of_duties(self):
        self.world.add_green_trip("t1")
        self.world.make_batch()
        self.s.freeze_batch(actor_id="op-1", batch_id="b1")
        # 冻结人本人不能核验。
        with self.assertRaises(PermissionDenied):
            self.s.review_batch(actor_id="op-1", batch_id="b1", decision="approve")
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="approve")
        # 核验人本人不能发布。
        with self.assertRaises(PermissionDenied):
            self.s.publish_batch(actor_id="rv-1", batch_id="b1")
        with self.assertRaises(PermissionDenied):
            self.s.publish_batch(actor_id="op-1", batch_id="b1")
        self.s.publish_batch(actor_id="au-1", batch_id="b1")

    def test_published_inputs_frozen_and_only_restate_changes(self):
        self.world.add_green_trip("t1")
        self.world.make_batch()
        self.freeze_review_publish()
        v1_before = self.s.recompute_batch("b1", 1)
        # 已发布行程允许补录迟到运营数据，但已冻结的 v1 快照不受影响。
        self.world.advance(days=5)
        self.s.report_trip_payload(actor_id="op-1", trip_id="t1", payload_t=20)
        v1_after = self.s.recompute_batch("b1", 1)
        self.assertEqual(v1_before["stored_result_hash"], v1_after["stored_result_hash"])
        # 没有任何变化依据时不能创建重述版本。
        with self.assertRaises(ValidationError):
            self.s.restate_batch(actor_id="op-1", batch_id="b1",
                                 revision_kind="factor_revision", change_note="无变化")

    def test_late_data_restatement_creates_v2_and_keeps_v1_recomputable(self):
        self.world.add_green_trip("t1")
        self.world.make_batch()
        _, v1 = self.freeze_review_publish()
        v1_hash = v1["result_hash"]
        # 行程结束后补传更低的载货量。
        self.world.advance(days=5)
        self.s.report_trip_payload(actor_id="op-1", trip_id="t1", payload_t=15)
        restated = self.s.restate_batch(actor_id="op-1", batch_id="b1",
                                        revision_kind="late_data",
                                        change_note="载荷迟到补传")
        self.assertEqual(restated["version"], 2)
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="approve",
                            note="重述核对")
        self.s.publish_batch(actor_id="au-1", batch_id="b1")
        versions = self.s.list_batch_versions("b1")
        statuses = {item["version"]: item["status"] for item in versions}
        self.assertEqual(statuses[1], "restated")
        self.assertEqual(statuses[2], "published")
        # v1 历史报告仍可按原输入复算，哈希不变。
        recompute_v1 = self.s.recompute_batch("b1", 1)
        self.assertTrue(recompute_v1["result_matches"])
        self.assertEqual(recompute_v1["stored_result_hash"], v1_hash)
        recompute_v2 = self.s.recompute_batch("b1", 2)
        self.assertTrue(recompute_v2["result_matches"])
        self.assertLess(recompute_v2["result"]["total_avoided_tco2"],
                        recompute_v1["result"]["total_avoided_tco2"])

    def test_factor_revision_restatement_picks_new_factor(self):
        # 充电但无绿证的行程，实际排放全部走电网因子，因子修订会改变结果。
        self.world.add_trip("t1")
        self.world.add_payload("t1")
        self.world.add_energy("e1", "t1", source_status="reported")
        self.world.make_batch()
        self.freeze_review_publish()
        before = self.s.explain_batch("b1", 1)
        self.assertEqual(before["trip_contributions"][0]["grid_kwh"], 150.0)
        # 发布新电网因子版本（因子修订不影响已冻结的 v1）。
        self.s.publish_factor(request_id="f-grid-2", actor_id="admin-1",
                              factor_key="electricity_grid", value=0.30,
                              unit="kgCO2e/kWh", source="区域电网公告",
                              valid_from="2026-01-01T00:00:00Z",
                              valid_to="2026-12-31T23:59:59Z", change_note="电网清洁化")
        restated = self.s.restate_batch(actor_id="op-1", batch_id="b1",
                                        revision_kind="factor_revision",
                                        change_note="采用修订后电网因子")
        after = self.s.explain_batch("b1", restated["version"])
        self.assertEqual(after["config_versions"]["factors"]["electricity_grid"], 2)
        self.assertNotEqual(after["totals"]["avoided_tco2"],
                            before["totals"]["avoided_tco2"])
        self.assertEqual(
            self.s.explain_batch("b1", 1)["config_versions"]["factors"]["electricity_grid"], 1)

    def test_certificate_withdrawal_revocation_version_zeros_green_benefit(self):
        self.world.add_green_trip("t1")
        self.world.make_batch()
        self.freeze_review_publish()
        v1 = self.s.explain_batch("b1", 1)
        green_before = v1["trip_contributions"][0]["matched_green_kwh"]
        self.assertEqual(green_before, 150.0)
        self.s.withdraw_certificate(actor_id="au-1", certificate_id="g1",
                                    reason="凭证重复签发调查")
        revoked = self.s.restate_batch(actor_id="op-1", batch_id="b1",
                                       revision_kind="certificate_revocation",
                                       change_note="凭证撤回，电量回退电网口径")
        self.assertEqual(revoked["revision_kind"], "certificate_revocation")
        v2 = self.s.explain_batch("b1", revoked["version"])
        self.assertEqual(v2["trip_contributions"][0]["matched_green_kwh"], 0.0)
        self.assertLess(v2["totals"]["avoided_tco2"], v1["totals"]["avoided_tco2"])
        # 撤回产生的补证单为非阻断性问题，核验员裁定豁免后方可通过。
        pending = self.s.list_evidence(batch_id="b1", version=2, status_filter="open")
        self.assertEqual({item["code"] for item in pending}, {"CERTIFICATE_WITHDRAWN"})
        self.s.adjudicate_evidence(actor_id="rv-1", request_id=pending[0]["request_id"],
                                   decision="waived", note="凭证已撤回，电量已回退电网")
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="approve")
        self.s.publish_batch(actor_id="au-1", batch_id="b1")
        versions = {item["version"]: item["status"]
                    for item in self.s.list_batch_versions("b1")}
        self.assertEqual(versions[1], "revoked")
        self.assertEqual(versions[2], "published")
        # 吊销后走廊口径只计回退电网后的 v2，且 v1 仍可按原输入复算。
        progress = self.s.corridor_progress("c1")
        self.assertEqual(progress.included_batches[0]["version"], 2)
        self.assertTrue(self.s.recompute_batch("b1", 1)["result_matches"])

    def test_rejected_revision_restores_prior_published_version(self):
        self.world.add_green_trip("t1")
        self.world.make_batch()
        self.freeze_review_publish()
        self.world.advance(days=5)
        self.s.report_trip_payload(actor_id="op-1", trip_id="t1", payload_t=15)
        self.s.restate_batch(actor_id="op-1", batch_id="b1", revision_kind="late_data",
                             change_note="迟到载荷")
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="reject",
                            note="新证据不成立")
        statuses = {item["version"]: item["status"]
                    for item in self.s.list_batch_versions("b1")}
        self.assertEqual(statuses[1], "published")
        self.assertEqual(statuses[2], "rejected")
        # 走廊口径仍按 v1 计数。
        progress = self.s.corridor_progress("c1")
        self.assertEqual(len(progress.included_batches), 1)
        self.assertEqual(progress.included_batches[0]["version"], 1)

    def test_corridor_progress_uses_latest_version_per_family(self):
        self.world.add_green_trip("t1")
        self.world.make_batch("b1")
        self.freeze_review_publish("b1")
        progress = self.s.corridor_progress("c1")
        first_achieved = progress.achieved_reduction_t
        self.assertGreater(progress.completion_ratio, 0)
        self.assertEqual(progress.included_batches[0]["version"], 1)
        # 重述后目标口径只计 v2，不重复计 v1+v2。
        self.world.advance(days=5)
        self.s.report_trip_payload(actor_id="op-1", trip_id="t1", payload_t=15)
        self.s.restate_batch(actor_id="op-1", batch_id="b1", revision_kind="late_data",
                             change_note="迟到载荷")
        self.s.review_batch(actor_id="rv-1", batch_id="b1", decision="approve")
        self.s.publish_batch(actor_id="au-1", batch_id="b1")
        progress2 = self.s.corridor_progress("c1")
        self.assertEqual(len(progress2.included_batches), 1)
        self.assertEqual(progress2.included_batches[0]["version"], 2)
        self.assertNotEqual(progress2.achieved_reduction_t, first_achieved)

    def test_recompute_detects_tampered_snapshot(self):
        self.world.add_green_trip("t1")
        self.world.make_batch()
        self.freeze_review_publish()
        conn = self.s.database.connection
        row = conn.execute("SELECT input_snapshot_json FROM carbon_batches WHERE batch_id='b1'"
                           ).fetchone()
        snapshot = json.loads(row["input_snapshot_json"])
        snapshot["trips"][0]["distance_km"] = 999
        from transport_coordination.carbon_engine import compute_batch
        tampered = compute_batch(snapshot)
        from transport_coordination.audit import digest
        from transport_coordination.carbon_engine import result_to_dict
        # 篡改输入后结果哈希必然与发布时不一致。
        self.assertNotEqual(digest(result_to_dict(tampered)),
                            self.s.get_batch("b1").result_hash)

    def test_evidence_policy_expiry_excludes_late_payload(self):
        # 行程发生在很早的时间，补传时已超过证据有效期。
        self.world.add_trip("t1", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.world.advance(days=40)
        self.world.add_payload("t1")
        self.world.add_energy("e1", "t1")
        self.world.add_certificate("g1")
        self.world.add_claim("cl1", event_id="e1")
        self.world.make_batch()
        frozen = self.s.freeze_batch(actor_id="op-1", batch_id="b1")
        self.assertEqual(frozen["trips_accepted"], 0)
        codes = set(frozen["evidence_open"])
        self.assertTrue({"EVIDENCE_PAYLOAD_EXPIRED", "EVIDENCE_ENERGY_EXPIRED"} & codes)


if __name__ == "__main__":
    unittest.main()
