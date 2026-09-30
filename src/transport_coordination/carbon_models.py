"""走廊碳核算在模块边界使用的不可变数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Segment:
    """走廊中的一个路段边界。"""

    segment_id: str
    name: str
    origin: str
    destination: str
    distance_km: float


@dataclass(frozen=True)
class BoundaryVersion:
    """路段边界的一个不可变版本。"""

    corridor_id: str
    version: int
    segments: tuple[Segment, ...]
    change_note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class TargetVersion:
    """走廊减排目标的一个口径版本。"""

    corridor_id: str
    version: int
    period_start: str
    period_end: str
    target_reduction_t: float
    methodology: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class VehicleTypeVersion:
    """车辆与能源类型的一个版本。"""

    vehicle_type_id: str
    version: int
    energy_type: str
    consumption_rate: float
    rated_payload_t: float
    change_note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class EmissionFactorVersion:
    """排放因子的一个版本，数值与适用区间同时冻结。"""

    factor_key: str
    version: int
    value: float
    unit: str
    source: str
    valid_from: str
    valid_to: str
    change_note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class BaselineVersion:
    """基准方案的一个版本，记录柴油重卡的基准能耗口径。"""

    baseline_id: str
    version: int
    corridor_id: str
    vehicle_class: str
    fuel_intensity_l_per_km: float
    factor_key: str
    change_note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class EvidencePolicyVersion:
    """证据有效期与是否必需的策略版本。"""

    evidence_type: str
    version: int
    validity_days: int
    required: bool
    change_note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class EnergyEvent:
    """重卡在行程中的一次补能记录。"""

    event_id: str
    trip_id: str
    station_id: str | None
    source_status: str
    amount_kwh: float
    occurred_at: str


@dataclass(frozen=True)
class Certificate:
    """绿色电力凭证及其有效期与申报方。"""

    certificate_id: str
    claimant_type: str
    claimant_id: str
    kwh_total: float
    generation_start: str
    generation_end: str
    valid_from: str
    valid_to: str
    status: str
    withdrawn_at: str | None
    withdrawal_reason: str | None


@dataclass(frozen=True)
class Claim:
    """凭证对某次补能事件的申报结果。"""

    claim_id: str
    certificate_id: str
    energy_event_id: str
    claimant_type: str
    claimant_id: str
    kwh: float
    status: str
    conflict_code: str | None


@dataclass(frozen=True)
class EvidenceRequest:
    """补证单：缺失或冲突的证据，不按零排放处理。"""

    request_id: str
    family_batch_id: str | None
    batch_id: str | None
    version: int | None
    trip_id: str | None
    code: str
    detail: dict[str, Any]
    status: str
    resolution_note: str | None
    created_by: str
    created_at: str
    resolved_at: str | None
    resolved_by: str | None


@dataclass(frozen=True)
class TripContribution:
    """单个有效行程的减排量与口径解释。"""

    trip_id: str
    segment_id: str
    vehicle_id: str
    vehicle_type_id: str
    energy_type: str
    distance_km: float
    payload_t: float
    payload_factor: float
    baseline_tco2: float
    actual_tco2: float
    avoided_tco2: float
    energy_kwh: float
    matched_green_kwh: float
    grid_kwh: float
    applied_factors: dict[str, float]
    certificate_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BatchResult:
    """一个核算批次的完整计算结果。"""

    formula_version: str
    total_baseline_tco2: float
    total_actual_tco2: float
    total_avoided_tco2: float
    trips_in_scope: int
    trips_accepted: int
    trips_excluded: int
    contributions: tuple[TripContribution, ...]
    exclusions: tuple[dict[str, Any], ...]
    evidence_items: tuple[dict[str, Any], ...]
    evidence_codes: tuple[str, ...]
    evidence_open_at_freeze: tuple[str, ...]
    certificate_usage: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BatchRecord:
    """一个核算批次版本的持久化视图。"""

    batch_id: str
    version: int
    corridor_id: str
    period_start: str
    period_end: str
    status: str
    revision_kind: str
    change_note: str
    created_by: str
    created_at: str
    snapshot_hash: str | None
    result_hash: str | None
    formula_version: str | None
    frozen_by: str | None
    frozen_at: str | None
    verified_by: str | None
    verified_at: str | None
    verification_decision: str | None
    verification_note: str | None
    published_by: str | None
    published_at: str | None


@dataclass(frozen=True)
class TargetProgress:
    """走廊目标完成口径：只汇总每个批次族最新发布版本。"""

    corridor_id: str
    target_version: int
    period_start: str
    period_end: str
    target_reduction_t: float
    achieved_reduction_t: float
    completion_ratio: float
    included_batches: tuple[dict[str, Any], ...]
    methodology: str
