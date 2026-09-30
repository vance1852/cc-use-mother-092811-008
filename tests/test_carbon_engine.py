"""测试纯函数核算引擎的口径与确定性。"""

import copy
import unittest

from transport_coordination.audit import digest
from transport_coordination.carbon_engine import FORMULA_VERSION, compute_batch, result_to_dict


def make_snapshot():
    """构造一个含两条完整行程的基础快照。"""

    return {
        "formula_version": FORMULA_VERSION,
        "corridor_id": "c1",
        "period_start": "2026-01-01T00:00:00Z",
        "period_end": "2026-01-31T23:59:59Z",
        "frozen_at": "2026-02-01T00:00:00Z",
        "boundary": {"version": 1, "segments": [
            {"segment_id": "seg-a", "name": "A-B", "origin": "A", "destination": "B",
             "distance_km": 100},
            {"segment_id": "seg-b", "name": "B-C", "origin": "B", "destination": "C",
             "distance_km": 200},
        ]},
        "baseline": {"baseline_id": "bl1", "version": 1, "vehicle_class": "重卡",
                     "fuel_intensity_l_per_km": 0.32, "factor_key": "diesel_lcv_per_liter"},
        "factors": {
            "diesel_lcv_per_liter": {"factor_key": "diesel_lcv_per_liter", "version": 1,
                                     "value": 2.68, "unit": "kgCO2e/L", "source": "指南",
                                     "valid_from": "2025-01-01", "valid_to": "2026-12-31"},
            "electricity_grid": {"factor_key": "electricity_grid", "version": 1,
                                 "value": 0.58, "unit": "kgCO2e/kWh", "source": "电网",
                                 "valid_from": "2025-01-01", "valid_to": "2026-12-31"},
            "electricity_renewable": {"factor_key": "electricity_renewable", "version": 1,
                                      "value": 0.01, "unit": "kgCO2e/kWh", "source": "绿电",
                                      "valid_from": "2025-01-01", "valid_to": "2026-12-31"},
        },
        "vehicle_types": {"evt": {"vehicle_type_id": "evt", "version": 1,
                                  "energy_type": "electric", "consumption_rate": 1.5,
                                  "rated_payload_t": 30}},
        "policies": {
            "payload_report": {"evidence_type": "payload_report", "version": 1,
                               "validity_days": 30, "required": True},
            "energy_source": {"evidence_type": "energy_source", "version": 1,
                              "validity_days": 30, "required": True},
        },
        "trips": [],
        "config_versions": {},
    }


def trip(trip_id, segment="seg-a", *, payload=30, flags=None, events=None,
         payload_updated=None, payload_status="reported", distance=100):
    events = events if events is not None else [event("e-" + trip_id)]
    return {
        "trip_id": trip_id, "segment_id": segment, "vehicle_id": "v1",
        "vehicle_type_id": "evt", "distance_km": distance,
        "occurred_at": "2026-01-10T08:00:00Z",
        "payload_t": payload, "payload_status": payload_status,
        "updated_at": payload_updated or "2026-01-10T09:00:00Z",
        "conflict_flags": flags or [], "energy_events": events,
    }


def event(event_id, *, amount=150, source="reported", created="2026-01-10T09:00:00Z",
          claims=None):
    return {"event_id": event_id, "station_id": "st1", "source_status": source,
            "amount": amount, "occurred_at": "2026-01-10T08:30:00Z",
            "created_at": created, "claims": claims if claims is not None else []}


def claim(cert_id="g1", *, kwh=150, status="accepted", claimant=("fleet", "fleet-1"),
          cert_status="active", valid_to="2026-12-31T23:59:59Z",
          gen_start="2026-01-01T00:00:00Z", gen_end="2026-01-31T23:59:59Z",
          total=100000):
    return {"claim_id": "cl-" + cert_id + "-" + event_unused(), "certificate_id": cert_id,
            "claimant_type": claimant[0], "claimant_id": claimant[1], "kwh": kwh,
            "status": status, "conflict_code": None,
            "certificate": {"certificate_id": cert_id, "claimant_type": claimant[0],
                            "claimant_id": claimant[1], "kwh_total": total,
                            "generation_start": gen_start, "generation_end": gen_end,
                            "valid_from": "2025-01-01T00:00:00Z", "valid_to": valid_to,
                            "status": cert_status}}


_counter = {"n": 0}


def event_unused():
    _counter["n"] += 1
    return str(_counter["n"])


