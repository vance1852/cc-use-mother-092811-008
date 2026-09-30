"""碳核算服务测试：版本化、凭证容量、核验发布、重述吊销与复算。"""

import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.service import DomainService

from corridor_carbon.service import CarbonService
from corridor_carbon.storage import CarbonDatabase


def trip(trip_id="t1", payload=None, cert="c1", station_kwh=140.0, fleet_kwh=140.0,
         segment="s1", distance=100.0, load=40.0, energy_type="ev"):
    if payload is None:
        payload = {"load_t": load, "recorded_at": "2026-09-10T06:00:00Z"}
    records = []
    declarations = []
    if cert is not None:
        records.append({"record_id": f"e-{trip_id}", "station_id": "st1",
                        "energy_kwh": station_kwh, "occurred_at": "2026-09-10T02:00:00Z",
                        "certificate_id": cert})
        declarations.append({"certificate_id": cert, "fleet_id": "f1", "energy_kwh": fleet_kwh,
                             "declared_at": "2026-09-10T05:00:00Z"})
    return {
        "trip_id": trip_id, "vehicle_id": "v1", "energy_type_id": energy_type,
        "started_at": "2026-09-10T00:00:00Z", "completed_at": "2026-09-10T04:00:00Z",
        "logged_at": "2026-09-10T04:05:00Z",
        "segment_legs": [{"segment_id": segment, "distance_km": distance}],
        "payload": payload, "energy_records": records, "declarations": declarations,
    }


class CarbonServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = CarbonDatabase()
        clock = FixedClock(datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = CarbonService(self.database, clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="走廊机构")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                 display_name="核算员", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                 display_name="核验人", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        self._master_data()

    def tearDown(self):
        self.database.close()

    def _master_data(self):
        s = self.service
        s.register_segment_version(request_id="seg", actor_id="op1", segment_id="s1",
                                   corridor_id="c1", name="北段", length_km=100.0,
                                   boundary=[{"p": 1}])
        s.register_energy_type_version(request_id="et", actor_id="op1", energy_type_id="ev",
                                       code="EV", name="电动重卡", energy_carrier="electricity",
                                       grid_ef_kg_per_kwh=0.5)
        s.register_factor_version(request_id="ef", actor_id="op1", factor_id="f1",
                                  factor_key="grid_default", name="默认电网", unit="kg/kWh",
                                  value=0.5)
        s.register_baseline_version(request_id="bl", actor_id="op1", baseline_id="b1",
                                    corridor_id="c1", name="柴油基准", baseline_ef_kg_per_km=1.2,
                                    load_correction={"method": "payload_ratio",
                                                     "reference_load_t": 40.0, "min_factor": 0.5})
        s.register_policy_version(request_id="pol", actor_id="op1", policy_id="p1", name="标准政策")
        s.register_target_version(request_id="tgt", actor_id="op1", target_id="g1",
                                  corridor_id="c1", period_start="2026-09-01",
                                  period_end="2026-09-30", target_tco2=0.5)

    def _certificate(self, certificate_id="c1", energy_kwh=1000.0):
        self.service.register_certificate(request_id=f"cert-{certificate_id}", actor_id="op1",
                                          certificate_id=certificate_id, batch_no="B",
                                          energy_kwh=energy_kwh,
                                          valid_from="2026-09-01", valid_to="2026-09-30")

    def _freeze_verify_publish(self, trips, req="b"):
        self._certificate()
        frozen = self.service.freeze_batch(request_id=f"f-{req}", actor_id="op1",
                                           corridor_id="c1", period_start="2026-09-01",
                                           period_end="2026-09-30", trips=trips)
        self.service.verify_batch(request_id=f"v-{req}", actor_id="rv1", batch_id=frozen["batch_id"])
        self.service.publish_batch(request_id=f"p-{req}", actor_id="rv1", batch_id=frozen["batch_id"])
        return frozen

    # ---- 主数据版本 ----
    def test_master_data_is_append_only_versioned(self):
        self.service.register_factor_version(request_id="ef2", actor_id="op1", factor_id="f1",
                                             factor_key="grid_default", name="默认电网修订",
                                             unit="kg/kWh", value=0.45)
        versions = self.service.list_master_versions("factor", "f1")
        self.assertEqual([1, 2], [row["version"] for row in versions])
        self.assertEqual(["active", "superseded"], [row["status"] for row in reversed(versions)])
        self.assertEqual(0.5, versions[0]["value"])  # 旧值原样保留

    def test_auditor_cannot_register_master_data(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_segment_version(request_id="x", actor_id="au1", segment_id="s9",
                                                  corridor_id="c1", name="x", length_km=1.0,
                                                  boundary=[{"p": 1}])

    # ---- 凭证容量与防重复 ----
    def test_certificate_overcapacity_is_rejected(self):
        self._certificate(energy_kwh=100.0)
        with self.assertRaises(ConflictError):
            self.service.freeze_batch(
                request_id="over", actor_id="op1", corridor_id="c1",
                period_start="2026-09-01", period_end="2026-09-30",
                trips=[trip("t1", station_kwh=140.0, fleet_kwh=140.0)],
            )

    def test_certificate_capacity_freed_after_revoke_then_reusable(self):
        frozen = self._freeze_verify_publish([trip("t1", station_kwh=100.0, fleet_kwh=100.0)])
        # 已结清占用 100kWh；另一族再申请 950kWh 会超额（容量 1000）
        with self.assertRaises(ConflictError):
            self.service.freeze_batch(
                request_id="other", actor_id="op1", corridor_id="c1",
                period_start="2026-09-01", period_end="2026-09-30", scope_tag="other",
                trips=[trip("t2", station_kwh=950.0, fleet_kwh=950.0)],
            )
        self.service.revoke_published_batch(request_id="rev", actor_id="rv1",
                                            batch_id=frozen["batch_id"], reason="测试吊销")
        # 吊销后容量释放，950kWh 可被另一口径族再次占用
        again = self.service.freeze_batch(
            request_id="other", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", scope_tag="other",
            trips=[trip("t2", station_kwh=950.0, fleet_kwh=950.0)],
        )
        self.assertEqual("frozen", again["status"])

    def test_withdrawn_certificate_sends_trip_to_deficits_not_zero_emission(self):
        self._certificate()
        self.service.withdraw_certificate(request_id="w", actor_id="op1",
                                          certificate_id="c1", reason="重复签发")
        # 冻结不被拒绝，但引用撤回凭证的行程必须进补证，且不产生任何凭证占用
        frozen = self.service.freeze_batch(
            request_id="bad", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", trips=[trip()],
        )
        self.assertEqual(0, frozen["result"]["valid_trip_count"])
        self.assertEqual(0.0, frozen["result"]["reductions_tco2"])
        deficits = self.service.evidence_deficits(frozen["batch_id"])
        self.assertIn("certificate_withdrawn:c1", deficits["items"][0]["reasons"])
        explanation = self.service.explain_reductions(frozen["batch_id"])
        self.assertEqual([], explanation["certificate_claims"])

    # ---- 核验闸门 ----
    def test_frozen_batch_cannot_be_published_without_verification(self):
        self._certificate()
        frozen = self.service.freeze_batch(
            request_id="f", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", trips=[trip()])
        with self.assertRaises(ConflictError):
            self.service.publish_batch(request_id="p", actor_id="rv1", batch_id=frozen["batch_id"])

    def test_freezer_cannot_verify_own_batch(self):
        self._certificate()
        frozen = self.service.freeze_batch(
            request_id="f", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", trips=[trip()])
        with self.assertRaises(PermissionDenied):
            self.service.verify_batch(request_id="v", actor_id="op1", batch_id=frozen["batch_id"])

    def test_operator_cannot_verify(self):
        self._certificate()
        frozen = self.service.freeze_batch(
            request_id="f", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", trips=[trip()])
        # 即便换一个 operator 账号也不行，角色必须是 reviewer
        self.base.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                 display_name="核算员二", role="operator", organization_id="o1")
        with self.assertRaises(PermissionDenied):
            self.service.verify_batch(request_id="v", actor_id="op2", batch_id=frozen["batch_id"])

    def test_blocking_finding_must_be_resolved_before_verify(self):
        self._certificate()
        frozen = self.service.freeze_batch(
            request_id="f", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", trips=[trip()])
        finding = self.service.add_finding(request_id="fd", actor_id="rv1",
                                           batch_id=frozen["batch_id"], code="check_x",
                                           message="需要说明", blocking=True)
        with self.assertRaises(ConflictError):
            self.service.verify_batch(request_id="v", actor_id="rv1", batch_id=frozen["batch_id"])
        self.service.resolve_finding(request_id="rs", actor_id="rv1",
                                     finding_id=finding["finding_id"])
        verified = self.service.verify_batch(request_id="v", actor_id="rv1",
                                             batch_id=frozen["batch_id"])
        self.assertEqual("verified", verified["status"])

    def test_missing_evidence_trip_excluded_but_batch_verifiable(self):
        self._certificate()
        frozen = self.service.freeze_batch(
            request_id="f", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30",
            trips=[trip("good"), trip("bad", payload=None, cert=None)],
        )
        self.assertEqual(1, frozen["result"]["valid_trip_count"])
        self.assertEqual(1, frozen["result"]["pending_trip_count"])
        deficits = self.service.evidence_deficits(frozen["batch_id"])
        self.assertEqual("bad", deficits["items"][0]["trip_id"])

    # ---- 复算 ----
    def test_recompute_matches_frozen_hash(self):
        frozen = self._freeze_verify_publish([trip()])
        check = self.service.recompute_batch(frozen["batch_id"])
        self.assertTrue(check["hash_matches"])

    # ---- 重述与吊销 ----
    def test_restatement_supersedes_and_history_remains_recomputable(self):
        first = self._freeze_verify_publish([trip("t1", load=20.0)], req="1")
        first_hash = first["result_hash"]
        first_value = first["result"]["reductions_tco2"]
        restated = self.service.restate_batch(
            request_id="r", actor_id="op1", parent_batch_id=first["batch_id"],
            trips=[trip("t1", load=40.0)], reason="载货量补传")
        self.assertEqual(2, restated["version"])
        self.service.verify_batch(request_id="rv2", actor_id="rv1", batch_id=restated["batch_id"])
        published = self.service.publish_batch(request_id="p2", actor_id="rv1",
                                               batch_id=restated["batch_id"])
        self.assertEqual("restatement", published["kind"])
        # 旧批次保留为 restated，原 hash 与原数值仍可复算
        self.assertEqual("restated", self.service.get_batch_header(first["batch_id"])["status"])
        history = self.service.recompute_batch(first["batch_id"])
        self.assertTrue(history["hash_matches"])
        self.assertEqual(first_hash, history["stored_result_hash"])
        self.assertEqual(first_value, history["result"]["reductions_tco2"])
        self.assertNotEqual(first_value, restated["result"]["reductions_tco2"])

    def test_cannot_restate_non_latest_version(self):
        first = self._freeze_verify_publish([trip("t1")], req="1")
        restated = self.service.restate_batch(request_id="r", actor_id="op1",
                                              parent_batch_id=first["batch_id"],
                                              trips=[trip("t1", load=30.0)], reason="补传")
        with self.assertRaises(ConflictError):
            self.service.restate_batch(request_id="r2", actor_id="op1",
                                       parent_batch_id=first["batch_id"],
                                       trips=[trip("t1", load=30.0)], reason="越级重述")
        self.assertEqual(2, restated["version"])

    def test_revocation_then_correction_restatement(self):
        first = self._freeze_verify_publish([trip("t1")], req="1")
        self.service.revoke_published_batch(request_id="rev", actor_id="rv1",
                                            batch_id=first["batch_id"], reason="凭证撤回")
        # 吊销后当前口径减排为 0
        progress = self.service.corridor_progress("c1", "2026-09-01", "2026-09-30")
        self.assertEqual(0.0, progress["achieved_reductions_tco2"])
        with self.assertRaises(ConflictError):
            self.service.revoke_published_batch(request_id="rev2", actor_id="rv1",
                                                batch_id=first["batch_id"], reason="重复吊销")
        # 用新凭证更正重述
        self._certificate("c2")
        corrected = self.service.restate_batch(
            request_id="r", actor_id="op1", parent_batch_id=first["batch_id"],
            trips=[trip("t1", cert="c2")], reason="替换凭证")
        self.service.verify_batch(request_id="rv2", actor_id="rv1", batch_id=corrected["batch_id"])
        published = self.service.publish_batch(request_id="p2", actor_id="rv1",
                                               batch_id=corrected["batch_id"])
        self.assertEqual("restatement", published["kind"])
        progress = self.service.corridor_progress("c1", "2026-09-01", "2026-09-30")
        self.assertGreater(progress["achieved_reductions_tco2"], 0.0)

    def test_verify_rejects_withdrawn_certificate_reserved_by_batch(self):
        self._certificate()
        frozen = self.service.freeze_batch(
            request_id="f", actor_id="op1", corridor_id="c1",
            period_start="2026-09-01", period_end="2026-09-30", trips=[trip()])
        self.service.withdraw_certificate(request_id="w", actor_id="op1",
                                          certificate_id="c1", reason="事后撤回")
        with self.assertRaises(ConflictError):
            self.service.verify_batch(request_id="v", actor_id="rv1", batch_id=frozen["batch_id"])

    # ---- 解释与目标口径 ----
    def test_explain_attributes_every_ton_to_valid_trips(self):
        frozen = self._freeze_verify_publish(
            [trip("a", station_kwh=100.0, fleet_kwh=100.0),
             trip("b", station_kwh=100.0, fleet_kwh=100.0)], req="x")
        explanation = self.service.explain_reductions(frozen["batch_id"])
        self.assertEqual(2, len(explanation["valid_trips"]))
        shares = sum(item["share_of_reductions"] for item in explanation["valid_trips"])
        self.assertAlmostEqual(1.0, shares, places=6)
        self.assertEqual(explanation["total_reductions_tco2"],
                         frozen["result"]["reductions_tco2"])
        self.assertTrue(explanation["certificate_claims"])

    def test_corridor_progress_target_completion(self):
        self._freeze_verify_publish([trip("t1", station_kwh=100.0, fleet_kwh=100.0)])
        progress = self.service.corridor_progress("c1", "2026-09-01", "2026-09-30")
        self.assertEqual(0.5, progress["target_tco2"])
        # 基准 100km*1.2 = 120kg = 0.12t，全绿电 -> 完成率 0.24
        self.assertAlmostEqual(0.12, progress["achieved_reductions_tco2"], places=6)
        self.assertAlmostEqual(0.24, progress["completion_rate"], places=6)

    def test_progress_warns_when_published_report_uses_withdrawn_cert(self):
        frozen = self._freeze_verify_publish([trip("t1")])
        self.service.withdraw_certificate(request_id="w", actor_id="op1",
                                          certificate_id="c1", reason="撤回")
        progress = self.service.corridor_progress("c1", "2026-09-01", "2026-09-30")
        self.assertEqual(1, len(progress["integrity_warnings"]))
        self.assertEqual(frozen["batch_id"], progress["integrity_warnings"][0]["batch_id"])

    # ---- 幂等 ----
    def test_same_request_replays_freeze(self):
        self._certificate()
        kwargs = dict(actor_id="op1", corridor_id="c1", period_start="2026-09-01",
                      period_end="2026-09-30", trips=[trip()])
        first = self.service.freeze_batch(request_id="same", **kwargs)
        second = self.service.freeze_batch(request_id="same", **kwargs)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["batch_id"], second["batch_id"])

    def test_duplicate_scope_initial_freeze_conflicts(self):
        self._certificate()
        self.service.freeze_batch(request_id="f1", actor_id="op1", corridor_id="c1",
                                  period_start="2026-09-01", period_end="2026-09-30", trips=[trip()])
        with self.assertRaises(ConflictError):
            self.service.freeze_batch(request_id="f2", actor_id="op1", corridor_id="c1",
                                      period_start="2026-09-01", period_end="2026-09-30",
                                      trips=[trip()])


if __name__ == "__main__":
    unittest.main()
