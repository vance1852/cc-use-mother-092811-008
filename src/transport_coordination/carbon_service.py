"""实现走廊碳核算与独立核验的应用服务。

角色分工：

- admin：发布路段边界、目标、车型、因子、基准方案、证据有效期等配置版本；
- operator：登记行程、载荷、补能、凭证与申报，创建并冻结核算批次；
- reviewer：独立核验（不得由冻结人本人执行），批准或驳回；
- auditor：发布已批准批次（不得由核验人本人执行）、撤回凭证。

所有写操作复用 request_id 幂等和哈希审计链；批次一旦冻结，输入即拍成快照，
后续任何迟到数据、因子修订或凭证撤回都只能产生重述或吊销新版本。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .carbon_engine import (
    FORMULA_VERSION,
    GRID_FACTOR_KEY,
    NON_BLOCKING_CODES,
    RENEWABLE_FACTOR_KEY,
    certificate_demand,
    compute_batch,
    parse_ts,
    result_to_dict,
)
from .carbon_models import BatchRecord, TargetProgress
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

ENERGY_TYPES = frozenset({"electric"})
EVIDENCE_TYPES = frozenset({"payload_report", "energy_source"})
CLAIMANT_TYPES = frozenset({"fleet", "station"})


class CarbonService(DomainService):
    """在基础服务之上提供碳核算配置、输入、批次与核验能力。"""

    # -- 配置版本化 --------------------------------------------------------

    def register_corridor(self, *, request_id: str, actor_id: str,
                          corridor_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "corridor_id": corridor_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            corridor_id = self._identifier(corridor_id, "corridor_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO carbon_corridors(corridor_id,name,created_by,created_at) "
                        "VALUES(?,?,?,?)",
                        (corridor_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("走廊编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="carbon.corridor.registered",
                             resource_type="carbon_corridor", resource_id=corridor_id,
                             detail={"name": name}, occurred_at=self._now())
                return "carbon_corridor", corridor_id, {"corridor_id": corridor_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_register_corridor", payload=payload, create=create)

    def publish_boundary(self, *, request_id: str, actor_id: str, corridor_id: str,
                         segments: list[dict[str, Any]], change_note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "corridor_id": corridor_id,
                   "segments": segments, "change_note": change_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            self._require_corridor(conn, corridor_id)
            clean_segments = self._clean_segments(segments)
            change_note = self._text(change_note, "change_note")
            version = self._next_version(conn, "carbon_boundary_versions",
                                         ("corridor_id", corridor_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO carbon_boundary_versions(corridor_id,version,segments_json,"
                    "payload_hash,change_note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (corridor_id, version, canonical_json(clean_segments), digest(clean_segments),
                     change_note, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.boundary.published",
                             resource_type="carbon_boundary", resource_id=corridor_id,
                             detail={"version": version, "segments": len(clean_segments),
                                     "payload_hash": digest(clean_segments)},
                             occurred_at=self._now())
                return "carbon_boundary", f"{corridor_id}:v{version}", \
                    {"corridor_id": corridor_id, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_publish_boundary", payload=payload, create=create)

    def publish_target(self, *, request_id: str, actor_id: str, corridor_id: str,
                       period_start: str, period_end: str, target_reduction_t: float,
                       methodology: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "corridor_id": corridor_id, "period_start": period_start,
                   "period_end": period_end, "target_reduction_t": target_reduction_t,
                   "methodology": methodology}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            self._require_corridor(conn, corridor_id)
            period_start, period_end = self._period(period_start, period_end)
            target_reduction_t = self._non_negative(target_reduction_t, "target_reduction_t")
            methodology = self._text(methodology, "methodology")
            version = self._next_version(conn, "carbon_target_versions",
                                         ("corridor_id", corridor_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO carbon_target_versions(corridor_id,version,period_start,period_end,"
                    "target_reduction_t,methodology,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (corridor_id, version, period_start, period_end, target_reduction_t,
                     methodology, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.target.published",
                             resource_type="carbon_target", resource_id=corridor_id,
                             detail={"version": version, "target_reduction_t": target_reduction_t,
                                     "period_start": period_start, "period_end": period_end},
                             occurred_at=self._now())
                return "carbon_target", f"{corridor_id}:v{version}", \
                    {"corridor_id": corridor_id, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_publish_target", payload=payload, create=create)

    def publish_vehicle_type(self, *, request_id: str, actor_id: str, vehicle_type_id: str,
                             energy_type: str, consumption_rate: float,
                             rated_payload_t: float, change_note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "vehicle_type_id": vehicle_type_id,
                   "energy_type": energy_type, "consumption_rate": consumption_rate,
                   "rated_payload_t": rated_payload_t, "change_note": change_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            vehicle_type_id = self._identifier(vehicle_type_id, "vehicle_type_id")
            if energy_type not in ENERGY_TYPES:
                raise ValidationError("当前核算口径只支持 electric 能源类型")
            consumption_rate = self._positive(consumption_rate, "consumption_rate")
            rated_payload_t = self._positive(rated_payload_t, "rated_payload_t")
            change_note = self._text(change_note, "change_note")
            version = self._next_version(conn, "vehicle_type_versions",
                                         ("vehicle_type_id", vehicle_type_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO vehicle_type_versions(vehicle_type_id,version,energy_type,"
                    "consumption_rate,rated_payload_t,change_note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (vehicle_type_id, version, energy_type, consumption_rate, rated_payload_t,
                     change_note, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.vehicle_type.published",
                             resource_type="vehicle_type", resource_id=vehicle_type_id,
                             detail={"version": version, "energy_type": energy_type},
                             occurred_at=self._now())
                return "vehicle_type", f"{vehicle_type_id}:v{version}", \
                    {"vehicle_type_id": vehicle_type_id, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_publish_vehicle_type", payload=payload,
                                    create=create)

    def publish_factor(self, *, request_id: str, actor_id: str, factor_key: str, value: float,
                       unit: str, source: str, valid_from: str, valid_to: str,
                       change_note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "factor_key": factor_key, "value": value, "unit": unit,
                   "source": source, "valid_from": valid_from, "valid_to": valid_to,
                   "change_note": change_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            factor_key = self._identifier(factor_key, "factor_key")
            value = self._non_negative(value, "value")
            unit = self._text(unit, "unit", 60)
            source = self._text(source, "source")
            valid_from, valid_to = self._period(valid_from, valid_to)
            change_note = self._text(change_note, "change_note")
            version = self._next_version(conn, "emission_factor_versions",
                                         ("factor_key", factor_key))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO emission_factor_versions(factor_key,version,value,unit,source,"
                    "valid_from,valid_to,change_note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (factor_key, version, value, unit, source, valid_from, valid_to,
                     change_note, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.factor.published",
                             resource_type="emission_factor", resource_id=factor_key,
                             detail={"version": version, "value": value, "unit": unit,
                                     "valid_from": valid_from, "valid_to": valid_to},
                             occurred_at=self._now())
                return "emission_factor", f"{factor_key}:v{version}", \
                    {"factor_key": factor_key, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_publish_factor", payload=payload, create=create)

    def publish_baseline(self, *, request_id: str, actor_id: str, baseline_id: str,
                         corridor_id: str, vehicle_class: str, fuel_intensity_l_per_km: float,
                         factor_key: str, change_note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "baseline_id": baseline_id, "corridor_id": corridor_id,
                   "vehicle_class": vehicle_class,
                   "fuel_intensity_l_per_km": fuel_intensity_l_per_km, "factor_key": factor_key,
                   "change_note": change_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            self._require_corridor(conn, corridor_id)
            baseline_id = self._identifier(baseline_id, "baseline_id")
            vehicle_class = self._text(vehicle_class, "vehicle_class", 80)
            fuel_intensity_l_per_km = self._positive(fuel_intensity_l_per_km,
                                                    "fuel_intensity_l_per_km")
            factor_key = self._identifier(factor_key, "factor_key")
            if self._latest_version_row(conn, "emission_factor_versions",
                                        ("factor_key", factor_key)) is None:
                raise ValidationError("基准方案引用的排放因子不存在")
            change_note = self._text(change_note, "change_note")
            version = self._next_version(conn, "baseline_versions",
                                         ("baseline_id", baseline_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO baseline_versions(baseline_id,version,corridor_id,vehicle_class,"
                    "fuel_intensity_l_per_km,factor_key,change_note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (baseline_id, version, corridor_id, vehicle_class, fuel_intensity_l_per_km,
                     factor_key, change_note, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.baseline.published",
                             resource_type="baseline", resource_id=baseline_id,
                             detail={"version": version, "corridor_id": corridor_id,
                                     "factor_key": factor_key}, occurred_at=self._now())
                return "baseline", f"{baseline_id}:v{version}", \
                    {"baseline_id": baseline_id, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_publish_baseline", payload=payload, create=create)

    def publish_evidence_policy(self, *, request_id: str, actor_id: str, evidence_type: str,
                                validity_days: int, required: bool,
                                change_note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "evidence_type": evidence_type,
                   "validity_days": validity_days, "required": required,
                   "change_note": change_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            if evidence_type not in EVIDENCE_TYPES:
                raise ValidationError("evidence_type 不在允许范围内")
            if not isinstance(validity_days, int) or validity_days < 0:
                raise ValidationError("validity_days 必须是非负整数")
            if not isinstance(required, bool):
                raise ValidationError("required 必须是布尔值")
            change_note = self._text(change_note, "change_note")
            version = self._next_version(conn, "evidence_policy_versions",
                                         ("evidence_type", evidence_type))

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO evidence_policy_versions(evidence_type,version,validity_days,"
                    "required,change_note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (evidence_type, version, validity_days, 1 if required else 0,
                     change_note, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.evidence_policy.published",
                             resource_type="evidence_policy", resource_id=evidence_type,
                             detail={"version": version, "validity_days": validity_days,
                                     "required": required}, occurred_at=self._now())
                return "evidence_policy", f"{evidence_type}:v{version}", \
                    {"evidence_type": evidence_type, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_publish_evidence_policy", payload=payload,
                                    create=create)

    # -- 运营输入：行程、载荷、补能 ----------------------------------------

    def record_trip(self, *, actor_id: str, trip_id: str, corridor_id: str, segment_id: str,
                    vehicle_id: str, vehicle_type_id: str, distance_km: float,
                    occurred_at: str, conflict_flags: list[str] | None = None) -> dict[str, Any]:
        """登记或更新尚未被批次锁定的行程；载荷允许行程结束后补传。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._require_corridor(conn, corridor_id)
            trip_id = self._identifier(trip_id, "trip_id")
            segment_id = self._identifier(segment_id, "segment_id")
            vehicle_id = self._identifier(vehicle_id, "vehicle_id")
            vehicle = self._latest_version_row(conn, "vehicle_type_versions",
                                               ("vehicle_type_id", vehicle_type_id))
            if vehicle is None:
                raise ValidationError("车型不存在")
            distance_km = self._positive(distance_km, "distance_km")
            occurred_at = self._instant(occurred_at, "occurred_at")
            flags = sorted({self._text(item, "conflict_flag", 60)
                            for item in (conflict_flags or [])})
            now = self._now()
            existing = conn.execute("SELECT * FROM carbon_trips WHERE trip_id=?",
                                    (trip_id,)).fetchone()
            if existing:
                lock = self._trip_lock(conn, trip_id)
                if lock is not None and lock["state"] == "held":
                    raise ConflictError("行程处于批次核验中，不能修改；请等待驳回后补录")
                if lock is not None:
                    raise ConflictError(
                        "行程已纳入核算批次，主数据不可修改；迟到的载荷与补能请走补传接口，"
                        "或对已发布批次发起重述")
                conn.execute(
                    "UPDATE carbon_trips SET corridor_id=?,segment_id=?,vehicle_id=?,"
                    "vehicle_type_id=?,distance_km=?,occurred_at=?,conflict_flags_json=?,"
                    "updated_at=? WHERE trip_id=?",
                    (corridor_id, segment_id, vehicle_id, vehicle_type_id, distance_km,
                     occurred_at, canonical_json(flags), now, trip_id),
                )
                action = "carbon.trip.updated"
                created = False
            else:
                conn.execute(
                    "INSERT INTO carbon_trips(trip_id,corridor_id,segment_id,vehicle_id,"
                    "vehicle_type_id,distance_km,occurred_at,payload_t,payload_status,"
                    "conflict_flags_json,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,NULL,'absent',?,?,?,?)",
                    (trip_id, corridor_id, segment_id, vehicle_id, vehicle_type_id, distance_km,
                     occurred_at, canonical_json(flags), actor_id, now, now),
                )
                action = "carbon.trip.recorded"
                created = True
            append_event(conn, actor_id=actor_id, action=action,
                         resource_type="carbon_trip", resource_id=trip_id,
                         detail={"corridor_id": corridor_id, "segment_id": segment_id,
                                 "distance_km": distance_km, "occurred_at": occurred_at,
                                 "conflict_flags": flags}, occurred_at=now)
            return {"trip_id": trip_id, "created": created, "payload_status": "absent" if created
                    else existing["payload_status"]}

    def report_trip_payload(self, *, actor_id: str, trip_id: str, payload_t: float) -> dict[str, Any]:
        """补传行程载货量（迟到数据）；锁定后需先驳回或对已发布批次重述。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            trip = self._require_trip(conn, trip_id)
            lock = self._trip_lock(conn, trip_id)
            if lock is not None and lock["state"] == "held":
                raise ConflictError("行程处于批次核验中；驳回后可补录，已发布批次则触发重述")
            payload_t = self._non_negative(payload_t, "payload_t")
            now = self._now()
            conn.execute(
                "UPDATE carbon_trips SET payload_t=?,payload_status='reported',updated_at=? "
                "WHERE trip_id=?",
                (payload_t, now, trip_id),
            )
            append_event(conn, actor_id=actor_id, action="carbon.payload.reported",
                         resource_type="carbon_trip", resource_id=trip_id,
                         detail={"payload_t": payload_t,
                                 "previously_reported": trip["payload_status"] == "reported"},
                         occurred_at=now)
            return {"trip_id": trip_id, "payload_t": payload_t, "payload_status": "reported"}

    def flag_trip_conflict(self, *, actor_id: str, trip_id: str, flag: str) -> dict[str, Any]:
        """登记证据冲突标记（payload/energy_source），冲突证据不按零排放处理。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._require_trip(conn, trip_id)
            flag = self._text(flag, "flag", 60)
            row = conn.execute("SELECT conflict_flags_json FROM carbon_trips WHERE trip_id=?",
                               (trip_id,)).fetchone()
            flags = set(json.loads(row["conflict_flags_json"]))
            flags.add(flag)
            flags_sorted = sorted(flags)
            conn.execute("UPDATE carbon_trips SET conflict_flags_json=? WHERE trip_id=?",
                         (canonical_json(flags_sorted), trip_id))
            append_event(conn, actor_id=actor_id, action="carbon.trip.conflict_flagged",
                         resource_type="carbon_trip", resource_id=trip_id,
                         detail={"flag": flag}, occurred_at=self._now())
            return {"trip_id": trip_id, "conflict_flags": flags_sorted}

    def record_energy_event(self, *, actor_id: str, event_id: str, trip_id: str, amount: float,
                            station_id: str | None = None, source_status: str = "unknown",
                            occurred_at: str | None = None) -> dict[str, Any]:
        """登记一次补能；充电来源允许行程结束后补报。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            trip = self._require_trip(conn, trip_id)
            lock = self._trip_lock(conn, trip_id)
            if lock is not None and lock["state"] == "held":
                raise ConflictError("行程处于批次核验中；驳回后可补录，已发布批次则触发重述")
            event_id = self._identifier(event_id, "event_id")
            amount = self._positive(amount, "amount")
            if source_status not in ("unknown", "reported"):
                raise ValidationError("source_status 只能是 unknown 或 reported")
            occurred_at = self._instant(occurred_at or trip["occurred_at"], "occurred_at")
            if station_id is not None:
                station_id = self._identifier(station_id, "station_id")
            now = self._now()
            existing = conn.execute("SELECT event_id FROM carbon_energy_events WHERE event_id=?",
                                    (event_id,)).fetchone()
            if existing:
                raise ConflictError("补能事件编号已经存在")
            conn.execute(
                "INSERT INTO carbon_energy_events(event_id,trip_id,station_id,source_status,"
                "amount,occurred_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, trip_id, station_id, source_status, amount, occurred_at,
                 actor_id, now),
            )
            append_event(conn, actor_id=actor_id, action="carbon.energy.recorded",
                         resource_type="carbon_energy_event", resource_id=event_id,
                         detail={"trip_id": trip_id, "amount": amount, "station_id": station_id,
                                 "source_status": source_status}, occurred_at=now)
            return {"event_id": event_id, "trip_id": trip_id, "amount": amount,
                    "source_status": source_status}

    def report_energy_source(self, *, actor_id: str, event_id: str,
                             source_status: str = "reported") -> dict[str, Any]:
        """补报充电来源；未补报前按区域电网因子计算而不是零排放。"""

        if source_status != "reported":
            raise ValidationError("补报来源只能置为 reported")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            row = conn.execute("SELECT * FROM carbon_energy_events WHERE event_id=?",
                               (event_id,)).fetchone()
            if row is None:
                raise NotFoundError("补能事件不存在")
            lock = self._trip_lock(conn, row["trip_id"])
            if lock is not None and lock["state"] == "held":
                raise ConflictError("行程处于批次核验中；驳回后可补报，已发布批次则触发重述")
            conn.execute("UPDATE carbon_energy_events SET source_status='reported' WHERE event_id=?",
                         (event_id,))
            append_event(conn, actor_id=actor_id, action="carbon.energy_source.reported",
                         resource_type="carbon_energy_event", resource_id=event_id,
                         detail={"trip_id": row["trip_id"]}, occurred_at=self._now())
            return {"event_id": event_id, "source_status": "reported"}

    def clear_trip_conflict(self, *, actor_id: str, trip_id: str, flag: str) -> dict[str, Any]:
        """冲突经核查不成立时清除标记（核验中/已锁定行程除外）。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._require_trip(conn, trip_id)
            lock = self._trip_lock(conn, trip_id)
            if lock is not None and lock["state"] == "held":
                raise ConflictError("批次核验中的冲突标记不能清除")
            row = conn.execute("SELECT conflict_flags_json FROM carbon_trips WHERE trip_id=?",
                               (trip_id,)).fetchone()
            flags = set(json.loads(row["conflict_flags_json"]))
            flags.discard(flag)
            flags_sorted = sorted(flags)
            conn.execute("UPDATE carbon_trips SET conflict_flags_json=? WHERE trip_id=?",
                         (canonical_json(flags_sorted), trip_id))
            append_event(conn, actor_id=actor_id, action="carbon.trip.conflict_cleared",
                         resource_type="carbon_trip", resource_id=trip_id,
                         detail={"flag": flag}, occurred_at=self._now())
            return {"trip_id": trip_id, "conflict_flags": flags_sorted}

    def void_claim(self, *, actor_id: str, claim_id: str, reason: str) -> dict[str, Any]:
        """核验裁定撤销错误申报，释放其对补能事件的占用。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer", "admin")
            row = conn.execute("SELECT * FROM carbon_claims WHERE claim_id=?",
                               (claim_id,)).fetchone()
            if row is None:
                raise NotFoundError("申报不存在")
            reason = self._text(reason, "reason")
            if row["status"] == "voided":
                return {"claim_id": claim_id, "status": "voided", "replayed": True}
            event = conn.execute("SELECT trip_id FROM carbon_energy_events WHERE event_id=?",
                                 (row["energy_event_id"],)).fetchone()
            lock = self._trip_lock(conn, event["trip_id"])
            if lock is not None and lock["state"] == "held":
                raise ConflictError("批次核验中的申报不能撤销")
            conn.execute("UPDATE carbon_claims SET status='voided' WHERE claim_id=?",
                         (claim_id,))
            append_event(conn, actor_id=actor_id, action="carbon.claim.voided",
                         resource_type="carbon_claim", resource_id=claim_id,
                         detail={"certificate_id": row["certificate_id"],
                                 "energy_event_id": row["energy_event_id"],
                                 "reason": reason}, occurred_at=self._now())
            return {"claim_id": claim_id, "status": "voided", "replayed": False}

    # -- 凭证与申报（防重复计算） ------------------------------------------

    def register_certificate(self, *, request_id: str, actor_id: str, certificate_id: str,
                             claimant_type: str, claimant_id: str, kwh_total: float,
                             generation_start: str, generation_end: str,
                             valid_from: str, valid_to: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "certificate_id": certificate_id,
                   "claimant_type": claimant_type, "claimant_id": claimant_id,
                   "kwh_total": kwh_total, "generation_start": generation_start,
                   "generation_end": generation_end, "valid_from": valid_from,
                   "valid_to": valid_to}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            certificate_id = self._identifier(certificate_id, "certificate_id")
            if claimant_type not in CLAIMANT_TYPES:
                raise ValidationError("claimant_type 只能是 fleet 或 station")
            claimant_id = self._identifier(claimant_id, "claimant_id")
            kwh_total = self._positive(kwh_total, "kwh_total")
            generation_start, generation_end = self._period(generation_start, generation_end)
            valid_from, valid_to = self._period(valid_from, valid_to)
            payload_hash = digest({k: payload[k] for k in
                                   ("certificate_id", "claimant_type", "claimant_id",
                                    "kwh_total", "generation_start", "generation_end",
                                    "valid_from", "valid_to")})

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO carbon_certificates(certificate_id,claimant_type,claimant_id,"
                        "kwh_total,generation_start,generation_end,valid_from,valid_to,status,"
                        "payload_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?)",
                        (certificate_id, claimant_type, claimant_id, kwh_total, generation_start,
                         generation_end, valid_from, valid_to, payload_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("凭证编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="carbon.certificate.registered",
                             resource_type="carbon_certificate", resource_id=certificate_id,
                             detail={"claimant_type": claimant_type, "claimant_id": claimant_id,
                                     "kwh_total": kwh_total}, occurred_at=self._now())
                return "carbon_certificate", certificate_id, {"certificate_id": certificate_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_register_certificate", payload=payload,
                                    create=create)

    def withdraw_certificate(self, *, actor_id: str, certificate_id: str,
                             reason: str) -> dict[str, Any]:
        """撤回凭证：不删除历史，已发布批次只能随后走凭证撤回重述。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "auditor", "admin")
            row = conn.execute("SELECT * FROM carbon_certificates WHERE certificate_id=?",
                               (certificate_id,)).fetchone()
            if row is None:
                raise NotFoundError("凭证不存在")
            reason = self._text(reason, "reason")
            if row["status"] == "withdrawn":
                return {"certificate_id": certificate_id, "status": "withdrawn", "replayed": True}
            now = self._now()
            conn.execute(
                "UPDATE carbon_certificates SET status='withdrawn',withdrawn_at=?,"
                "withdrawal_reason=? WHERE certificate_id=?",
                (now, reason, certificate_id),
            )
            append_event(conn, actor_id=actor_id, action="carbon.certificate.withdrawn",
                         resource_type="carbon_certificate", resource_id=certificate_id,
                         detail={"reason": reason}, occurred_at=now)
            return {"certificate_id": certificate_id, "status": "withdrawn", "replayed": False}

    def file_claim(self, *, actor_id: str, claim_id: str, certificate_id: str,
                   energy_event_id: str, claimant_type: str, claimant_id: str,
                   kwh: float) -> dict[str, Any]:
        """对补能事件申报凭证；同一事件只允许一个 accepted 申报，冲突进补证。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            claim_id = self._identifier(claim_id, "claim_id")
            if claimant_type not in CLAIMANT_TYPES:
                raise ValidationError("claimant_type 只能是 fleet 或 station")
            claimant_id = self._identifier(claimant_id, "claimant_id")
            certificate = conn.execute(
                "SELECT * FROM carbon_certificates WHERE certificate_id=?", (certificate_id,)
            ).fetchone()
            if certificate is None:
                raise NotFoundError("凭证不存在")
            event = conn.execute("SELECT * FROM carbon_energy_events WHERE event_id=?",
                                 (energy_event_id,)).fetchone()
            if event is None:
                raise NotFoundError("补能事件不存在")
            kwh = self._positive(kwh, "kwh")
            if kwh > event["amount"] + 1e-9:
                raise ValidationError("申报电量不能超过补能事件电量")
            if kwh > certificate["kwh_total"] + 1e-9:
                raise ValidationError("申报电量不能超过凭证总量")
            existing = conn.execute("SELECT * FROM carbon_claims WHERE claim_id=?",
                                    (claim_id,)).fetchone()
            if existing:
                return {"claim_id": claim_id, "status": existing["status"], "replayed": True}
            accepted = conn.execute(
                "SELECT * FROM carbon_claims WHERE energy_event_id=? AND status='accepted'",
                (energy_event_id,),
            ).fetchone()
            conflict_code = None
            if accepted is not None:
                status = "rejected_conflict"
                if accepted["certificate_id"] != certificate_id:
                    conflict_code = "event_already_claimed_other_certificate"
                elif accepted["claimant_type"] != claimant_type or accepted["claimant_id"] != claimant_id:
                    conflict_code = "event_already_claimed_other_party"
                else:
                    conflict_code = "duplicate_claim"
            elif certificate["claimant_type"] != claimant_type or certificate["claimant_id"] != claimant_id:
                raise PermissionDenied("申报方与凭证登记方不一致")
            else:
                status = "accepted"
            conn.execute(
                "INSERT INTO carbon_claims(claim_id,certificate_id,energy_event_id,claimant_type,"
                "claimant_id,kwh,status,conflict_code,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (claim_id, certificate_id, energy_event_id, claimant_type, claimant_id, kwh,
                 status, conflict_code, actor_id, self._now()),
            )
            append_event(conn, actor_id=actor_id, action="carbon.claim.filed",
                         resource_type="carbon_claim", resource_id=claim_id,
                         detail={"certificate_id": certificate_id,
                                 "energy_event_id": energy_event_id, "status": status,
                                 "conflict_code": conflict_code}, occurred_at=self._now())
            result = {"claim_id": claim_id, "status": status, "replayed": False}
            if conflict_code:
                result["conflict_code"] = conflict_code
            return result

    # -- 核算批次：冻结、核验、发布、重述、吊销 -----------------------------

    def create_batch(self, *, request_id: str, actor_id: str, batch_id: str, corridor_id: str,
                     period_start: str, period_end: str, baseline_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "corridor_id": corridor_id,
                   "period_start": period_start, "period_end": period_end,
                   "baseline_id": baseline_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._require_corridor(conn, corridor_id)
            batch_id = self._identifier(batch_id, "batch_id")
            period_start, period_end = self._period(period_start, period_end)
            baseline = self._latest_version_row(conn, "baseline_versions",
                                                ("baseline_id", baseline_id))
            if baseline is None or baseline["corridor_id"] != corridor_id:
                raise ValidationError("基准方案不存在或不属于该走廊")
            if conn.execute("SELECT 1 FROM carbon_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise ConflictError("批次编号已经存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO carbon_batches(batch_id,version,corridor_id,period_start,"
                    "period_end,baseline_id,status,revision_kind,change_note,created_by,created_at)"
                    " VALUES(?,1,?,?,?,?,'draft','original','初始批次',?,?)",
                    (batch_id, corridor_id, period_start, period_end, baseline_id,
                     actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="carbon.batch.created",
                             resource_type="carbon_batch", resource_id=batch_id,
                             detail={"corridor_id": corridor_id, "period_start": period_start,
                                     "period_end": period_end, "baseline_id": baseline_id},
                             occurred_at=self._now())
                return "carbon_batch", f"{batch_id}:v1", {"batch_id": batch_id, "version": 1}

            return self._idempotent(conn, request_id=request_id,
                                    action="carbon_create_batch", payload=payload, create=create)

    def freeze_batch(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        """把批次周期内的全部输入按当前最新配置版本冻结并立即核算。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._require_open_batch(conn, batch_id)
            if batch["status"] not in ("draft", "rejected"):
                raise ConflictError("只有 draft 或被驳回的批次可以冻结")
            return self._freeze_locked(conn, batch=batch, actor_id=actor_id, version=1,
                                       revision_kind="original", change_note="初始冻结")

    def review_batch(self, *, actor_id: str, batch_id: str, decision: str,
                     note: str = "") -> dict[str, Any]:
        """reviewer 独立核验：批准进入待发布，驳回则释放全部占用。"""

        if decision not in ("approve", "reject"):
            raise ValidationError("decision 只能是 approve 或 reject")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer", "admin")
            batch = self._require_latest_batch(conn, batch_id)
            if batch["status"] != "frozen":
                raise ConflictError("只有 frozen 批次可以核验")
            if actor.actor_id == batch["frozen_by"]:
                raise PermissionDenied("核验人不能是批次冻结人，必须独立核验")
            open_evidence = conn.execute(
                "SELECT COUNT(*) AS c FROM carbon_evidence_requests WHERE family_batch_id=? "
                "AND version=? AND status='open'",
                (batch_id, batch["version"]),
            ).fetchone()["c"]
            if decision == "approve" and open_evidence > 0:
                raise ConflictError(
                    f"存在 {open_evidence} 张未处理补证单，缺失或冲突证据不得按零排放通过")
            now = self._now()
            if decision == "approve":
                conn.execute(
                    "UPDATE carbon_batches SET status='verified',verified_by=?,verified_at=?,"
                    "verification_decision='approve',verification_note=? WHERE batch_id=? AND version=?",
                    (actor_id, now, note, batch_id, batch["version"]),
                )
                action = "carbon.batch.verified"
            else:
                conn.execute(
                    "UPDATE carbon_batches SET status='rejected',verified_by=?,verified_at=?,"
                    "verification_decision='reject',verification_note=? WHERE batch_id=? AND version=?",
                    (actor_id, now, note, batch_id, batch["version"]),
                )
                self._release_family(conn, batch_id, batch["version"])
                if batch["version"] > 1:
                    self._restore_prior_publication(conn, batch_id, batch["version"] - 1)
                action = "carbon.batch.rejected"
            append_event(conn, actor_id=actor_id, action=action,
                         resource_type="carbon_batch",
                         resource_id=f"{batch_id}:v{batch['version']}",
                         detail={"decision": decision, "note": note}, occurred_at=now)
            return {"batch_id": batch_id, "version": batch["version"],
                    "status": "verified" if decision == "approve" else "rejected"}

    def publish_batch(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        """auditor 发布；发布后历史版本始终可按原快照复算。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "auditor", "admin")
            batch = self._require_latest_batch(conn, batch_id)
            if batch["status"] != "verified":
                raise ConflictError("只有独立核验通过的批次才能发布")
            if actor.actor_id == batch["verified_by"]:
                raise PermissionDenied("发布人不能是核验人，保持独立性")
            now = self._now()
            conn.execute(
                "UPDATE carbon_batches SET status='published',published_by=?,published_at=? "
                "WHERE batch_id=? AND version=?",
                (actor_id, now, batch_id, batch["version"]),
            )
            conn.execute(
                "UPDATE carbon_allocations SET status='committed' WHERE family_batch_id=? "
                "AND version=? AND status='held'",
                (batch_id, batch["version"]),
            )
            conn.execute(
                "UPDATE carbon_trip_locks SET state='committed' WHERE family_batch_id=? "
                "AND active_version=?",
                (batch_id, batch["version"]),
            )
            result = json.loads(batch["result_json"])
            append_event(conn, actor_id=actor_id, action="carbon.batch.published",
                         resource_type="carbon_batch",
                         resource_id=f"{batch_id}:v{batch['version']}",
                         detail={"total_avoided_tco2": result["total_avoided_tco2"],
                                 "trips_accepted": result["trips_accepted"],
                                 "result_hash": batch["result_hash"]}, occurred_at=now)
            return {"batch_id": batch_id, "version": batch["version"], "status": "published",
                    "total_avoided_tco2": result["total_avoided_tco2"],
                    "result_hash": batch["result_hash"]}

    def restate_batch(self, *, actor_id: str, batch_id: str, revision_kind: str,
                      change_note: str) -> dict[str, Any]:
        """对已发布批次创建重述/吊销新版本，旧版本原样保留可复算。"""

        allowed = {"late_data", "factor_revision", "certificate_revocation"}
        if revision_kind not in allowed:
            raise ValidationError("revision_kind 不合法")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._require_latest_batch(conn, batch_id)
            if batch["status"] != "published":
                raise ConflictError("只有已发布批次才能重述")
            change_note = self._text(change_note, "change_note")
            self._validate_revision(conn, batch, revision_kind)
            new_version = batch["version"] + 1
            prior_status = "revoked" if revision_kind == "certificate_revocation" else "restated"
            conn.execute(
                "UPDATE carbon_batches SET status=? WHERE batch_id=? AND version=?",
                (prior_status, batch_id, batch["version"]),
            )
            self._supersede_family(conn, batch_id, batch["version"])
            conn.execute(
                "UPDATE carbon_evidence_requests SET status='resolved',resolution_note=?,"
                "resolved_at=?,resolved_by=? WHERE family_batch_id=? AND version=? AND status='open'",
                (f"由版本 {new_version} 替代", self._now(), actor_id, batch_id, batch["version"]),
            )
            frozen = self._freeze_locked(conn, batch=batch, actor_id=actor_id, version=new_version,
                                         revision_kind=revision_kind, change_note=change_note,
                                         corridor_id=batch["corridor_id"],
                                         period_start=batch["period_start"],
                                         period_end=batch["period_end"],
                                         baseline_id=batch["baseline_id"],
                                         prior_status=prior_status)
            append_event(conn, actor_id=actor_id,
                         action="carbon.batch.restated" if prior_status == "restated"
                         else "carbon.batch.revoked",
                         resource_type="carbon_batch", resource_id=batch_id,
                         detail={"prior_version": batch["version"], "new_version": new_version,
                                 "revision_kind": revision_kind, "prior_status": prior_status},
                         occurred_at=self._now())
            return frozen

    def recompute_batch(self, batch_id: str, version: int | None = None) -> dict[str, Any]:
        """用冻结时的原始输入重新计算并比对结果哈希。"""

        conn = self.database.connection
        if version is None:
            batch = self._require_latest_batch(conn, batch_id)
        else:
            batch = self._require_batch_version(conn, batch_id, version)
        if batch["input_snapshot_json"] is None:
            raise ConflictError("该批次版本尚未冻结")
        snapshot = json.loads(batch["input_snapshot_json"])
        result = compute_batch(snapshot)
        recomputed_hash = digest(result_to_dict(result))
        return {
            "batch_id": batch_id, "version": batch["version"],
            "stored_snapshot_hash": batch["snapshot_hash"],
            "recomputed_snapshot_hash": digest(snapshot),
            "snapshot_matches": digest(snapshot) == batch["snapshot_hash"],
            "stored_result_hash": batch["result_hash"],
            "recomputed_result_hash": recomputed_hash,
            "result_matches": recomputed_hash == batch["result_hash"],
            "result": result_to_dict(result),
        }

    def adjudicate_evidence(self, *, actor_id: str, request_id: str, decision: str,
                            note: str) -> dict[str, Any]:
        """核验员裁定冻结批次上的补证单。

        - waived：仅允许非阻断性问题（如重复申报已拒绝、电量已回退电网口径），
          问题本身不产生减排量；裁定后批次可继续核验。
        - 阻断性缺证（载荷/来源缺失、冲突、过期）不能豁免，只能驳回批次补证。
        """

        if decision not in ("waived", "upheld"):
            raise ValidationError("decision 只能是 waived 或 upheld")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer", "admin")
            row = conn.execute("SELECT * FROM carbon_evidence_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise NotFoundError("补证单不存在")
            if row["status"] == "resolved":
                return {"request_id": request_id, "status": "resolved", "replayed": True}
            batch = conn.execute(
                "SELECT * FROM carbon_batches WHERE batch_id=? AND version=?",
                (row["family_batch_id"], row["version"]),
            ).fetchone()
            if batch is None or batch["status"] != "frozen":
                raise ConflictError("只能裁定处于 frozen 状态版本上的补证单")
            note = self._text(note, "note")
            now = self._now()
            if decision == "upheld":
                conn.execute(
                    "UPDATE carbon_batches SET status='rejected',verified_by=?,verified_at=?,"
                    "verification_decision='reject',verification_note=? WHERE batch_id=? AND version=?",
                    (actor_id, now, f"补证单 {request_id} 裁定成立：{note}",
                     row["family_batch_id"], row["version"]),
                )
                self._release_family(conn, row["family_batch_id"], row["version"])
                if row["version"] > 1:
                    self._restore_prior_publication(conn, row["family_batch_id"],
                                                    row["version"] - 1)
                append_event(conn, actor_id=actor_id, action="carbon.evidence.upheld",
                             resource_type="carbon_batch",
                             resource_id=f"{row['family_batch_id']}:v{row['version']}",
                             detail={"request_id": request_id, "code": row["code"]},
                             occurred_at=now)
                return {"request_id": request_id, "batch_status": "rejected"}
            if row["code"] not in NON_BLOCKING_CODES:
                raise PermissionDenied(
                    "缺失、冲突或过期的阻断性证据不能豁免，必须驳回批次补证后重新冻结")
            conn.execute(
                "UPDATE carbon_evidence_requests SET status='resolved',resolution_note=?,"
                "resolved_at=?,resolved_by=? WHERE request_id=?",
                (f"核验裁定豁免（不产生绿电减排）：{note}", now, actor_id, request_id),
            )
            append_event(conn, actor_id=actor_id, action="carbon.evidence.waived",
                         resource_type="carbon_evidence_request", resource_id=request_id,
                         detail={"code": row["code"], "note": note}, occurred_at=now)
            return {"request_id": request_id, "status": "resolved", "replayed": False}

    def resolve_evidence(self, *, actor_id: str, request_id: str, note: str) -> dict[str, Any]:
        """关闭补证单（仅被驳回批次的补证单可手工关闭）。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            row = conn.execute("SELECT * FROM carbon_evidence_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise NotFoundError("补证单不存在")
            if row["status"] == "resolved":
                return {"request_id": request_id, "status": "resolved", "replayed": True}
            batch = conn.execute(
                "SELECT status FROM carbon_batches WHERE batch_id=? AND version=?",
                (row["batch_id"], row["version"]),
            ).fetchone()
            if batch is None or batch["status"] != "rejected":
                raise ConflictError("进行中或已发布版本的补证单只能由新版本自动替代")
            note = self._text(note, "note")
            now = self._now()
            conn.execute(
                "UPDATE carbon_evidence_requests SET status='resolved',resolution_note=?,"
                "resolved_at=?,resolved_by=? WHERE request_id=?",
                (note, now, actor_id, request_id),
            )
            append_event(conn, actor_id=actor_id, action="carbon.evidence.resolved",
                         resource_type="carbon_evidence_request", resource_id=request_id,
                         detail={"note": note}, occurred_at=now)
            return {"request_id": request_id, "status": "resolved", "replayed": False}

    # -- 查询与解释 --------------------------------------------------------

    def list_boundary_versions(self, corridor_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        self._require_corridor(conn, corridor_id)
        rows = conn.execute(
            "SELECT corridor_id,version,segments_json,payload_hash,change_note,created_by,"
            "created_at FROM carbon_boundary_versions WHERE corridor_id=? ORDER BY version",
            (corridor_id,),
        ).fetchall()
        return [{"corridor_id": row["corridor_id"], "version": row["version"],
                 "segments": json.loads(row["segments_json"]),
                 "payload_hash": row["payload_hash"], "change_note": row["change_note"],
                 "created_by": row["created_by"], "created_at": row["created_at"]}
                for row in rows]

    def list_factor_versions(self, factor_key: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT factor_key,version,value,unit,source,valid_from,valid_to,change_note,"
            "created_by,created_at FROM emission_factor_versions WHERE factor_key=? "
            "ORDER BY version",
            (factor_key,),
        ).fetchall()
        if not rows:
            raise NotFoundError("排放因子不存在")
        return [dict(row) for row in rows]

    def list_evidence(self, *, batch_id: str | None = None, version: int | None = None,
                      status_filter: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT request_id,family_batch_id,batch_id,version,trip_id,code,detail_json,"
                 "status,resolution_note,created_by,created_at,resolved_at,resolved_by "
                 "FROM carbon_evidence_requests WHERE 1=1")
        params: list[Any] = []
        if batch_id:
            query += " AND family_batch_id=?"
            params.append(batch_id)
            if version is not None:
                query += " AND version=?"
                params.append(version)
        if status_filter:
            query += " AND status=?"
            params.append(status_filter)
        query += " ORDER BY created_at, request_id"
        items = []
        for row in self.database.connection.execute(query, params):
            items.append({"request_id": row["request_id"], "family_batch_id": row["family_batch_id"],
                          "batch_id": row["batch_id"], "version": row["version"],
                          "trip_id": row["trip_id"], "code": row["code"],
                          "detail": json.loads(row["detail_json"]), "status": row["status"],
                          "resolution_note": row["resolution_note"],
                          "created_by": row["created_by"], "created_at": row["created_at"],
                          "resolved_at": row["resolved_at"], "resolved_by": row["resolved_by"]})
        return items

    def get_batch(self, batch_id: str, version: int | None = None) -> BatchRecord:
        conn = self.database.connection
        batch = self._require_batch_version(conn, batch_id, version) if version \
            else self._require_latest_batch(conn, batch_id)
        return BatchRecord(
            batch_id=batch["batch_id"], version=batch["version"], corridor_id=batch["corridor_id"],
            period_start=batch["period_start"], period_end=batch["period_end"],
            status=batch["status"], revision_kind=batch["revision_kind"],
            change_note=batch["change_note"], created_by=batch["created_by"],
            created_at=batch["created_at"], snapshot_hash=batch["snapshot_hash"],
            result_hash=batch["result_hash"], formula_version=batch["formula_version"],
            frozen_by=batch["frozen_by"], frozen_at=batch["frozen_at"],
            verified_by=batch["verified_by"], verified_at=batch["verified_at"],
            verification_decision=batch["verification_decision"],
            verification_note=batch["verification_note"], published_by=batch["published_by"],
            published_at=batch["published_at"])

    def list_batch_versions(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT batch_id,version,corridor_id,status,revision_kind,change_note,snapshot_hash,"
            "result_hash,created_at,frozen_at,published_at FROM carbon_batches "
            "WHERE batch_id=? ORDER BY version",
            (batch_id,),
        ).fetchall()
        if not rows:
            raise NotFoundError("批次不存在")
        return [dict(row) for row in rows]

    def explain_batch(self, batch_id: str, version: int | None = None) -> dict[str, Any]:
        """解释每一吨减排来自哪些有效行程，并列明排除与补证。"""

        conn = self.database.connection
        batch = self._require_batch_version(conn, batch_id, version) if version \
            else self._require_latest_batch(conn, batch_id)
        if batch["input_snapshot_json"] is None:
            raise ConflictError("批次尚未冻结，没有可解释的核算结果")
        result = json.loads(batch["result_json"])
        snapshot = json.loads(batch["input_snapshot_json"])
        per_trip = round(sum(item["avoided_tco2"] for item in result["contributions"]), 6)
        return {
            "batch_id": batch_id, "version": batch["version"], "status": batch["status"],
            "revision_kind": batch["revision_kind"],
            "formula_version": result["formula_version"],
            "frozen_at": snapshot["frozen_at"],
            "config_versions": snapshot["config_versions"],
            "totals": {
                "baseline_tco2": result["total_baseline_tco2"],
                "actual_tco2": result["total_actual_tco2"],
                "avoided_tco2": result["total_avoided_tco2"],
                "sum_of_trip_contributions_tco2": per_trip,
                "trips_in_scope": result["trips_in_scope"],
                "trips_accepted": result["trips_accepted"],
                "trips_excluded": result["trips_excluded"],
            },
            "trip_contributions": result["contributions"],
            "excluded_trips": result["exclusions"],
            "evidence_codes": result["evidence_codes"],
            "certificate_usage": result["certificate_usage"],
            "snapshot_hash": batch["snapshot_hash"],
            "result_hash": batch["result_hash"],
        }

    def corridor_progress(self, corridor_id: str) -> TargetProgress:
        """按最新目标口径汇总：每个批次族只取最新版本。"""

        conn = self.database.connection
        self._require_corridor(conn, corridor_id)
        target = self._latest_version_row(conn, "carbon_target_versions",
                                          ("corridor_id", corridor_id))
        if target is None:
            raise NotFoundError("走廊尚未发布减排目标")
        rows = conn.execute(
            "SELECT b.batch_id,b.version,b.status,b.result_json,b.period_start,b.period_end "
            "FROM carbon_batches b JOIN (SELECT batch_id,MAX(version) AS mv FROM carbon_batches "
            "WHERE status='published' GROUP BY batch_id) m ON b.batch_id=m.batch_id AND b.version=m.mv "
            "WHERE b.corridor_id=? AND b.status='published' ORDER BY b.batch_id",
            (corridor_id,),
        ).fetchall()
        included = []
        achieved = 0.0
        for row in rows:
            if row["period_start"] < target["period_start"] or \
                    row["period_end"] > target["period_end"]:
                continue
            # 每个批次族只汇总最新且仍处发布态的版本；被吊销或驳回的族不计数。
            if row["status"] != "published":
                continue
            avoided = json.loads(row["result_json"])["total_avoided_tco2"] if row["result_json"] else 0.0
            achieved += avoided
            included.append({"batch_id": row["batch_id"], "version": row["version"],
                             "status": row["status"], "avoided_tco2": round(avoided, 6),
                             "period_start": row["period_start"], "period_end": row["period_end"]})
        target_value = float(target["target_reduction_t"])
        ratio = round(achieved / target_value, 6) if target_value > 0 else None
        return TargetProgress(
            corridor_id=corridor_id, target_version=target["version"],
            period_start=target["period_start"], period_end=target["period_end"],
            target_reduction_t=target_value, achieved_reduction_t=round(achieved, 6),
            completion_ratio=ratio, included_batches=tuple(included),
            methodology=target["methodology"])

    # -- 内部辅助 ----------------------------------------------------------

    def _freeze_locked(self, conn, *, batch, actor_id: str, version: int,
                       revision_kind: str, change_note: str, corridor_id: str | None = None,
                       period_start: str | None = None, period_end: str | None = None,
                       baseline_id: str | None = None, prior_status: str | None = None
                       ) -> dict[str, Any]:
        corridor_id = corridor_id or batch["corridor_id"]
        period_start = period_start or batch["period_start"]
        period_end = period_end or batch["period_end"]
        baseline_id = baseline_id or batch["baseline_id"]
        frozen_at = self._now()

        trips = conn.execute(
            "SELECT * FROM carbon_trips WHERE corridor_id=? AND occurred_at>=? AND occurred_at<=? "
            "ORDER BY trip_id",
            (corridor_id, period_start, period_end),
        ).fetchall()
        trip_ids = [row["trip_id"] for row in trips]
        if trip_ids:
            placeholders = ",".join("?" for _ in trip_ids)
            blocked = conn.execute(
                f"SELECT trip_id,family_batch_id FROM carbon_trip_locks WHERE trip_id IN "
                f"({placeholders}) AND family_batch_id!=?",
                (*trip_ids, batch["batch_id"]),
            ).fetchall()
            if blocked:
                raise ConflictError(
                    "行程已属于其他核算批次：" + ",".join(row["trip_id"] for row in blocked))

        boundary = self._latest_version_row(conn, "carbon_boundary_versions",
                                            ("corridor_id", corridor_id))
        if boundary is None:
            raise ValidationError("走廊尚未发布路段边界版本")
        segments = json.loads(boundary["segments_json"])

        baseline = self._latest_version_row(conn, "baseline_versions",
                                            ("baseline_id", baseline_id))
        if baseline is None or baseline["corridor_id"] != corridor_id:
            raise ValidationError("基准方案无效")

        factor_keys = {baseline["factor_key"], GRID_FACTOR_KEY, RENEWABLE_FACTOR_KEY}
        factors: dict[str, Any] = {}
        for key in sorted(factor_keys):
            row = self._resolve_factor(conn, key, frozen_at)
            if row is None:
                raise ValidationError(f"缺少排放因子：{key}")
            factors[key] = {"factor_key": key, "version": row["version"], "value": row["value"],
                            "unit": row["unit"], "source": row["source"],
                            "valid_from": row["valid_from"], "valid_to": row["valid_to"]}

        vehicle_types: dict[str, Any] = {}
        policies: dict[str, Any] = {}
        snapshot_trips: list[dict[str, Any]] = []

        for trip in trips:
            vt_id = trip["vehicle_type_id"]
            if vt_id not in vehicle_types:
                vt = self._latest_version_row(conn, "vehicle_type_versions",
                                              ("vehicle_type_id", vt_id))
                if vt is None:
                    raise ValidationError(f"车型不存在：{vt_id}")
                vehicle_types[vt_id] = {
                    "vehicle_type_id": vt_id, "version": vt["version"],
                    "energy_type": vt["energy_type"], "consumption_rate": vt["consumption_rate"],
                    "rated_payload_t": vt["rated_payload_t"]}
            events = conn.execute(
                "SELECT * FROM carbon_energy_events WHERE trip_id=? ORDER BY event_id",
                (trip["trip_id"],),
            ).fetchall()
            snapshot_events = []
            for event in events:
                claims = conn.execute(
                    "SELECT * FROM carbon_claims WHERE energy_event_id=? ORDER BY claim_id",
                    (event["event_id"],),
                ).fetchall()
                snapshot_claims = []
                for claim in claims:
                    cert = conn.execute(
                        "SELECT * FROM carbon_certificates WHERE certificate_id=?",
                        (claim["certificate_id"],),
                    ).fetchone()
                    snapshot_claims.append({
                        "claim_id": claim["claim_id"], "certificate_id": claim["certificate_id"],
                        "claimant_type": claim["claimant_type"],
                        "claimant_id": claim["claimant_id"], "kwh": claim["kwh"],
                        "status": claim["status"], "conflict_code": claim["conflict_code"],
                        "certificate": {
                            "certificate_id": cert["certificate_id"],
                            "claimant_type": cert["claimant_type"],
                            "claimant_id": cert["claimant_id"],
                            "kwh_total": cert["kwh_total"],
                            "generation_start": cert["generation_start"],
                            "generation_end": cert["generation_end"],
                            "valid_from": cert["valid_from"], "valid_to": cert["valid_to"],
                            "status": cert["status"]},
                    })
                snapshot_events.append({
                    "event_id": event["event_id"], "station_id": event["station_id"],
                    "source_status": event["source_status"], "amount": event["amount"],
                    "occurred_at": event["occurred_at"], "created_at": event["created_at"],
                    "claims": snapshot_claims})
            snapshot_trips.append({
                "trip_id": trip["trip_id"], "segment_id": trip["segment_id"],
                "vehicle_id": trip["vehicle_id"], "vehicle_type_id": vt_id,
                "distance_km": trip["distance_km"], "occurred_at": trip["occurred_at"],
                "payload_t": trip["payload_t"], "payload_status": trip["payload_status"],
                "updated_at": trip["updated_at"],
                "conflict_flags": json.loads(trip["conflict_flags_json"]),
                "energy_events": snapshot_events})

        for evidence_type in sorted(EVIDENCE_TYPES):
            row = self._latest_version_row(conn, "evidence_policy_versions",
                                           ("evidence_type", evidence_type))
            if row is None:
                raise ValidationError(f"缺少证据有效期策略：{evidence_type}")
            policies[evidence_type] = {"evidence_type": evidence_type, "version": row["version"],
                                       "validity_days": row["validity_days"],
                                       "required": bool(row["required"])}

        snapshot = {
            "formula_version": FORMULA_VERSION,
            "corridor_id": corridor_id,
            "period_start": period_start,
            "period_end": period_end,
            "frozen_at": frozen_at,
            "boundary": {"version": boundary["version"], "segments": segments},
            "baseline": {"baseline_id": baseline["baseline_id"], "version": baseline["version"],
                         "vehicle_class": baseline["vehicle_class"],
                         "fuel_intensity_l_per_km": baseline["fuel_intensity_l_per_km"],
                         "factor_key": baseline["factor_key"]},
            "factors": factors,
            "vehicle_types": vehicle_types,
            "policies": policies,
            "trips": snapshot_trips,
            "config_versions": None,
        }
        snapshot["config_versions"] = {
            "boundary": boundary["version"], "baseline": baseline["version"],
            "factors": {key: factors[key]["version"] for key in sorted(factors)},
            "vehicle_types": {key: vehicle_types[key]["version"] for key in sorted(vehicle_types)},
            "policies": {key: policies[key]["version"] for key in sorted(policies)},
        }

        result = compute_batch(snapshot)
        result_dict = result_to_dict(result)
        snapshot_hash = digest(snapshot)
        result_hash = digest(result_dict)

        # 凭证防重复计算：批次内需求（不按凭证总量截断）与其他批次族的
        # held/committed 占用之和不得超过凭证总量，否则冻结失败进入补证。
        event_to_trip = {event["event_id"]: trip["trip_id"]
                         for trip in snapshot_trips for event in trip["energy_events"]}
        wanted_by_cert = certificate_demand(snapshot)
        for cert_id, wanted in wanted_by_cert.items():
            row = conn.execute(
                "SELECT COALESCE(SUM(kwh),0) AS used FROM carbon_allocations "
                "WHERE certificate_id=? AND status IN ('held','committed') "
                "AND family_batch_id!=?",
                (cert_id, batch["batch_id"]),
            ).fetchone()
            total = conn.execute(
                "SELECT kwh_total,claimant_type,claimant_id FROM carbon_certificates "
                "WHERE certificate_id=?", (cert_id,),
            ).fetchone()
            if row["used"] + wanted > total["kwh_total"] + 1e-6:
                raise ConflictError(
                    f"凭证 {cert_id} 电量不足：已占用 {round(row['used'], 3)}，"
                    f"本批次需要 {round(wanted, 3)}，总量 {total['kwh_total']}")

        existing_version = conn.execute(
            "SELECT 1 FROM carbon_batches WHERE batch_id=? AND version=?",
            (batch["batch_id"], version),
        ).fetchone()
        if existing_version:
            conn.execute(
                "UPDATE carbon_batches SET status='frozen',revision_kind=?,"
                "input_snapshot_json=?,snapshot_hash=?,result_json=?,result_hash=?,"
                "formula_version=?,frozen_by=?,frozen_at=?,submitted_at=? "
                "WHERE batch_id=? AND version=?",
                (revision_kind, canonical_json(snapshot), snapshot_hash,
                 canonical_json(result_dict), result_hash, FORMULA_VERSION, actor_id,
                 frozen_at, frozen_at, batch["batch_id"], version),
            )
        elif version == 1:
            conn.execute(
                "UPDATE carbon_batches SET status='frozen',revision_kind='original',"
                "input_snapshot_json=?,snapshot_hash=?,result_json=?,result_hash=?,"
                "formula_version=?,frozen_by=?,frozen_at=?,submitted_at=? "
                "WHERE batch_id=? AND version=1",
                (canonical_json(snapshot), snapshot_hash, canonical_json(result_dict),
                 result_hash, FORMULA_VERSION, actor_id, frozen_at, frozen_at,
                 batch["batch_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO carbon_batches(batch_id,version,corridor_id,period_start,period_end,"
                "baseline_id,status,revision_kind,change_note,input_snapshot_json,snapshot_hash,"
                "result_json,result_hash,formula_version,created_by,created_at,frozen_by,frozen_at,"
                "submitted_at) VALUES(?,?,?,?,?,?,'frozen',?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch["batch_id"], version, corridor_id, period_start, period_end, baseline_id,
                 revision_kind, change_note, canonical_json(snapshot), snapshot_hash,
                 canonical_json(result_dict), result_hash, FORMULA_VERSION, actor_id,
                 frozen_at, actor_id, frozen_at, frozen_at),
            )

        for usage in result_dict["certificate_usage"]:
            for allocation in usage["allocations"]:
                conn.execute(
                    "INSERT INTO carbon_allocations(allocation_id,family_batch_id,batch_id,version,"
                    "certificate_id,energy_event_id,trip_id,kwh,claimant_type,claimant_id,status,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, batch["batch_id"], batch["batch_id"], version,
                     usage["certificate_id"], allocation["event_id"],
                     event_to_trip[allocation["event_id"]], allocation["kwh"],
                     usage["claimant_type"], usage["claimant_id"], "held", frozen_at),
                )

        for trip_id in trip_ids:
            conn.execute(
                "INSERT INTO carbon_trip_locks(trip_id,family_batch_id,active_version,state) "
                "VALUES(?,?,?,'held') ON CONFLICT(trip_id) DO UPDATE SET active_version=?,state='held'",
                (trip_id, batch["batch_id"], version, version),
            )

        self._insert_evidence(conn, family=batch["batch_id"], version=version, result=result,
                              actor_id=actor_id, now=frozen_at)

        append_event(conn, actor_id=actor_id, action="carbon.batch.frozen",
                     resource_type="carbon_batch",
                     resource_id=f"{batch['batch_id']}:v{version}",
                     detail={"version": version, "revision_kind": revision_kind,
                             "snapshot_hash": snapshot_hash, "result_hash": result_hash,
                             "trips_in_scope": result.trips_in_scope,
                             "trips_excluded": result.trips_excluded,
                             "evidence_codes": list(result.evidence_codes),
                             "total_avoided_tco2": result.total_avoided_tco2},
                     occurred_at=frozen_at)
        return {"batch_id": batch["batch_id"], "version": version, "status": "frozen",
                "revision_kind": revision_kind, "snapshot_hash": snapshot_hash,
                "result_hash": result_hash,
                "total_avoided_tco2": result.total_avoided_tco2,
                "trips_in_scope": result.trips_in_scope,
                "trips_accepted": result.trips_accepted,
                "trips_excluded": result.trips_excluded,
                "evidence_open": list(result.evidence_codes),
                "prior_status": prior_status}

    def _insert_evidence(self, conn, *, family: str, version: int, result,
                         actor_id: str, now: str) -> None:
        # 重新冻结时，上一轮冻结产生的未结补证单按新输入重新生成。
        conn.execute(
            "UPDATE carbon_evidence_requests SET status='resolved',resolution_note=?,"
            "resolved_at=?,resolved_by=? WHERE family_batch_id=? AND version=? AND status='open'",
            ("批次重新冻结，按新输入重新生成补证单", now, actor_id, family, version),
        )
        # 一个（批次、行程、问题代码）只开一张补证单，多条明细合并保存。
        merged: dict[tuple[str | None, str], dict[str, Any]] = {}

        def remember(trip_id, code: str, detail: dict[str, Any]) -> None:
            key = (trip_id, code)
            if key not in merged:
                merged[key] = {"code": code, "trip_id": trip_id, "detail": dict(detail)}
            elif detail and detail != merged[key]["detail"]:
                occurrences = merged[key]["detail"].setdefault("occurrences", [])
                if not occurrences:
                    occurrences.append({k: v for k, v in merged[key]["detail"].items()
                                       if k != "occurrences"})
                occurrences.append(detail)

        for item in result.evidence_items:
            remember(item["trip_id"], item["code"], item["detail"])
        for exclusion in result.exclusions:
            if exclusion.get("code") == "SEGMENT_OUT_OF_BOUNDARY":
                codes = ["SEGMENT_OUT_OF_BOUNDARY"]
            else:
                codes = exclusion.get("codes", [])
            for code in codes:
                remember(exclusion["trip_id"], code, exclusion.get("detail", {}))
        for (trip_id, code), payload in merged.items():
            conn.execute(
                "INSERT INTO carbon_evidence_requests(request_id,family_batch_id,batch_id,version,"
                "trip_id,code,detail_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
                (uuid.uuid4().hex, family, family, version, trip_id, code,
                 canonical_json(payload["detail"]), actor_id, now),
            )

    def _validate_revision(self, conn, batch, kind: str) -> None:
        snapshot = json.loads(batch["input_snapshot_json"])
        frozen_at = snapshot["frozen_at"]
        if kind == "late_data":
            changed = conn.execute(
                "SELECT COUNT(*) AS c FROM carbon_trips WHERE corridor_id=? AND occurred_at>=? "
                "AND occurred_at<=? AND (updated_at>? OR created_at>?)",
                (batch["corridor_id"], batch["period_start"], batch["period_end"],
                 frozen_at, frozen_at),
            ).fetchone()["c"]
            new_events = conn.execute(
                "SELECT COUNT(*) AS c FROM carbon_energy_events e JOIN carbon_trips t "
                "ON e.trip_id=t.trip_id WHERE t.corridor_id=? AND t.occurred_at>=? "
                "AND t.occurred_at<=? AND e.created_at>?",
                (batch["corridor_id"], batch["period_start"], batch["period_end"], frozen_at),
            ).fetchone()["c"]
            # 与已冻结快照逐字段比对，捕获“既有补能事件补报来源”等不产生新时间戳的变化。
            snapshot_diff = 0
            live_trips = {row["trip_id"]: row for row in conn.execute(
                "SELECT * FROM carbon_trips WHERE corridor_id=? AND occurred_at>=? "
                "AND occurred_at<=?",
                (batch["corridor_id"], batch["period_start"], batch["period_end"])).fetchall()}
            for frozen_trip in snapshot["trips"]:
                live = live_trips.get(frozen_trip["trip_id"])
                if live is None:
                    continue
                if live["payload_t"] != frozen_trip["payload_t"] or \
                        live["payload_status"] != frozen_trip["payload_status"] or \
                        json.loads(live["conflict_flags_json"]) != frozen_trip["conflict_flags"]:
                    snapshot_diff += 1
                live_events = conn.execute(
                    "SELECT * FROM carbon_energy_events WHERE trip_id=?",
                    (frozen_trip["trip_id"],)).fetchall()
                frozen_events = {item["event_id"]: item
                                 for item in frozen_trip["energy_events"]}
                for event in live_events:
                    prior = frozen_events.get(event["event_id"])
                    if prior is None or event["source_status"] != prior["source_status"]:
                        snapshot_diff += 1
            if changed == 0 and new_events == 0 and snapshot_diff == 0:
                raise ValidationError("没有检测到冻结后的迟到数据，不能以迟到数据为由重述")
        elif kind == "factor_revision":
            changed_factors = []
            for key, info in snapshot["factors"].items():
                latest = self._resolve_factor(conn, key, self._now())
                if latest is None or latest["version"] != info["version"]:
                    changed_factors.append(key)
            if not changed_factors:
                raise ValidationError("没有检测到因子版本变化，不能以因子修订为由重述")
        elif kind == "certificate_revocation":
            used = {row["certificate_id"] for row in conn.execute(
                "SELECT DISTINCT certificate_id FROM carbon_allocations WHERE family_batch_id=? "
                "AND version=?", (batch["batch_id"], batch["version"])).fetchall()}
            withdrawn = {row["certificate_id"] for row in conn.execute(
                "SELECT certificate_id FROM carbon_certificates WHERE status='withdrawn'").fetchall()}
            if not (used & withdrawn):
                raise ValidationError("本批次占用的凭证均未撤回，不能以凭证撤回为由重述")

    def _release_family(self, conn, family: str, version: int) -> None:
        conn.execute(
            "UPDATE carbon_allocations SET status='released' WHERE family_batch_id=? AND version=? "
            "AND status='held'",
            (family, version),
        )
        conn.execute("DELETE FROM carbon_trip_locks WHERE family_batch_id=? AND active_version=?",
                     (family, version))

    def _restore_prior_publication(self, conn, family: str, prior_version: int) -> None:
        """重述版本被驳回时，恢复此前已发布版本的效力（吊销情形除外）。"""

        prior = conn.execute(
            "SELECT status,input_snapshot_json FROM carbon_batches WHERE batch_id=? AND version=?",
            (family, prior_version),
        ).fetchone()
        if prior is None or prior["status"] != "restated":
            return
        conn.execute(
            "UPDATE carbon_batches SET status='published' WHERE batch_id=? AND version=?",
            (family, prior_version),
        )
        conn.execute(
            "UPDATE carbon_allocations SET status='committed' WHERE family_batch_id=? "
            "AND version=? AND status='superseded'",
            (family, prior_version),
        )
        prior_trip_ids = [trip["trip_id"] for trip in
                          json.loads(prior["input_snapshot_json"])["trips"]]
        for trip_id in prior_trip_ids:
            conn.execute(
                "INSERT INTO carbon_trip_locks(trip_id,family_batch_id,active_version,state) "
                "VALUES(?,?,?,'committed') ON CONFLICT(trip_id) DO UPDATE SET "
                "family_batch_id=excluded.family_batch_id,active_version=excluded.active_version,"
                "state='committed'",
                (trip_id, family, prior_version),
            )

    def _supersede_family(self, conn, family: str, version: int) -> None:
        conn.execute(
            "UPDATE carbon_allocations SET status='superseded' WHERE family_batch_id=? AND version=?",
            (family, version),
        )

    # -- 小型工具 ----------------------------------------------------------

    def _clean_segments(self, segments: Any) -> list[dict[str, Any]]:
        if not isinstance(segments, list) or not segments:
            raise ValidationError("segments 必须是非空数组")
        clean = []
        seen = set()
        for item in segments:
            if not isinstance(item, dict):
                raise ValidationError("路段必须是对象")
            seg_id = self._identifier(item.get("segment_id", ""), "segment_id")
            if seg_id in seen:
                raise ValidationError(f"路段编号重复：{seg_id}")
            seen.add(seg_id)
            clean.append({
                "segment_id": seg_id,
                "name": self._text(item.get("name", ""), "segment.name"),
                "origin": self._text(item.get("origin", ""), "segment.origin"),
                "destination": self._text(item.get("destination", ""), "segment.destination"),
                "distance_km": self._positive(item.get("distance_km"), "segment.distance_km"),
            })
        return clean

    def _period(self, start: str, end: str) -> tuple[str, str]:
        start_iso = self._instant(start, "period_start")
        end_iso = self._instant(end, "period_end")
        if parse_ts(start_iso) > parse_ts(end_iso):
            raise ValidationError("开始时间不能晚于结束时间")
        return start_iso, end_iso

    def _instant(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} 必须是 ISO 时间字符串")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _positive(self, value: Any, field: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} 必须是正数")
        return float(value)

    def _non_negative(self, value: Any, field: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"{field} 必须是非负数")
        return float(value)

    def _require_corridor(self, conn, corridor_id: str) -> None:
        if conn.execute("SELECT 1 FROM carbon_corridors WHERE corridor_id=?",
                        (corridor_id,)).fetchone() is None:
            raise NotFoundError("走廊不存在")

    def _require_trip(self, conn, trip_id: str):
        row = conn.execute("SELECT * FROM carbon_trips WHERE trip_id=?", (trip_id,)).fetchone()
        if row is None:
            raise NotFoundError("行程不存在")
        return row

    def _trip_lock(self, conn, trip_id: str):
        return conn.execute("SELECT * FROM carbon_trip_locks WHERE trip_id=?",
                            (trip_id,)).fetchone()

    def _require_open_batch(self, conn, batch_id: str):
        batch = self._require_latest_batch(conn, batch_id)
        return batch

    def _require_latest_batch(self, conn, batch_id: str):
        row = conn.execute(
            "SELECT * FROM carbon_batches WHERE batch_id=? ORDER BY version DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def _require_batch_version(self, conn, batch_id: str, version: int):
        row = conn.execute(
            "SELECT * FROM carbon_batches WHERE batch_id=? AND version=?", (batch_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("批次版本不存在")
        return row

    def _next_version(self, conn, table: str, where: tuple[str, str]) -> int:
        row = conn.execute(
            f"SELECT COALESCE(MAX(version),0)+1 AS v FROM {table} WHERE {where[0]}=?",
            (where[1],),
        ).fetchone()
        return int(row["v"])

    def _latest_version_row(self, conn, table: str, where: tuple[str, str]):
        return conn.execute(
            f"SELECT * FROM {table} WHERE {where[0]}=? ORDER BY version DESC LIMIT 1",
            (where[1],),
        ).fetchone()

    def _resolve_factor(self, conn, factor_key: str, at_iso: str):
        row = conn.execute(
            "SELECT * FROM emission_factor_versions WHERE factor_key=? AND valid_from<=? "
            "AND valid_to>=? ORDER BY version DESC LIMIT 1",
            (factor_key, at_iso, at_iso),
        ).fetchone()
        if row is not None:
            return row
        return conn.execute(
            "SELECT * FROM emission_factor_versions WHERE factor_key=? ORDER BY version DESC LIMIT 1",
            (factor_key,),
        ).fetchone()
