"""走廊碳核算与核验领域服务。

职责边界：
- 路段边界、车辆与能源类型、排放因子、基准方案、证据政策、走廊目标
  一律按版本只追加保存；
- 核算批次冻结原始输入（行程、载货量、补能记录、双侧电力声明）、
  主数据精确版本与凭证快照，形成 manifest/result 双摘要；
- 缺失或冲突证据进入补证清单，行程减排不予计入（不按零排放处理）；
- 绿证双侧声明匹配后只占用一次容量，冻结预留、发布结清；
- 独立核验人复算通过且无阻断性发现后才能发布；
- 迟到数据、因子修订、凭证撤回只生成重述或吊销版本，
  历史已发布报告始终保留并可按原输入复算。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from transport_coordination.audit import append_event, canonical_json, digest, verify_chain
from transport_coordination.clock import Clock, SystemClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.models import Actor

from . import rules


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

VERSIONED_TABLES: dict[str, tuple[str, ...]] = {
    # 表名 -> (编号列, 走廊列或 None)
    "corridor_segments": ("segment_id", "corridor_id"),
    "vehicle_energy_types": ("energy_type_id", None),
    "emission_factors": ("factor_id", None),
    "baselines": ("baseline_id", "corridor_id"),
    "evidence_policies": ("policy_id", None),
    "corridor_targets": ("target_id", "corridor_id"),
}


class CarbonService:
    """协调版本化主数据、证据闸门、凭证占用、核验与发布。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: str, field: str) -> str:
        text = self._text(value, field, 40)
        try:
            return rules.parse_time(text).date().isoformat()
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc

    def _number(self, value: Any, field: str, *, minimum: float | None = None) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数字") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise ValidationError(f"{field} 必须是有限数字")
        if minimum is not None and number < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        return number

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        receipt_base = {"request_id": request_id}
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**receipt_base, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {**receipt_base, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _append_version(self, connection, table: str, identity: str, columns: dict[str, Any],
                        corridor_id: str | None) -> int:
        """在只追加版本表中写入新版本并把上一版标记为 superseded。"""

        id_column = VERSIONED_TABLES[table][0]
        row = connection.execute(
            f"SELECT version FROM {table} WHERE {id_column}=? ORDER BY version DESC LIMIT 1",
            (identity,),
        ).fetchone()
        version = (row["version"] + 1) if row else 1
        now = self._now()
        all_columns = {id_column: identity, "version": version, "status": "active",
                       "effective_to": None, "superseded_by": None, "created_at": now, **columns}
        names = ", ".join(all_columns)
        marks = ", ".join(f":{key}" for key in all_columns)
        connection.execute(f"INSERT INTO {table}({names}) VALUES({marks})", all_columns)
        if row:
            connection.execute(
                f"UPDATE {table} SET status='superseded', effective_to=?, superseded_by=? "
                f"WHERE {id_column}=? AND version=?",
                (columns["effective_from"], version, identity, row["version"]),
            )
        return version

    def _active(self, connection, table: str, identity: str):
        id_column = VERSIONED_TABLES[table][0]
        return connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=? AND status='active' ORDER BY version DESC LIMIT 1",
            (identity,),
        ).fetchone()

    def _exact_version(self, connection, table: str, identity: str, version: int):
        id_column = VERSIONED_TABLES[table][0]
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=? AND version=?", (identity, version)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{table} {identity} 版本 {version} 不存在")
        return row

    def _versioned_write(self, *, request_id: str, actor_id: str, action: str, resource_type: str,
                         table: str, identity_field: str, identity: str,
                         columns: dict[str, Any], audit_detail: dict[str, Any]):
        payload = {"actor_id": actor_id, "identity": identity, "columns": columns}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            identity = self._id(identity, identity_field)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = self._append_version(connection, table, identity, columns, None)
                self._audit(connection, actor_id=actor_id, action=action,
                            resource_type=resource_type, resource_id=identity,
                            detail={**audit_detail, "version": version})
                response = {"id": identity, "version": version}
                return resource_type, f"{identity}:v{version}", response

            return self._idempotent(connection, request_id=request_id, action=action,
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 主数据版本登记
    # ------------------------------------------------------------------

    def register_segment_version(self, *, request_id: str, actor_id: str, segment_id: str,
                                 corridor_id: str, name: str, length_km: float,
                                 boundary: list[Any], effective_from: str | None = None):
        segment_id = self._id(segment_id, "segment_id")
        corridor_id = self._id(corridor_id, "corridor_id")
        name = self._text(name, "name")
        length_km = self._number(length_km, "length_km", minimum=0.000001)
        if not isinstance(boundary, list) or not boundary:
            raise ValidationError("boundary 必须是非空数组")
        columns = {
            "corridor_id": corridor_id, "name": name, "length_km": length_km,
            "boundary_json": canonical_json(boundary),
            "effective_from": self._date(effective_from or self._now(), "effective_from"),
            "created_by": actor_id,
        }
        return self._versioned_write(
            request_id=request_id, actor_id=actor_id, action="segment.version_registered",
            resource_type="segment", table="corridor_segments", identity_field="segment_id",
            identity=segment_id, columns=columns,
            audit_detail={"corridor_id": corridor_id, "name": name, "length_km": length_km},
        )

    def register_energy_type_version(self, *, request_id: str, actor_id: str, energy_type_id: str,
                                     code: str, name: str, energy_carrier: str,
                                     grid_ef_kg_per_kwh: float | None = None,
                                     fuel_intensity_l_per_km: float | None = None,
                                     fuel_ef_kg_per_l: float | None = None,
                                     effective_from: str | None = None):
        energy_type_id = self._id(energy_type_id, "energy_type_id")
        code = self._id(code, "code")
        name = self._text(name, "name")
        if energy_carrier not in ("electricity", "diesel", "hydrogen", "lng", "other"):
            raise ValidationError("energy_carrier 不在允许范围内")
        columns: dict[str, Any] = {
            "code": code, "name": name, "energy_carrier": energy_carrier,
            "energy_intensity_kwh_per_km": None,
            "grid_ef_kg_per_kwh": (None if grid_ef_kg_per_kwh is None
                                   else self._number(grid_ef_kg_per_kwh, "grid_ef_kg_per_kwh", minimum=0)),
            "fuel_intensity_l_per_km": (None if fuel_intensity_l_per_km is None
                                        else self._number(fuel_intensity_l_per_km, "fuel_intensity_l_per_km", minimum=0)),
            "fuel_ef_kg_per_l": (None if fuel_ef_kg_per_l is None
                                 else self._number(fuel_ef_kg_per_l, "fuel_ef_kg_per_l", minimum=0)),
            "effective_from": self._date(effective_from or self._now(), "effective_from"),
            "created_by": actor_id,
        }
        return self._versioned_write(
            request_id=request_id, actor_id=actor_id, action="energy_type.version_registered",
            resource_type="energy_type", table="vehicle_energy_types",
            identity_field="energy_type_id", identity=energy_type_id, columns=columns,
            audit_detail={"code": code, "energy_carrier": energy_carrier},
        )

    def register_factor_version(self, *, request_id: str, actor_id: str, factor_id: str,
                                factor_key: str, name: str, unit: str, value: float,
                                effective_from: str | None = None):
        factor_id = self._id(factor_id, "factor_id")
        factor_key = self._id(factor_key, "factor_key")
        name = self._text(name, "name")
        unit = self._text(unit, "unit", 40)
        value = self._number(value, "value", minimum=0)
        columns = {
            "factor_key": factor_key, "name": name, "unit": unit, "value": value,
            "effective_from": self._date(effective_from or self._now(), "effective_from"),
            "created_by": actor_id,
        }
        return self._versioned_write(
            request_id=request_id, actor_id=actor_id, action="factor.version_registered",
            resource_type="emission_factor", table="emission_factors",
            identity_field="factor_id", identity=factor_id, columns=columns,
            audit_detail={"factor_key": factor_key, "value": value},
        )

    def register_baseline_version(self, *, request_id: str, actor_id: str, baseline_id: str,
                                  corridor_id: str, name: str, baseline_ef_kg_per_km: float,
                                  load_correction: dict[str, Any] | None = None,
                                  effective_from: str | None = None):
        baseline_id = self._id(baseline_id, "baseline_id")
        corridor_id = self._id(corridor_id, "corridor_id")
        name = self._text(name, "name")
        baseline_ef = self._number(baseline_ef_kg_per_km, "baseline_ef_kg_per_km", minimum=0.000001)
        correction = load_correction or {"method": "none"}
        if correction.get("method") == "payload_ratio":
            reference = self._number(correction.get("reference_load_t"), "reference_load_t", minimum=0.000001)
            correction = {**correction, "reference_load_t": reference}
            self._number(correction.get("min_factor", 0.0), "min_factor", minimum=0)
        elif correction.get("method") not in (None, "none"):
            raise ValidationError("load_correction.method 仅支持 none/payload_ratio")
        columns = {
            "corridor_id": corridor_id, "name": name, "baseline_ef_kg_per_km": baseline_ef,
            "load_correction_json": canonical_json(correction),
            "effective_from": self._date(effective_from or self._now(), "effective_from"),
            "created_by": actor_id,
        }
        return self._versioned_write(
            request_id=request_id, actor_id=actor_id, action="baseline.version_registered",
            resource_type="baseline", table="baselines", identity_field="baseline_id",
            identity=baseline_id, columns=columns,
            audit_detail={"corridor_id": corridor_id, "baseline_ef_kg_per_km": baseline_ef},
        )

    def register_policy_version(self, *, request_id: str, actor_id: str, policy_id: str,
                                name: str, rules_data: dict[str, Any] | None = None,
                                before_grace_days: int = 2,
                                effective_from: str | None = None):
        policy_id = self._id(policy_id, "policy_id")
        name = self._text(name, "name")
        merged = json.loads(canonical_json(rules.DEFAULT_POLICY["rules"]))
        if rules_data:
            for kind, rule in rules_data.items():
                if kind not in rules.EVIDENCE_KINDS or not isinstance(rule, dict):
                    raise ValidationError(f"证据种类 {kind} 不受支持")
                merged[kind].update(rule)
        for kind in rules.EVIDENCE_KINDS:
            days = merged[kind].get("validity_days")
            if days is not None and (not isinstance(days, (int, float)) or days < 0):
                raise ValidationError(f"{kind} 的 validity_days 无效")
            if not isinstance(merged[kind].get("required"), bool):
                raise ValidationError(f"{kind} 的 required 必须是布尔值")
        grace = int(before_grace_days)
        if grace < 0:
            raise ValidationError("before_grace_days 不能为负")
        columns = {
            "name": name, "rules_json": canonical_json(merged), "before_grace_days": grace,
            "effective_from": self._date(effective_from or self._now(), "effective_from"),
            "created_by": actor_id,
        }
        return self._versioned_write(
            request_id=request_id, actor_id=actor_id, action="policy.version_registered",
            resource_type="evidence_policy", table="evidence_policies",
            identity_field="policy_id", identity=policy_id, columns=columns,
            audit_detail={"name": name, "before_grace_days": grace},
        )

    def register_target_version(self, *, request_id: str, actor_id: str, target_id: str,
                                corridor_id: str, period_start: str, period_end: str,
                                target_tco2: float, metric: str = "published",
                                effective_from: str | None = None):
        target_id = self._id(target_id, "target_id")
        corridor_id = self._id(corridor_id, "corridor_id")
        period_start = self._date(period_start, "period_start")
        period_end = self._date(period_end, "period_end")
        if period_end < period_start:
            raise ValidationError("目标周期结束早于开始")
        target_tco2 = self._number(target_tco2, "target_tco2", minimum=0)
        if metric not in ("verified", "published"):
            raise ValidationError("metric 仅支持 verified/published")
        columns = {
            "corridor_id": corridor_id, "period_start": period_start, "period_end": period_end,
            "target_tco2": target_tco2, "metric": metric,
            "effective_from": self._date(effective_from or self._now(), "effective_from"),
            "created_by": actor_id,
        }
        return self._versioned_write(
            request_id=request_id, actor_id=actor_id, action="target.version_registered",
            resource_type="corridor_target", table="corridor_targets",
            identity_field="target_id", identity=target_id, columns=columns,
            audit_detail={"corridor_id": corridor_id, "target_tco2": target_tco2, "metric": metric},
        )

    # ------------------------------------------------------------------
    # 绿色电力凭证登记与撤回
    # ------------------------------------------------------------------

    def register_certificate(self, *, request_id: str, actor_id: str, certificate_id: str,
                             batch_no: str, energy_kwh: float, valid_from: str, valid_to: str):
        payload = {"actor_id": actor_id, "certificate_id": certificate_id, "batch_no": batch_no,
                   "energy_kwh": energy_kwh, "valid_from": valid_from, "valid_to": valid_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            certificate_id = self._id(certificate_id, "certificate_id")
            batch_no = self._text(batch_no, "batch_no", 80)
            energy_kwh = self._number(energy_kwh, "energy_kwh", minimum=0.000001)
            valid_from = self._date(valid_from, "valid_from")
            valid_to = self._date(valid_to, "valid_to")
            if valid_to < valid_from:
                raise ValidationError("凭证有效期结束早于开始")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO green_certificates(certificate_id,batch_no,energy_kwh,"
                        "valid_from,valid_to,status,registered_by,registered_at) "
                        "VALUES(?,?,?,?,?,'available',?,?)",
                        (certificate_id, batch_no, energy_kwh, valid_from, valid_to,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("凭证编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="certificate.registered",
                            resource_type="green_certificate", resource_id=certificate_id,
                            detail={"batch_no": batch_no, "energy_kwh": energy_kwh,
                                    "valid_from": valid_from, "valid_to": valid_to})
                response = {"certificate_id": certificate_id, "status": "available"}
                return "green_certificate", certificate_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="register_certificate", payload=payload, create=create)

    def withdraw_certificate(self, *, request_id: str, actor_id: str,
                             certificate_id: str, reason: str) -> dict[str, Any]:
        """撤回凭证。已发布报告不会被静默改写，只列出其影响供重述/吊销。"""

        reason = self._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "certificate_id": certificate_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            row = connection.execute(
                "SELECT * FROM green_certificates WHERE certificate_id=?", (certificate_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("凭证不存在")
            if row["status"] == "withdrawn":
                raise ConflictError("凭证已经撤回")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE green_certificates SET status='withdrawn', withdrawn_at=?, withdraw_reason=? "
                    "WHERE certificate_id=?",
                    (self._now(), reason, certificate_id),
                )
                impact = self._certificate_impact(connection, certificate_id)
                self._audit(connection, actor_id=actor_id, action="certificate.withdrawn",
                            resource_type="green_certificate", resource_id=certificate_id,
                            detail={"reason": reason, "impact": impact})
                response = {"certificate_id": certificate_id, "status": "withdrawn", "impact": impact}
                return "green_certificate", certificate_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_certificate", payload=payload, create=create)

    def _certificate_impact(self, connection, certificate_id: str) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT batch_id, scope_key, version, state, energy_kwh FROM certificate_claims "
            "WHERE certificate_id=? AND state!='released' ORDER BY scope_key, version",
            (certificate_id,),
        ).fetchall()
        return {"active_claims": [dict(row) for row in rows],
                "published_batch_ids": [row["batch_id"] for row in rows if row["state"] == "settled"]}

    # ------------------------------------------------------------------
    # 批次冻结
    # ------------------------------------------------------------------

    def _scope_key(self, corridor_id: str, period_start: str, period_end: str, scope_tag: str) -> str:
        return f"{corridor_id}|{period_start}|{period_end}|{scope_tag}"

    def _load_policy(self, connection, policy_id: str | None):
        if policy_id:
            row = self._active(connection, "evidence_policies", policy_id)
            if row is None:
                raise NotFoundError("证据政策不存在或尚无生效版本")
        else:
            row = connection.execute(
                "SELECT * FROM evidence_policies WHERE status='active' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise NotFoundError("尚未登记证据政策版本，无法冻结批次")
        return row

    def _load_baseline(self, connection, corridor_id: str, baseline_id: str | None):
        if baseline_id:
            row = self._active(connection, "baselines", baseline_id)
            if row is None:
                raise NotFoundError("基准方案不存在或尚无生效版本")
            if row["corridor_id"] != corridor_id:
                raise ValidationError("基准方案不属于该走廊")
        else:
            row = connection.execute(
                "SELECT * FROM baselines WHERE corridor_id=? AND status='active' "
                "ORDER BY version DESC LIMIT 1",
                (corridor_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("该走廊尚无生效基准方案版本")
        return row

    def _load_target(self, connection, corridor_id: str, period_start: str, period_end: str,
                     target_id: str | None):
        if target_id is not None:
            row = self._active(connection, "corridor_targets", target_id)
            if row is None:
                raise NotFoundError("目标不存在或尚无生效版本")
            return row
        return connection.execute(
            "SELECT * FROM corridor_targets WHERE corridor_id=? AND status='active' "
            "AND period_start<=? AND period_end>=? ORDER BY version DESC LIMIT 1",
            (corridor_id, period_start, period_end),
        ).fetchone()

    def _build_snapshot(self, connection, *, policy_row, baseline_row, trips: list[dict[str, Any]]):
        """根据行程实际引用解析主数据精确版本并构建复算快照。"""

        segment_ids: set[str] = set()
        energy_type_ids: set[str] = set()
        for trip in trips:
            energy_type_ids.add(trip["energy_type_id"])
            for leg in trip.get("segment_legs") or []:
                segment_ids.add(leg["segment_id"])

        segment_rows = {}
        for segment_id in sorted(segment_ids):
            row = self._active(connection, "corridor_segments", segment_id)
            if row is None:
                raise NotFoundError(f"路段边界 {segment_id} 尚无生效版本")
            segment_rows[segment_id] = row
        energy_rows = {}
        for energy_type_id in sorted(energy_type_ids):
            row = self._active(connection, "vehicle_energy_types", energy_type_id)
            if row is None:
                raise NotFoundError(f"车辆能源类型 {energy_type_id} 尚无生效版本")
            energy_rows[energy_type_id] = row
        factor_rows = connection.execute(
            "SELECT * FROM emission_factors WHERE status='active' ORDER BY factor_key, version DESC"
        ).fetchall()
        factors_by_key: dict[str, Any] = {}
        for row in factor_rows:
            factors_by_key.setdefault(row["factor_key"], row)

        master_versions = {
            "policy": {"policy_id": policy_row["policy_id"], "version": policy_row["version"]},
            "baseline": {"baseline_id": baseline_row["baseline_id"], "version": baseline_row["version"]},
            "segments": {sid: row["version"] for sid, row in segment_rows.items()},
            "energy_types": {eid: row["version"] for eid, row in energy_rows.items()},
            "factors": {row["factor_key"]: {"factor_id": row["factor_id"], "version": row["version"],
                                            "value": row["value"]}
                        for row in factors_by_key.values()},
        }
        snapshot = {
            "policy": {"rules": json.loads(policy_row["rules_json"]),
                       "before_grace_days": policy_row["before_grace_days"]},
            "segments": {sid: {"segment_id": sid, "version": row["version"],
                               "length_km": row["length_km"], "boundary": json.loads(row["boundary_json"])}
                         for sid, row in segment_rows.items()},
            "energy_types": {eid: {"energy_carrier": row["energy_carrier"],
                                   "grid_ef_kg_per_kwh": row["grid_ef_kg_per_kwh"],
                                   "fuel_intensity_l_per_km": row["fuel_intensity_l_per_km"],
                                   "fuel_ef_kg_per_l": row["fuel_ef_kg_per_l"]}
                             for eid, row in energy_rows.items()},
            "factors": {key: {"value": row["value"]} for key, row in factors_by_key.items()},
            "baseline": {"baseline_ef_kg_per_km": baseline_row["baseline_ef_kg_per_km"],
                         "load_correction": json.loads(baseline_row["load_correction_json"])},
        }
        return master_versions, snapshot

    def _referenced_certificates(self, trips: list[dict[str, Any]]) -> set[str]:
        certs: set[str] = set()
        for trip in trips:
            for record in trip.get("energy_records") or []:
                if record.get("certificate_id"):
                    certs.add(record["certificate_id"])
            for declaration in trip.get("declarations") or []:
                if declaration.get("certificate_id"):
                    certs.add(declaration["certificate_id"])
        return certs

    def _validate_trip_input(self, trip: dict[str, Any], period_start: str, period_end: str) -> None:
        for field in ("trip_id", "vehicle_id", "energy_type_id", "started_at", "completed_at"):
            if not trip.get(field):
                raise ValidationError(f"行程缺少 {field}")
        self._id(trip["trip_id"], "trip_id")
        started = self._date(trip["started_at"], "started_at")
        completed = self._date(trip["completed_at"], "completed_at")
        if completed < started:
            raise ValidationError(f"行程 {trip['trip_id']} 结束时间早于开始时间")
        if started < period_start or completed > period_end:
            raise ValidationError(f"行程 {trip['trip_id']} 超出核算周期")
        legs = trip.get("segment_legs") or []
        if not isinstance(legs, list) or not legs:
            raise ValidationError(f"行程 {trip['trip_id']} 缺少路段边界 leg")
        for leg in legs:
            self._number(leg.get("distance_km"), "segment_leg.distance_km", minimum=0.000001)
        for record in trip.get("energy_records") or []:
            self._number(record.get("energy_kwh"), "energy_record.energy_kwh", minimum=0.000001)
            if not record.get("record_id") or not record.get("occurred_at"):
                raise ValidationError("补能记录缺少 record_id/occurred_at")
        for declaration in trip.get("declarations") or []:
            self._number(declaration.get("energy_kwh"), "declaration.energy_kwh", minimum=0.000001)
            if not declaration.get("certificate_id"):
                raise ValidationError("车队电力声明缺少 certificate_id")
        payload = trip.get("payload")
        if payload is not None:
            if not isinstance(payload, dict) or payload.get("load_t") is None:
                raise ValidationError(f"行程 {trip['trip_id']} 载货量记录不完整")
            self._number(payload.get("load_t"), "payload.load_t", minimum=0)

    def freeze_batch(self, *, request_id: str, actor_id: str, corridor_id: str,
                     period_start: str, period_end: str, trips: list[dict[str, Any]],
                     scope_tag: str = "default", policy_id: str | None = None,
                     baseline_id: str | None = None, target_id: str | None = None,
                     _parent_batch_id: str | None = None, _restate_reason: str | None = None) -> dict[str, Any]:
        """冻结一个核算批次（初版或重述版）。"""

        corridor_id = self._id(corridor_id, "corridor_id")
        period_start = self._date(period_start, "period_start")
        period_end = self._date(period_end, "period_end")
        if period_end < period_start:
            raise ValidationError("核算周期结束早于开始")
        scope_tag = self._id(scope_tag, "scope_tag")
        if not isinstance(trips, list) or not trips:
            raise ValidationError("trips 必须是非空数组")
        payload = {"actor_id": actor_id, "corridor_id": corridor_id, "period_start": period_start,
                   "period_end": period_end, "scope_tag": scope_tag, "trips": trips,
                   "policy_id": policy_id, "baseline_id": baseline_id, "target_id": target_id,
                   "parent_batch_id": _parent_batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            scope_key = self._scope_key(corridor_id, period_start, period_end, scope_tag)

            parent_row = None
            if _parent_batch_id:
                parent_row = connection.execute(
                    "SELECT * FROM accounting_batches WHERE batch_id=?", (_parent_batch_id,)
                ).fetchone()
                if parent_row is None:
                    raise NotFoundError("被重述批次不存在")
                if parent_row["scope_key"] != scope_key:
                    raise ValidationError("重述批次与原批次的走廊/周期/口径不一致")
                if parent_row["status"] not in ("frozen", "verified", "published", "revoked"):
                    raise ConflictError("只能重述该口径下的最新批次版本")
                latest = connection.execute(
                    "SELECT batch_id, status FROM accounting_batches WHERE scope_key=? "
                    "ORDER BY version DESC LIMIT 1", (scope_key,)
                ).fetchone()
                if latest["batch_id"] != _parent_batch_id:
                    raise ConflictError("只能重述该口径下的最新批次版本")
                version = parent_row["version"] + 1
            else:
                version = 1

            for trip in trips:
                self._validate_trip_input(trip, period_start, period_end)
            if len({trip["trip_id"] for trip in trips}) != len(trips):
                raise ValidationError("行程 trip_id 重复")

            policy_row = self._load_policy(connection, policy_id)
            baseline_row = self._load_baseline(connection, corridor_id, baseline_id)
            target_row = self._load_target(connection, corridor_id, period_start, period_end, target_id)

            master_versions, snapshot = self._build_snapshot(
                connection, policy_row=policy_row, baseline_row=baseline_row, trips=trips)

            cert_ids = self._referenced_certificates(trips)
            cert_rows = connection.execute(
                "SELECT * FROM green_certificates WHERE certificate_id IN (%s)"
                % ",".join("?" * len(cert_ids)), tuple(cert_ids),
            ).fetchall() if cert_ids else []
            certificates = {row["certificate_id"]: self._cert_dict(row) for row in cert_rows}
            cert_snapshot = dict(sorted(certificates.items()))

            # 按 trip_id 排序保证冻结与复算的结果摘要与输入顺序无关
            trip_results = sorted(
                (rules.evaluate_trip(trip, snapshot, certificates) for trip in trips),
                key=lambda item: item["trip_id"],
            )
            result = rules.summarize(trip_results)
            result_by_trip = {item["trip_id"]: item for item in trip_results}

            manifest = {
                "corridor_id": corridor_id, "period_start": period_start, "period_end": period_end,
                "scope_tag": scope_tag, "version": version, "trips": trips,
                "master_versions": master_versions, "certificates": cert_snapshot,
            }
            manifest_hash = digest(manifest)
            result_hash = digest({"manifest_hash": manifest_hash, "result": result,
                                  "trips": trip_results})

            batch_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                if version == 1 and connection.execute(
                    "SELECT 1 FROM accounting_batches WHERE scope_key=? AND version=1", (scope_key,)
                ).fetchone():
                    raise ConflictError("该走廊周期口径已存在批次，请使用重述版本")
                connection.execute(
                    "INSERT INTO accounting_batches(batch_id,corridor_id,period_start,period_end,"
                    "scope_key,scope_tag,version,parent_batch_id,status,policy_id,policy_version,"
                    "baseline_id,baseline_version,target_id,target_version,master_versions_json,"
                    "certificate_snapshot_json,frozen_by,frozen_at,restate_reason,manifest_hash,"
                    "result_hash,result_json) VALUES(?,?,?,?,?,?,?,?,'frozen',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, corridor_id, period_start, period_end, scope_key, scope_tag, version,
                     _parent_batch_id, policy_row["policy_id"], policy_row["version"],
                     baseline_row["baseline_id"], baseline_row["version"],
                     target_row["target_id"] if target_row else None,
                     target_row["version"] if target_row else None,
                     canonical_json(master_versions), canonical_json(cert_snapshot),
                     actor_id, self._now(), _restate_reason, manifest_hash, result_hash,
                     canonical_json(result)),
                )
                for trip in trips:
                    item = result_by_trip[trip["trip_id"]]
                    energy_type_row = self._active(connection, "vehicle_energy_types", trip["energy_type_id"])
                    legs = trip.get("segment_legs") or []
                    payload = trip.get("payload")
                    records = trip.get("energy_records") or []
                    declarations = trip.get("declarations") or []
                    connection.execute(
                        "INSERT INTO batch_trips(uid,batch_id,trip_id,vehicle_id,energy_type_id,"
                        "energy_type_version,started_at,completed_at,logged_at,segment_legs_json,"
                        "distance_km,payload_json,payload_status,energy_records_json,energy_status,"
                        "declarations_json,evidence_valid_json,claim_status,verification_status,"
                        "pending_reasons_json,baseline_emissions_kg,actual_emissions_kg,reductions_kg,"
                        "calc_detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, batch_id, trip["trip_id"], trip["vehicle_id"],
                         trip["energy_type_id"], energy_type_row["version"], trip["started_at"],
                         trip["completed_at"], trip.get("logged_at"), canonical_json(legs),
                         item["distance_km"], canonical_json(payload or {}), item["payload_status"],
                         canonical_json(records), item["energy_status"], canonical_json(declarations),
                         canonical_json(item["evidence_valid"]), item["claim_status"],
                         item["verification_status"], canonical_json(item["pending_reasons"]),
                         item["baseline_emissions_kg"], item["actual_emissions_kg"],
                         item["reductions_kg"], canonical_json(item["calc_detail"])),
                    )
                self._reserve_claims(connection, batch_id=batch_id, scope_key=scope_key,
                                     version=version, trips=trips, result_by_trip=result_by_trip,
                                     certificates=certificates)
                if parent_row is not None and parent_row["status"] in ("frozen", "verified"):
                    # 尚未对外发布的旧版本立即被取代并释放预留
                    connection.execute(
                        "UPDATE accounting_batches SET status='restated' WHERE batch_id=?",
                        (_parent_batch_id,),
                    )
                    connection.execute(
                        "UPDATE certificate_claims SET state='released', updated_at=? "
                        "WHERE batch_id=? AND state='reserved'",
                        (self._now(), _parent_batch_id),
                    )
                self._audit(connection, actor_id=actor_id, action="batch.frozen",
                            resource_type="accounting_batch", resource_id=batch_id,
                            detail={"scope_key": scope_key, "version": version,
                                    "parent_batch_id": _parent_batch_id,
                                    "manifest_hash": manifest_hash, "result_hash": result_hash,
                                    "valid_trips": result["valid_trip_count"],
                                    "pending_trips": result["pending_trip_count"],
                                    "reductions_tco2": result["reductions_tco2"]})
                response = {"batch_id": batch_id, "scope_key": scope_key, "version": version,
                            "status": "frozen", "manifest_hash": manifest_hash,
                            "result_hash": result_hash, "result": result}
                return "accounting_batch", batch_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="restate_batch" if _parent_batch_id else "freeze_batch",
                                    payload=payload, create=create)

    def _cert_dict(self, row) -> dict[str, Any]:
        return {"certificate_id": row["certificate_id"], "batch_no": row["batch_no"],
                "energy_kwh": row["energy_kwh"], "valid_from": row["valid_from"],
                "valid_to": row["valid_to"], "status": row["status"]}

    def _reserve_claims(self, connection, *, batch_id: str, scope_key: str, version: int,
                        trips: list[dict[str, Any]], result_by_trip: dict[str, dict[str, Any]],
                        certificates: dict[str, dict[str, Any]]) -> None:
        """双侧匹配的绿证只占用一次容量；占用量不得超过凭证容量或既有占用。"""

        wanted: dict[str, float] = {}
        claimants: dict[str, str] = {}
        for trip in trips:
            item = result_by_trip[trip["trip_id"]]
            if item["verification_status"] != "valid":
                continue
            for cert_id, kwh in item["matched_kwh_by_cert"].items():
                wanted[cert_id] = wanted.get(cert_id, 0.0) + kwh
                station_ids = sorted({r.get("station_id", "?") for r in trip.get("energy_records") or []
                                      if r.get("certificate_id") == cert_id})
                fleet_ids = sorted({d.get("fleet_id", "?") for d in trip.get("declarations") or []
                                    if d.get("certificate_id") == cert_id})
                claimants[cert_id] = f"fleet:{'+'.join(fleet_ids)}|station:{'+'.join(station_ids)}"
        now = self._now()
        for cert_id, kwh in wanted.items():
            cert = certificates.get(cert_id)
            if cert is None or cert["status"] == "withdrawn":
                raise ConflictError(f"凭证 {cert_id} 不可用，匹配行程不能占用")
            used = connection.execute(
                "SELECT COALESCE(SUM(energy_kwh),0) AS total FROM certificate_claims "
                "WHERE certificate_id=? AND state!='released' AND scope_key!=?",
                (cert_id, scope_key),
            ).fetchone()["total"]
            # 同批次族（scope_key）的旧版本占用不计入容量：它与本版本互斥，
            # 新版本发布时旧占用会在同一事务内释放，杜绝跨族重复计算。
            if used + kwh > cert["energy_kwh"] + 1e-6:
                raise ConflictError(
                    f"凭证 {cert_id} 容量不足：已占用 {used} kWh，本次申请 {kwh} kWh，"
                    f"容量 {cert['energy_kwh']} kWh")
            connection.execute(
                "INSERT INTO certificate_claims(claim_id,certificate_id,batch_id,scope_key,version,"
                "claimant_type,claimant_id,energy_kwh,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'matched',?,?,'reserved',?,?)",
                (uuid.uuid4().hex, cert_id, batch_id, scope_key, version,
                 claimants[cert_id], kwh, now, now),
            )

    def restate_batch(self, *, request_id: str, actor_id: str, parent_batch_id: str,
                      trips: list[dict[str, Any]], reason: str,
                      policy_id: str | None = None, baseline_id: str | None = None,
                      target_id: str | None = None) -> dict[str, Any]:
        """迟到数据、因子修订或凭证撤回后，按相同口径生成重述版本。"""

        reason = self._text(reason, "reason", 500)
        parent = self.get_batch_header(parent_batch_id)
        return self.freeze_batch(
            request_id=request_id, actor_id=actor_id, corridor_id=parent["corridor_id"],
            period_start=parent["period_start"], period_end=parent["period_end"],
            trips=trips, scope_tag=parent["scope_tag"], policy_id=policy_id,
            baseline_id=baseline_id, target_id=target_id,
            _parent_batch_id=parent_batch_id, _restate_reason=reason,
        )

    # ------------------------------------------------------------------
    # 独立核验与发布
    # ------------------------------------------------------------------

    def add_finding(self, *, request_id: str, actor_id: str, batch_id: str, code: str,
                    message: str, blocking: bool = True, trip_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "code": code,
                   "message": message, "blocking": blocking, "trip_id": trip_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "auditor")
            batch = connection.execute(
                "SELECT * FROM accounting_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("核算批次不存在")
            code = self._id(code, "code")
            message = self._text(message, "message", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                finding_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO verification_findings(finding_id,batch_id,reviewer_id,code,"
                    "trip_id,message,blocking,resolved,created_at) VALUES(?,?,?,?,?,?,?,0,?)",
                    (finding_id, batch_id, actor_id, code, trip_id, message,
                     1 if blocking else 0, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="finding.added",
                            resource_type="verification_finding", resource_id=finding_id,
                            detail={"batch_id": batch_id, "code": code, "blocking": blocking})
                response = {"finding_id": finding_id, "batch_id": batch_id, "blocking": blocking}
                return "verification_finding", finding_id, response

            return self._idempotent(connection, request_id=request_id, action="add_finding",
                                    payload=payload, create=create)

    def resolve_finding(self, *, request_id: str, actor_id: str, finding_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "auditor")
            row = connection.execute(
                "SELECT * FROM verification_findings WHERE finding_id=?", (finding_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("核验发现不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE verification_findings SET resolved=1 WHERE finding_id=?", (finding_id,)
                )
                self._audit(connection, actor_id=actor_id, action="finding.resolved",
                            resource_type="verification_finding", resource_id=finding_id,
                            detail={"batch_id": row["batch_id"]})
                return "verification_finding", finding_id, {"finding_id": finding_id, "resolved": True}

            return self._idempotent(connection, request_id=request_id, action="resolve_finding",
                                    payload={"actor_id": actor_id, "finding_id": finding_id},
                                    create=create)

    def recompute_batch(self, batch_id: str) -> dict[str, Any]:
        """按批次冻结的原始输入与精确主数据版本重新计算，核对 result_hash。"""

        connection = self.database.connection
        batch = connection.execute(
            "SELECT * FROM accounting_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFoundError("核算批次不存在")
        policy_row = self._exact_version(connection, "evidence_policies",
                                         batch["policy_id"], batch["policy_version"])
        baseline_row = self._exact_version(connection, "baselines",
                                           batch["baseline_id"], batch["baseline_version"])
        master = json.loads(batch["master_versions_json"])
        segments = {}
        for sid, version in master["segments"].items():
            row = self._exact_version(connection, "corridor_segments", sid, version)
            segments[sid] = {"segment_id": sid, "version": version, "length_km": row["length_km"],
                             "boundary": json.loads(row["boundary_json"])}
        energy_types = {}
        for eid, version in master["energy_types"].items():
            row = self._exact_version(connection, "vehicle_energy_types", eid, version)
            energy_types[eid] = {"energy_carrier": row["energy_carrier"],
                                 "grid_ef_kg_per_kwh": row["grid_ef_kg_per_kwh"],
                                 "fuel_intensity_l_per_km": row["fuel_intensity_l_per_km"],
                                 "fuel_ef_kg_per_l": row["fuel_ef_kg_per_l"]}
        factors = {}
        for key, ref in master["factors"].items():
            row = self._exact_version(connection, "emission_factors", ref["factor_id"], ref["version"])
            factors[key] = {"value": row["value"]}
        snapshot = {
            "policy": {"rules": json.loads(policy_row["rules_json"]),
                       "before_grace_days": policy_row["before_grace_days"]},
            "segments": segments, "energy_types": energy_types, "factors": factors,
            "baseline": {"baseline_ef_kg_per_km": baseline_row["baseline_ef_kg_per_km"],
                         "load_correction": json.loads(baseline_row["load_correction_json"])},
        }
        certificates = json.loads(batch["certificate_snapshot_json"])
        trips = self._frozen_raw_trips(batch_id)
        trip_results = [rules.evaluate_trip(trip, snapshot, certificates) for trip in trips]
        result = rules.summarize(trip_results)
        result_hash = digest({"manifest_hash": batch["manifest_hash"], "result": result,
                              "trips": trip_results})
        return {"batch_id": batch_id, "stored_result_hash": batch["result_hash"],
                "recomputed_result_hash": result_hash,
                "hash_matches": result_hash == batch["result_hash"],
                "result": result}

    def _frozen_raw_trips(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM batch_trips WHERE batch_id=? ORDER BY trip_id", (batch_id,)
        ).fetchall()
        trips = []
        for row in rows:
            trip = {
                "trip_id": row["trip_id"], "vehicle_id": row["vehicle_id"],
                "energy_type_id": row["energy_type_id"], "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "segment_legs": json.loads(row["segment_legs_json"]),
                "energy_records": json.loads(row["energy_records_json"]),
                "declarations": json.loads(row["declarations_json"]),
            }
            if row["logged_at"]:
                trip["logged_at"] = row["logged_at"]
            payload = json.loads(row["payload_json"])
            if payload:
                trip["payload"] = payload
            trips.append(trip)
        return trips

    def verify_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                     notes: str = "") -> dict[str, Any]:
        """独立核验：核验人必须不同于冻结人，复算一致且无未解决阻断发现。"""

        notes = str(notes or "")[:500]
        payload = {"actor_id": actor_id, "batch_id": batch_id, "notes": notes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            batch = connection.execute(
                "SELECT * FROM accounting_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("核算批次不存在")
            if batch["status"] != "frozen":
                raise ConflictError("只有 frozen 批次可以提交核验")
            if batch["frozen_by"] == actor_id:
                raise PermissionDenied("独立核验要求核验人不是批次冻结人")

            check = self.recompute_batch(batch_id)
            if not check["hash_matches"]:
                raise ConflictError("复算结果与冻结结果不一致，拒绝核验")
            blocking = connection.execute(
                "SELECT COUNT(*) AS count FROM verification_findings "
                "WHERE batch_id=? AND blocking=1 AND resolved=0", (batch_id,)
            ).fetchone()["count"]
            if blocking:
                raise ConflictError(f"存在 {blocking} 条未解决的阻断性核验发现")
            drift = []
            for cert_id in json.loads(batch["certificate_snapshot_json"]):
                row = connection.execute(
                    "SELECT status FROM green_certificates WHERE certificate_id=?", (cert_id,)
                ).fetchone()
                if row is not None and row["status"] == "withdrawn":
                    claimed = connection.execute(
                        "SELECT 1 FROM certificate_claims WHERE batch_id=? AND certificate_id=? "
                        "AND state='reserved' LIMIT 1", (batch_id, cert_id)
                    ).fetchone()
                    if claimed:
                        drift.append(cert_id)
            if drift:
                raise ConflictError(f"凭证已撤回且占用仍未解除：{', '.join(drift)}，需重述批次")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE accounting_batches SET status='verified', verified_by=?, "
                    "verified_at=?, verify_notes=? WHERE batch_id=?",
                    (actor_id, self._now(), notes, batch_id),
                )
                self._audit(connection, actor_id=actor_id, action="batch.verified",
                            resource_type="accounting_batch", resource_id=batch_id,
                            detail={"result_hash": batch["result_hash"], "notes": notes})
                response = {"batch_id": batch_id, "status": "verified",
                            "result_hash": batch["result_hash"]}
                return "accounting_batch", batch_id, response

            return self._idempotent(connection, request_id=request_id, action="verify_batch",
                                    payload=payload, create=create)

    def publish_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> dict[str, Any]:
        """发布已核验批次；若族内已有发布版本，旧版本转为 restated 并释放其凭证占用。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            batch = connection.execute(
                "SELECT * FROM accounting_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("核算批次不存在")
            if batch["status"] != "verified":
                raise ConflictError("只有通过独立核验的批次才能发布")
            now = self._now()
            prior_row = connection.execute(
                "SELECT * FROM published_versions WHERE scope_key=? ORDER BY version DESC LIMIT 1",
                (batch["scope_key"],),
            ).fetchone()
            prior_published = None
            if prior_row is not None and prior_row["kind"] != "revocation":
                prior_published = connection.execute(
                    "SELECT pv.*, b.status AS batch_status FROM published_versions pv "
                    "JOIN accounting_batches b ON b.batch_id=pv.batch_id "
                    "WHERE pv.scope_key=? AND pv.kind!='revocation' "
                    "ORDER BY pv.version DESC LIMIT 1",
                    (batch["scope_key"],),
                ).fetchone()
            report_version = (prior_row["version"] + 1) if prior_row else 1

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE accounting_batches SET status='published', published_at=? WHERE batch_id=?",
                    (now, batch_id),
                )
                connection.execute(
                    "UPDATE certificate_claims SET state='settled', updated_at=? "
                    "WHERE batch_id=? AND state='reserved'", (now, batch_id),
                )
                result = json.loads(batch["result_json"])
                if prior_published is not None:
                    kind = "restatement"
                    connection.execute(
                        "UPDATE accounting_batches SET status='restated' WHERE batch_id=?",
                        (prior_published["batch_id"],),
                    )
                    connection.execute(
                        "UPDATE certificate_claims SET state='released', updated_at=? "
                        "WHERE batch_id=? AND state='settled'", (now, prior_published["batch_id"]),
                    )
                else:
                    # 首次发布，或吊销之后发布的更正重述版本
                    kind = "initial" if prior_row is None else "restatement"
                connection.execute(
                    "INSERT INTO published_versions(scope_key,version,batch_id,kind,"
                    "reductions_tco2,result_hash,published_at,published_by) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (batch["scope_key"], report_version, batch_id, kind,
                     result["reductions_tco2"], batch["result_hash"], now, actor_id),
                )
                self._audit(connection, actor_id=actor_id,
                            action="batch.published" if kind == "initial" else "batch.restated_published",
                            resource_type="accounting_batch", resource_id=batch_id,
                            detail={"scope_key": batch["scope_key"], "report_version": report_version,
                                    "batch_version": batch["version"], "kind": kind,
                                    "reductions_tco2": result["reductions_tco2"],
                                    "superseded_batch_id": None if prior_published is None
                                    else prior_published["batch_id"]})
                response = {"batch_id": batch_id, "scope_key": batch["scope_key"],
                            "version": report_version, "batch_version": batch["version"],
                            "kind": kind, "status": "published",
                            "reductions_tco2": result["reductions_tco2"],
                            "result_hash": batch["result_hash"]}
                return "accounting_batch", batch_id, response

            return self._idempotent(connection, request_id=request_id, action="publish_batch",
                                    payload=payload, create=create)

    def revoke_published_batch(self, *, request_id: str, actor_id: str,
                               batch_id: str, reason: str) -> dict[str, Any]:
        """吊销当前对外发布版本：追加 revocation 发布记录，原批次与输入保留可复算。"""

        reason = self._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            batch = connection.execute(
                "SELECT * FROM accounting_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("核算批次不存在")
            if batch["status"] != "published":
                raise ConflictError("只能吊销已发布的当前版本")
            latest = connection.execute(
                "SELECT * FROM published_versions WHERE scope_key=? ORDER BY version DESC LIMIT 1",
                (batch["scope_key"],),
            ).fetchone()
            if latest is None:
                raise ConflictError("该口径尚无对外发布版本")
            if latest["kind"] == "revocation":
                raise ConflictError("当前发布版本已经处于吊销状态，请发布更正重述版本")
            if latest["batch_id"] != batch_id:
                raise ConflictError("只能吊销该口径最新发布版本对应的批次")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE accounting_batches SET status='revoked', revoked_by=?, revoked_at=?, "
                    "revoke_reason=? WHERE batch_id=?", (actor_id, now, reason, batch_id),
                )
                connection.execute(
                    "UPDATE certificate_claims SET state='released', updated_at=? "
                    "WHERE batch_id=? AND state='settled'", (now, batch_id),
                )
                next_version = latest["version"] + 1
                connection.execute(
                    "INSERT INTO published_versions(scope_key,version,batch_id,kind,"
                    "reductions_tco2,result_hash,published_at,published_by) "
                    "VALUES(?,?,?,'revocation',0,?,?,?)",
                    (batch["scope_key"], next_version, batch_id, batch["result_hash"], now, actor_id),
                )
                self._audit(connection, actor_id=actor_id, action="batch.revoked",
                            resource_type="accounting_batch", resource_id=batch_id,
                            detail={"scope_key": batch["scope_key"], "version": next_version,
                                    "reason": reason})
                response = {"batch_id": batch_id, "scope_key": batch["scope_key"],
                            "version": next_version, "kind": "revocation",
                            "reductions_tco2": 0.0}
                return "accounting_batch", batch_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_published_batch", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询与解释接口
    # ------------------------------------------------------------------

    def get_batch_header(self, batch_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM accounting_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("核算批次不存在")
        return dict(row)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch_header(batch_id)
        result = json.loads(batch.pop("result_json"))
        batch["result"] = result
        batch["master_versions"] = json.loads(batch.pop("master_versions_json"))
        batch["certificate_snapshot"] = json.loads(batch.pop("certificate_snapshot_json"))
        return batch

    def list_batches(self, corridor_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT batch_id,corridor_id,period_start,period_end,scope_key,scope_tag,version,"
               "parent_batch_id,status,frozen_by,frozen_at,verified_by,published_at,result_hash "
               "FROM accounting_batches")
        clauses, params = [], []
        if corridor_id:
            clauses.append("corridor_id=?")
            params.append(corridor_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY corridor_id, period_start, scope_key, version"
        return [dict(row) for row in self.database.connection.execute(sql, params).fetchall()]

    def evidence_deficits(self, batch_id: str) -> dict[str, Any]:
        """返回补证清单：每条缺失/冲突证据对应到行程与原因。"""

        self.get_batch_header(batch_id)
        rows = self.database.connection.execute(
            "SELECT trip_id, payload_status, energy_status, claim_status, pending_reasons_json "
            "FROM batch_trips WHERE batch_id=? AND verification_status='pending_evidence' ORDER BY trip_id",
            (batch_id,),
        ).fetchall()
        items = []
        for row in rows:
            items.append({"trip_id": row["trip_id"], "payload_status": row["payload_status"],
                          "energy_status": row["energy_status"], "claim_status": row["claim_status"],
                          "reasons": json.loads(row["pending_reasons_json"])})
        return {"batch_id": batch_id, "deficit_count": len(items), "items": items}

    def explain_reductions(self, batch_id: str) -> dict[str, Any]:
        """解释每一吨减排来自哪些有效行程，并列出未计入行程及凭证占用。"""

        batch = self.get_batch_header(batch_id)
        rows = self.database.connection.execute(
            "SELECT * FROM batch_trips WHERE batch_id=? ORDER BY trip_id", (batch_id,)
        ).fetchall()
        valid_items, pending_items = [], []
        total = 0.0
        for row in rows:
            entry = {
                "trip_id": row["trip_id"], "vehicle_id": row["vehicle_id"],
                "distance_km": row["distance_km"],
                "baseline_emissions_kg": row["baseline_emissions_kg"],
                "actual_emissions_kg": row["actual_emissions_kg"],
                "reductions_kg": row["reductions_kg"],
                "evidence_valid": json.loads(row["evidence_valid_json"]),
                "calc_detail": json.loads(row["calc_detail_json"]),
            }
            if row["verification_status"] == "valid":
                total += row["reductions_kg"]
                entry["share_of_reductions"] = (round(row["reductions_kg"] / total, 6) if total else 0.0)
                valid_items.append(entry)
            else:
                entry["reasons"] = json.loads(row["pending_reasons_json"])
                pending_items.append(entry)
        # 用最终总量重算占比
        for entry in valid_items:
            entry["share_of_reductions"] = round(entry["reductions_kg"] / total, 6) if total else 0.0
        claims = self.database.connection.execute(
            "SELECT certificate_id, claimant_id, energy_kwh, state FROM certificate_claims "
            "WHERE batch_id=? ORDER BY certificate_id", (batch_id,),
        ).fetchall()
        return {
            "batch_id": batch_id, "scope_key": batch["scope_key"], "version": batch["version"],
            "status": batch["status"], "result_hash": batch["result_hash"],
            "total_reductions_tco2": round(total / 1000.0, 6),
            "valid_trips": valid_items,
            "excluded_pending_trips": pending_items,
            "certificate_claims": [dict(row) for row in claims],
            "note": "仅 verification_status=valid 的行程计入减排；证据不足行程 actual_emissions 为 null，"
                    "不按零排放处理",
        }

    def corridor_progress(self, corridor_id: str, period_start: str, period_end: str,
                          metric: str | None = None) -> dict[str, Any]:
        """展示走廊目标完成口径：取每个口径族最新发布版本，周期重叠部分相加。"""

        corridor_id = self._id(corridor_id, "corridor_id")
        period_start = self._date(period_start, "period_start")
        period_end = self._date(period_end, "period_end")
        connection = self.database.connection
        target_rows = connection.execute(
            "SELECT * FROM corridor_targets WHERE corridor_id=? AND status='active' "
            "AND period_start<=? AND period_end>=? ORDER BY version DESC",
            (corridor_id, period_end, period_start),
        ).fetchall()
        chosen_metric = metric
        target = None
        if chosen_metric is None:
            target = target_rows[0] if target_rows else None
            chosen_metric = target["metric"] if target else "published"
        else:
            for row in target_rows:
                if row["metric"] == chosen_metric:
                    target = row
                    break

        if chosen_metric == "published":
            # 每个 scope_key 取最新发布版本；revocation 行减排为 0
            published = connection.execute(
                "SELECT pv.* FROM published_versions pv WHERE pv.scope_key LIKE ? "
                "AND pv.version=(SELECT MAX(version) FROM published_versions WHERE scope_key=pv.scope_key)",
                (corridor_id + "|%",),
            ).fetchall()
            components = []
            for row in published:
                batch = connection.execute(
                    "SELECT * FROM accounting_batches WHERE batch_id=?", (row["batch_id"],)
                ).fetchone()
                if batch["period_start"] > period_end or batch["period_end"] < period_start:
                    continue
                components.append({"scope_key": row["scope_key"], "batch_id": row["batch_id"],
                                   "version": row["version"], "kind": row["kind"],
                                   "period_start": batch["period_start"],
                                   "period_end": batch["period_end"],
                                   "reductions_tco2": row["reductions_tco2"]})
            achieved = sum(item["reductions_tco2"] for item in components)
        else:
            # verified 口径：每个族最新 verified/published 批次（尚未发布的已核验结果也展示）
            rows = connection.execute(
                "SELECT * FROM accounting_batches WHERE corridor_id=? AND status IN ('verified','published') "
                "AND period_start<=? AND period_end>=? ORDER BY scope_key, version DESC",
                (corridor_id, period_end, period_start),
            ).fetchall()
            seen: dict[str, dict[str, Any]] = {}
            for row in rows:
                seen.setdefault(row["scope_key"], dict(row))
            components = []
            for scope_key, row in seen.items():
                result = json.loads(row["result_json"])
                components.append({"scope_key": scope_key, "batch_id": row["batch_id"],
                                   "version": row["version"], "kind": row["status"],
                                   "period_start": row["period_start"], "period_end": row["period_end"],
                                   "reductions_tco2": result["reductions_tco2"]})
            achieved = sum(item["reductions_tco2"] for item in components)

        warnings = self._integrity_warnings(connection, corridor_id)
        response = {
            "corridor_id": corridor_id, "metric": chosen_metric,
            "period_start": period_start, "period_end": period_end,
            "target_tco2": target["target_tco2"] if target else None,
            "target_version": ({"target_id": target["target_id"], "version": target["version"]}
                               if target else None),
            "achieved_reductions_tco2": round(achieved, 6),
            "completion_rate": (round(achieved / target["target_tco2"], 6)
                                if target and target["target_tco2"] > 0 else None),
            "components": sorted(components, key=lambda item: item["scope_key"]),
            "integrity_warnings": warnings,
            "caliber_note": ("published 口径只包含每个批次族最新发布版本（重述取代旧版，吊销计 0）；"
                             "verified 口径还包含已核验未发布的最新批次"),
        }
        return response

    def _integrity_warnings(self, connection, corridor_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT DISTINCT cc.batch_id, cc.scope_key, cc.certificate_id, b.version "
            "FROM certificate_claims cc JOIN accounting_batches b ON b.batch_id=cc.batch_id "
            "JOIN green_certificates g ON g.certificate_id=cc.certificate_id "
            "WHERE b.corridor_id=? AND b.status='published' AND cc.state='settled' "
            "AND g.status='withdrawn'",
            (corridor_id,),
        ).fetchall()
        return [{"batch_id": row["batch_id"], "scope_key": row["scope_key"], "version": row["version"],
                 "certificate_id": row["certificate_id"],
                 "message": "已发布报告占用的凭证事后被撤回，应发布重述或吊销版本"} for row in rows]

    def list_published_reports(self, corridor_id: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT pv.scope_key, pv.version, pv.batch_id, pv.kind, pv.reductions_tco2,"
                "pv.result_hash, pv.published_at, pv.published_by, b.corridor_id,"
                "b.period_start, b.period_end FROM published_versions pv "
                "JOIN accounting_batches b ON b.batch_id=pv.batch_id")
        params: list[Any] = []
        if corridor_id:
            sql += " WHERE b.corridor_id=?"
            params.append(corridor_id)
        sql += " ORDER BY b.corridor_id, pv.scope_key, pv.version"
        return [dict(row) for row in self.database.connection.execute(sql, params).fetchall()]

    def list_master_versions(self, table_key: str, identity: str) -> list[dict[str, Any]]:
        table = {
            "segment": "corridor_segments", "energy_type": "vehicle_energy_types",
            "factor": "emission_factors", "baseline": "baselines",
            "policy": "evidence_policies", "target": "corridor_targets",
        }.get(table_key)
        if table is None:
            raise ValidationError("未知主数据类型")
        id_column = VERSIONED_TABLES[table][0]
        rows = self.database.connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=? ORDER BY version", (identity,)
        ).fetchall()
        if not rows:
            raise NotFoundError("主数据不存在")
        return [dict(row) for row in rows]

    def findings(self, batch_id: str) -> list[dict[str, Any]]:
        self.get_batch_header(batch_id)
        rows = self.database.connection.execute(
            "SELECT finding_id,reviewer_id,code,trip_id,message,blocking,resolved,created_at "
            "FROM verification_findings WHERE batch_id=? ORDER BY created_at", (batch_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)
        ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"], "actor_id": row["actor_id"],
                 "action": row["action"], "resource_type": row["resource_type"],
                 "resource_id": row["resource_id"], "detail": json.loads(row["detail_json"]),
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "occurred_at": row["occurred_at"]} for row in rows]
