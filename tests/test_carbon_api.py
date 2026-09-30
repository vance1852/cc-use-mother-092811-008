"""测试碳核算 HTTP/JSON 路由。"""

import json
import unittest

from transport_coordination.api import route
from transport_coordination.carbon_service import CarbonService
from transport_coordination.clock import FixedClock
from transport_coordination.storage import Database
from datetime import datetime, timezone


class CarbonApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CarbonService(
            self.database, FixedClock(datetime(2026, 2, 1, tzinfo=timezone.utc)))
        self._setup_config()

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op-1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor})

    def _setup_config(self):
        c = self.call
        c("POST", "/organizations",
          {"request_id": "org", "organization_id": "o1", "name": "机构"}, "bootstrap")
        for rid, aid, role in [("admin", "admin-1", "admin"), ("op", "op-1", "operator"),
                               ("rv", "rv-1", "reviewer"), ("au", "au-1", "auditor")]:
            actor = "bootstrap" if aid == "admin-1" else "admin-1"
            c("POST", "/actors", {"request_id": rid, "new_actor_id": aid,
                                  "display_name": aid, "role": role,
                                  "organization_id": "o1"}, actor)
        c("POST", "/carbon/corridors",
          {"request_id": "corridor", "corridor_id": "c1", "name": "走廊"}, "admin-1")
        c("POST", "/carbon/corridors/c1/boundary-versions",
          {"request_id": "bdy", "change_note": "v1", "segments": [
              {"segment_id": "seg-a", "name": "A-B", "origin": "A", "destination": "B",
               "distance_km": 100}]}, "admin-1")
        c("POST", "/carbon/corridors/c1/target-versions",
          {"request_id": "tgt", "period_start": "2026-01-01T00:00:00Z",
           "period_end": "2026-12-31T23:59:59Z", "target_reduction_t": 5,
           "methodology": "基准减实际"}, "admin-1")
        c("POST", "/carbon/vehicle-types",
          {"request_id": "vt", "vehicle_type_id": "evt", "energy_type": "electric",
           "consumption_rate": 1.5, "rated_payload_t": 30, "change_note": "v1"}, "admin-1")
        for rid, key, value in [("fd", "diesel_lcv_per_liter", 2.68),
                                ("fg", "electricity_grid", 0.58),
                                ("fr", "electricity_renewable", 0.01)]:
            c("POST", "/carbon/factors",
              {"request_id": rid, "factor_key": key, "value": value, "unit": "kg",
               "source": "指南", "valid_from": "2025-01-01T00:00:00Z",
               "valid_to": "2026-12-31T23:59:59Z", "change_note": "v1"}, "admin-1")
        c("POST", "/carbon/baselines",
          {"request_id": "bl", "baseline_id": "bl1", "corridor_id": "c1",
           "vehicle_class": "重卡", "fuel_intensity_l_per_km": 0.32,
           "factor_key": "diesel_lcv_per_liter", "change_note": "v1"}, "admin-1")
        c("POST", "/carbon/evidence-policies",
          {"request_id": "ep1", "evidence_type": "payload_report", "validity_days": 30,
           "required": True, "change_note": "v1"}, "admin-1")
        c("POST", "/carbon/evidence-policies",
          {"request_id": "ep2", "evidence_type": "energy_source", "validity_days": 30,
           "required": True, "change_note": "v1"}, "admin-1")

    def _green_trip(self, trip_id="t1"):
        c = self.call
        c("POST", "/carbon/trips",
          {"trip_id": trip_id, "corridor_id": "c1", "segment_id": "seg-a",
           "vehicle_id": "truck-1", "vehicle_type_id": "evt", "distance_km": 100,
           "occurred_at": "2026-01-10T08:00:00Z"})
        c("POST", f"/carbon/trips/{trip_id}/payload", {"payload_t": 30})
        c("POST", "/carbon/energy-events",
          {"event_id": "e1", "trip_id": trip_id, "amount": 150, "station_id": "st-1",
           "source_status": "reported", "occurred_at": "2026-01-10T08:30:00Z"})
        c("POST", "/carbon/certificates",
          {"request_id": "g1", "certificate_id": "g1", "claimant_type": "fleet",
           "claimant_id": "fleet-1", "kwh_total": 100000,
           "generation_start": "2026-01-01T00:00:00Z",
           "generation_end": "2026-01-31T23:59:59Z",
           "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z"})
        c("POST", "/carbon/claims",
          {"claim_id": "cl1", "certificate_id": "g1", "energy_event_id": "e1",
           "claimant_type": "fleet", "claimant_id": "fleet-1", "kwh": 150})

    def test_full_lifecycle_and_explanation(self):
        c = self.call
        self._green_trip()
        status, body = c("POST", "/carbon/batches",
                         {"request_id": "b1", "batch_id": "b1", "corridor_id": "c1",
                          "period_start": "2026-01-01T00:00:00Z",
                          "period_end": "2026-01-31T23:59:59Z", "baseline_id": "bl1"})
        self.assertEqual(201, status)
        status, frozen = c("POST", "/carbon/batches/b1/freeze", {})
        self.assertEqual(200, status)
        self.assertEqual(frozen["trips_accepted"], 1)
        self.assertGreater(frozen["total_avoided_tco2"], 0)
        # 冻结人不能核验。
        status, body = c("POST", "/carbon/batches/b1/review",
                         {"decision": "approve", "note": "ok"})
        self.assertEqual(403, status)
        status, body = c("POST", "/carbon/batches/b1/review",
                         {"decision": "approve", "note": "ok"}, "rv-1")
        self.assertEqual(200, status)
        status, body = c("POST", "/carbon/batches/b1/publish", {}, "rv-1")
        self.assertEqual(403, status)
        status, published = c("POST", "/carbon/batches/b1/publish", {}, "au-1")
        self.assertEqual(200, status)
        status, explanation = c("GET", "/carbon/batches/b1/explanation")
        self.assertEqual(200, status)
        self.assertEqual(len(explanation["trip_contributions"]), 1)
        self.assertEqual(explanation["trip_contributions"][0]["trip_id"], "t1")
        self.assertEqual(explanation["totals"]["avoided_tco2"],
                         published["total_avoided_tco2"])
        status, rec = c("POST", "/carbon/batches/b1/recompute")
        self.assertEqual(200, status)
        self.assertTrue(rec["result_matches"])
        self.assertTrue(rec["snapshot_matches"])
        status, progress = c("GET", "/carbon/corridors/c1/progress")
        self.assertEqual(200, status)
        self.assertEqual(progress["included_batches"][0]["version"], 1)
        self.assertGreater(progress["completion_ratio"], 0)

    def test_missing_evidence_route_returns_conflict_on_publish(self):
        c = self.call
        # 只有行程，载荷与充电来源均缺。
        c("POST", "/carbon/trips",
          {"trip_id": "t1", "corridor_id": "c1", "segment_id": "seg-a",
           "vehicle_id": "truck-1", "vehicle_type_id": "evt", "distance_km": 100,
           "occurred_at": "2026-01-10T08:00:00Z"})
        c("POST", "/carbon/batches",
          {"request_id": "b1", "batch_id": "b1", "corridor_id": "c1",
           "period_start": "2026-01-01T00:00:00Z",
           "period_end": "2026-01-31T23:59:59Z", "baseline_id": "bl1"})
        status, frozen = c("POST", "/carbon/batches/b1/freeze", {})
        self.assertEqual(frozen["trips_excluded"], 1)
        self.assertEqual(frozen["total_avoided_tco2"], 0.0)
        status, evidence = c("GET", "/carbon/evidence?batch_id=b1&status=open")
        self.assertEqual(200, status)
        codes = {item["code"] for item in evidence["items"]}
        self.assertIn("EVIDENCE_PAYLOAD_MISSING", codes)
        self.assertIn("EVIDENCE_ENERGY_RECORD_MISSING", codes)

    def test_cross_party_claim_conflict_visible_via_api(self):
        c = self.call
        self._green_trip()
        c("POST", "/carbon/certificates",
          {"request_id": "g2", "certificate_id": "g2", "claimant_type": "station",
           "claimant_id": "st-7", "kwh_total": 100000,
           "generation_start": "2026-01-01T00:00:00Z",
           "generation_end": "2026-01-31T23:59:59Z",
           "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z"})
        status, body = c("POST", "/carbon/claims",
                         {"claim_id": "cl2", "certificate_id": "g2", "energy_event_id": "e1",
                          "claimant_type": "station", "claimant_id": "st-7", "kwh": 150})
        self.assertEqual(201, status)
        self.assertEqual("rejected_conflict", body["status"])

    def test_idempotent_certificate_post_replays(self):
        c = self.call
        self._green_trip()
        payload = {"request_id": "g-dup", "certificate_id": "g9", "claimant_type": "fleet",
                   "claimant_id": "fleet-1", "kwh_total": 10,
                   "generation_start": "2026-01-01T00:00:00Z",
                   "generation_end": "2026-01-31T23:59:59Z",
                   "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2027-12-31T23:59:59Z"}
        s1, b1 = c("POST", "/carbon/certificates", payload)
        s2, b2 = c("POST", "/carbon/certificates", payload)
        self.assertEqual(201, s1)
        self.assertEqual(200, s2)
        self.assertFalse(b1["replayed"])
        self.assertTrue(b2["replayed"])


if __name__ == "__main__":
    unittest.main()
