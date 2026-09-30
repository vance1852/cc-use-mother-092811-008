"""碳核算 HTTP 路由测试。"""

import unittest

from corridor_carbon.api import route
from corridor_carbon.service import CarbonService
from corridor_carbon.storage import CarbonDatabase
from transport_coordination.api import route as base_route
from transport_coordination.service import DomainService


class CarbonApiTest(unittest.TestCase):
    def setUp(self):
        self.database = CarbonDatabase()
        self.service = CarbonService(self.database)
        self.base = DomainService(self.database)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        actor = {"X-Actor-Id": "bootstrap"}
        base_route(self.base, "POST", "/organizations",
                   {"request_id": "org", "organization_id": "o1", "name": "机构"}, actor)
        base_route(self.base, "POST", "/actors",
                   {"request_id": "adm", "new_actor_id": "a1", "display_name": "管理员",
                    "role": "admin", "organization_id": "o1"}, actor)
        base_route(self.base, "POST", "/actors",
                   {"request_id": "opr", "new_actor_id": "op1", "display_name": "核算员",
                    "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        base_route(self.base, "POST", "/actors",
                   {"request_id": "rev", "new_actor_id": "rv1", "display_name": "核验人",
                    "role": "reviewer", "organization_id": "o1"}, {"X-Actor-Id": "a1"})

    def _master(self):
        op = {"X-Actor-Id": "op1"}
        route(self.service, "POST", "/segments",
              {"request_id": "seg", "segment_id": "s1", "corridor_id": "c1", "name": "北段",
               "length_km": 100.0, "boundary": [{"p": 1}]}, op)
        route(self.service, "POST", "/energy-types",
              {"request_id": "ent", "energy_type_id": "ev", "code": "EV", "name": "电动重卡",
               "energy_carrier": "electricity", "grid_ef_kg_per_kwh": 0.5}, op)
        route(self.service, "POST", "/baselines",
              {"request_id": "bln", "baseline_id": "b1", "corridor_id": "c1", "name": "柴油基准",
               "baseline_ef_kg_per_km": 1.2}, op)
        route(self.service, "POST", "/evidence-policies",
              {"request_id": "pol", "policy_id": "p1", "name": "政策"}, op)
        route(self.service, "POST", "/targets",
              {"request_id": "tgt", "target_id": "g1", "corridor_id": "c1",
               "period_start": "2026-09-01", "period_end": "2026-09-30",
               "target_tco2": 0.5}, op)
        route(self.service, "POST", "/certificates",
              {"request_id": "crt", "certificate_id": "c1", "batch_no": "B", "energy_kwh": 1000.0,
               "valid_from": "2026-09-01", "valid_to": "2026-09-30"}, op)

    def _trip(self):
        return {"trip_id": "t1", "vehicle_id": "v1", "energy_type_id": "ev",
                "started_at": "2026-09-10T00:00:00Z", "completed_at": "2026-09-10T04:00:00Z",
                "logged_at": "2026-09-10T04:05:00Z",
                "segment_legs": [{"segment_id": "s1", "distance_km": 100.0}],
                "payload": {"load_t": 40.0, "recorded_at": "2026-09-10T06:00:00Z"},
                "energy_records": [
                    {"record_id": "e1", "station_id": "st1", "energy_kwh": 140.0,
                     "occurred_at": "2026-09-10T02:00:00Z", "certificate_id": "c1"}],
                "declarations": [
                    {"certificate_id": "c1", "fleet_id": "f1", "energy_kwh": 140.0,
                     "declared_at": "2026-09-10T05:00:00Z"}]}

    def test_full_publish_flow_over_http(self):
        self._master()
        status, frozen = route(self.service, "POST", "/batches/freeze", {
            "request_id": "frz", "corridor_id": "c1", "period_start": "2026-09-01",
            "period_end": "2026-09-30", "trips": [self._trip()]}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        batch_id = frozen["batch_id"]

        # 无 actor 的健康检查
        status, health = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertTrue(health["audit_valid"])

        # 冻结人核验被拒
        status, payload = route(self.service, "POST", f"/batches/{batch_id}/verify",
                                {"request_id": "vself"}, {"X-Actor-Id": "op1"})
        self.assertEqual(403, status)

        # 独立核验人通过并发布
        status, verified = route(self.service, "POST", f"/batches/{batch_id}/verify",
                                 {"request_id": "ver"}, {"X-Actor-Id": "rv1"})
        self.assertEqual(200, status)
        self.assertEqual("verified", verified["status"])
        status, published = route(self.service, "POST", f"/batches/{batch_id}/publish",
                                  {"request_id": "pub"}, {"X-Actor-Id": "rv1"})
        self.assertEqual(200, status)
        self.assertEqual("initial", published["kind"])

        # 减排解释
        status, explain = route(self.service, "GET", f"/batches/{batch_id}/explain", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(explain["valid_trips"]))
        self.assertEqual(0.12, explain["total_reductions_tco2"])

        # 走廊目标进度
        status, progress = route(
            self.service, "GET",
            "/corridors/c1/progress?period_start=2026-09-01&period_end=2026-09-30", None)
        self.assertEqual(200, status)
        self.assertEqual(0.12, progress["achieved_reductions_tco2"])
        self.assertEqual(0.24, progress["completion_rate"])

        # 历史报告列表
        status, reports = route(self.service, "GET", "/corridors/c1/reports", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(reports["items"]))

    def test_publish_before_verify_is_conflict(self):
        self._master()
        _, frozen = route(self.service, "POST", "/batches/freeze", {
            "request_id": "frz2", "corridor_id": "c1", "period_start": "2026-09-01",
            "period_end": "2026-09-30", "trips": [self._trip()]}, {"X-Actor-Id": "op1"})
        status, payload = route(self.service, "POST",
                                f"/batches/{frozen['batch_id']}/publish",
                                {"request_id": "pub2"}, {"X-Actor-Id": "rv1"})
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_unknown_route_404_and_bad_request_400(self):
        status, payload = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        status, payload = route(self.service, "POST", "/segments",
                                {"request_id": "x"}, {"X-Actor-Id": "op1"})
        self.assertEqual(400, status)

    def test_recompute_endpoint(self):
        self._master()
        _, frozen = route(self.service, "POST", "/batches/freeze", {
            "request_id": "frz2", "corridor_id": "c1", "period_start": "2026-09-01",
            "period_end": "2026-09-30", "trips": [self._trip()]}, {"X-Actor-Id": "op1"})
        status, check = route(self.service, "GET",
                              f"/batches/{frozen['batch_id']}/recompute", None)
        self.assertEqual(200, status)
        self.assertTrue(check["hash_matches"])


if __name__ == "__main__":
    unittest.main()
