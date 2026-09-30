"""碳核算纯规则测试：证据闸门、双侧声明匹配与排放计算。"""

import unittest

from corridor_carbon import rules


POLICY = {
    "name": "测试政策",
    "rules": {
        "trip_log": {"validity_days": 7, "required": True},
        "payload_record": {"validity_days": 30, "required": True},
        "energy_record": {"validity_days": 30, "required": True},
        "electricity_declaration": {"validity_days": 30, "required": True},
        "certificate": {"validity_days": None, "required": True},
    },
    "before_grace_days": 2,
}


def snapshot(grid_ef=0.5):
    return {
        "policy": {"rules": POLICY["rules"], "before_grace_days": 2},
        "segments": {"s1": {"segment_id": "s1", "version": 1, "length_km": 100.0, "boundary": []}},
        "energy_types": {
            "ev": {"energy_carrier": "electricity", "grid_ef_kg_per_kwh": grid_ef,
                   "fuel_intensity_l_per_km": None, "fuel_ef_kg_per_l": None},
            "diesel": {"energy_carrier": "diesel", "grid_ef_kg_per_kwh": None,
                       "fuel_intensity_l_per_km": 0.3, "fuel_ef_kg_per_l": 2.6},
        },
        "factors": {},
        "baseline": {"baseline_ef_kg_per_km": 1.2,
                     "load_correction": {"method": "payload_ratio",
                                         "reference_load_t": 40.0, "min_factor": 0.5}},
    }


def electric_trip(**overrides):
    trip = {
        "trip_id": "t1", "vehicle_id": "v1", "energy_type_id": "ev",
        "started_at": "2026-09-10T00:00:00Z", "completed_at": "2026-09-10T04:00:00Z",
        "logged_at": "2026-09-10T04:05:00Z",
        "segment_legs": [{"segment_id": "s1", "distance_km": 100.0}],
        "payload": {"load_t": 40.0, "recorded_at": "2026-09-10T06:00:00Z"},
        "energy_records": [
            {"record_id": "e1", "station_id": "st1", "energy_kwh": 140.0,
             "occurred_at": "2026-09-10T02:00:00Z", "certificate_id": "c1"}
        ],
        "declarations": [
            {"certificate_id": "c1", "fleet_id": "f1", "energy_kwh": 140.0,
             "declared_at": "2026-09-10T05:00:00Z"}
        ],
    }
    trip.update(overrides)
    return trip


CERT = {"c1": {"certificate_id": "c1", "batch_no": "b", "energy_kwh": 500.0,
               "valid_from": "2026-09-01", "valid_to": "2026-09-30", "status": "available"}}