class EngineTest(unittest.TestCase):
    def test_fully_green_trip_formula(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event("e1", claims=[claim("g1")])])]
        result = compute_batch(snap)
        # 载荷系数=1：基准 0.32*100*2.68/1000=0.08576；
        # 实际 150*0.01/1000=0.0015；减排 0.08426
        self.assertAlmostEqual(result.total_baseline_tco2, 0.08576, places=6)
        self.assertAlmostEqual(result.total_actual_tco2, 0.0015, places=6)
        self.assertAlmostEqual(result.total_avoided_tco2, 0.08426, places=6)
        self.assertEqual(result.trips_accepted, 1)
        self.assertEqual(result.trips_excluded, 0)
        self.assertEqual(result.contributions[0].certificate_ids, ("g1",))

    def test_missing_payload_excludes_trip_not_zero_emission(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", payload=None, payload_status="absent",
                              events=[event("e1", claims=[claim("g1")])])]
        result = compute_batch(snap)
        self.assertEqual(result.trips_accepted, 0)
        self.assertEqual(result.trips_excluded, 1)
        self.assertEqual(result.total_avoided_tco2, 0.0)
        self.assertIn("EVIDENCE_PAYLOAD_MISSING", result.evidence_codes)

    def test_energy_source_unknown_uses_grid_factor_not_zero(self):
        # 有充电记录但来源未补报：整条行程进入补证并排除，绝不按零排放。
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event("e1", source="unknown", claims=[])])]
        result = compute_batch(snap)
        self.assertEqual(result.trips_accepted, 0)
        self.assertIn("EVIDENCE_ENERGY_SOURCE_MISSING", result.evidence_codes)

    def test_charging_without_certificate_uses_grid_factor(self):
        # 来源已补报但没有绿证：可核算，电量全部按电网因子，不按零排放。
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event("e1", source="reported", claims=[])])]
        result = compute_batch(snap)
        self.assertEqual(result.trips_accepted, 1)
        contribution = result.contributions[0]
        self.assertAlmostEqual(contribution.actual_tco2, 150 * 0.58 / 1000, places=6)
        self.assertEqual(contribution.matched_green_kwh, 0.0)
        self.assertEqual(contribution.grid_kwh, 150.0)

    def test_conflict_flag_excludes_trip(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", flags=["payload"],
                              events=[event("e1", claims=[claim("g1")])])]
        result = compute_batch(snap)
        self.assertEqual(result.trips_accepted, 0)
        self.assertIn("EVIDENCE_PAYLOAD_CONFLICT", result.evidence_codes)

    def test_expired_evidence_excludes_trip(self):
        snap = make_snapshot()
        # 行程后 40 天才补传载荷，超过 30 天有效期。
        snap["trips"] = [trip("t1", payload_updated="2026-02-20T09:00:00Z",
                              events=[event("e1", created="2026-02-20T09:00:00Z",
                                            claims=[claim("g1")])])]
        result = compute_batch(snap)
        self.assertEqual(result.trips_accepted, 0)
        codes = set(result.evidence_codes)
        self.assertIn("EVIDENCE_PAYLOAD_EXPIRED", codes)
        self.assertIn("EVIDENCE_ENERGY_EXPIRED", codes)

    def test_segment_out_of_boundary_excluded(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", segment="seg-zzz")]
        result = compute_batch(snap)
        self.assertEqual(result.trips_excluded, 1)
        self.assertEqual(result.exclusions[0]["code"], "SEGMENT_OUT_OF_BOUNDARY")

    def test_partial_payload_factor(self):
        snap = make_snapshot()
        # 半载：载荷系数 0.75，基准与实际同比例缩放。
        snap["trips"] = [trip("t1", payload=15,
                              events=[event("e1", source="reported", claims=[])])]
        result = compute_batch(snap)
        self.assertAlmostEqual(result.contributions[0].payload_factor, 0.75, places=6)
        self.assertAlmostEqual(result.total_baseline_tco2, 0.08576 * 0.75, places=6)

    def test_withdrawn_certificate_falls_back_to_grid_and_raises_evidence(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1",
                              events=[event("e1", claims=[claim("g1", cert_status="withdrawn")])])]
        result = compute_batch(snap)
        self.assertEqual(result.trips_accepted, 1)
        self.assertEqual(result.contributions[0].matched_green_kwh, 0.0)
        self.assertAlmostEqual(result.contributions[0].actual_tco2, 150 * 0.58 / 1000, places=6)
        self.assertIn("CERTIFICATE_WITHDRAWN", result.evidence_codes)

    def test_generation_window_mismatch_raises_evidence(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event(
            "e1", claims=[claim("g1", gen_start="2026-03-01T00:00:00Z",
                                gen_end="2026-03-31T23:59:59Z")])])]
        result = compute_batch(snap)
        self.assertIn("CERTIFICATE_WINDOW_MISMATCH", result.evidence_codes)
        self.assertEqual(result.contributions[0].matched_green_kwh, 0.0)

    def test_rejected_claim_conflict_creates_evidence_and_grid_energy(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event("e1", claims=[
            claim("g1", status="rejected_conflict")])])]
        result = compute_batch(snap)
        self.assertIn("CERTIFICATE_CLAIM_CONFLICT", result.evidence_codes)
        self.assertEqual(result.contributions[0].matched_green_kwh, 0.0)

    def test_certificate_demand_caps_at_event_and_trip_energy(self):
        from transport_coordination.carbon_engine import certificate_demand
        snap = make_snapshot()
        # 申报 1000kWh，但事件只充 150kWh、行程总电耗也是 150kWh。
        snap["trips"] = [trip("t1", events=[event("e1", claims=[claim("g1", kwh=1000)])])]
        demand = certificate_demand(snap)
        self.assertEqual(demand, {"g1": 150.0})

    def test_certificate_demand_ignores_unsupported_claims(self):
        from transport_coordination.carbon_engine import certificate_demand
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event("e1", claims=[
            claim("g1"), claim("g2", status="rejected_conflict"),
            claim("g3", cert_status="withdrawn")])])]
        demand = certificate_demand(snap)
        self.assertEqual(demand, {"g1": 150.0})

    def test_deterministic_hash_for_identical_snapshot(self):
        snap = make_snapshot()
        snap["trips"] = [trip("t1", events=[event("e1", claims=[claim("g1")])])]
        r1 = compute_batch(copy.deepcopy(snap))
        r2 = compute_batch(copy.deepcopy(snap))
        self.assertEqual(digest(result_to_dict(r1)), digest(result_to_dict(r2)))
        self.assertEqual(digest(snap), digest(copy.deepcopy(snap)))


if __name__ == "__main__":
    unittest.main()
