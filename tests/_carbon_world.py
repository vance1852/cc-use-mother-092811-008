"""为碳核算测试构建一个完整的走廊世界。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from transport_coordination.carbon_service import CarbonService
from transport_coordination.clock import FixedClock
from transport_coordination.storage import Database

EPOCH = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class World:
    """封装组织、角色和标准配置的登记过程。"""

    def __init__(self, clock: datetime | None = None):
        self.database = Database()
        self.clock = FixedClock(clock or EPOCH)
        self.service = CarbonService(self.database, self.clock)
        self._seq = 0
        self._bootstrap()
        self._publish_config()

    def _rid(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def advance(self, days: float = 0, hours: float = 0) -> None:
        value = self.clock.now() + timedelta(days=days, hours=hours)
        self.clock = FixedClock(value)
        self.service.clock = self.clock

    def _bootstrap(self) -> None:
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="走廊运营机构")
        actors = [
            ("admin", "admin-1", "管理员", "admin"),
            ("op", "op-1", "运营员", "operator"),
            ("rv", "rv-1", "核验员", "reviewer"),
            ("au", "au-1", "发布审计员", "auditor"),
        ]
        for index, (rid, aid, name, role) in enumerate(actors):
            s.register_actor(request_id=rid,
                             actor_id="bootstrap" if index == 0 else "admin-1",
                             new_actor_id=aid, display_name=name, role=role,
                             organization_id="o1")

    def _publish_config(self) -> None:
        s = self.service
        s.register_corridor(request_id="corridor", actor_id="admin-1",
                            corridor_id="c1", name="沪杭零碳走廊")
        s.publish_boundary(request_id="boundary", actor_id="admin-1", corridor_id="c1",
                           segments=[{"segment_id": "seg-a", "name": "A-B", "origin": "A",
                                      "destination": "B", "distance_km": 100},
                                     {"segment_id": "seg-b", "name": "B-C", "origin": "B",
                                      "destination": "C", "distance_km": 200}],
                           change_note="首版边界")
        s.publish_target(request_id="target", actor_id="admin-1", corridor_id="c1",
                         period_start="2026-01-01T00:00:00Z",
                         period_end="2026-03-31T23:59:59Z",
                         target_reduction_t=10.0, methodology="柴油基准-电动实际")
        s.publish_vehicle_type(request_id="vt", actor_id="admin-1", vehicle_type_id="evt",
                               energy_type="electric", consumption_rate=1.5,
                               rated_payload_t=30.0, change_note="新能源重卡")
        s.publish_factor(request_id="f-diesel", actor_id="admin-1",
                         factor_key="diesel_lcv_per_liter", value=2.68, unit="kgCO2e/L",
                         source="省级温室气体指南", valid_from="2025-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z", change_note="柴油因子v1")
        s.publish_factor(request_id="f-grid", actor_id="admin-1",
                         factor_key="electricity_grid", value=0.58, unit="kgCO2e/kWh",
                         source="区域电网公告", valid_from="2025-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z", change_note="电网因子v1")
        s.publish_factor(request_id="f-renew", actor_id="admin-1",
                         factor_key="electricity_renewable", value=0.01,
                         unit="kgCO2e/kWh", source="绿电核算口径",
                         valid_from="2025-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z", change_note="可再生因子v1")
        s.publish_baseline(request_id="bl", actor_id="admin-1", baseline_id="bl1",
                           corridor_id="c1", vehicle_class="重型柴油货车",
                           fuel_intensity_l_per_km=0.32, factor_key="diesel_lcv_per_liter",
                           change_note="基准方案v1")
        s.publish_evidence_policy(request_id="ep-payload", actor_id="admin-1",
                                  evidence_type="payload_report", validity_days=30,
                                  required=True, change_note="载荷30日")
        s.publish_evidence_policy(request_id="ep-energy", actor_id="admin-1",
                                  evidence_type="energy_source", validity_days=30,
                                  required=True, change_note="来源30日")

    # -- 便捷构造 ----------------------------------------------------------

    def add_trip(self, trip_id: str, *, segment: str = "seg-a", distance: float = 100.0,
                 when: datetime | None = None, actor: str = "op-1") -> None:
        when = when or datetime(2026, 1, 10, 8, tzinfo=timezone.utc)
        self.service.record_trip(actor_id=actor, trip_id=trip_id, corridor_id="c1",
                                 segment_id=segment, vehicle_id="truck-1",
                                 vehicle_type_id="evt", distance_km=distance,
                                 occurred_at=iso(when))

    def add_payload(self, trip_id: str, payload_t: float = 30.0, actor: str = "op-1"):
        return self.service.report_trip_payload(actor_id=actor, trip_id=trip_id,
                                                payload_t=payload_t)

    def add_energy(self, event_id: str, trip_id: str, *, amount: float = 150.0,
                   source_status: str = "reported", actor: str = "op-1",
                   occurred_at: str = "2026-01-10T08:30:00Z"):
        return self.service.record_energy_event(actor_id=actor, event_id=event_id,
                                                trip_id=trip_id, amount=amount,
                                                station_id="st-1",
                                                source_status=source_status,
                                                occurred_at=occurred_at)

    def add_certificate(self, cert_id: str = "g1", *, kwh_total: float = 100000.0,
                        claimant_type: str = "fleet", claimant_id: str = "fleet-1",
                        request_id: str | None = None, actor: str = "op-1",
                        generation_start: str = "2026-01-01T00:00:00Z",
                        generation_end: str = "2026-01-31T23:59:59Z"):
        return self.service.register_certificate(
            request_id=request_id or f"cert-{cert_id}", actor_id=actor,
            certificate_id=cert_id, claimant_type=claimant_type, claimant_id=claimant_id,
            kwh_total=kwh_total,
            generation_start=generation_start,
            generation_end=generation_end,
            valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    def add_claim(self, claim_id: str, *, cert_id: str = "g1", event_id: str,
                  kwh: float = 150.0, claimant_type: str = "fleet",
                  claimant_id: str = "fleet-1", actor: str = "op-1"):
        return self.service.file_claim(actor_id=actor, claim_id=claim_id,
                                       certificate_id=cert_id, energy_event_id=event_id,
                                       claimant_type=claimant_type, claimant_id=claimant_id,
                                       kwh=kwh)

    def add_green_trip(self, trip_id: str, *, cert_id: str = "g1", payload: float = 30.0,
                       amount: float = 150.0,
                       when: datetime | None = None,
                       energy_at: str | None = None):
        exists = self.service.database.connection.execute(
            "SELECT 1 FROM carbon_certificates WHERE certificate_id=?", (cert_id,)
        ).fetchone()
        if exists is None:
            self.add_certificate(cert_id, request_id="cert-" + cert_id)
        self.add_trip(trip_id, when=when)
        self.add_payload(trip_id, payload)
        if energy_at is None and when is not None:
            energy_at = iso(when.replace(minute=30))
        self.add_energy("e-" + trip_id, trip_id, amount=amount,
                        occurred_at=energy_at or "2026-01-10T08:30:00Z")
        return self.add_claim("cl-" + trip_id, cert_id=cert_id, event_id="e-" + trip_id,
                              kwh=amount)

    def make_batch(self, batch_id: str = "b1", *, baseline_id: str = "bl1",
                   actor: str = "op-1"):
        self.service.create_batch(request_id="batch-" + batch_id, actor_id=actor,
                                  batch_id=batch_id, corridor_id="c1",
                                  period_start="2026-01-01T00:00:00Z",
                                  period_end="2026-01-31T23:59:59Z",
                                  baseline_id=baseline_id)

    def close(self) -> None:
        self.database.close()