class RulesTest(unittest.TestCase):
    def test_complete_electric_trip_counts_reduction(self):
        result = rules.evaluate_trip(electric_trip(), snapshot(), CERT)
        self.assertEqual("valid", result["verification_status"])
        # 基准 100*1.2*1.0=120kg；绿证全额匹配，实际排放 0；减排 120kg
        self.assertEqual(120.0, result["baseline_emissions_kg"])
        self.assertEqual(0.0, result["actual_emissions_kg"])
        self.assertEqual(120.0, result["reductions_kg"])
        self.assertEqual({"c1": 140.0}, result["matched_kwh_by_cert"])

    def test_quantity_mismatch_between_fleet_and_station_is_conflict(self):
        trip = electric_trip(declarations=[
            {"certificate_id": "c1", "fleet_id": "f1", "energy_kwh": 100.0,
             "declared_at": "2026-09-10T05:00:00Z"}
        ])
        result = rules.evaluate_trip(trip, snapshot(), CERT)
        # 双侧量不一致 -> 冲突，不匹配任何绿电
        self.assertEqual("conflict", result["claim_status"])
        self.assertEqual("pending_evidence", result["verification_status"])
        self.assertIsNone(result["actual_emissions_kg"])

    def test_partial_green_match_grid_portion_uses_grid_factor(self):
        # 站点 140kWh 中 100kWh 有双侧匹配绿证，40kWh 为普通并网电量
        records = [
            {"record_id": "e1", "station_id": "st1", "energy_kwh": 100.0,
             "occurred_at": "2026-09-10T02:00:00Z", "certificate_id": "c1"},
            {"record_id": "e2", "station_id": "st1", "energy_kwh": 40.0,
             "occurred_at": "2026-09-10T02:30:00Z"},
        ]
        trip = electric_trip(
            energy_records=records,
            declarations=[{"certificate_id": "c1", "fleet_id": "f1", "energy_kwh": 100.0,
                           "declared_at": "2026-09-10T05:00:00Z"}],
        )
        result = rules.evaluate_trip(trip, snapshot(), CERT)
        self.assertEqual("valid", result["verification_status"])
        self.assertEqual(40.0, result["calc_detail"]["grid_kwh"])
        self.assertEqual(20.0, result["actual_emissions_kg"])  # 40*0.5
        self.assertEqual(100.0, result["reductions_kg"])       # 120-20

    def test_missing_payload_becomes_pending_not_zero(self):
        trip = electric_trip()
        del trip["payload"]
        result = rules.evaluate_trip(trip, snapshot(), CERT)
        self.assertEqual("pending_evidence", result["verification_status"])
        self.assertIn("missing_payload", result["pending_reasons"])
        self.assertEqual(0.0, result["reductions_kg"])
        self.assertIsNone(result["actual_emissions_kg"])

    def test_missing_energy_records_becomes_pending(self):
        trip = electric_trip(energy_records=[])
        result = rules.evaluate_trip(trip, snapshot(), CERT)
        self.assertEqual("pending", result["energy_status"])
        self.assertIn("missing_energy_records", result["pending_reasons"])

    def test_fleet_only_declaration_is_conflict(self):
        # 站点补能记录无 certificate_id，只有车队侧声明
        records = [{"record_id": "e1", "station_id": "st1", "energy_kwh": 140.0,
                    "occurred_at": "2026-09-10T02:00:00Z"}]
        result = rules.evaluate_trip(electric_trip(energy_records=records), snapshot(), CERT)
        self.assertIn("conflict", result["claim_status"])
        self.assertIn("station_record_missing:c1", result["pending_reasons"])

    def test_station_only_record_is_missing_claim(self):
        result = rules.evaluate_trip(electric_trip(declarations=[]), snapshot(), CERT)
        self.assertEqual("missing", result["claim_status"])
        self.assertIn("fleet_declaration_missing:c1", result["pending_reasons"])
        self.assertEqual("pending_evidence", result["verification_status"])

    def test_withdrawn_certificate_blocks_counting(self):
        certs = {"c1": {**CERT["c1"], "status": "withdrawn"}}
        result = rules.evaluate_trip(electric_trip(), snapshot(), certs)
        self.assertEqual("conflict", result["claim_status"])
        self.assertIn("certificate_withdrawn:c1", result["pending_reasons"])

    def test_certificate_outside_validity_window(self):
        certs = {"c1": {**CERT["c1"], "valid_from": "2026-09-20", "valid_to": "2026-10-20"}}
        result = rules.evaluate_trip(electric_trip(), snapshot(), certs)
        self.assertIn("certificate_out_of_validity:c1", result["pending_reasons"])

    def test_expired_payload_record_pends(self):
        # 行程结束 9-10，载货记录 11-20，超过 30 天有效期
        trip = electric_trip(payload={"load_t": 40.0, "recorded_at": "2026-11-20T06:00:00Z"})
        result = rules.evaluate_trip(trip, snapshot(), CERT)
        self.assertIn("payload_record_expired", result["pending_reasons"])
        self.assertEqual("pending_evidence", result["verification_status"])

    def test_duplicate_energy_record_with_conflicting_value(self):
        records = [
            {"record_id": "e1", "station_id": "st1", "energy_kwh": 140.0,
             "occurred_at": "2026-09-10T02:00:00Z", "certificate_id": "c1"},
            {"record_id": "e1", "station_id": "st1", "energy_kwh": 999.0,
             "occurred_at": "2026-09-10T02:00:00Z", "certificate_id": "c1"},
        ]
        result = rules.evaluate_trip(electric_trip(energy_records=records), snapshot(), CERT)
        self.assertEqual("conflict", result["energy_status"])
        self.assertTrue(any(r.startswith("energy_record_conflict") for r in result["pending_reasons"]))

    def test_diesel_trip_uses_fuel_factor(self):
        trip = {"trip_id": "t2", "vehicle_id": "v2", "energy_type_id": "diesel",
                "started_at": "2026-09-10T00:00:00Z", "completed_at": "2026-09-10T02:00:00Z",
                "logged_at": "2026-09-10T02:05:00Z",
                "segment_legs": [{"segment_id": "s1", "distance_km": 100.0}],
                "payload": {"load_t": 40.0, "recorded_at": "2026-09-10T03:00:00Z"},
                "energy_records": [], "declarations": []}
        result = rules.evaluate_trip(trip, snapshot(), {})
        self.assertEqual("valid", result["verification_status"])
        # 实际 100*0.3*2.6=78kg，基准 120kg，减排 42kg
        self.assertEqual(78.0, result["actual_emissions_kg"])
        self.assertEqual(42.0, result["reductions_kg"])

    def test_segment_not_in_boundary_pends(self):
        snap = snapshot()
        del snap["segments"]["s1"]
        result = rules.evaluate_trip(electric_trip(), snap, CERT)
        self.assertIn("segment_not_in_boundary:s1", result["pending_reasons"])

    def test_summary_separates_valid_and_pending(self):
        good = rules.evaluate_trip(electric_trip(), snapshot(), CERT)
        bad = rules.evaluate_trip(electric_trip(trip_id="t2", payload=None), snapshot(), CERT)
        summary = rules.summarize([good, bad])
        self.assertEqual(2, summary["trip_count"])
        self.assertEqual(1, summary["valid_trip_count"])
        self.assertEqual(1, summary["pending_trip_count"])
        self.assertEqual(0.12, summary["reductions_tco2"])


if __name__ == "__main__":
    unittest.main()
